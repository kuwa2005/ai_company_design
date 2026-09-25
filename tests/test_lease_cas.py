"""Atomic lease compare-and-swap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ai_company.services.scheduler import acquire_lease
from ai_company.services.tasks import create_task


def test_lease_cas_blocks_second_holder(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="lease-me", priority=50)
    assert acquire_lease(db_session, task, holder="sched-a", minutes=10)
    assert not acquire_lease(db_session, task, holder="sched-b", minutes=10)
    # Same holder can refresh
    assert acquire_lease(db_session, task, holder="sched-a", minutes=10)


def test_lease_cas_allows_after_expiry(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="lease-exp", priority=50)
    assert acquire_lease(db_session, task, holder="sched-a", minutes=10)
    task.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    db_session.flush()
    assert acquire_lease(db_session, task, holder="sched-b", minutes=10)
