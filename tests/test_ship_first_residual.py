"""Ship-first ignored findings become residual tasks (C-003)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import Task, ValidationPlan, ValidationResult
from ai_company.services.tasks import create_task
from ai_company.services.validation import finalize_judge, start_validation_gate
from tests.conftest import write_validation_override


def test_ship_first_enqueues_residual(db_session: Session, tmp_env: Path) -> None:
    write_validation_override(
        tmp_env,
        judge={"delivery_mode": "ship_first"},
        repair_loop={"auto_fire_stubborn_supervisor": False},
    )
    clear_config_cache()
    parent = create_task(db_session, title="mvp", priority=50, risk_level="low")
    start_validation_gate(db_session, parent=parent, source_execution_id="EXEC-S")
    db_session.refresh(parent)
    plan = db_session.get(ValidationPlan, parent.validation_plan_id)
    assert plan is not None
    db_session.add(
        ValidationResult(
            id="VRES-SF",
            plan_id=plan.id,
            task_id=parent.id,
            review_type="test",
            worker_id="W",
            execution_id="E",
            verdict="FAIL",
            findings=[{"severity": "high", "category": "spec", "description": "admin gap"}],
            evidence=[],
            confidence=0.8,
        )
    )
    db_session.flush()
    judges = list(
        db_session.scalars(
            select(Task).where(Task.parent_task_id == parent.id, Task.kind == "judge")
        ).all()
    )
    assert judges
    verdict = finalize_judge(db_session, judge_task=judges[0])
    assert verdict == "PASS"
    residuals = list(
        db_session.scalars(select(Task).where(Task.title.like("残件(ship_first):%"))).all()
    )
    assert residuals
