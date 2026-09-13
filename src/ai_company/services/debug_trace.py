"""Development-time debug / trace dumps for stuck investigations."""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Approval, AuditEvent, JudgeDecision, Task, ValidationPlan, ValidationResult
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.paths import audit_log_path, data_dir, db_path, project_root
from ai_company.services.secretary import _explain_block_reason, _task_id_from_reason


def debug_enabled(*, flag: bool | None = None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get("AI_COMPANY_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def runtime_paths() -> dict[str, str]:
    return {
        "cwd": str(project_root()),
        "data_dir": str(data_dir()),
        "db_path": str(db_path()),
        "audit_log": str(audit_log_path()),
        "AI_COMPANY_DATA_DIR": os.environ.get("AI_COMPANY_DATA_DIR", ""),
        "AI_COMPANY_DB_PATH": os.environ.get("AI_COMPANY_DB_PATH", ""),
        "AI_COMPANY_PROJECT_DIR": os.environ.get("AI_COMPANY_PROJECT_DIR", ""),
    }


def _compact(obj: Any, *, limit: int = 400) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def task_trace(session: Session, task_id: str, *, audit_limit: int = 12) -> str:
    task = session.get(Task, task_id)
    lines = ["【デバッグ／トレース】", f"task_id={task_id}"]
    if task is None:
        lines.append("ERROR: Task not found in current DB")
        lines.append(f"runtime={_compact(runtime_paths())}")
        return "\n".join(lines)

    lines.extend(
        [
            f"title={task.title}",
            f"status={task.status} kind={task.kind} role={task.role_required}",
            f"priority={task.priority} retries={task.retry_count}/{task.max_retries}",
            f"project_id={task.project_id} parent={task.parent_task_id}",
            f"validation_plan_id={task.validation_plan_id}",
            f"source_execution_id={getattr(task, 'source_execution_id', None)}",
        ]
    )

    plan_id = task.validation_plan_id
    if not plan_id and task.parent_task_id:
        parent = session.get(Task, task.parent_task_id)
        plan_id = parent.validation_plan_id if parent else None
    if plan_id:
        plan = session.get(ValidationPlan, plan_id)
        if plan:
            lines.append(
                f"plan id={plan.id} status={plan.status} risk={plan.risk_level} "
                f"required={plan.required_review_types}"
            )
            results = list(
                session.scalars(
                    select(ValidationResult).where(ValidationResult.plan_id == plan.id)
                ).all()
            )
            for r in results:
                lines.append(
                    f"  result {r.review_type}={r.verdict} findings={_compact(r.findings, limit=180)}"
                )
            decisions = list(
                session.scalars(
                    select(JudgeDecision)
                    .where(JudgeDecision.plan_id == plan.id)
                    .order_by(JudgeDecision.created_at.desc())
                ).all()
            )
            for d in decisions[:5]:
                lines.append(
                    f"  judge {d.id} verdict={d.verdict} reasons={_compact(d.reasons, limit=180)}"
                )

    approvals = list(
        session.scalars(
            select(Approval)
            .where(Approval.reason.contains(task_id))
            .order_by(Approval.created_at.desc())
            .limit(8)
        ).all()
    )
    if approvals:
        lines.append("approvals:")
        for a in approvals:
            lines.append(
                f"  {a.id} status={a.status} action={a.action} "
                f"explain={_explain_block_reason(a.reason)[:120]}"
            )
    else:
        lines.append("approvals: （このタスク ID を含む承認なし）")

    audits = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.target_id == task_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(audit_limit)
        ).all()
    )
    # Also pull audits that mention this task in metadata text (best-effort).
    if len(audits) < audit_limit:
        extra = list(
            session.scalars(
                select(AuditEvent)
                .order_by(AuditEvent.created_at.desc())
                .limit(80)
            ).all()
        )
        seen = {e.id for e in audits}
        for e in extra:
            if e.id in seen:
                continue
            blob = json.dumps(e.metadata_json or {}, ensure_ascii=False)
            if task_id in blob or task_id == e.target_id:
                audits.append(e)
                seen.add(e.id)
            if len(audits) >= audit_limit:
                break
        audits.sort(key=lambda e: e.created_at or "", reverse=True)
        audits = audits[:audit_limit]

    if audits:
        lines.append("recent audit:")
        for e in audits:
            lines.append(
                f"  {e.created_at} {e.event_type} actor={e.actor_type}:{e.actor_id} "
                f"meta={_compact(e.metadata_json or {}, limit=160)}"
            )
    return "\n".join(lines)


def stuck_trace(session: Session, *, project_id: str | None = None) -> str:
    """Dump investigation info for BLOCKED / PENDING / empty-approval cases."""
    lines = ["【デバッグ／スタック調査】"]
    paths = runtime_paths()
    lines.append(f"runtime cwd={paths['cwd']}")
    lines.append(f"runtime data_dir={paths['data_dir']}")
    lines.append(f"runtime db={paths['db_path']}")
    if paths["AI_COMPANY_DATA_DIR"]:
        lines.append(
            f"注意: AI_COMPANY_DATA_DIR={paths['AI_COMPANY_DATA_DIR']} "
            "が有効なので、cwd/.ai_company とは別 DB の可能性があります"
        )

    stmt = select(Task).where(Task.status == TaskStatus.BLOCKED)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    blocked = list(session.scalars(stmt.limit(10)).all())

    pending = list(
        session.scalars(
            select(Approval)
            .where(Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.created_at.desc())
            .limit(10)
        ).all()
    )

    lines.append(f"BLOCKED={len(blocked)} PENDING_APPROVAL={len(pending)}")
    for a in pending:
        tid = _task_id_from_reason(a.reason)
        lines.append(f"- pending {a.id} task={tid} :: {_explain_block_reason(a.reason)[:140]}")

    for t in blocked:
        lines.append("")
        lines.append(task_trace(session, t.id, audit_limit=8))

    if not blocked and not pending:
        lines.append("（停止タスクも未承認もなし）")
    return "\n".join(lines)
