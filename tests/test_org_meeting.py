"""部/課 organization and executive meeting (重役会議)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_company.db.models import AuditEvent
from ai_company.org import DEPARTMENTS, department_label, home_of_role, validate_org
from ai_company.roles import escalation_chain, supervisor_of
from ai_company.services.executive_meeting import (
    convene_executive_meeting,
    render_minutes_markdown,
    section_for_task,
)
from ai_company.services.secretary import start_goal, understand_goal


def test_org_chart_is_consistent() -> None:
    assert validate_org() == []
    names = {d.name_ja for d in DEPARTMENTS}
    for required in ("開発部", "営業部", "マーケティング部", "品質保証部", "法務部"):
        assert required in names
    dev = next(d for d in DEPARTMENTS if d.id == "development")
    assert [s.name_ja for s in dev.sections] == ["設計課", "開発課", "検証課"]


def test_department_heads_do_not_change_escalation_route() -> None:
    # Workers still escalate via the proven manager chain, never via 部長.
    assert escalation_chain("coding_worker") == [
        "engineering_manager",
        "technical_director",
        "human_owner",
    ]
    for head in ("qa_director", "sales_director", "legal_director"):
        assert supervisor_of(head) == "human_owner"
        assert home_of_role(head) is not None
    assert department_label("compliance_auditor") == "法務部／コンプライアンス課"


def test_section_for_task_splits_development_by_work_kind() -> None:
    assert section_for_task("データモデル設計", "backend_engineer")[1].name_ja == "設計課"
    assert section_for_task("API 実装", "backend_engineer")[1].name_ja == "開発課"
    assert section_for_task("E2E / 受け入れ確認", "coding_worker")[1].name_ja == "検証課"
    dept, section = section_for_task("提案資料", "sales")
    assert (dept.name_ja, section.name_ja) == ("営業部", "営業課")


def test_meeting_summons_departments_by_goal(tmp_env) -> None:
    minutes = convene_executive_meeting(understand_goal("社内用のCLIツールを作る"))
    assert {"secretariat", "development", "quality_assurance"} <= set(minutes.attendees)
    assert "legal" not in minutes.attendees
    assert "legal" in minutes.absentees

    minutes = convene_executive_meeting(
        understand_goal("顧客向けの勤怠管理Webサービスを作って販売したい")
    )
    assert {"sales", "legal", "marketing"} <= set(minutes.attendees)
    assert any("価格" in q for q in minutes.open_questions)
    md = render_minutes_markdown(minutes)
    assert "重役会議 議事録" in md and "設計課" in md and "判定課" in md
    # every planned task has exactly one owning unit
    assert len(minutes.assignments) == len(understand_goal("顧客向けの勤怠管理Webサービス").tasks)


def test_all_departments_flag(tmp_env) -> None:
    minutes = convene_executive_meeting(
        understand_goal("社内用のCLIツールを作る"), all_departments=True
    )
    assert len(minutes.attendees) == len(DEPARTMENTS)
    assert minutes.absentees == {}


def test_start_goal_records_minutes(db_session: Session, tmp_env) -> None:
    result = start_goal(db_session, "勤怠Webアプリを作る")
    assert result.meeting is not None
    assert result.meeting.path is not None and result.meeting.path.exists()
    assert result.meeting.project_id == result.project.id
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "executive_meeting_held")
    ).all()
    assert len(events) == 1
    assert events[0].target_id == result.project.id

    skipped = start_goal(db_session, "勤怠Webアプリを作る", hold_meeting=False)
    assert skipped.meeting is None


def test_cli_org_and_meeting(tmp_env) -> None:
    from ai_company.cli import app

    runner = CliRunner()
    out = runner.invoke(app, ["org"])
    assert out.exit_code == 0, out.output
    assert "品質保証部" in out.output and "設計課" in out.output
    out = runner.invoke(app, ["meeting", "顧客向けSaaSを販売する"])
    assert out.exit_code == 0, out.output
    assert "重役会議 議事録" in out.output and "営業部" in out.output
