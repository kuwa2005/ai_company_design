"""Lightweight schema version tracking (Alembic-ready stub)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ai_company.db.models import Base

SCHEMA_VERSION = 3


class SchemaVersion(Base):
    __tablename__ = "schema_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    application_version: Mapped[str] = mapped_column(String(64), default="0.1.0")
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None)
    )


def ensure_schema_version(session: Session) -> int:
    """Record current schema version if missing. create_all still creates tables."""
    current = session.scalar(
        select(SchemaVersion).order_by(SchemaVersion.version.desc()).limit(1)
    )
    if current is None:
        row = SchemaVersion(version=SCHEMA_VERSION, application_version="0.1.0")
        session.add(row)
        session.flush()
        return SCHEMA_VERSION
    return int(current.version)


def get_schema_version(session: Session) -> int | None:
    current = session.scalar(
        select(SchemaVersion).order_by(SchemaVersion.version.desc()).limit(1)
    )
    return int(current.version) if current else None
