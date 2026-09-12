"""Audit event recording (DB + JSONL)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent
from ai_company.ids import new_id
from ai_company.paths import audit_log_path, data_dir


def record_audit(
    session: Session,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any] | None = None,
    jsonl_path: Path | None = None,
) -> AuditEvent:
    event = AuditEvent(
        id=new_id("AUD"),
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        metadata_json=metadata or {},
    )
    session.add(event)
    session.flush()

    path = jsonl_path or audit_log_path()
    data_dir().mkdir(parents=True, exist_ok=True)
    line = {
        "id": event.id,
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "target_type": target_type,
        "target_id": target_id,
        "metadata": metadata or {},
        "created_at": datetime.now(UTC).isoformat(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")

    return event


def list_audit_events(session: Session, *, limit: int = 50) -> list[AuditEvent]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
        ).all()
    )
