"""Review / escalation / lease reclaim tests."""

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
from tests.conftest import write_settings_override


def test_review_pass_marks_parent_done(db_session: Session, tmp_env: Path) -> None:
    parent = create_task(db_session, title="feature", priority=70)
    result = run_once(db_session, max_to_start=4)
    assert result.started >= 2  # coding + review
    db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE
    review = db_session.scalar(select(Task).where(Task.parent_task_id == parent.id))
    assert review is not None
    assert review.status == TaskStatus.DONE


def test_review_reject_retries_parent(db_session: Session, tmp_env: Path) -> None:
    parent = create_task(db_session, title="feature", priority=70, max_retries=2)
    # First cycle: coding succeeds, review will be created. Force review fail by
    # running coding success then failing subsequent (review) via second call.
    run_once(db_session, mock_succeed=True, max_to_start=1)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.REVIEW
    review = db_session.scalar(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.READY)
    )
    assert review is not None

    run_once(db_session, mock_succeed=False, max_to_start=1)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.READY
    assert parent.retry_count == 1


def test_blocked_escalation_and_approval_requeue(
    db_session: Session, tmp_env: Path
) -> None:
    write_settings_override(tmp_env, review={"enabled": False})
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
    write_settings_override(tmp_env, review={"enabled": False})
    clear_config_cache()
    task = create_task(db_session, title="leased", priority=50)
    assert acquire_lease(db_session, task, holder="crashed-scheduler", minutes=10)
    # Expire lease
    task.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db_session.flush()

    n = reclaim_expired_leases(db_session)
    assert n == 1
    db_session.refresh(task)
    assert task.leased_by is None

    # New scheduler can take it
    result = run_once(db_session, max_to_start=1)
    assert result.started == 1
    db_session.refresh(task)
    assert task.status == TaskStatus.DONE


def test_zombie_cleanup(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        worker={"hard_timeout_minutes": 0},  # immediate zombie
        review={"enabled": False},
    )
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
