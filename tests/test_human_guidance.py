"""Human next-action guidance when work is blocked."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ai_company.db.models import Approval, Task
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.secretary import human_next_actions
from ai_company.services.tasks import create_task


def test_human_next_actions_for_judge_reject(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="要件整理", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    db_session.add(
        Approval(
            id=new_id("APPR"),
            action="task.unblock_or_replan",
            requested_by="system",
            status=ApprovalStatus.PENDING,
            reason=(
                f"judge_reject:review_fail:test; task={task.id}; title={task.title}"
            ),
        )
    )
    db_session.flush()

    text = human_next_actions(
        db_session, project_id=task.project_id, stopped_reason="blocked_exception"
    )
    assert "いまやること" in text
    assert "テスト検証が FAIL" in text
    assert "approval list --status PENDING" in text
    assert "再度検討せよ" in text
    assert task.id in text


def test_human_next_actions_blocked_without_pending_is_not_urgent(
    db_session: Session, tmp_env
) -> None:
    task = create_task(db_session, title="データモデル設計", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    text = human_next_actions(db_session, project_id=task.project_id)
    assert "実装マネージャー" in text or "handoff-manager" in text
    assert "scheduler continue" in text
    assert f"handoff-manager {task.id}" in text


def test_human_next_actions_throttle_points_to_continue(
    db_session: Session, tmp_env
) -> None:
    task = create_task(db_session, title="続きの検証", priority=80)
    assert task.status == TaskStatus.READY
    text = human_next_actions(
        db_session,
        project_id=task.project_id,
        stopped_reason="scheduler_pause",
        runner="cursor",
        throttled=2,
        started=3,
    )
    assert "同時起動上限" in text
    assert "scheduler continue --runner cursor" in text
    assert "人の操作は不要" in text
    assert "run-once` を毎回" in text or "run-once を毎回" in text
