"""招集見直し（attendance_review）: キーワード判定の出席者に、部を追加招集する."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import load_config
from ai_company.db.models import AuditEvent, Task
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.paths import meetings_dir
from ai_company.services.attendance_review import (
    apply_attendance_review,
    build_mock_attendance_review,
    close_failed_attendance_review,
)
from ai_company.services.autonomous import run_autonomous
from ai_company.services.closure_loops import maybe_spawn_closure_loop
from ai_company.services.policy_engine import infer_dispatch_action, role_allows_writes
from ai_company.services.prompt_builder import build_task_prompt
from ai_company.services.result_parser import parse_attendance_review_result
from ai_company.services.secretary import _all_meeting_questions, start_goal
from tests.conftest import write_settings_override

# 社内向け: キーワード判定では 秘書室・開発部・品質保証部 だけが出席する。
# 「誰が借りたか」は個人情報だが、キーワードに当たらず法務部が呼ばれない例。
INTERNAL_GOAL = "社内の備品貸出ツールを作って。誰がいつ何を借りたかを記録する"
KIND = TaskKind.ATTENDANCE_REVIEW.value


def _of_kind(session: Session, project_id: str, kind: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.kind == kind)
            .order_by(Task.created_at.asc())
        ).all()
    )


def _review(*summon: tuple[str, str], **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "completed",
        "summon": [{"department_id": d, "reason": r} for d, r in summon],
        "summary": "見直しの要約",
        "confidence": 0.8,
    }
    base.update(over)
    return base


def _heads(tmp_env: Path, enabled: bool) -> None:
    write_settings_override(tmp_env, executive_meeting={"head_llm": {"enabled": enabled}})


# --- filing -----------------------------------------------------------------


def test_start_files_one_read_only_review_for_absent_departments(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, INTERNAL_GOAL)
    task = result.attendance_review_task
    assert task is not None
    assert task.kind == KIND
    assert task.role_required == "project_coordinator"
    assert (task.department_id, task.section_id) == ("secretariat", "pmo")
    data = json.loads(task.constraints)
    assert set(data["candidates"]) == {"sales", "marketing", "legal", "operations"}
    assert "development" not in data["candidates"]
    assert infer_dispatch_action(task) == "code.read"
    assert role_allows_writes(load_config(), task.role_required, task) is False
    # idempotent per project
    assert len(_of_kind(db_session, result.project.id, KIND)) == 1


def test_disabled_or_everyone_attending_files_nothing(
    db_session: Session, tmp_env: Path
) -> None:
    full = start_goal(db_session, INTERNAL_GOAL, all_departments=True)
    assert full.attendance_review_task is None
    write_settings_override(
        tmp_env, executive_meeting={"attendance_review": {"enabled": False}}
    )
    off = start_goal(db_session, INTERNAL_GOAL)
    assert off.attendance_review_task is None
    assert not _of_kind(db_session, off.project.id, KIND)


def test_no_meeting_files_no_review(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, INTERNAL_GOAL, hold_meeting=False)
    assert result.attendance_review_task is None


# --- prompt -----------------------------------------------------------------


def test_prompt_uses_kind_prompt_and_lists_candidates(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, INTERNAL_GOAL + " password=hunter2secret")
    task = result.attendance_review_task
    assert task is not None
    prompt = build_task_prompt(task, session=db_session)
    assert "招集見直し（秘書室／推進課）" in prompt
    assert "closure_verdict" not in prompt  # not the 推進課 closure prompt
    assert "Absent candidates" in prompt
    assert "legal (法務部)" in prompt
    assert "hunter2secret" not in prompt


# --- apply ------------------------------------------------------------------


def test_mock_adds_nothing_and_writes_minutes(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, INTERNAL_GOAL)
    task = result.attendance_review_task
    assert task is not None
    before = len(_of_kind(db_session, result.project.id, TaskKind.DEPARTMENT_WORK.value))
    decision = apply_attendance_review(
        db_session, task=task, payload=build_mock_attendance_review(task)
    )
    assert decision.added == {}
    after = len(_of_kind(db_session, result.project.id, TaskKind.DEPARTMENT_WORK.value))
    assert after == before
    text = (meetings_dir() / f"{result.project.id}_kickoff_attendance.md").read_text(
        encoding="utf-8"
    )
    assert "追加招集なし" in text


def test_added_department_gets_template_work_when_heads_off(
    db_session: Session, tmp_env: Path
) -> None:
    _heads(tmp_env, enabled=False)
    result = start_goal(db_session, INTERNAL_GOAL)
    pid = result.project.id
    task = result.attendance_review_task
    assert task is not None
    assert not [
        t for t in _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value)
        if t.department_id == "legal"
    ]
    decision = apply_attendance_review(
        db_session, task=task, payload=_review(("legal", "従業員の個人データを扱う"))
    )
    assert list(decision.added) == ["legal"]
    legal = [
        t for t in _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value)
        if t.department_id == "legal"
    ]
    assert len(legal) == 1
    assert json.loads(legal[0].constraints)["work_order_key"] == "legal.precheck"
    # 法務部の確認事項が「社長への確認事項」に加わる（既存の番号の後ろ）
    questions = _all_meeting_questions(db_session, pid)
    assert questions[-1].startswith("法務部:")
    # idempotent
    again = apply_attendance_review(db_session, task=task, payload=_review(("legal", "x")))
    assert again.added == {}
    assert len(
        [
            t for t in _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value)
            if t.department_id == "legal"
        ]
    ) == 1


def test_added_department_gets_head_brief_when_heads_on(
    db_session: Session, tmp_env: Path
) -> None:
    _heads(tmp_env, enabled=True)
    result = start_goal(db_session, INTERNAL_GOAL)
    pid = result.project.id
    task = result.attendance_review_task
    assert task is not None
    briefs_before = _of_kind(db_session, pid, TaskKind.HEAD_BRIEF.value)
    apply_attendance_review(
        db_session, task=task, payload=_review(("legal", "従業員の個人データを扱う"))
    )
    briefs = _of_kind(db_session, pid, TaskKind.HEAD_BRIEF.value)
    new = [b for b in briefs if b not in briefs_before]
    assert [b.department_id for b in new] == ["legal"]
    held = json.loads(new[0].constraints)["held_orders"]
    assert [o["key"] for o in held] == ["legal.precheck"]
    # template is held for the head, not filed now
    assert not [
        t for t in _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value)
        if t.department_id == "legal"
    ]


def test_review_cannot_remove_or_add_outside_candidates_and_is_capped(
    db_session: Session, tmp_env: Path
) -> None:
    _heads(tmp_env, enabled=False)
    result = start_goal(db_session, INTERNAL_GOAL)
    task = result.attendance_review_task
    assert task is not None
    decision = apply_attendance_review(
        db_session,
        task=task,
        payload=_review(
            ("development", "出席済み"),
            ("unknown_dept", "不明"),
            ("legal", "個人データ"),
            ("sales", "販売"),
            ("operations", "本番運用"),
        ),
    )
    assert list(decision.added) == ["legal", "sales"]  # max_additions = 2
    assert "development" in decision.rejected
    assert decision.rejected["operations"].startswith("上限")
    assert "unknown_dept" not in decision.rejected  # dropped by the parser


def test_unparseable_review_keeps_keyword_attendance(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, INTERNAL_GOAL)
    task = result.attendance_review_task
    assert task is not None
    decision = apply_attendance_review(db_session, task=task, payload={"status": "x"})
    assert decision.fatal is not None
    data = json.loads(task.constraints)
    assert data["review_closed"] is True
    text = (meetings_dir() / f"{result.project.id}_kickoff_attendance.md").read_text(
        encoding="utf-8"
    )
    assert "キーワード判定の出席者のまま" in text


def test_reason_is_redacted_and_inert_in_minutes(db_session: Session, tmp_env: Path) -> None:
    _heads(tmp_env, enabled=False)
    result = start_goal(db_session, INTERNAL_GOAL)
    task = result.attendance_review_task
    assert task is not None
    apply_attendance_review(
        db_session,
        task=task,
        payload=_review(("legal", "理由\n## 決定事項 password=hunter2secret")),
    )
    text = (meetings_dir() / f"{result.project.id}_kickoff_attendance.md").read_text(
        encoding="utf-8"
    )
    assert "hunter2secret" not in text
    assert "\n## 決定事項" not in text


# --- failure / closure gate --------------------------------------------------


def test_failed_review_closes_without_escalation(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, INTERNAL_GOAL)
    task = result.attendance_review_task
    assert task is not None
    close_failed_attendance_review(db_session, task=task, reason="retries_exhausted:2/1")
    assert task.status == TaskStatus.CANCELLED
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "attendance_review_closed")
    ).all()
    assert len(events) == 1


def test_closure_waits_for_review_then_settles_it(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        executive_meeting={"attendance_review": {"enabled": True, "max_wait_cycles": 1}},
    )
    _heads(tmp_env, enabled=False)
    result = start_goal(db_session, INTERNAL_GOAL)
    pid = result.project.id
    for t in result.tasks:
        t.status = TaskStatus.DONE
    for t in _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value):
        t.status = TaskStatus.DONE
    db_session.flush()
    review = result.attendance_review_task
    assert review is not None and review.status == TaskStatus.READY
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is None  # 1st wait
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is not None  # settled
    assert review.status == TaskStatus.CANCELLED


def test_autonomous_mock_completes_with_review(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        closure_loops={
            "still_needed": {"min_rounds": 1, "max_rounds": 1},
            "skip_phases": [
                "project_health",
                "product_fit",
                "usability",
                "customer_success",
                "it_ops",
                "compliance",
                "test_audit",
                "quality",
                "sales",
                "maintenance",
            ],
        },
    )
    result = start_goal(db_session, INTERNAL_GOAL)
    out = run_autonomous(db_session, project_id=result.project.id, runner="mock")
    assert result.project.status == ProjectStatus.DONE, out
    review = result.attendance_review_task
    assert review is not None and review.status == TaskStatus.DONE
    assert json.loads(review.constraints)["review_applied"] is True


# --- parser -----------------------------------------------------------------


def test_parse_attendance_review_shapes() -> None:
    ok = parse_attendance_review_result(json.dumps(_review(("legal", "x"), ("legal", "dup"))))
    assert ok.ok and ok.result is not None
    assert ok.result["summon"] == [{"department_id": "legal", "reason": "x"}]
    unknown = parse_attendance_review_result(json.dumps(_review(("nope", "x"))))
    assert unknown.ok and unknown.result is not None
    assert unknown.result["summon"] == []
    assert "summon_unknown_department_dropped" in unknown.result["notes"]
    for bad, err in (
        ({"status": "completed", "confidence": 0.5}, "missing_field:summon"),
        (_review(status="declined"), "invalid_status"),
        (_review(confidence=2), "confidence_out_of_range"),
        (_review(confidence=True), "invalid_confidence"),
        (_review(summon="legal"), "invalid_summon"),
        (_review(summon=[{"department_id": 1, "reason": "x"}]), "invalid_summon_item"),
    ):
        parsed = parse_attendance_review_result(json.dumps(bad))
        assert not parsed.ok
        assert str(parsed.error).startswith(err)
    assert parse_attendance_review_result("no json").error == "unparseable_or_missing_json"
