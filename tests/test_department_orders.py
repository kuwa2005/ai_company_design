"""重役会議の決議 → 部門作業指示（Work Order）の起票."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache, load_config
from ai_company.db.models import AuditEvent, Task, TaskDependency
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.services.autonomous import run_autonomous
from ai_company.services.closure_loops import maybe_spawn_closure_loop
from ai_company.services.department_orders import (
    WorkOrder,
    derive_work_orders,
    issue_work_orders,
    validate_work_order,
)
from ai_company.services.executive_meeting import (
    convene_executive_meeting,
    render_minutes_markdown,
)
from ai_company.services.policy_engine import infer_dispatch_action, role_allows_writes
from ai_company.services.secretary import TaskProposal, start_goal, understand_goal
from tests.conftest import (
    write_budget_override,
    write_settings_override,
    write_validation_override,
)

SAAS_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい"


def _order_tasks(session: Session, project_id: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.kind == TaskKind.DEPARTMENT_WORK)
            .order_by(Task.created_at.asc())
        ).all()
    )


def _audit_sequence(session: Session) -> list[tuple[int, str, str]]:
    """(rowid, event_type, target_id) in insertion order.

    Audit events are append-only, so SQLite's implicit rowid is monotonic and
    reflects the order record_audit() calls were made, unlike created_at.
    """
    session.flush()
    rows = session.execute(
        select(literal_column("rowid"), AuditEvent.event_type, AuditEvent.target_id)
        .select_from(AuditEvent)
        .order_by(literal_column("rowid"))
    ).all()
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _key(task: Task) -> str:
    return json.loads(task.constraints)["work_order_key"]


# --- pure derivation -------------------------------------------------------


def test_only_summoned_departments_get_orders(tmp_env: Path) -> None:
    plan = understand_goal("社内用のCLIツールを作る")
    minutes = convene_executive_meeting(plan)
    assert minutes.work_orders == []
    assert minutes.deferred_orders == []

    minutes = convene_executive_meeting(understand_goal(SAAS_GOAL))
    keys = [o.key for o in minutes.work_orders]
    assert keys == ["sales.proposal", "sales.onboarding", "marketing.value", "legal.precheck"]
    for o in minutes.work_orders:
        assert o.department_id in minutes.attendees
        assert validate_work_order(o) == []


def test_caps_are_deterministic_and_deferred_are_recorded(tmp_env: Path) -> None:
    plan = understand_goal(SAAS_GOAL)
    every = ["sales", "marketing", "legal", "operations"]
    orders, deferred = derive_work_orders(plan.goal, every, plan.tasks)
    # max_orders_per_meeting = 4 (DEPARTMENTS order → template order)
    assert [o.key for o in orders] == [
        "sales.proposal",
        "sales.onboarding",
        "marketing.value",
        "legal.precheck",
    ]
    assert [(d.order.key, "1会議" in d.reason) for d in deferred] == [
        ("operations.premise", True)
    ]
    again = derive_work_orders(plan.goal, every, plan.tasks)
    assert [o.key for o in again[0]] == [o.key for o in orders]

    write_settings_override(
        tmp_env,
        meeting_resolutions={"max_orders_per_department": 1, "max_orders_per_meeting": 10},
    )
    orders, deferred = derive_work_orders(plan.goal, every, plan.tasks)
    assert "sales.onboarding" not in [o.key for o in orders]
    assert any(d.order.key == "sales.onboarding" and "1部" in d.reason for d in deferred)
    assert "operations.premise" in [o.key for o in orders]


def test_priority_ceiling_whitelist_and_disable(tmp_env: Path) -> None:
    plan = understand_goal(SAAS_GOAL)
    write_settings_override(
        tmp_env, meeting_resolutions={"priority_ceiling": 60, "departments": ["legal"]}
    )
    orders, _ = derive_work_orders(plan.goal, ["sales", "legal"], plan.tasks)
    assert [o.key for o in orders] == ["legal.precheck"]
    assert orders[0].priority == 60

    write_settings_override(tmp_env, meeting_resolutions={"enabled": False})
    assert derive_work_orders(plan.goal, ["sales", "legal"], plan.tasks) == ([], [])


def test_planned_unit_suppresses_template(tmp_env: Path) -> None:
    plan = understand_goal(SAAS_GOAL)
    tasks = [*plan.tasks, TaskProposal(title="提案書", objective="x", role_required="sales")]
    orders, deferred = derive_work_orders(plan.goal, ["sales"], tasks)
    assert [o.key for o in orders] == ["sales.onboarding"]
    # dependency on the suppressed order is dropped, not left dangling
    assert orders[0].depends_on_keys == []
    assert [d.order.key for d in deferred] == ["sales.proposal"]


def test_validate_work_order_rejects_cross_department() -> None:
    bad = WorkOrder(
        key="sales.legal_check",
        department_id="sales",
        section_id="compliance",
        role_required="compliance_auditor",
        title="x",
        objective="x",
        acceptance_criteria="x",
        priority=50,
    )
    problems = validate_work_order(bad)
    assert any(p.startswith("unknown_section") for p in problems)
    bad.key = "legal.check"
    bad.department_id = "legal"
    bad.role_required = "sales"
    problems = validate_work_order(bad)
    assert not any(p.startswith("key_prefix_mismatch") for p in problems)
    assert any(p.startswith("role_not_in_section") for p in problems)


def test_minutes_show_orders_and_deferred(tmp_env: Path) -> None:
    minutes = convene_executive_meeting(understand_goal(SAAS_GOAL), all_departments=True)
    md = render_minutes_markdown(minutes)
    assert "## 決定事項：部門別追加タスク" in md
    assert "legal.precheck" in md and "コンプライアンス課" in md
    assert "### 見送り" in md and "operations.premise" in md


# --- DB --------------------------------------------------------------------


def test_start_goal_issues_orders_with_units_and_is_idempotent(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    tasks = _order_tasks(db_session, pid)
    assert [_key(t) for t in tasks] == [
        "sales.proposal",
        "sales.onboarding",
        "marketing.value",
        "legal.precheck",
    ]
    assert [t.id for t in result.work_order_tasks] == [t.id for t in tasks]
    by_key = {_key(t): t for t in tasks}
    legal = by_key["legal.precheck"]
    assert (legal.department_id, legal.section_id) == ("legal", "compliance")
    assert legal.role_required == "compliance_auditor"
    assert legal.status == TaskStatus.READY  # runs in parallel with design
    assert all(t.priority <= 80 for t in tasks)
    # constraints carry the idempotency key and the meeting tag
    assert json.loads(legal.constraints)["meeting"] == f"{pid}_kickoff"

    # onboarding waits for the proposal; proposal waits for the core build
    onboarding = by_key["sales.onboarding"]
    dep_ids = set(
        db_session.scalars(
            select(TaskDependency.depends_on_task_id).where(
                TaskDependency.task_id == onboarding.id
            )
        ).all()
    )
    assert by_key["sales.proposal"].id in dep_ids
    assert onboarding.status == TaskStatus.WAITING

    # re-issuing the same resolution never duplicates work
    again = issue_work_orders(
        db_session, project_id=pid, orders=result.meeting.work_orders  # type: ignore[union-attr]
    )
    assert again.issued == []
    assert {d.reason for d in again.skipped} == {"起票済み（冪等）"}
    assert len(_order_tasks(db_session, pid)) == 4

    issued_events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "meeting_work_order_issued")
    ).all()
    assert len(issued_events) == 4


def test_deferred_orders_are_audited(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS_GOAL, all_departments=True)
    keys = [_key(t) for t in _order_tasks(db_session, result.project.id)]
    assert "operations.premise" not in keys
    deferred = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "meeting_work_order_deferred")
    ).all()
    assert any(e.metadata_json.get("key") == "operations.premise" for e in deferred)


def test_department_work_is_read_only_and_never_gated(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    task = result.work_order_tasks[0]
    assert infer_dispatch_action(task) == "code.read"
    assert role_allows_writes(load_config(), task.role_required, task) is False


def test_closure_loop_waits_for_open_department_work(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    for t in result.tasks:
        t.status = TaskStatus.DONE
    db_session.flush()
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is None
    for t in _order_tasks(db_session, pid):
        t.status = TaskStatus.DONE
    db_session.flush()
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is not None


def test_autonomous_mock_completes_with_work_orders(
    db_session: Session, tmp_env: Path
) -> None:
    write_settings_override(
        tmp_env,
        scheduler={"max_validation_workers": 8, "max_concurrent_workers": 10},
    )
    phases = {
        k: {"max_rounds": 1, "consecutive_clear_to_advance": 1}
        for k in (
            "still_needed",
            "project_health",
            "product_fit",
            "usability",
            "customer_success",
            "it_operations",
            "compliance",
            "test_audit",
            "quality",
            "sales",
            "maintenance",
        )
    }
    write_validation_override(tmp_env, closure_loops={"enabled": True, **phases})
    # ~43 worker runs: keep well clear of the rolling-window caps so this test
    # checks completion, not runaway protection (budget_wait has its own tests).
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": 1000, "max_cost_usd": 0},
            "daily": {"max_runs": 1000, "max_cost_usd": 0},
            "monthly": {"soft_limit_usd": 200.0, "hard_limit_usd": 250.0},
        },
    )
    clear_config_cache()
    result = start_goal(db_session, SAAS_GOAL)
    auto = run_autonomous(
        db_session,
        project_id=result.project.id,
        max_cycles=300,
        runner="mock",
        mock_succeed=True,
    )
    assert auto.release_candidate_id is not None, auto.stopped_reason
    assert auto.budget_wait is None
    db_session.refresh(result.project)
    assert result.project.status == ProjectStatus.DONE
    orders = _order_tasks(db_session, result.project.id)
    assert len(orders) == 4
    assert all(t.status == TaskStatus.DONE for t in orders)
    # the first closure task is created only after every work order finished.
    # Judged by audit-event *insertion order* (SQLite rowid), not created_at:
    # created_at is second-precision CURRENT_TIMESTAMP and rows pending in one
    # flush are inserted in mapper order, so timestamps can't order events.
    seq = _audit_sequence(db_session)
    closure_ids = set(
        db_session.scalars(
            select(Task.id).where(
                Task.project_id == result.project.id, Task.kind == TaskKind.STILL_NEEDED
            )
        ).all()
    )
    closure_created = [
        rowid for rowid, ev, target in seq if ev == "task_created" and target in closure_ids
    ]
    assert closure_created, "no still_needed closure task was created"
    first_closure_seq = min(closure_created)
    for t in orders:
        completed = [
            rowid for rowid, ev, target in seq if ev == "task_completed" and target == t.id
        ]
        assert completed, f"work order {t.id} has no task_completed event"
        assert min(completed) < first_closure_seq, (
            f"closure created (seq {first_closure_seq}) before work order {t.id} "
            f"completed (seq {min(completed)})"
        )
    # department work never went through the code validation gate
    assert all(t.validation_plan_id is None for t in orders)
