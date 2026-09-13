"""Independent Validation Plan orchestration (No-look gate). Coding cannot self-PASS."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Task, ValidationPlan, ValidationResult
from ai_company.enums import (
    JudgeVerdict,
    ReviewVerdict,
    TaskKind,
    TaskStatus,
    ValidationPlanStatus,
)
from ai_company.ids import new_id
from ai_company.services.audit import record_audit
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.tasks import create_task, refresh_dependents

REVIEW_TYPE_TO_ROLE = {
    "test": "test_reviewer",
    "spec": "spec_reviewer",
    "security": "security_reviewer",
    "edge_case": "edge_case_reviewer",
    "architecture": "architecture_reviewer",
}

VALIDATION_ROLES = set(REVIEW_TYPE_TO_ROLE.values()) | {"reviewer", "qa", "qa_worker"}


def is_implementation_task(task: Task) -> bool:
    return task.kind in {TaskKind.IMPLEMENTATION, TaskKind.REPAIR, "implementation", "repair"}


def is_validation_task(task: Task) -> bool:
    return task.kind == TaskKind.VALIDATION or task.kind == "validation"


def is_judge_task(task: Task) -> bool:
    return task.kind == TaskKind.JUDGE or task.kind == "judge"


def required_review_types(risk_level: str, config: AppConfig) -> list[str]:
    profile = config.validation.risk_profiles.get(risk_level)
    if profile is None:
        profile = config.validation.risk_profiles.get("medium")
    if profile is None:
        return ["test"]
    types = list(profile.required)
    cap = config.validation.max_reviewers_per_task
    return types[:cap]


def start_validation_gate(
    session: Session,
    *,
    parent: Task,
    source_execution_id: str | None,
    config: AppConfig | None = None,
) -> ValidationPlan:
    """After coding success: REVIEW_REQUIRED + Validation Tasks + Judge Task (Queue only)."""
    cfg = config or load_config()
    if not cfg.validation.enabled:
        # Still forbid self-approve: require at least mock judge path via minimal plan
        pass
    if cfg.validation.coding_worker_can_self_approve:
        raise RuntimeError("coding_worker_can_self_approve must remain false")

    review_types = required_review_types(parent.risk_level or "medium", cfg)
    plan = ValidationPlan(
        id=new_id("VPLAN"),
        task_id=parent.id,
        risk_level=parent.risk_level or "medium",
        required_review_types=review_types,
        judge_required=cfg.judge.required,
        status=ValidationPlanStatus.WAITING_REVIEWS,
        source_execution_id=source_execution_id,
    )
    session.add(plan)
    session.flush()

    parent.status = TaskStatus.REVIEW_REQUIRED
    parent.validation_plan_id = plan.id
    parent.source_execution_id = source_execution_id
    session.flush()

    record_audit(
        session,
        event_type="validation_plan_created",
        actor_type="system",
        actor_id="validation",
        target_type="validation_plan",
        target_id=plan.id,
        metadata={
            "parent_task_id": parent.id,
            "required": review_types,
            "risk_level": plan.risk_level,
        },
    )

    validation_task_ids: list[str] = []
    for rtype in review_types:
        role = REVIEW_TYPE_TO_ROLE.get(rtype, "reviewer")
        from ai_company.services.staffing import ensure_staffing_request

        ensure_staffing_request(
            session,
            project_id=parent.project_id,
            role=role,
            requested_count=1,
            reason=f"on-demand {rtype} reviewer for {parent.id}",
            config=cfg,
        )
        vt = create_task(
            session,
            title=f"Validate[{rtype}]: {parent.title}",
            priority=min(100, parent.priority + 15),
            project_id=parent.project_id,
            objective=(
                f"Independent {rtype} review of task {parent.id}. "
                "Do not modify code. Emit verdict PASS|FAIL|UNCERTAIN with findings/evidence."
            ),
            role_required=role,
            estimated_cost=parent.estimated_cost * 0.3,
            parent_task_id=parent.id,
            kind=TaskKind.VALIDATION,
            risk_level=parent.risk_level,
            validation_plan_id=plan.id,
            review_type=rtype,
            source_execution_id=source_execution_id,
            config=cfg,
        )
        validation_task_ids.append(vt.id)

    if cfg.judge.required:
        from ai_company.services.staffing import ensure_staffing_request

        ensure_staffing_request(
            session,
            project_id=parent.project_id,
            role="judge",
            requested_count=1,
            reason=f"on-demand judge for {parent.id}",
            config=cfg,
        )
        create_task(
            session,
            title=f"Judge: {parent.title}",
            priority=min(100, parent.priority + 20),
            project_id=parent.project_id,
            objective=(
                f"Aggregate validation results for plan {plan.id} / task {parent.id}. "
                "Emit PASS|REJECT|HUMAN_ESCALATION. Do not modify code."
            ),
            role_required="judge",
            estimated_cost=parent.estimated_cost * 0.2,
            parent_task_id=parent.id,
            depends_on=validation_task_ids,
            kind=TaskKind.JUDGE,
            risk_level=parent.risk_level,
            validation_plan_id=plan.id,
            source_execution_id=source_execution_id,
            config=cfg,
        )

    record_audit(
        session,
        event_type="task_review_required",
        actor_type="system",
        actor_id="validation",
        target_type="task",
        target_id=parent.id,
        metadata={"plan_id": plan.id, "validation_tasks": validation_task_ids},
    )
    return plan


def record_validation_result(
    session: Session,
    *,
    validation_task: Task,
    worker_id: str,
    execution_id: str,
    verdict: str,
    findings: list[dict] | None = None,
    evidence: list[dict] | None = None,
    confidence: float | None = 0.9,
) -> ValidationResult:
    from pathlib import Path

    from ai_company.db.models import Artifact
    from ai_company.paths import executions_dir

    plan_id = validation_task.validation_plan_id
    if not plan_id:
        raise ValueError("validation task missing plan_id")
    result = ValidationResult(
        id=new_id("VRES"),
        plan_id=plan_id,
        task_id=validation_task.parent_task_id or validation_task.id,
        review_type=validation_task.review_type or "unknown",
        worker_id=worker_id,
        execution_id=execution_id,
        verdict=verdict,
        findings=findings or [],
        evidence=evidence or [],
        confidence=confidence,
    )
    session.add(result)
    session.flush()

    # Persist evidence artifact for No-look provenance (M4)
    out_dir = executions_dir() / (execution_id or result.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = out_dir / f"evidence_{result.review_type}.json"
    import json

    evidence_path.write_text(
        json.dumps(
            {
                "validation_result_id": result.id,
                "verdict": verdict,
                "findings": findings or [],
                "evidence": evidence or [],
                "worker_id": worker_id,
                "execution_id": execution_id,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    session.add(
        Artifact(
            id=new_id("ART"),
            project_id=validation_task.project_id,
            task_id=validation_task.id,
            kind="validation_evidence",
            path=str(evidence_path),
            metadata_json={"validation_result_id": result.id, "review_type": result.review_type},
        )
    )
    session.flush()

    record_audit(
        session,
        event_type="validation_result_recorded",
        actor_type="worker",
        actor_id=worker_id,
        target_type="validation_result",
        target_id=result.id,
        metadata={
            "verdict": verdict,
            "review_type": result.review_type,
            "plan_id": plan_id,
            "evidence_path": str(evidence_path),
        },
    )
    return result


def detect_reviewer_conflicts(results: list[ValidationResult]) -> list[dict]:
    """Surface PASS vs FAIL / contradictory high-severity findings across reviewers."""
    conflicts: list[dict] = []
    by_verdict: dict[str, list[str]] = {}
    for r in results:
        by_verdict.setdefault(str(r.verdict), []).append(r.review_type)
    if (
        ReviewVerdict.PASS in by_verdict
        and ReviewVerdict.FAIL in by_verdict
    ):
        conflicts.append(
            {
                "type": "verdict_disagreement",
                "pass": by_verdict.get(ReviewVerdict.PASS, []),
                "fail": by_verdict.get(ReviewVerdict.FAIL, []),
            }
        )
    if (
        ReviewVerdict.PASS in by_verdict
        and ReviewVerdict.UNCERTAIN in by_verdict
    ):
        conflicts.append(
            {
                "type": "uncertain_vs_pass",
                "pass": by_verdict.get(ReviewVerdict.PASS, []),
                "uncertain": by_verdict.get(ReviewVerdict.UNCERTAIN, []),
            }
        )
    return conflicts


def count_repair_cycles(session: Session, parent_task_id: str) -> int:
    return len(
        list(
            session.scalars(
                select(Task).where(
                    Task.parent_task_id == parent_task_id,
                    Task.kind.in_((TaskKind.REPAIR, "repair")),
                )
            ).all()
        )
    )


def apply_judge_hard_rules(
    session: Session,
    *,
    plan: ValidationPlan,
    config: AppConfig | None = None,
) -> tuple[str, list[str], list[dict]]:
    """Code-side hard rules. Returns (verdict, reasons, conflicts)."""
    cfg = config or load_config()
    reasons: list[str] = []
    results = list(
        session.scalars(
            select(ValidationResult).where(ValidationResult.plan_id == plan.id)
        ).all()
    )
    conflicts = detect_reviewer_conflicts(results)
    got = {r.review_type for r in results}
    required = set(plan.required_review_types or [])

    if cfg.judge.missing_required_review_blocks_pass and not required.issubset(got):
        missing = sorted(required - got)
        reasons.append(f"missing_required_reviews:{missing}")
        return JudgeVerdict.REJECT, reasons, conflicts

    for r in results:
        findings = r.findings or []
        if cfg.judge.critical_finding_blocks_pass:
            if any(
                isinstance(f, dict) and str(f.get("severity", "")).lower() == "critical"
                for f in findings
            ):
                reasons.append(f"critical_finding:{r.review_type}")
                return JudgeVerdict.REJECT, reasons, conflicts
        if cfg.judge.security_fail_blocks_pass:
            if r.review_type == "security" and r.verdict == ReviewVerdict.FAIL:
                reasons.append("security_fail")
                return JudgeVerdict.REJECT, reasons, conflicts
        if r.verdict == ReviewVerdict.FAIL:
            reasons.append(f"review_fail:{r.review_type}")
            return JudgeVerdict.REJECT, reasons, conflicts
        if r.verdict == ReviewVerdict.UNCERTAIN:
            reasons.append(f"review_uncertain:{r.review_type}")
            return JudgeVerdict.HUMAN_ESCALATION, reasons, conflicts

    if conflicts and any(c.get("type") == "uncertain_vs_pass" for c in conflicts):
        reasons.append("reviewer_disagreement")
        return JudgeVerdict.HUMAN_ESCALATION, reasons, conflicts

    if not reasons:
        reasons.append("all_required_reviews_pass")
    return JudgeVerdict.PASS, reasons, conflicts


def finalize_judge(
    session: Session,
    *,
    judge_task: Task,
    config: AppConfig | None = None,
) -> str:
    from ai_company.db.models import JudgeDecision

    cfg = config or load_config()
    plan = session.get(ValidationPlan, judge_task.validation_plan_id)
    if plan is None:
        raise ValueError("judge task missing validation plan")

    verdict, reasons, conflicts = apply_judge_hard_rules(
        session, plan=plan, config=cfg
    )

    # Infinite-loop guard: too many repair cycles → escalate
    parent_preview = session.get(Task, plan.task_id)
    if (
        verdict == JudgeVerdict.REJECT
        and parent_preview is not None
        and count_repair_cycles(session, parent_preview.id) >= cfg.repair_loop.max_cycles
    ):
        verdict = JudgeVerdict.HUMAN_ESCALATION
        reasons = reasons + [
            f"repair_loop_max_cycles:{cfg.repair_loop.max_cycles}"
        ]

    # Optional AI judge proposal from execution (advisory only)
    ai_proposal = None
    if judge_task.source_execution_id:
        from ai_company.db.models import Execution

        ex = session.get(Execution, judge_task.source_execution_id)
        if ex and isinstance(ex.result_json, dict):
            ai_proposal = ex.result_json.get("verdict")

    decision = JudgeDecision(
        id=new_id("JUDGE"),
        plan_id=plan.id,
        task_id=plan.task_id,
        verdict=verdict,
        reasons=reasons,
        conflicting_reviews=conflicts,
        repair_tasks=[],
    )
    session.add(decision)
    plan.status = ValidationPlanStatus.CLOSED
    judge_task.status = TaskStatus.DONE
    session.flush()

    record_audit(
        session,
        event_type="judge_decision",
        actor_type="system",
        actor_id="judge",
        target_type="judge_decision",
        target_id=decision.id,
        metadata={
            "verdict": verdict,
            "reasons": reasons,
            "plan_id": plan.id,
            "conflicts": conflicts,
            "ai_proposal": ai_proposal,
        },
    )

    parent = session.get(Task, plan.task_id)
    if parent is None:
        return verdict

    if verdict == JudgeVerdict.PASS:
        parent.status = TaskStatus.DONE
        session.flush()
        record_audit(
            session,
            event_type="task_completed",
            actor_type="system",
            actor_id="judge",
            target_type="task",
            target_id=parent.id,
            metadata={"via": "judge_pass", "decision_id": decision.id},
        )
        refresh_dependents(session, parent.id)
    elif verdict == JudgeVerdict.REJECT:
        parent.retry_count += 1
        if parent.retry_count > parent.max_retries:
            parent.status = TaskStatus.BLOCKED
            session.flush()
            escalate_blocked_task(
                session, task=parent, reason=f"judge_reject:{','.join(reasons)}"
            )
        else:
            repair = create_task(
                session,
                title=f"Repair: {parent.title}",
                priority=parent.priority + 5,
                project_id=parent.project_id,
                objective=f"Repair after judge REJECT: {reasons}",
                role_required="coding_worker",
                estimated_cost=parent.estimated_cost,
                parent_task_id=parent.id,
                kind=TaskKind.REPAIR,
                risk_level=parent.risk_level,
                config=cfg,
            )
            decision.repair_tasks = [repair.id]
            parent.status = TaskStatus.FAILED
            session.flush()
            record_audit(
                session,
                event_type="repair_task_created",
                actor_type="system",
                actor_id="judge",
                target_type="task",
                target_id=repair.id,
                metadata={"parent": parent.id, "reasons": reasons},
            )
    else:
        parent.status = TaskStatus.BLOCKED
        session.flush()
        escalate_blocked_task(
            session, task=parent, reason=f"judge_human_escalation:{','.join(reasons)}"
        )
    return verdict


def validation_status(session: Session, task_id: str) -> dict:
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    plan_id = task.validation_plan_id
    if not plan_id and task.parent_task_id:
        parent = session.get(Task, task.parent_task_id)
        plan_id = parent.validation_plan_id if parent else None
    plan = session.get(ValidationPlan, plan_id) if plan_id else None
    results = []
    if plan:
        results = list(
            session.scalars(
                select(ValidationResult).where(ValidationResult.plan_id == plan.id)
            ).all()
        )
    from ai_company.db.models import JudgeDecision

    decisions = []
    if plan:
        decisions = list(
            session.scalars(
                select(JudgeDecision).where(JudgeDecision.plan_id == plan.id)
            ).all()
        )
    return {
        "task_id": task.id,
        "task_status": task.status,
        "kind": task.kind,
        "plan": None
        if plan is None
        else {
            "id": plan.id,
            "status": plan.status,
            "risk_level": plan.risk_level,
            "required": plan.required_review_types,
        },
        "results": [
            {
                "review_type": r.review_type,
                "verdict": r.verdict,
                "findings": r.findings,
            }
            for r in results
        ],
        "judge": [
            {"id": d.id, "verdict": d.verdict, "reasons": d.reasons} for d in decisions
        ],
    }
