"""Rolling-window budget caps stop the autonomous loop as "budget_wait".

Before: hourly ``max_runs`` returned DEFER, ``revive_deferred_budget_tasks``
put the task back to READY every cycle, and the loop spun hundreds of cycles
in a second until ``max_cycles`` — with guidance saying "no human action
needed, run scheduler continue once", which re-spun for the rest of the hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_company.config import clear_config_cache
from ai_company.db.models import BudgetEntry
from ai_company.enums import BudgetKind
from ai_company.ids import new_id
from ai_company.services.autonomous import continue_until_gate, run_autonomous
from ai_company.services.cost_governor import budget_window_wait
from ai_company.services.secretary import human_next_actions, start_goal
from tests.conftest import write_budget_override

GOAL = "社内向けの勤怠Webアプリを作る"


def _caps(tmp_env: Path, *, hourly_runs: int = 1000, daily_runs: int = 1000) -> None:
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": hourly_runs, "max_cost_usd": 0},
            "daily": {"max_runs": daily_runs, "max_cost_usd": 0},
            "monthly": {"soft_limit_usd": 200.0, "hard_limit_usd": 250.0},
        },
    )
    clear_config_cache()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _spend(session: Session, *, at: datetime, amount: float = 0.0) -> None:
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


def test_window_wait_none_below_cap(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, hourly_runs=3)
    now = _utcnow()
    _spend(db_session, at=now - timedelta(minutes=10))
    _spend(db_session, at=now - timedelta(minutes=5))
    assert budget_window_wait(db_session) is None


def test_window_wait_resume_time_is_when_oldest_run_leaves_window(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env, hourly_runs=3)
    now = _utcnow()
    _spend(db_session, at=now - timedelta(hours=2))  # outside the window
    for minutes in (50, 40, 30):
        _spend(db_session, at=now - timedelta(minutes=minutes))
    wait = budget_window_wait(db_session)
    assert wait is not None
    assert wait.reason == "hourly_max_runs"
    assert (wait.used, wait.limit) == (3, 3)
    assert wait.resume_at is not None
    # oldest in-window run (50 min ago) leaves the window in ~10 minutes
    assert abs((wait.resume_at - (now + timedelta(minutes=10))).total_seconds()) <= 2
    assert wait.wait_minutes(now) in {10, 11}
    assert "直近1時間" in wait.reason_ja


def test_window_wait_reports_daily_cap(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, hourly_runs=1000, daily_runs=2)
    now = _utcnow()
    _spend(db_session, at=now - timedelta(hours=5))
    _spend(db_session, at=now - timedelta(hours=3))
    wait = budget_window_wait(db_session)
    assert wait is not None and wait.reason == "daily_max_runs"
    assert wait.resume_at is not None
    assert wait.resume_at > now + timedelta(hours=18)


def test_autonomous_stops_as_budget_wait_instead_of_spinning(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env, hourly_runs=3)
    result = start_goal(db_session, GOAL)
    auto = run_autonomous(
        db_session, project_id=result.project.id, max_cycles=300, runner="mock"
    )
    assert auto.stopped_reason == "budget_wait"
    assert auto.started_total == 3
    assert auto.cycles < 10, "must stop promptly, not spin to max_cycles"
    assert auto.budget_wait is not None
    assert auto.budget_wait.reason == "hourly_max_runs"
    mins = auto.budget_wait.wait_minutes()
    assert mins is not None and 55 <= mins <= 60

    guide = human_next_actions(
        db_session,
        project_id=result.project.id,
        stopped_reason=auto.stopped_reason,
        runner="mock",
    )
    assert "予算待ち" in guide
    assert "直近1時間の実行回数上限" in guide
    assert "分後" in guide
    assert "ai-company scheduler continue --runner mock" in guide
    assert "config/budget.yaml" in guide
    assert "人の操作は不要です。機械側が続ける作業です。" not in guide

    # `scheduler continue` inside the same window stops at once, too
    again = continue_until_gate(
        db_session, project_id=result.project.id, max_cycles=300, runner="mock"
    )
    assert again.stopped_reason == "budget_wait"
    assert again.started_total == 0
    assert again.cycles == 1


def test_continue_resumes_after_window_moves_on(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, hourly_runs=3)
    result = start_goal(db_session, GOAL)
    auto = run_autonomous(
        db_session, project_id=result.project.id, max_cycles=300, runner="mock"
    )
    assert auto.stopped_reason == "budget_wait"
    # an hour passes: every recorded run leaves the hourly window
    db_session.execute(
        update(BudgetEntry).values(created_at=_utcnow() - timedelta(hours=2))
    )
    db_session.flush()
    assert budget_window_wait(db_session) is None
    nxt = continue_until_gate(
        db_session, project_id=result.project.id, max_cycles=300, runner="mock"
    )
    assert nxt.started_total >= 1
    assert nxt.started_total <= 3  # and the cap applies again in the new window
    assert nxt.stopped_reason == "budget_wait"


def test_cli_start_prints_budget_wait(tmp_env: Path) -> None:
    from ai_company.cli import app

    _caps(tmp_env, hourly_runs=2)
    out = CliRunner().invoke(
        app, ["start", GOAL, "--autonomous", "--runner", "mock", "--quiet", "--max-cycles", "200"]
    )
    assert out.exit_code == 0, out.output
    assert "停止理由=予算待ち（時間枠の上限）" in out.output
    assert "scheduler continue" in out.output
