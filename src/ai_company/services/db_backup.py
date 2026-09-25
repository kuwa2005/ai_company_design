"""SQLite backup / restore helpers (P0-9 / §11)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from ai_company.paths import data_dir, db_path


def backup_sqlite(
    *,
    dest_dir: Path | None = None,
    source: Path | None = None,
) -> Path:
    """Copy a SQLite DB to a timestamped backup file."""
    src = source or db_path()
    if not src.exists():
        raise FileNotFoundError(f"DB not found: {src}")
    out_dir = dest_dir or (data_dir() / "backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"ai_company-{stamp}.db"
    # Use sqlite backup API when possible for consistency under WAL
    try:
        import sqlite3

        src_conn = sqlite3.connect(str(src), timeout=30)
        try:
            try:
                src_conn.execute("PRAGMA wal_checkpoint(FULL)")
            except sqlite3.Error:
                pass
            dest_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dest_conn)
                dest_conn.commit()
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    except Exception:
        shutil.copy2(src, dest)
    return dest


def restore_sqlite(
    backup_file: Path,
    *,
    dest: Path | None = None,
) -> Path:
    """Restore DB from a backup file (overwrites target)."""
    if not backup_file.exists():
        raise FileNotFoundError(f"Backup not found: {backup_file}")
    target = dest or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_file, target)
    return target
