"""Queue / dependency / concurrency / budget / retry / circuit tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache, load_config
from ai_company.db.models import AuditEvent, BudgetEntry, Task, Worker
from ai_company.enums import BudgetKind, TaskStatus, WorkerStatus
from ai_company.services import cost_governor as cg
from ai_company.services.scheduler import run_once
from ai_company.services.tasks import create_task
from tests.conftest import write_budget_override, write_settings_override


def test_queue_ready_on_create(db_session: Session, tmp_env: Path) -> None:
    task = create_task(db_session, title="t1", priority=60)
    assert task.status == TaskStatus.READY


def test_dependency_waiting_then_ready(db_session: Session, tmp_env: Path) -> None:
    a = create_task(db_session, title="schema", priority=80)
    b = create_task(db_session, title="api", priority=70, depends_on=[a.id])
    assert a.status == TaskStatus.READY
    assert b.status == TaskStatus.WAITING

    # coding then review for A (need 2 slots across cycles if max_to_start=1)
    run_once(db_session, max_to_start=1)
    run_once(db_session, max_to_start=1)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.status == TaskStatus.DONE
    assert b.status == TaskStatus.READY

    run_once(db_session, max_to_start=2)
    db_session.refresh(b)
    assert b.status == TaskStatus.DONE


def test_concurrency_cap(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        scheduler={"max_concurrent_workers": 2},
        review={"enabled": False},
    )
    clear_config_cache()
    for i in range(10):
        create_task(db_session, title=f"t{i}", priority=50)

    result = run_once(db_session, max_to_start=2)
    assert result.started == 2
    done = list(db_session.scalars(select(Task).where(Task.status == TaskStatus.DONE)).all())
    ready = list(db_session.scalars(select(Task).where(Task.status == TaskStatus.READY)).all())
    assert len(done) == 2
    assert len(ready) == 8


def test_hard_budget_blocks_workers(db_session: Session, tmp_env: Path) -> None:
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": 100, "max_cost_usd": 100},
            "daily": {"max_runs": 100, "max_cost_usd": 100},
            "monthly": {"soft_limit_usd": 0.05, "hard_limit_usd": 0.05},
        },
    )
    clear_config_cache()
    task = create_task(db_session, title="expensive", priority=90, estimated_cost=0.10)
    result = run_once(db_session)
    assert result.started == 0
    assert result.denied >= 1
    db_session.refresh(task)
    assert task.status == TaskStatus.DEFERRED_BUDGET
    workers = list(db_session.scalars(select(Worker)).all())
    assert workers == []


def test_soft_budget_throttle_reduces_capacity(db_session: Session, tmp_env: Path) -> None:
    # Spend enough to enter critical tier (0.95 of soft=200 => 190)
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": 1000, "max_cost_usd": 1000},
            "daily": {"max_runs": 1000, "max_cost_usd": 1000},
            "monthly": {"soft_limit_usd": 200, "hard_limit_usd": 250},
        },
    )
    clear_config_cache()
    # Inject prior spend
    db_session.add(
        BudgetEntry(
            id="BUD-PRESPEND",
            scope_type="task",
            scope_id="prespend",
            amount=190.0,
            kind=BudgetKind.SPEND,
        )
    )
    db_session.flush()

    for i in range(5):
        create_task(db_session, title=f"t{i}", priority=90, estimated_cost=0.1)

    cfg = load_config()
    snap = cg.get_budget_snapshot(db_session, cfg)
    assert snap.soft_usage_ratio >= 0.95

    # critical max_workers=1
    result = run_once(db_session, max_to_start=5)
    assert result.started == 1


def test_retry_then_blocked(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        retry={"default_max_retries": 1},
        review={"enabled": False},
    )
    clear_config_cache()
    task = create_task(db_session, title="flaky", priority=50, max_retries=1)

    # fail until blocked: retry_count goes 1 then 2 > max_retries 1
    r1 = run_once(db_session, mock_succeed=False)
    assert r1.started == 1
    db_session.refresh(task)
    assert task.status == TaskStatus.READY
    assert task.retry_count == 1

    r2 = run_once(db_session, mock_succeed=False)
    assert r2.started == 1
    db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    assert task.retry_count == 2

    from ai_company.db.models import Approval
    from ai_company.enums import ApprovalStatus

    approvals = list(db_session.scalars(select(Approval)).all())
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.PENDING


def test_circuit_breaker(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        circuit_breaker={"failure_threshold": 2, "window_minutes": 10, "cooldown_minutes": 30},
        retry={"default_max_retries": 10},
    )
    clear_config_cache()

    t1 = create_task(db_session, title="f1", priority=50)
    t2 = create_task(db_session, title="f2", priority=50)
    t3 = create_task(db_session, title="f3", priority=50)

    run_once(db_session, mock_succeed=False, max_to_start=1)
    run_once(db_session, mock_succeed=False, max_to_start=1)

    # circuit should be open now
    open_, _ = cg.is_circuit_open(db_session)
    assert open_ is True

    result = run_once(db_session, mock_succeed=True, max_to_start=1)
    assert result.started == 0
    assert result.denied >= 1

    events = {
        e.event_type for e in db_session.scalars(select(AuditEvent)).all()
    }
    assert "circuit_opened" in events


def test_budget_reservation_no_double_spend(db_session: Session, tmp_env: Path) -> None:
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": 100, "max_cost_usd": 100},
            "daily": {"max_runs": 100, "max_cost_usd": 100},
            "monthly": {"soft_limit_usd": 0.15, "hard_limit_usd": 0.15},
        },
    )
    write_settings_override(tmp_env, review={"enabled": False})
    clear_config_cache()
    # Two tasks each costing 0.10; only one should fit under hard 0.15
    a = create_task(db_session, title="a", priority=80, estimated_cost=0.10)
    b = create_task(db_session, title="b", priority=70, estimated_cost=0.10)

    result = run_once(db_session, max_to_start=2)
    assert result.started == 1
    db_session.refresh(a)
    db_session.refresh(b)
    statuses = {a.status, b.status}
    assert TaskStatus.DONE in statuses
    assert TaskStatus.DEFERRED_BUDGET in statuses or TaskStatus.READY in statuses
