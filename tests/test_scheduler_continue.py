"""scheduler continue loops past THROTTLE without asking a human."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import Task
from ai_company.enums import TaskStatus
from ai_company.services.autonomous import continue_until_gate
from ai_company.services.tasks import create_task
from tests.conftest import write_validation_override


def test_continue_drains_ready_without_human(db_session: Session, tmp_env: Path) -> None:
    write_validation_override(
        tmp_env,
        validation={"enabled": False},
        closure_loops={"enabled": False},
    )
    clear_config_cache()
    for i in range(3):
        create_task(db_session, title=f"job-{i}", priority=80 - i)

    auto = continue_until_gate(
        db_session, max_cycles=10, runner="mock", mock_succeed=True
    )
    assert auto.started_total >= 3
    assert auto.stopped_reason in {"idle_incomplete", "all_implementation_done"}
    ready = list(
        db_session.scalars(select(Task).where(Task.status == TaskStatus.READY)).all()
    )
    assert ready == []
