"""Independent Validation Plan orchestration (No-look gate). Coding cannot self-PASS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    return task.kind in {
        TaskKind.IMPLEMENTATION,
        TaskKind.REPAIR,
        TaskKind.REPLAN,
        "implementation",
        "repair",
        "replan",
    }


def is_replan_task(task: Task) -> bool:
    return task.kind in {TaskKind.REPLAN, "replan"}


def is_validation_task(task: Task) -> bool:
    return task.kind == TaskKind.VALIDATION or task.kind == "validation"


def is_judge_task(task: Task) -> bool:
    return task.kind == TaskKind.JUDGE or task.kind == "judge"


def required_review_types(
    risk_level: str,
    config: AppConfig,
    *,
    phase_gate: str | None = None,
) -> list[str]:
    """
    Resolve review types from risk profile, optionally capped by phase gate (C-005).

    critical risk always uses the full risk profile (never thinned by MVP gate).
    """
    profile = config.validation.risk_profiles.get(risk_level)
    if profile is None:
        profile = config.validation.risk_profiles.get("medium")
    if profile is None:
        return ["test"]
    types = list(profile.required)
    cap = config.validation.max_reviewers_per_task

    phase_key = phase_gate or getattr(config.validation, "default_phase_gate", None)
    gates = getattr(config.validation, "phase_gates", None) or {}
    if phase_key and risk_level != "critical" and phase_key in gates:
        gate = gates[phase_key]
        if not getattr(gate, "use_risk_profile", False) and gate.required:
            gate_set = set(gate.required)
            # Cap richer profiles down to the phase set; do not expand thinner ones (e.g. low).
            if set(types) - gate_set:
                types = list(gate.required)
            else:
                types = [t for t in types if t in gate_set]
    return types[:cap]


def _phase_gate_for_task(session: Session, parent: Task, config: AppConfig) -> str | None:
    """Map project delivery phase to a validation phase_gates key."""
    if not parent.project_id:
        return getattr(config.validation, "default_phase_gate", "mvp")
    from ai_company.db.models import Project
    from ai_company.services.secretary import project_delivery_phase

    project = session.get(Project, parent.project_id)
    phase = project_delivery_phase(session, project)
    if phase in {"release_ready", "impl_done_closure_pending"}:
        return "release"
    if phase in {"implementing", "impl_queue_empty_closure_running"}:
        return "implementing"
    return getattr(config.validation, "default_phase_gate", "mvp")


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

    phase_key = _phase_gate_for_task(session, parent, cfg)
    review_types = required_review_types(
        parent.risk_level or "medium", cfg, phase_gate=phase_key
    )
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
    findings: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    confidence: float | None = 0.9,
) -> ValidationResult:

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


def detect_reviewer_conflicts(
    results: list[ValidationResult],
) -> list[dict[str, Any]]:
    """Surface PASS vs FAIL / contradictory high-severity findings across reviewers."""
    conflicts: list[dict[str, Any]] = []
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
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Code-side hard rules. Returns (verdict, reasons, conflicts)."""
    cfg = config or load_config()
    ship_first = str(cfg.judge.delivery_mode or "strict").lower() in {
        "ship_first",
        "prototype",
    }
    reasons: list[str] = []
    soft: list[str] = []
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
            if ship_first:
                soft.append(f"ship_first_ignored:review_fail:{r.review_type}")
                continue
            reasons.append(f"review_fail:{r.review_type}")
            return JudgeVerdict.REJECT, reasons, conflicts
        if r.verdict == ReviewVerdict.UNCERTAIN:
            if ship_first:
                soft.append(f"ship_first_ignored:review_uncertain:{r.review_type}")
                continue
            reasons.append(f"review_uncertain:{r.review_type}")
            return JudgeVerdict.HUMAN_ESCALATION, reasons, conflicts

    if conflicts and any(c.get("type") == "uncertain_vs_pass" for c in conflicts):
        if ship_first:
            soft.append("ship_first_ignored:reviewer_disagreement")
        else:
            reasons.append("reviewer_disagreement")
            return JudgeVerdict.HUMAN_ESCALATION, reasons, conflicts

    reasons.extend(soft)
    if soft:
        reasons.append("ship_first_delivery")
    elif not reasons:
        reasons.append("all_required_reviews_pass")
    return JudgeVerdict.PASS, reasons, conflicts


