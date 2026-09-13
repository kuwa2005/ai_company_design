"""Role catalog and responsibility reporting."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ai_company.enums import TaskStatus
from ai_company.roles import format_role_label, org_chart_text, supervisor_of
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.secretary import responsibility_digest
from ai_company.services.tasks import create_task


def test_org_chart_lists_manager_and_president() -> None:
    text = org_chart_text()
    assert "実装マネージャー" in text
    assert "社長（人間）" in text
    assert "engineering_manager" in text
    assert supervisor_of("coding_worker") == "engineering_manager"
    assert supervisor_of("engineering_manager") == "human_owner"


def test_responsibility_digest_shows_manager_route(
    db_session: Session, tmp_env
) -> None:
    task = create_task(db_session, title="データモデル", priority=80)
    escalate_blocked_task(db_session, task=task, reason="judge_reject:security_fail")
    text = responsibility_digest(db_session, project_id=task.project_id)
    assert "実装マネージャー" in text
    assert "再計画" in text or "対応予定" in text
    assert format_role_label("engineering_manager") in text or "engineering_manager" in text
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
