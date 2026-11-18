"""実 Cursor 検証（live2, 2026-11-18）で見つかった問題の回帰テスト.

1. 完了自問（closure）の結果に ``status`` が無く、正しい GAPS 回答が毎回失敗扱いになった。
   さらに coding 用の正規化が ``closure_verdict`` / ``proposed_tasks`` を捨てていた。
2. 上司の RETRY が読み取り専用の監査役に REPAIR（code.edit）を割り当て、
   権限拒否のまま 64 サイクルを空回りした。
3. 停止・失敗の表示に直接の原因（解析エラー等）が出ていなかった。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent, Execution, Task
from ai_company.enums import TaskKind, TaskStatus
from ai_company.services import worker_cursor as worker_cursor_mod
from ai_company.services import workers as workers_mod
from ai_company.services.autonomous import POLICY_DEADLOCK_CYCLES, continue_until_gate
from ai_company.services.cursor_runner import RunnerCapture
from ai_company.services.result_parser import parse_closure_result, parse_for_kind
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import _explain_block_reason, cause_ja
from ai_company.services.tasks import create_task
from ai_company.services.workers import failure_cause

# The shape a real completeness_auditor returned (Cursor json envelope, prose + JSON).
_GAPS = {
    "closure_verdict": "GAPS",
    "summary": "コアは足りるが、起動手順の文書が無い",
    "proposed_tasks": [
        {
            "title": "デモ起動・ログイン手順の文書化",
            "objective": "README に起動とログイン手順を書く",
            "role_required": "coding_worker",
            "acceptance_criteria": "文書だけで起動できる",
            "risk_level": "medium",
        }
    ],
    "evidence": [],
}


def _cursor_envelope(payload: dict[str, Any]) -> str:
    text = "残件監査を始めます。照合しました。" + json.dumps(payload, ensure_ascii=False)
    return json.dumps({"type": "result", "subtype": "success", "result": text})


# --- 1. closure parser ---------------------------------------------------------


def test_closure_result_without_status_is_success_and_keeps_proposals() -> None:
    parsed = parse_for_kind(_cursor_envelope(_GAPS), "closure")
    assert parsed.ok, parsed.error
    assert parsed.result is not None
    assert parsed.result["closure_verdict"] == "GAPS"
    assert parsed.result["proposed_tasks"][0]["title"] == "デモ起動・ログイン手順の文書化"
    assert parsed.result["summary"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"summary": "no verdict"}, "invalid_closure_verdict"),
        ({"closure_verdict": "MAYBE"}, "invalid_closure_verdict"),
        ({"closure_verdict": "CLEAR", "status": "failed"}, "status_not_completed"),
        ({"closure_verdict": "CLEAR", "status": "weird"}, "invalid_status"),
    ],
)
def test_closure_result_failures_stay_failures(payload: dict[str, Any], error: str) -> None:
    parsed = parse_closure_result(json.dumps(payload))
    assert not parsed.ok
    assert (parsed.error or "").startswith(error)


def test_closure_result_accepts_verdict_alias_and_caps_proposals() -> None:
    many = [{"title": f"t{i}", "objective": "x" * 10_000} for i in range(50)]
    parsed = parse_closure_result(
        json.dumps({"verdict": "gaps", "proposed_tasks": many + ["junk", 3]})
    )
    assert parsed.ok and parsed.result is not None
    assert parsed.result["closure_verdict"] == "GAPS"
    assert len(parsed.result["proposed_tasks"]) == 20
    assert len(parsed.result["proposed_tasks"][0]["objective"]) == 4_000


def _closure_task(db: Session, role: str = "completeness_auditor") -> Task:
    parent = create_task(db, title="core", priority=50)
    parent.status = TaskStatus.DONE
    db.flush()
    return create_task(
        db,
        title="本当にもう無いか？ #1",
        priority=95,
        project_id=parent.project_id,
        kind=TaskKind.STILL_NEEDED,
        role_required=role,
    )


def test_cursor_closure_gaps_spawns_tasks(
    db_session: Session, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = _closure_task(db_session)

    def fake_cli(task: Task, *, stdout_path: Path, **_: Any) -> RunnerCapture:
        out = _cursor_envelope(_GAPS)
        Path(stdout_path).write_text(out, encoding="utf-8")
        return RunnerCapture(
            exit_code=0, stdout=out, stderr="", timed_out=False, command=["agent"]
        )

    monkeypatch.setattr(worker_cursor_mod, "run_cursor_cli", fake_cli)
    sched = run_once(db_session, runner="cursor", max_to_start=1)
    assert sched.started == 1
    db_session.refresh(audit)
    assert audit.status == TaskStatus.DONE
    gaps = db_session.scalars(
        select(Task).where(Task.title == "デモ起動・ログイン手順の文書化")
    ).all()
    assert len(gaps) == 1 and gaps[0].kind == TaskKind.IMPLEMENTATION


# --- 2. supervisor RETRY on read-only work ---------------------------------------


def _finish_replan(db: Session, parent: Task, payload: dict[str, Any]) -> None:
    replan = create_task(
        db,
        title=f"Supervisor decision [secretary] #1: {parent.title}",
        priority=99,
        project_id=parent.project_id,
        parent_task_id=parent.id,
        kind=TaskKind.REPLAN,
        role_required="secretary",
    )
    worker = workers_mod.hire_worker(db, task=replan)
    execution = Execution(
        id=f"EXEC-T-{replan.id[-8:]}",
        task_id=replan.id,
        worker_id=worker.id,
        exit_code=0,
        success=True,
        estimated_cost=0.0,
        actual_cost=0.0,
        result_json=payload,
        runner_kind="mock",
    )
    db.add(execution)
    db.flush()
    workers_mod._finalize_execution(
        db, task=replan, worker=worker, execution=execution, success=True
    )


def test_retry_on_read_only_task_reopens_it_instead_of_repair(
    db_session: Session, tmp_env: Path
) -> None:
    audit = _closure_task(db_session)
    audit.status = TaskStatus.FAILED
    audit.retry_count = 3
    db_session.flush()

    guidance = {"decision": "RETRY", "instructions": "JSON に closure_verdict を必ず含める"}
    _finish_replan(db_session, audit, guidance)
    db_session.refresh(audit)
    assert audit.status == TaskStatus.READY
    assert audit.retry_count == 0
    assert audit.role_required == "completeness_auditor"
    assert "[上司の指示]" in (audit.objective or "")
    repairs = db_session.scalars(
        select(Task).where(Task.parent_task_id == audit.id, Task.kind == TaskKind.REPAIR)
    ).all()
    assert repairs == []
    events = {e.event_type for e in db_session.scalars(select(AuditEvent)).all()}
    assert "task_reopened_by_supervisor" in events

    # A second RETRY replaces the guidance instead of stacking it.
    _finish_replan(db_session, audit, {"decision": "RETRY", "instructions": "二回目"})
    db_session.refresh(audit)
    assert (audit.objective or "").count("[上司の指示]") == 1
    assert "二回目" in (audit.objective or "")


def test_reassign_read_only_task_to_role_without_permission_keeps_role(
    db_session: Session, tmp_env: Path
) -> None:
    audit = _closure_task(db_session)
    audit.status = TaskStatus.FAILED
    db_session.flush()
    _finish_replan(
        db_session,
        audit,
        {"decision": "REASSIGN", "target_role": "no_such_worker", "instructions": "x"},
    )
    db_session.refresh(audit)
    assert audit.status == TaskStatus.READY
    assert audit.role_required == "completeness_auditor"


def test_retry_on_write_task_still_creates_repair(db_session: Session, tmp_env: Path) -> None:
    impl = create_task(db_session, title="API 実装", priority=50, role_required="backend_engineer")
    impl.status = TaskStatus.FAILED
    db_session.flush()
    _finish_replan(db_session, impl, {"decision": "RETRY", "instructions": "fix"})
    repairs = db_session.scalars(
        select(Task).where(Task.parent_task_id == impl.id, Task.kind == TaskKind.REPAIR)
    ).all()
    assert len(repairs) == 1
    assert repairs[0].objective.startswith("Follow secretary guidance.")


# --- 2b. denied-only cycles stop instead of spinning -----------------------------


def test_policy_deadlock_stops_quickly(db_session: Session, tmp_env: Path) -> None:
    parent = create_task(db_session, title="audit", priority=50)
    # The live2 shape: a REPAIR (code.edit) task for a read-only role.
    create_task(
        db_session,
        title="Repair: 本当にもう無いか？ #1",
        priority=100,
        project_id=parent.project_id,
        parent_task_id=parent.id,
        kind=TaskKind.REPAIR,
        role_required="completeness_auditor",
    )
    parent.status = TaskStatus.FAILED
    db_session.flush()
    result = continue_until_gate(
        db_session, project_id=parent.project_id, max_cycles=80, runner="mock"
    )
    assert result.stopped_reason == "policy_denied"
    assert result.cycles == POLICY_DEADLOCK_CYCLES
    assert result.policy_denials and "code.edit" in result.policy_denials[0]


# --- 3. the underlying cause is shown --------------------------------------------


def test_failure_cause_and_japanese_label() -> None:
    ex = Execution(result_json={"parse_error": "missing_field:status"}, exit_code=0)
    assert failure_cause(ex) == "missing_field:status"
    assert "status" in cause_ja("missing_field:status")
    assert failure_cause(Execution(result_json={"evidence_error": "write_forbidden"})) == (
        "write_forbidden"
    )
    assert "書き込み禁止" in cause_ja("write_forbidden")
    assert failure_cause(None, timed_out=True) == "timeout"
    # Unsafe characters from anywhere never reach the explanation.
    assert " " not in failure_cause(Execution(result_json={"parse_error": "a b<script>"}))


def test_block_explanation_names_the_direct_cause() -> None:
    reason = (
        "manager_exhausted:supervisor_chain:after=secretary:"
        "retries_exhausted:3/2 (last_error=missing_field:status); task=TASK-1"
    )
    text = _explain_block_reason(reason)
    assert "直接の原因" in text and "status" in text
    assert "直接の原因" not in _explain_block_reason("retries_exhausted:3/2; task=TASK-1")


def test_next_actions_ask_a_person_when_only_denied_work_remains(
    db_session: Session, tmp_env: Path
) -> None:
    from ai_company.services.secretary import human_next_actions

    parent = create_task(db_session, title="audit", priority=50)
    repair = create_task(
        db_session,
        title="Repair: 本当にもう無いか？ #1",
        priority=100,
        project_id=parent.project_id,
        parent_task_id=parent.id,
        kind=TaskKind.REPAIR,
        role_required="completeness_auditor",
    )
    parent.status = TaskStatus.FAILED
    db_session.flush()
    text = human_next_actions(db_session, runner="cursor", stopped_reason="policy_denied")
    assert "人の判断が必要" in text
    assert f"task handoff-manager {repair.id}" in text
    assert "人の操作は不要" not in text
