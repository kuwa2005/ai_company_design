"""社長が会議の確認事項に回答・解決する（ai-company decide）."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_company.db.models import AuditEvent
from ai_company.services.secretary import (
    MEETING_QUESTION_RESOLVED,
    human_summary,
    meeting_open_questions,
    numbered_meeting_questions,
    resolve_meeting_question,
    start_goal,
)

SAAS_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい"


def _events(session: Session) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == MEETING_QUESTION_RESOLVED)
        ).all()
    )


def test_resolve_records_audit_and_hides_from_status(db_session: Session) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    before = numbered_meeting_questions(db_session, pid)
    assert len(before) >= 2
    first_n, first_q = before[0]
    second_n, second_q = before[1]

    got = resolve_meeting_question(
        db_session, project_id=pid, number=first_n, answer="月額 980円 / 人で提案"
    )
    assert got == first_q

    events = _events(db_session)
    assert len(events) == 1
    meta = events[0].metadata_json or {}
    assert events[0].target_id == pid and events[0].actor_id == "human_owner"
    assert meta["number"] == first_n and meta["question"] == first_q
    assert meta["answer"] == "月額 980円 / 人で提案"

    open_now = meeting_open_questions(db_session, pid)
    assert first_q not in open_now and second_q in open_now
    # numbers are stable: the remaining question keeps its number
    assert (second_n, second_q) in numbered_meeting_questions(db_session, pid)
    assert numbered_meeting_questions(db_session, pid, include_resolved=True) == before

    text = human_summary(db_session, project_id=pid)
    assert first_q not in text
    assert f"  - 会議の確認事項 #{second_n}: {second_q}" in text
    assert f"会議の確認事項 {len(before) - 1}）" in text


def test_resolving_all_clears_status(db_session: Session) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    for n, _ in numbered_meeting_questions(db_session, pid):
        resolve_meeting_question(db_session, project_id=pid, number=n, answer="決定")
    assert meeting_open_questions(db_session, pid) == []
    text = human_summary(db_session, project_id=pid)
    assert "会議の確認事項" not in text
    assert "ai-company decide" not in text


def test_resolve_rejects_bad_input(db_session: Session) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    total = len(numbered_meeting_questions(db_session, pid))
    with pytest.raises(ValueError, match="ありません"):
        resolve_meeting_question(db_session, project_id=pid, number=0, answer="x")
    with pytest.raises(ValueError, match="ありません"):
        resolve_meeting_question(db_session, project_id=pid, number=total + 1, answer="x")
    with pytest.raises(ValueError, match="空"):
        resolve_meeting_question(db_session, project_id=pid, number=1, answer="   ")
    resolve_meeting_question(db_session, project_id=pid, number=1, answer="ok")
    with pytest.raises(ValueError, match="解決済み"):
        resolve_meeting_question(db_session, project_id=pid, number=1, answer="again")
    assert len(_events(db_session)) == 1


def test_answer_is_redacted(db_session: Session) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    resolve_meeting_question(
        db_session,
        project_id=result.project.id,
        number=1,
        answer="本番は password=hunter2 で",
    )
    answer = str((_events(db_session)[0].metadata_json or {})["answer"])
    assert "hunter2" not in answer


def test_cli_decide(db_session: Session) -> None:
    from ai_company.cli import app

    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    n, q = numbered_meeting_questions(db_session, pid)[0]
    db_session.commit()

    runner = CliRunner()
    out = runner.invoke(app, ["status"])
    assert out.exit_code == 0, out.output
    assert f"#{n}" in out.output and "ai-company decide" in out.output

    out = runner.invoke(app, ["decide", str(n), "社長判断: 進めてよい"])
    assert out.exit_code == 0, out.output
    assert f"解決済み #{n}" in out.output

    out = runner.invoke(app, ["decide", str(n), "二回目"])
    assert out.exit_code == 2
    out = runner.invoke(app, ["decide", "99", "x", "--project", pid])
    assert out.exit_code == 2
    out = runner.invoke(app, ["decide", "1", "x", "--project", "NOPE"])
    assert out.exit_code == 2

    out = runner.invoke(app, ["status"])
    assert out.exit_code == 0, out.output
    assert f"会議の確認事項 #{n}:" not in out.output
