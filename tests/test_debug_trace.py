"""Debug / trace dump helpers."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ai_company.db.models import Approval
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.debug_trace import stuck_trace, task_trace
from ai_company.services.tasks import create_task


def test_task_trace_includes_runtime_and_status(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="データモデル設計", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    db_session.add(
        Approval(
            id=new_id("APPR"),
            action="task.unblock_or_replan",
            requested_by="system",
            status=ApprovalStatus.REJECTED,
            reason=f"judge_reject:security_fail; task={task.id}; title={task.title}",
        )
    )
    db_session.flush()

    text = task_trace(db_session, task.id)
    assert "デバッグ／トレース" in text
    assert task.id in text
    assert "BLOCKED" in text or "status=BLOCKED" in text
    assert "approvals:" in text


def test_stuck_trace_lists_blocked(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="停止中", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    text = stuck_trace(db_session, project_id=task.project_id)
    assert "デバッグ／スタック調査" in text
    assert "runtime db=" in text
    assert task.id in text
