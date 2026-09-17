"""Dismiss pedantic supervisors mid-process."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import JudgeDecision, Task, ValidationPlan, ValidationResult
from ai_company.enums import TaskStatus
from ai_company.services.dismissal import (
    fire_stubborn_supervisor,
    reasons_are_fireable,
)
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.tasks import create_task
from ai_company.services.validation import (
    apply_judge_hard_rules,
    finalize_judge,
    start_validation_gate,
)
from tests.conftest import write_validation_override


def test_reasons_fireable_vs_security() -> None:
    assert reasons_are_fireable("judge_reject:review_fail:test")
    assert reasons_are_fireable(["review_fail:test", "repair_loop_max_cycles:3"])
    assert not reasons_are_fireable("judge_reject:security_fail")
    assert not reasons_are_fireable("critical_finding:security")


def test_manual_fire_marks_done_and_unlocks_dependents(
    db_session: Session, tmp_env: Path
) -> None:
    parent = create_task(db_session, title="schema", priority=80, risk_level="high")
    child = create_task(
        db_session,
        title="api",
        priority=70,
        project_id=parent.project_id,
        depends_on=[parent.id],
    )
    db_session.refresh(child)
    assert child.status == TaskStatus.WAITING

    repair = create_task(
        db_session,
        title="Repair: schema",
        priority=85,
        project_id=parent.project_id,
        parent_task_id=parent.id,
        kind="repair",
    )
    parent.status = TaskStatus.BLOCKED
    db_session.flush()

    root = fire_stubborn_supervisor(
        db_session,
        task=parent,
        reason="manual_fire:ship_api",
        fired_role="test_reviewer",
        actor_id="human:owner",
    )
    db_session.refresh(root)
    db_session.refresh(repair)
    db_session.refresh(child)
    assert root.status == TaskStatus.DONE
    assert repair.status == TaskStatus.CANCELLED
    assert child.status == TaskStatus.READY


def test_auto_fire_after_stubborn_rejects(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(
        tmp_env,
        judge={"delivery_mode": "strict"},
        repair_loop={
            "max_cycles": 5,
            "manager_max_replans": 2,
            "auto_fire_stubborn_supervisor": True,
            "fire_after_non_security_rejects": 2,
        },
    )
    clear_config_cache()

    parent = create_task(
        db_session, title="loop", priority=70, risk_level="low", max_retries=5
    )
    # Seed one prior stubborn REJECT
    start_validation_gate(db_session, parent=parent, source_execution_id="EXEC-A")
    db_session.refresh(parent)
    plan1 = db_session.get(ValidationPlan, parent.validation_plan_id)
    assert plan1 is not None
    db_session.add(
        ValidationResult(
            id="VRES-A",
            plan_id=plan1.id,
            task_id=parent.id,
            review_type="test",
            worker_id="W",
            execution_id="E1",
            verdict="FAIL",
            findings=[{"severity": "medium", "category": "nit", "description": "x"}],
            evidence=[],
            confidence=0.9,
        )
    )
    db_session.add(
        JudgeDecision(
            id="JUDGE-A",
            plan_id=plan1.id,
            task_id=parent.id,
            verdict="REJECT",
            reasons=["review_fail:test"],
            conflicting_reviews=[],
            repair_tasks=[],
        )
    )
    db_session.flush()

    # Second plan + FAIL → finalize should fire
    start_validation_gate(db_session, parent=parent, source_execution_id="EXEC-B")
    db_session.refresh(parent)
    plan2 = db_session.get(ValidationPlan, parent.validation_plan_id)
    assert plan2 is not None
    db_session.add(
        ValidationResult(
            id="VRES-B",
            plan_id=plan2.id,
            task_id=parent.id,
            review_type="test",
            worker_id="W",
            execution_id="E2",
            verdict="FAIL",
            findings=[{"severity": "medium", "category": "nit", "description": "y"}],
            evidence=[],
            confidence=0.9,
        )
    )
    db_session.flush()
    verdict, _, _ = apply_judge_hard_rules(db_session, plan=plan2)
    assert verdict == "REJECT"
    judge = db_session.scalar(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.kind == "judge",
            Task.validation_plan_id == plan2.id,
        )
    )
    assert judge is not None
    finalize_judge(db_session, judge_task=judge)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE


def test_security_fail_does_not_auto_fire(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(
        tmp_env,
        judge={"delivery_mode": "strict"},
        repair_loop={
            "auto_fire_stubborn_supervisor": True,
            "fire_after_non_security_rejects": 1,
            "manager_max_replans": 0,
        },
    )
    clear_config_cache()
    parent = create_task(db_session, title="auth", priority=80, risk_level="high")
    result = escalate_blocked_task(
        db_session,
        task=parent,
        reason="judge_reject:security_fail",
    )
    db_session.refresh(parent)
    # Security → human (manager_max 0), not fired DONE
    assert parent.status == TaskStatus.BLOCKED
    assert getattr(result, "action", None) == "task.unblock_or_replan"


def test_ship_first_soft_passes_test_fail(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(tmp_env, judge={"delivery_mode": "ship_first"})
    clear_config_cache()
    parent = create_task(db_session, title="mvp", priority=50, risk_level="low")
    start_validation_gate(db_session, parent=parent, source_execution_id="EXEC-S")
    db_session.refresh(parent)
    plan = db_session.get(ValidationPlan, parent.validation_plan_id)
    assert plan is not None
    db_session.add(
        ValidationResult(
            id="VRES-S",
            plan_id=plan.id,
            task_id=parent.id,
            review_type="test",
            worker_id="W",
            execution_id="E",
            verdict="FAIL",
            findings=[{"severity": "high", "category": "nit", "description": "gap"}],
            evidence=[],
            confidence=0.8,
        )
    )
    db_session.flush()
    verdict, reasons, _ = apply_judge_hard_rules(db_session, plan=plan)
    assert verdict == "PASS"
    assert any("ship_first" in r for r in reasons)
