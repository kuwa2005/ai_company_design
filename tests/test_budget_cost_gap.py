"""M-1: a cost-cap DEFER below the raw cap (spent + estimate > cap) must stop
the autonomous loop as "budget_wait" instead of spinning to max_cycles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import BudgetEntry, Task
from ai_company.enums import BudgetKind
from ai_company.ids import new_id
from ai_company.services.autonomous import run_autonomous
from ai_company.services.cost_governor import budget_window_wait
from ai_company.services.secretary import start_goal
from tests.conftest import write_budget_override


def _cost_cap(tmp_env: Path, hourly_cost: float) -> None:
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": 1000, "max_cost_usd": hourly_cost},
            "daily": {"max_runs": 1000, "max_cost_usd": 0},
            "monthly": {"soft_limit_usd": 200.0, "hard_limit_usd": 250.0},
        },
    )
    clear_config_cache()


def _spend(session: Session, *, at: datetime, amount: float) -> None:
    session.add(
        BudgetEntry(
            id=new_id("BUD"),
            scope_type="task",
            scope_id="TASK-x",
            amount=amount,
            kind=BudgetKind.SPEND,
            created_at=at,
        )
    )
    session.flush()


def test_window_wait_uses_next_cost_like_evaluate(db_session: Session, tmp_env: Path) -> None:
    _cost_cap(tmp_env, 1.0)
    now = datetime.now(UTC).replace(tzinfo=None)
    for minutes in (50, 40, 30):
        _spend(db_session, at=now - timedelta(minutes=minutes), amount=0.3)
    # 0.9 < 1.0: without the next estimate this is not a cap hit (old behaviour)
    assert budget_window_wait(db_session) is None
    wait = budget_window_wait(db_session, next_cost=0.3)
    assert wait is not None and wait.reason == "hourly_max_cost"
    # dropping the oldest 0.3 leaves 0.6 + 0.3 <= 1.0 → resume ~10 min from now
    assert wait.resume_at is not None
    assert abs((wait.resume_at - (now + timedelta(minutes=10))).total_seconds()) < 5
    # a task larger than the cap itself never fits → resume time unknown
    never = budget_window_wait(db_session, next_cost=1.5)
    assert never is not None and never.resume_at is None


def test_autonomous_stops_on_cost_cap_defer_below_raw_cap(
    db_session: Session, tmp_env: Path
) -> None:
    _cost_cap(tmp_env, 1.0)
    result = start_goal(db_session, "社内向けの勤怠Webアプリを作る")
    db_session.execute(
        update(Task).where(Task.project_id == result.project.id).values(estimated_cost=0.3)
    )
    db_session.flush()
    auto = run_autonomous(
        db_session, project_id=result.project.id, max_cycles=60, runner="mock"
    )
    assert auto.stopped_reason == "budget_wait", auto.stopped_reason
    assert auto.cycles < 10, "must stop promptly, not spin to max_cycles"
    assert auto.budget_wait is not None
    assert auto.budget_wait.reason == "hourly_max_cost"
