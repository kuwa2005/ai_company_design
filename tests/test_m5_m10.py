"""M6–M10: Secretary validation proposals, staffing, repair guard, autonomous flow."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import Artifact, StaffingRequest, Task
from ai_company.enums import ProjectStatus, StaffingStatus, TaskStatus
from ai_company.services.autonomous import run_autonomous
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import (
    classify_risk,
    propose_validation_request,
    start_goal,
)
from ai_company.services.staffing import approve_staffing, ensure_staffing_request
from ai_company.services.tasks import create_task
from ai_company.services.validation import (
    count_repair_cycles,
    start_validation_gate,
)
from tests.conftest import write_settings_override, write_validation_override


def test_m6_risk_and_validation_proposal(db_session: Session, tmp_env: Path) -> None:
    from ai_company.db.models import AuditEvent

    assert classify_risk("本番決済のセキュリティ改修") == "critical"
    assert classify_risk("README typo fix") == "low"
    vr = propose_validation_request("high")
    assert "security" in vr.required_review_types
    assert vr.judge_required is True

    result = start_goal(db_session, "優秀なWeb勤怠を作って")
    assert result.validation_requests
    assert all(t.risk_level for t in result.tasks)
    assert any(t.title.startswith("PRD") for t in result.tasks)
    assert any("アーキテクチャ" in t.title for t in result.tasks)
    events = {e.event_type for e in db_session.scalars(select(AuditEvent)).all()}
    assert "validation_request_proposed" in events
    assert "ワーカーを直接起動しません" in result.summary
    assert "自己申告だけでは完了になりません" in result.summary


def test_m7_specialist_roles_from_risk(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        scheduler={"max_validation_workers": 8, "max_concurrent_workers": 12},
    )
    clear_config_cache()
    parent = create_task(
        db_session, title="critical-api", priority=80, risk_level="critical"
    )
    run_once(db_session, max_to_start=1)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.REVIEW_REQUIRED
    reviews = list(
        db_session.scalars(
            select(Task).where(Task.parent_task_id == parent.id, Task.kind == "validation")
        ).all()
    )
    roles = {t.role_required for t in reviews}
    assert "test_reviewer" in roles
    assert "security_reviewer" in roles
    assert "architecture_reviewer" in roles


def test_m8_repair_loop_max_cycles_escalates(
    db_session: Session, tmp_env: Path
) -> None:
    write_validation_override(
        tmp_env, repair_loop={"max_cycles": 1, "max_total_worker_runs": 12}
    )
    clear_config_cache()
    from ai_company.config import load_config
    from ai_company.db.models import JudgeDecision, ValidationPlan, ValidationResult
    from ai_company.services.validation import (
        apply_judge_hard_rules,
        finalize_judge,
        start_validation_gate,
    )

    assert load_config().repair_loop.max_cycles == 1

    parent = create_task(
        db_session, title="loop", priority=70, risk_level="low", max_retries=5
    )
    # Seed one prior repair so the next REJECT hits the loop guard
    create_task(
        db_session,
        title="Repair: prior",
        priority=70,
        project_id=parent.project_id,
        parent_task_id=parent.id,
        kind="repair",
    )
    assert count_repair_cycles(db_session, parent.id) == 1

    start_validation_gate(db_session, parent=parent, source_execution_id="EXEC-LOOP")
    db_session.refresh(parent)
    plan = db_session.get(ValidationPlan, parent.validation_plan_id)
    assert plan is not None
    db_session.add(
        ValidationResult(
            id="VRES-FAIL",
            plan_id=plan.id,
            task_id=parent.id,
            review_type="test",
            worker_id="W",
            execution_id="E",
            verdict="FAIL",
            findings=[{"severity": "high", "category": "test", "description": "x"}],
            evidence=[],
            confidence=0.9,
        )
    )
    db_session.flush()
    verdict, reasons, _ = apply_judge_hard_rules(db_session, plan=plan)
    assert verdict == "REJECT"

    judge = db_session.scalar(
        select(Task).where(Task.parent_task_id == parent.id, Task.kind == "judge")
    )
    assert judge is not None
    finalize_judge(db_session, judge_task=judge)
    db_session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    decisions = list(db_session.scalars(select(JudgeDecision)).all())
    assert decisions[-1].verdict == "HUMAN_ESCALATION"
    assert any("repair_loop_max_cycles" in str(r) for r in (decisions[-1].reasons or []))


def test_m9_staffing_approve_and_on_demand(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, "小さなCLIツール")
    req = result.staffing[0]
    assert req.status == StaffingStatus.REQUESTED
    approved = approve_staffing(db_session, request_id=req.id, approved_count=1)
    assert approved.status == StaffingStatus.APPROVED

    parent = create_task(db_session, title="impl", priority=50, risk_level="low")
    start_validation_gate(
        db_session, parent=parent, source_execution_id="EXEC-X"
    )
    staff = list(
        db_session.scalars(
            select(StaffingRequest).where(
                StaffingRequest.project_id == parent.project_id,
                StaffingRequest.role == "test_reviewer",
            )
        ).all()
    )
    assert staff
    assert staff[0].status in {StaffingStatus.REQUESTED, StaffingStatus.APPROVED}


def test_m9_role_capacity_throttles(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        staffing={
            "auto_approve": True,
            "role_capacity": {"coding_worker": 1},
        },
        scheduler={"max_concurrent_workers": 10, "max_validation_workers": 2},
    )
    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    create_task(db_session, title="a", priority=90)
    create_task(db_session, title="b", priority=80)
    result = run_once(db_session, max_to_start=5)
    assert result.started == 1
    assert result.throttled >= 1


def test_m10_autonomous_nolook_flow(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env,
        scheduler={"max_validation_workers": 8, "max_concurrent_workers": 12},
        staffing={"auto_approve": True, "role_capacity": {"coding_worker": 8, "reviewer": 8, "test_reviewer": 4, "judge": 4}},
    )
    clear_config_cache()
    # Small goal: 3 implementation tasks with deps — autonomous should complete
    result = start_goal(db_session, "小さなユーティリティを作って")
    auto = run_autonomous(
        db_session,
        project_id=result.project.id,
        max_cycles=60,
        runner="mock",
        mock_succeed=True,
    )
    assert auto.stopped_reason == "all_implementation_done"
    assert auto.release_candidate_id is not None
    db_session.refresh(result.project)
    assert result.project.status == ProjectStatus.DONE
    arts = list(
        db_session.scalars(
            select(Artifact).where(Artifact.kind == "release_candidate")
        ).all()
    )
    assert arts
    impl = [
        t
        for t in result.tasks
        if t.kind == "implementation"
    ]
    for t in impl:
        db_session.refresh(t)
        assert t.status == TaskStatus.DONE
