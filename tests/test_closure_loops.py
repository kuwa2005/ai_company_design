"""Closure loops: still-needed / usability / quality before release."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import Task
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.services.autonomous import run_autonomous
from ai_company.services.closure_loops import (
    apply_closure_result,
    closure_loops_finished,
    maybe_spawn_closure_loop,
)
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import start_goal
from ai_company.services.tasks import create_task
from tests.conftest import write_settings_override, write_validation_override


def test_gaps_spawn_implementation_tasks(db_session: Session, tmp_env: Path) -> None:
    parent = create_task(db_session, title="core", priority=50)
    parent.status = TaskStatus.DONE
    db_session.flush()
    audit = create_task(
        db_session,
        title="本当にもう無いか？ #1",
        priority=95,
        project_id=parent.project_id,
        kind=TaskKind.STILL_NEEDED,
        role_required="completeness_auditor",
    )
    created = apply_closure_result(
        db_session,
        task=audit,
        payload={
            "closure_verdict": "GAPS",
            "summary": "admin UI missing",
            "proposed_tasks": [
                {
                    "title": "管理者画面 S5-S7",
                    "objective": "Implement admin screens",
                    "role_required": "frontend_engineer",
                    "acceptance_criteria": "S5 S6 S7 reachable",
                }
            ],
        },
    )
    db_session.refresh(audit)
    assert audit.status == TaskStatus.DONE
    assert len(created) == 1
    gap = db_session.get(Task, created[0])
    assert gap is not None
    assert gap.kind == TaskKind.IMPLEMENTATION
    assert gap.status == TaskStatus.READY
    assert "管理者" in gap.title


def test_spawn_still_needed_when_impl_done(db_session: Session, tmp_env: Path) -> None:
    write_validation_override(
        tmp_env,
        closure_loops={
            "enabled": True,
            "still_needed": {"max_rounds": 3, "consecutive_clear_to_advance": 2},
            "usability": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "quality": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
        },
    )
    clear_config_cache()
    t = create_task(db_session, title="only", priority=50)
    t.status = TaskStatus.DONE
    db_session.flush()
    spawned = maybe_spawn_closure_loop(db_session, project_id=t.project_id)
    assert spawned is not None
    assert spawned.kind == TaskKind.STILL_NEEDED
    assert "無いか" in spawned.title


def test_autonomous_runs_closure_before_release(
    db_session: Session, tmp_env: Path
) -> None:
    write_settings_override(
        tmp_env,
        scheduler={"max_validation_workers": 8, "max_concurrent_workers": 10},
        staffing={
            "auto_approve": True,
            "role_capacity": {
                "coding_worker": 8,
                "reviewer": 8,
                "test_reviewer": 4,
                "judge": 4,
                "completeness_auditor": 2,
                "project_coordinator": 2,
                "product_planner": 2,
                "usability_auditor": 2,
                "customer_success": 2,
                "it_operations": 2,
                "compliance_auditor": 2,
                "test_auditor": 2,
                "quality_auditor": 2,
                "sales": 2,
                "maintenance": 2,
            },
        },
    )
    write_validation_override(
        tmp_env,
        closure_loops={
            "enabled": True,
            "still_needed": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "project_health": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "product_fit": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "usability": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "customer_success": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "it_operations": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "compliance": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "test_audit": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "quality": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "sales": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
            "maintenance": {"max_rounds": 1, "consecutive_clear_to_advance": 1},
        },
    )
    clear_config_cache()
    result = start_goal(db_session, "小さなユーティリティを作って")
    auto = run_autonomous(
        db_session,
        project_id=result.project.id,
        max_cycles=160,
        runner="mock",
        mock_succeed=True,
    )
    assert auto.stopped_reason == "all_implementation_done"
    assert auto.release_candidate_id is not None
    db_session.refresh(result.project)
    assert result.project.status == ProjectStatus.DONE
    assert closure_loops_finished(db_session, result.project.id)

    kinds = {
        t.kind
        for t in db_session.scalars(
            select(Task).where(Task.project_id == result.project.id)
        ).all()
    }
    assert TaskKind.STILL_NEEDED in kinds or "still_needed" in kinds
    assert TaskKind.PROJECT_HEALTH in kinds or "project_health" in kinds
    assert TaskKind.PRODUCT_FIT in kinds or "product_fit" in kinds
    assert TaskKind.CUSTOMER_SUCCESS in kinds or "customer_success" in kinds
    assert TaskKind.IT_OPS_AUDIT in kinds or "it_ops_audit" in kinds
    assert TaskKind.COMPLIANCE_AUDIT in kinds or "compliance_audit" in kinds
    assert TaskKind.TEST_AUDIT in kinds or "test_audit" in kinds
    assert TaskKind.QUALITY_AUDIT in kinds or "quality_audit" in kinds
    assert TaskKind.SALES_AUDIT in kinds or "sales_audit" in kinds
    assert TaskKind.MAINTENANCE_AUDIT in kinds or "maintenance_audit" in kinds


def test_scheduler_spawns_closure_on_idle_impl(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    t = create_task(db_session, title="done-impl", priority=50)
    t.status = TaskStatus.DONE
    db_session.flush()
    run_once(db_session, max_to_start=1, mock_succeed=True)
    sn = db_session.scalar(
        select(Task).where(
            Task.project_id == t.project_id,
            Task.kind == TaskKind.STILL_NEEDED,
        )
    )
    assert sn is not None
