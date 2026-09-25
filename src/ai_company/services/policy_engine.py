"""Enforce role permissions from config/permissions.yaml at dispatch time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Task
from ai_company.services.audit import record_audit
from ai_company.services.validation import (
    is_judge_task,
    is_validation_task,
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    action: str
    role: str
    reason: str
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "role": self.role,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
        }


# Actions that imply writing to the workspace
_WRITE_ACTIONS = frozenset({"code.edit", "code.commit", "shell.safe", "shell.dangerous"})

# Task kinds that must not write
_READ_ONLY_KINDS = frozenset(
    {
        "validation",
        "judge",
        "replan",
        "still_needed",
        "project_health",
        "product_fit",
        "usability_audit",
        "customer_success",
        "it_ops_audit",
        "compliance_audit",
        "test_audit",
        "quality_audit",
        "sales_audit",
        "maintenance_audit",
    }
)


def _profiles(config: AppConfig) -> dict[str, Any]:
    raw = config.permissions or {}
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    return profiles if isinstance(profiles, dict) else {}


def permissions_for_role(config: AppConfig, role: str) -> set[str]:
    profiles = _profiles(config)
    profile = profiles.get(role)
    if profile is None:
        # Exact aliases only — undefined roles deny by default (P0-3)
        aliases = {
            "qa": "reviewer",
            "qa_worker": "reviewer",
            "backend_engineer": "coding_worker",
            "frontend_engineer": "coding_worker",
            "judge": "validation_reviewer",
        }
        if role.endswith("_reviewer"):
            profile = profiles.get("validation_reviewer") or profiles.get("reviewer")
        elif role in aliases:
            profile = profiles.get(aliases[role])
        else:
            return set()
    if not isinstance(profile, dict):
        return set()
    perms = set(profile.get("permissions") or [])
    if "*" in perms:
        return {"*"}
    return perms


def role_may(config: AppConfig, role: str, action: str) -> bool:
    perms = permissions_for_role(config, role)
    if "*" in perms:
        return True
    return action in perms


def infer_dispatch_action(task: Task) -> str:
    if is_validation_task(task) or is_judge_task(task):
        return "code.read"
    kind = str(task.kind)
    if kind in {
        "replan",
        "still_needed",
        "project_health",
        "product_fit",
        "usability_audit",
        "customer_success",
        "it_ops_audit",
        "compliance_audit",
        "test_audit",
        "quality_audit",
        "sales_audit",
        "maintenance_audit",
    } or kind.endswith("_audit"):
        return "code.read"
    return "code.edit"


def evaluate_dispatch(
    *,
    task: Task,
    role: str,
    config: AppConfig | None = None,
    action: str | None = None,
) -> PolicyDecision:
    cfg = config or load_config()
    act = action or infer_dispatch_action(task)
    perms = permissions_for_role(cfg, role)

    if not perms:
        return PolicyDecision(
            allowed=False,
            action=act,
            role=role,
            reason=f"unknown_or_empty_role_profile:{role}",
        )

    # Read-only task kinds cannot receive write permissions even if misconfigured
    if str(task.kind) in _READ_ONLY_KINDS or is_validation_task(task) or is_judge_task(task):
        if act in _WRITE_ACTIONS:
            return PolicyDecision(
                allowed=False,
                action=act,
                role=role,
                reason="read_only_task_kind_cannot_write",
            )
        if "code.edit" in perms and "code.read" not in perms and "*" not in perms:
            # Strip write: still allow if they have read or validation.report
            if not (
                "code.read" in perms
                or "validation.report" in perms
                or "validation.read" in perms
                or "test.execute" in perms
            ):
                return PolicyDecision(
                    allowed=False,
                    action=act,
                    role=role,
                    reason="reviewer_missing_read_permission",
                )
        return PolicyDecision(allowed=True, action=act, role=role, reason="read_only_ok")

    if act == "code.edit" and not role_may(cfg, role, "code.edit") and "*" not in perms:
        if role_may(cfg, role, "code.read"):
            return PolicyDecision(
                allowed=False,
                action=act,
                role=role,
                reason="role_lacks_code.edit",
            )
        return PolicyDecision(
            allowed=False,
            action=act,
            role=role,
            reason="role_lacks_code.edit",
        )

    if not role_may(cfg, role, act) and "*" not in perms:
        # Soft: allow code.read if they have any validation/read perm
        if act == "code.read" and (
            "validation.read" in perms
            or "validation.report" in perms
            or "project.report" in perms
        ):
            return PolicyDecision(allowed=True, action=act, role=role, reason="read_via_report")
        return PolicyDecision(
            allowed=False,
            action=act,
            role=role,
            reason=f"permission_denied:{act}",
            requires_approval=act in {"shell.dangerous", "deploy", "network.external"},
        )

    return PolicyDecision(allowed=True, action=act, role=role, reason="allowed")


def assert_dispatch_allowed(
    session: Session,
    *,
    task: Task,
    role: str,
    config: AppConfig | None = None,
) -> PolicyDecision:
    decision = evaluate_dispatch(task=task, role=role, config=config)
    record_audit(
        session,
        event_type="policy_decision",
        actor_type="system",
        actor_id="policy_engine",
        target_type="task",
        target_id=task.id,
        metadata=decision.to_dict(),
    )
    if not decision.allowed:
        raise PermissionError(
            f"policy denied role={role} action={decision.action}: {decision.reason}"
        )
    return decision


def role_allows_writes(config: AppConfig, role: str, task: Task) -> bool:
    if is_validation_task(task) or is_judge_task(task) or str(task.kind) in _READ_ONLY_KINDS:
        return False
    return role_may(config, role, "code.edit") or "*" in permissions_for_role(config, role)
