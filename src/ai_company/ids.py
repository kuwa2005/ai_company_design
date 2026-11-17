"""ID generation helpers."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def new_id(prefix: str) -> str:
    """``<PREFIX>-<UTC yyyymmddHHMMSS>-<random hex>``.

    The random part is 64 bits. It used to be 24 bits, and a mock autonomous
    run issues ~200 ids of one prefix within a single second, which made
    birthday collisions (``UNIQUE constraint failed: audit_events.id``) a
    ~0.2-0.4% per-run flake.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(8).upper()
    return f"{prefix}-{stamp}-{suffix}"
