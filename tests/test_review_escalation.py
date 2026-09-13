"""Validation gate / judge / lease reclaim tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import Approval, Task, Worker
from ai_company.enums import ApprovalStatus, TaskStatus, WorkerStatus
from ai_company.services.cleanup import reclaim_expired_leases, run_cleanup
from ai_company.services.escalation import resolve_approval
from ai_company.services.scheduler import acquire_lease, run_once
from ai_company.services.tasks import create_task
from tests.conftest import write_settings_override, write_validation_override


def test_judge_pass_marks_parent_done(db_session: Session, tmp_env: Path) -> None:
    parent = create_task(db_session, title="feature", priority=70, risk_level="low")
    result = run_once(db_session, max_to_start=8)
    assert result.started >= 3  # coding + test + judge
    db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE


def test_validation_fail_triggers_judge_reject(
    db_session: Session, tmp_env: Path
) -> None:
    parent = create_task(
        db_session, title="feature", priority=70, risk_level="low", max_retries=2
    )
    # coding only
    run_once(db_session, mock_succeed=True, max_to_start=1)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.REVIEW_REQUIRED

    # fail validation reviewer
    run_once(db_session, mock_succeed=False, max_to_start=1)
    # judge can run after validation DONE (even with FAIL)
    run_once(db_session, mock_succeed=True, max_to_start=1)
    db_session.refresh(parent)
    # REJECT with retries left → repair path (FAILED + repair task) or READY/repair
    assert parent.status in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.READY}
    repairs = list(
        db_session.scalars(
            select(Task).where(Task.parent_task_id == parent.id, Task.kind == "repair")
        ).all()
    )
    assert repairs or parent.status == TaskStatus.BLOCKED


def test_blocked_escalation_goes_to_manager_first(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    task = create_task(db_session, title="hopeless", priority=50, max_retries=0)
    run_once(db_session, mock_succeed=False, max_to_start=1)
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    replan = db_session.scalar(
        select(Task).where(Task.parent_task_id == task.id, Task.kind == "replan")
    )
    assert replan is not None
    assert replan.role_required == "engineering_manager"
    assert db_session.scalar(select(Approval)) is None


def test_human_approval_requeue_after_manager_exhausted(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(
        tmp_env,
        validation={"enabled": False},
        repair_loop={"manager_max_replans": 0},
    )
    clear_config_cache()
    task = create_task(db_session, title="hopeless", priority=50, max_retries=0)
    run_once(db_session, mock_succeed=False, max_to_start=1)
    db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    approval = db_session.scalar(select(Approval))
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING

    resolve_approval(db_session, approval_id=approval.id, approve=True, note="retry once")
    db_session.refresh(task)
    assert task.status == TaskStatus.READY
    assert task.retry_count == 0


def test_expired_lease_reclaimed_no_duplicate_hold(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    task = create_task(db_session, title="leased", priority=50)
    assert acquire_lease(db_session, task, holder="crashed-scheduler", minutes=10)
    task.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db_session.flush()

    n = reclaim_expired_leases(db_session)
    assert n == 1
    db_session.refresh(task)
    assert task.leased_by is None

    result = run_once(db_session, max_to_start=1)
    assert result.started == 1
    db_session.refresh(task)
    assert task.status == TaskStatus.DONE


def test_zombie_cleanup(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(tmp_env, worker={"hard_timeout_minutes": 0})
    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    task = create_task(db_session, title="stuck", priority=50)
    task.status = TaskStatus.RUNNING
    worker = Worker(
        id="WORKER-ZOMBIE",
        role="coding_worker",
        project_id=task.project_id,
        task_id=task.id,
        status=WorkerStatus.ACTIVE,
        hired_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        cost_estimated=0.1,
    )
    db_session.add(worker)
    db_session.flush()

    result = run_cleanup(db_session)
    assert result.zombies_killed == 1
    db_session.refresh(worker)
    db_session.refresh(task)
    assert worker.status == WorkerStatus.KILLED
    assert task.status == TaskStatus.READY