def enqueue_ship_first_residuals(
    session: Session,
    *,
    parent: Task,
    reasons: list[str],
    plan: ValidationPlan,
    config: AppConfig | None = None,
) -> list[Task]:
    """Turn ship_first soft-passes into follow-up implementation backlog (C-003)."""
    cfg = config or load_config()
    ignored = [r for r in reasons if r.startswith("ship_first_ignored:")]
    if not ignored:
        return []

    # Collect concrete findings from FAIL/UNCERTAIN reviews
    findings_lines: list[str] = []
    results = list(
        session.scalars(
            select(ValidationResult).where(ValidationResult.plan_id == plan.id)
        ).all()
    )
    for r in results:
        if r.verdict not in {ReviewVerdict.FAIL, ReviewVerdict.UNCERTAIN}:
            continue
        findings_lines.append(f"[{r.review_type}] verdict={r.verdict}")
        for f in r.findings or []:
            if isinstance(f, dict):
                findings_lines.append(
                    f"  - {f.get('severity', '?')}: {f.get('description', f)}"
                )
            else:
                findings_lines.append(f"  - {f}")

    body = "\n".join(findings_lines) or "\n".join(ignored)
    title = f"残件(ship_first): {parent.title}"[:200]
    # Avoid duplicate open residual for same parent
    existing = session.scalars(
        select(Task).where(
            Task.parent_task_id == parent.id,
            Task.title == title,
            Task.status.in_(
                (
                    TaskStatus.BACKLOG,
                    TaskStatus.READY,
                    TaskStatus.WAITING,
                    TaskStatus.RUNNING,
                )
            ),
        )
    ).first()
    if existing is not None:
        return [existing]

    residual = create_task(
        session,
        title=title,
        priority=max(parent.priority - 5, 10),
        project_id=parent.project_id,
        objective=(
            "ship_first で一時通過した検証指摘を解消する。\n"
            f"親タスク: {parent.id}\n"
            f"無視理由: {ignored}\n"
            f"指摘:\n{body}"
        ),
        role_required="coding_worker",
        estimated_cost=parent.estimated_cost,
        parent_task_id=parent.id,
        kind=TaskKind.IMPLEMENTATION,
        risk_level=parent.risk_level or "medium",
        acceptance_criteria="無視された検証 FAIL/UNCERTAIN が再検証で PASS になる",
        config=cfg,
    )
    record_audit(
        session,
        event_type="ship_first_residual_enqueued",
        actor_type="system",
        actor_id="judge",
        target_type="task",
        target_id=residual.id,
        metadata={
            "parent": parent.id,
            "ignored": ignored,
            "plan_id": plan.id,
        },
    )
    return [residual]


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

    # Repair-loop guard: too many coding repairs → nearest manager (not human yet)
    parent_preview = session.get(Task, plan.task_id)
    repair_exhausted = (
        verdict == JudgeVerdict.REJECT
        and parent_preview is not None
        and count_repair_cycles(session, parent_preview.id) >= cfg.repair_loop.max_cycles
    )
    if repair_exhausted:
        reasons = reasons + [f"repair_loop_max_cycles:{cfg.repair_loop.max_cycles}"]

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
        # Integrate worktree branch when coding ran in isolation (P0-4)
        try:
            from ai_company.db.models import Execution
            from ai_company.services.worktree import (
                WorktreeInfo,
                merge_worktree_branch,
            )

            src_exec = (
                session.get(Execution, plan.source_execution_id)
                if plan.source_execution_id
                else None
            )
            if src_exec and src_exec.worktree_path:
                safe_id = "".join(
                    c if c.isalnum() or c in "-_" else "_" for c in parent.id
                )[:48]
                info = WorktreeInfo(
                    path=Path(src_exec.worktree_path),
                    branch=f"ai-company/{safe_id}",
                    base_ref=src_exec.base_commit or "HEAD",
                    task_id=parent.id,
                )
                merged = merge_worktree_branch(info)
                record_audit(
                    session,
                    event_type="worktree_merge",
                    actor_type="system",
                    actor_id="judge",
                    target_type="task",
                    target_id=parent.id,
                    metadata={
                        "ok": merged,
                        "worktree": src_exec.worktree_path,
                        "base_commit": src_exec.base_commit,
                    },
                )
                if not merged:
                    # Do not silently ship conflicting changes
                    reasons = list(reasons) + ["worktree_merge_conflict"]
                    # Leave parent DONE but surface repair via residual
                    from ai_company.services.tasks import create_task as _ct

                    _ct(
                        session,
                        title=f"worktree競合解消: {parent.title}"[:200],
                        priority=max(parent.priority - 5, 10),
                        project_id=parent.project_id,
                        objective=(
                            "Judge PASS 後の worktree merge が衝突した。"
                            "手動/再実装で統合する。"
                        ),
                        role_required="coding_worker",
                        kind=TaskKind.IMPLEMENTATION,
                        risk_level=parent.risk_level or "medium",
                        config=cfg,
                    )
        except Exception:
            pass
        record_audit(
            session,
            event_type="task_completed",
            actor_type="system",
            actor_id="judge",
            target_type="task",
            target_id=parent.id,
            metadata={"via": "judge_pass", "decision_id": decision.id, "reasons": reasons},
        )
        # C-003: soft-passed / ignored findings become residual implementation work
        enqueue_ship_first_residuals(
            session,
            parent=parent,
            reasons=reasons,
            plan=plan,
            config=cfg,
        )
        refresh_dependents(session, parent.id)
    elif verdict == JudgeVerdict.REJECT:
        from ai_company.services.dismissal import (
            count_stubborn_rejects,
            fire_stubborn_supervisor,
            reasons_are_fireable,
        )

        reject_reason = f"judge_reject:{','.join(reasons)}"
        # Decision already flushed: stubborn_n includes this REJECT
        stubborn_n = count_stubborn_rejects(session, parent.id)
        threshold = int(
            getattr(cfg.repair_loop, "fire_after_non_security_rejects", 2) or 2
        )
        should_fire = (
            getattr(cfg.repair_loop, "auto_fire_stubborn_supervisor", False)
            and reasons_are_fireable(reasons)
            and (
                stubborn_n >= threshold
                or repair_exhausted
            )
        )
        if should_fire:
            fire_stubborn_supervisor(
                session,
                task=parent,
                reason=f"auto_fire:{reject_reason}",
                fired_role="test_reviewer",
                config=cfg,
            )
        elif repair_exhausted:
            # Coding repair budget used up → escalate to engineering_manager
            escalate_blocked_task(
                session,
                task=parent,
                reason=reject_reason,
                config=cfg,
            )
        else:
            parent.retry_count += 1
            if parent.retry_count > parent.max_retries:
                escalate_blocked_task(
                    session,
                    task=parent,
                    reason=reject_reason,
                    config=cfg,
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
        # HUMAN_ESCALATION (e.g. UNCERTAIN) → nearest manager first
        escalate_blocked_task(
            session,
            task=parent,
            reason=f"judge_human_escalation:{','.join(reasons)}",
            config=cfg,
        )
    return verdict


def validation_status(session: Session, task_id: str) -> dict[str, Any]:
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
    ignored: list[str] = []
    for d in decisions:
        for r in d.reasons or []:
            if str(r).startswith("ship_first_ignored:"):
                ignored.append(str(r))
    residuals: list[str] = []
    parent_id = plan.task_id if plan else task.id
    for t in session.scalars(
        select(Task).where(
            Task.parent_task_id == parent_id,
            Task.title.like("残件(ship_first):%"),
        )
    ).all():
        residuals.append(f"{t.id}:{t.status}")
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
        "ship_first_ignored": ignored,
        "ship_first_residuals": residuals,
    }
