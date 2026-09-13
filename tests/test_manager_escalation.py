"""Nearest-supervisor escalation: engineering_manager before human."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import Approval, Task
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.escalation import (
    escalate_blocked_task,
    recover_orphaned_blocked_to_manager,
)
from ai_company.services.scheduler import run_once
from ai_company.services.tasks import create_task
from tests.conftest import write_validation_override


def test_recover_orphaned_blocked_to_manager(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="データモデル", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    db_session.add(
        Approval(
            id=new_id("APPR"),
            action="task.unblock_or_replan",
            requested_by="system",
            status=ApprovalStatus.REJECTED,
            reason=(
                f"judge_reject:security_fail; task={task.id}; title={task.title}"
                " | resolution: 人間判断: 再度検討"
            ),
        )
    )
    db_session.flush()

    recovered = recover_orphaned_blocked_to_manager(db_session)
    assert task.id in recovered
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    replan = db_session.scalar(
        select(Task).where(Task.parent_task_id == task.id, Task.kind == "replan")
    )
    assert replan is not None
    assert replan.role_required == "engineering_manager"


def test_escalate_creates_manager_replan(db_session: Session, tmp_env: Path) -> None:
    task = create_task(db_session, title="schema", priority=70)
    result = escalate_blocked_task(
        db_session, task=task, reason="judge_reject:security_fail"
    )
    assert isinstance(result, Task)
    assert result.kind == "replan"
    assert result.role_required == "engineering_manager"
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert db_session.scalar(select(Approval)) is None


def test_manager_replan_completion_spawns_repair(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    parent = create_task(db_session, title="api", priority=70, max_retries=0)
    run_once(db_session, mock_succeed=False, max_to_start=1)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    replan = db_session.scalar(
        select(Task).where(Task.parent_task_id == parent.id, Task.kind == "replan")
    )
    assert replan is not None
    assert replan.status == TaskStatus.READY

    result = run_once(db_session, mock_succeed=True, max_to_start=1)
    assert result.started >= 1
    db_session.refresh(replan)
    assert replan.status == TaskStatus.DONE
    repair = db_session.scalar(
        select(Task).where(Task.parent_task_id == parent.id, Task.kind == "repair")
    )
    assert repair is not None
    assert repair.status == TaskStatus.READY
