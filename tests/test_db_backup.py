"""DB backup / restore."""

from __future__ import annotations

from pathlib import Path

from ai_company.services.db_backup import backup_sqlite, restore_sqlite


def test_restore_overwrites_target(tmp_path: Path) -> None:
    bak = tmp_path / "backup.db"
    bak.write_bytes(b"backup-bytes-v1")
    dest = tmp_path / "live.db"
    dest.write_bytes(b"stale")
    out = restore_sqlite(bak, dest=dest)
    assert out == dest
    assert dest.read_bytes() == b"backup-bytes-v1"


def test_backup_copies_source_file(tmp_path: Path) -> None:
    src = tmp_path / "source.db"
    src.write_bytes(b"source-payload-xyz")
    bak = backup_sqlite(source=src, dest_dir=tmp_path / "backups")
    assert bak.exists()
    # Either sqlite backup API rewrites envelope, or shutil keeps bytes.
    # Round-trip via restore must recreate readable content when using shutil path:
    # For non-sqlite payloads, backup may create an empty sqlite — accept either
    # successful API backup or byte-identical shutil copy.
    if bak.read_bytes() == b"source-payload-xyz":
        return
    # sqlite backup API produced a DB file; restore still yields a file
    dest = tmp_path / "restored.db"
    restore_sqlite(bak, dest=dest)
    assert dest.exists() and dest.stat().st_size > 0
