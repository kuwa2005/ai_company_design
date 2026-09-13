"""Dynamic staffing: approve / fulfill role requests. Never spawns processes."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import StaffingRequest, Worker
from ai_company.enums import StaffingStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit

ACTIVE_WORKER = {
    WorkerStatus.HIRED,
    WorkerStatus.STARTING,
    WorkerStatus.ACTIVE,
    WorkerStatus.REPORTING,
}


def approve_staffing(
    session: Session,
    *,
    request_id: str,
    approved_count: int | None = None,
) -> StaffingRequest:
    req = session.get(StaffingRequest, request_id)
    if req is None:
        raise ValueError(f"Staffing request not found: {request_id}")
    if req.status not in {StaffingStatus.REQUESTED, StaffingStatus.APPROVED}:
        raise ValueError(f"Cannot approve staffing in status {req.status}")
    count = approved_count if approved_count is not None else req.requested_count
    req.approved_count = max(0, count)
    req.status = StaffingStatus.APPROVED
    session.flush()
    record_audit(
        session,
        event_type="staffing_approved",
        actor_type="human",
        actor_id="operator",
        target_type="staffing_request",
        target_id=req.id,
        metadata={"approved_count": req.approved_count, "role": req.role},
    )
    return req


def reject_staffing(session: Session, *, request_id: str, reason: str = "") -> StaffingRequest:
    req = session.get(StaffingRequest, request_id)
    if req is None:
        raise ValueError(f"Staffing request not found: {request_id}")
    req.status = StaffingStatus.REJECTED
    if reason:
        req.reason = f"{req.reason} | rejected: {reason}".strip(" |")
    session.flush()
    record_audit(
        session,
        event_type="staffing_rejected",
        actor_type="human",
        actor_id="operator",
        target_type="staffing_request",
        target_id=req.id,
        metadata={"reason": reason},
    )
    return req


def auto_approve_open_requests(
    session: Session,
    *,
    project_id: str | None = None,
    config: AppConfig | None = None,
) -> int:
    """MVP default: auto-approve REQUESTED staffing so Scheduler can hire on demand."""
    cfg = config or load_config()
    if not cfg.settings.staffing.auto_approve:
        return 0
    stmt = select(StaffingRequest).where(StaffingRequest.status == StaffingStatus.REQUESTED)
    if project_id:
        stmt = stmt.where(StaffingRequest.project_id == project_id)
    approved = 0
    for req in session.scalars(stmt).all():
        approve_staffing(session, request_id=req.id)
        approved += 1
    return approved


def count_active_workers_for_role(
    session: Session, *, role: str, project_id: str | None = None
) -> int:
    stmt = select(func.count()).select_from(Worker).where(
        Worker.role == role,
        Worker.status.in_(tuple(ACTIVE_WORKER)),
    )
    if project_id:
        stmt = stmt.where(Worker.project_id == project_id)
    return int(session.scalar(stmt) or 0)


def role_capacity_for(role: str, config: AppConfig | None = None) -> int | None:
    cfg = config or load_config()
    caps = cfg.settings.staffing.role_capacity or {}
    if role in caps:
        return int(caps[role])
    # Validation specialists share reviewer pool if not listed
    if role.endswith("_reviewer") or role in {"reviewer", "judge", "qa", "qa_worker"}:
        return int(caps.get("reviewer", caps.get("validation", 4)))
    return caps.get("coding_worker")


def within_role_capacity(
    session: Session,
    *,
    role: str,
    project_id: str | None = None,
    config: AppConfig | None = None,
) -> tuple[bool, str]:
    cap = role_capacity_for(role, config)
    if cap is None:
        return True, "no_role_cap"
    active = count_active_workers_for_role(session, role=role, project_id=project_id)
    if active >= cap:
        return False, f"role_capacity:{role}:{active}/{cap}"
    return True, f"role_ok:{role}:{active}/{cap}"


def note_staffing_hire(
    session: Session,
    *,
    project_id: str,
    role: str,
) -> None:
    """Mark APPROVED staffing fulfilled when enough hires observed for the role."""
    stmt = (
        select(StaffingRequest)
        .where(
            StaffingRequest.project_id == project_id,
            StaffingRequest.role == role,
            StaffingRequest.status == StaffingStatus.APPROVED,
        )
        .order_by(StaffingRequest.created_at.asc())
    )
    req = session.scalars(stmt).first()
    if req is None:
        return
    hired = int(
        session.scalar(
            select(func.count())
            .select_from(Worker)
            .where(Worker.project_id == project_id, Worker.role == role)
        )
        or 0
    )
    if hired >= max(1, req.approved_count or req.requested_count):
        req.status = StaffingStatus.FULFILLED
        session.flush()
        record_audit(
            session,
            event_type="staffing_fulfilled",
            actor_type="system",
            actor_id="staffing",
            target_type="staffing_request",
            target_id=req.id,
            metadata={"role": role, "hired": hired},
        )


def ensure_staffing_request(
    session: Session,
    *,
    project_id: str,
    role: str,
    requested_count: int = 1,
    reason: str,
    auto_approve: bool = True,
    config: AppConfig | None = None,
) -> StaffingRequest:
    """Create or reuse open staffing request for a role (on-demand specialist hiring)."""
    existing = session.scalar(
        select(StaffingRequest)
        .where(
            StaffingRequest.project_id == project_id,
            StaffingRequest.role == role,
            StaffingRequest.status.in_(
                (StaffingStatus.REQUESTED, StaffingStatus.APPROVED)
            ),
        )
        .order_by(StaffingRequest.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing

    req = StaffingRequest(
        id=new_id("STAFF"),
        project_id=project_id,
        role=role,
        requested_count=requested_count,
        approved_count=0,
        reason=reason,
        status=StaffingStatus.REQUESTED,
    )
    session.add(req)
    session.flush()
    record_audit(
        session,
        event_type="staffing_requested",
        actor_type="system",
        actor_id="staffing",
        target_type="staffing_request",
        target_id=req.id,
        metadata={"role": role, "requested_count": requested_count, "reason": reason},
    )
    if auto_approve and (config or load_config()).settings.staffing.auto_approve:
        approve_staffing(session, request_id=req.id)
    return req
