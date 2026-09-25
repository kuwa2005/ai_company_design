"""Build worker prompts from templates + task fields + validation context."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Execution, Task, ValidationPlan, ValidationResult
from ai_company.paths import repo_root


def prompts_dir() -> Path:
    override = os.environ.get("AI_COMPANY_PROMPTS_DIR")
    if override:
        return Path(override).resolve()
    return repo_root() / "prompts"


def load_role_prompt(role: str) -> str:
    mapping = {
        "coding_worker": "coding_worker.md",
        "backend_engineer": "coding_worker.md",
        "frontend_engineer": "coding_worker.md",
        "reviewer": "reviewer.md",
        "test_reviewer": "test_reviewer.md",
        "spec_reviewer": "spec_reviewer.md",
        "security_reviewer": "security_reviewer.md",
        "edge_case_reviewer": "edge_case_reviewer.md",
        "architecture_reviewer": "architecture_reviewer.md",
        "judge": "judge.md",
        "secretary": "secretary.md",
        "engineering_manager": "supervisor.md",
        "technical_director": "supervisor.md",
        "completeness_auditor": "completeness_auditor.md",
        "usability_auditor": "usability_auditor.md",
        "quality_auditor": "quality_auditor.md",
        "project_coordinator": "project_coordinator.md",
        "product_planner": "product_planner.md",
        "customer_success": "customer_success.md",
        "compliance_auditor": "compliance_auditor.md",
        "test_auditor": "test_auditor.md",
        "it_operations": "it_operations.md",
        "sales": "sales.md",
        "maintenance": "maintenance.md",
    }
    name = mapping.get(role, "coding_worker.md")
    path = prompts_dir() / name
    if not path.exists():
        return f"You are a short-lived {role} worker. Complete only the assigned task."
    return path.read_text(encoding="utf-8")


def _source_execution_summary(
    session: Session | None, source_execution_id: str | None
) -> str:
    if session is None or not source_execution_id:
        return "(no source execution)"
    execution = session.get(Execution, source_execution_id)
    if execution is None:
        return f"(missing execution {source_execution_id})"
    payload = execution.result_json or {}
    changed = payload.get("changed_files") or []
    tests = payload.get("tests") or {}
    return (
        f"execution_id={execution.id} success={execution.success} "
        f"changed_files={changed} tests={tests}"
    )


def _validation_results_block(session: Session | None, plan_id: str | None) -> str:
    if session is None or not plan_id:
        return "(no validation results yet)"
    rows = list(
        session.scalars(
            select(ValidationResult)
            .where(ValidationResult.plan_id == plan_id)
            .order_by(ValidationResult.created_at.asc())
        ).all()
    )
    if not rows:
        return "(no validation results yet)"
    lines: list[str] = []
    for row in rows:
        lines.append(
            json.dumps(
                {
                    "review_type": row.review_type,
                    "verdict": row.verdict,
                    "confidence": row.confidence,
                    "findings": row.findings,
                    "evidence": row.evidence,
                    "worker_id": row.worker_id,
                    "execution_id": row.execution_id,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def _parent_context(session: Session | None, task: Task) -> dict[str, Any]:
    if session is None or not task.parent_task_id:
        return {}
    parent = session.get(Task, task.parent_task_id)
    if parent is None:
        return {}
    return {
        "parent_id": parent.id,
        "parent_title": parent.title,
        "parent_objective": parent.objective or parent.title,
        "parent_acceptance": parent.acceptance_criteria or "(none)",
        "parent_risk": parent.risk_level,
        "source_execution_id": task.source_execution_id or parent.source_execution_id,
    }


def build_task_prompt(task: Task, *, session: Session | None = None) -> str:
    role_prompt = load_role_prompt(task.role_required)
    allowed = task.allowed_paths or []
    forbidden = task.forbidden_paths or []
    parent_ctx = _parent_context(session, task)
    source_id = parent_ctx.get("source_execution_id") or task.source_execution_id

    extra = ""
    if task.kind in {"validation", "judge"} or task.review_type:
        plan = None
        if session is not None and task.validation_plan_id:
            plan = session.get(ValidationPlan, task.validation_plan_id)
        required = (plan.required_review_types if plan else []) or []
        extra = f"""
---
Validation Context
Parent Task: {parent_ctx.get("parent_id", "-")}
Parent Title: {parent_ctx.get("parent_title", "-")}
Parent Objective: {parent_ctx.get("parent_objective", "-")}
Parent Acceptance Criteria: {parent_ctx.get("parent_acceptance", "-")}
Parent Risk: {parent_ctx.get("parent_risk", "-")}
Required Review Types: {required}
Source Execution: {_source_execution_summary(session, source_id)}
"""
        if task.kind == "judge" or task.role_required == "judge":
            extra += f"""
Existing Validation Results:
{_validation_results_block(session, task.validation_plan_id)}

Hard rules still apply in the orchestrator. Your JSON is advisory input.
"""
    elif task.kind == "replan":
        extra = f"""
---
Supervisor Context
Reported Task: {parent_ctx.get("parent_id", task.parent_task_id or "-")}
Reported Title: {parent_ctx.get("parent_title", "-")}
Reported Objective: {parent_ctx.get("parent_objective", "-")}
Reported Acceptance Criteria: {parent_ctx.get("parent_acceptance", "-")}
Failure Report: {task.objective}
"""

    return f"""{role_prompt}

---
Task ID: {task.id}
Kind: {task.kind}
Review Type: {task.review_type or "-"}
Title: {task.title}
Objective: {task.objective or task.title}
Role: {task.role_required}
Priority: {task.priority}
Risk: {task.risk_level}
Constraints: {task.constraints or "(none)"}
Acceptance Criteria: {task.acceptance_criteria or "(none)"}
Allowed Paths: {allowed or "(unspecified)"}
Forbidden Paths: {forbidden or "(unspecified)"}
{extra}
When finished, print a single JSON object (no markdown fences) appropriate for your role.
Coding: status/changed_files/tests/commit/issues.
Reviewer: verdict (PASS|FAIL|UNCERTAIN), summary, findings[], evidence[], confidence.
Judge: verdict (PASS|REJECT|HUMAN_ESCALATION), reason, blocking_findings[], repair_tasks[].
Supervisor: status, decision (RETRY|REASSIGN|ESCALATE), reason, instructions,
target_role, confidence.
"""
