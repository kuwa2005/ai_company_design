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
    assert "止まっている理由" in text
    assert "テスト検証が FAIL" in text
    assert "approval resolve" in text
    assert "--approve" in text
    assert task.id in text
