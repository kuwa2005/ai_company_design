"""Interactive approval list / choice parsing."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ai_company.db.models import Approval
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.approval_flow import (
    ApprovalChoice,
    describe_approval_prompt,
    parse_approval_choice,
    resolve_approval_choice,
    should_run_interactive,
)
from ai_company.services.tasks import create_task


def test_parse_approval_choice_japanese_and_shortcuts() -> None:
    assert parse_approval_choice("はい") == ApprovalChoice.YES
    assert parse_approval_choice("いいえ") == ApprovalChoice.NO
    assert parse_approval_choice("再度検討せよ") == ApprovalChoice.RECONSIDER
    assert parse_approval_choice("1") == ApprovalChoice.YES
    assert parse_approval_choice("y") == ApprovalChoice.YES
    assert parse_approval_choice("3") == ApprovalChoice.RECONSIDER
    assert parse_approval_choice("maybe") is None


def test_should_run_interactive_defaults() -> None:
    assert should_run_interactive(status="PENDING", row_count=1, interactive=True) is True
    assert should_run_interactive(status="PENDING", row_count=1, interactive=False) is False
    assert should_run_interactive(status="APPROVED", row_count=1, interactive=None) is False
    assert should_run_interactive(status="PENDING", row_count=0, interactive=None) is False


def test_describe_approval_prompt_includes_question(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="データモデル設計", priority=80)
    approval = Approval(
        id=new_id("APPR"),
        action="task.unblock_or_replan",
        requested_by="system",
        status=ApprovalStatus.PENDING,
        reason=f"judge_reject:review_fail:test; task={task.id}; title={task.title}",
    )
    text = describe_approval_prompt(approval, task=task)
    assert "承認確認" in text
    assert task.id in text
    assert "1) はい" in text
    assert "2) いいえ" in text
    assert "3) 再度検討せよ" in text
    assert "自動修正を続けてよい" in text


def test_resolve_yes_unblocks_task(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="要件整理", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    approval = Approval(
        id=new_id("APPR"),
        action="task.unblock_or_replan",
        requested_by="system",
        status=ApprovalStatus.PENDING,
        reason=f"judge_reject:review_fail:test; task={task.id}; title={task.title}",
    )
    db_session.add(approval)
    db_session.flush()

    row = resolve_approval_choice(
        db_session, approval_id=approval.id, choice=ApprovalChoice.YES
    )
    db_session.refresh(task)
    assert row.status == ApprovalStatus.APPROVED
    assert task.status == TaskStatus.READY


def test_resolve_reconsider_rejects_with_note(db_session: Session, tmp_env) -> None:
    task = create_task(db_session, title="要件整理", priority=80)
    task.status = TaskStatus.BLOCKED
    db_session.flush()
    approval = Approval(
        id=new_id("APPR"),
        action="task.unblock_or_replan",
        requested_by="system",
        status=ApprovalStatus.PENDING,
        reason=f"judge_reject:review_fail:test; task={task.id}; title={task.title}",
    )
    db_session.add(approval)
    db_session.flush()

    row = resolve_approval_choice(
        db_session, approval_id=approval.id, choice=ApprovalChoice.RECONSIDER
    )
    db_session.refresh(task)
    assert row.status == ApprovalStatus.REJECTED
    assert "再度検討" in row.reason
    assert task.status == TaskStatus.BLOCKED
