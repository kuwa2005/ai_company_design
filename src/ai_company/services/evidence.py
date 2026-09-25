"""Evidence collection: verify worker-reported changes against real git state."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EvidencePack:
    workspace: str
    baseline_ref: str | None = None
    changed_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    reported_files: list[str] = field(default_factory=list)
    matched_files: list[str] = field(default_factory=list)
    missing_reported: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)
    write_roles_violated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.write_roles_violated:
            return False
        # Reported files that don't appear in the real diff are a mismatch
        if self.reported_files and self.missing_reported:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "baseline_ref": self.baseline_ref,
            "changed_files": self.changed_files,
            "untracked_files": self.untracked_files,
            "reported_files": self.reported_files,
            "matched_files": self.matched_files,
            "missing_reported": self.missing_reported,
            "unexpected_files": self.unexpected_files,
            "write_roles_violated": self.write_roles_violated,
            "ok": self.ok,
            "notes": self.notes,
        }


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except Exception:
        return 1, ""


def snapshot_head(workspace: Path) -> str | None:
    code, out = _git(workspace, "rev-parse", "HEAD")
    return out if code == 0 and out else None


def list_changed_since(workspace: Path, baseline: str | None) -> tuple[list[str], list[str]]:
    """Return (tracked_changes, untracked) relative to baseline or working tree dirty set."""
    changed: list[str] = []
    if baseline:
        code, out = _git(workspace, "diff", "--name-only", baseline)
        if code == 0 and out:
            changed = [ln.strip() for ln in out.splitlines() if ln.strip()]
    else:
        code, out = _git(workspace, "diff", "--name-only", "HEAD")
        if code == 0 and out:
            changed = [ln.strip() for ln in out.splitlines() if ln.strip()]
        code2, out2 = _git(workspace, "diff", "--name-only", "--cached")
        if code2 == 0 and out2:
            for ln in out2.splitlines():
                p = ln.strip()
                if p and p not in changed:
                    changed.append(p)

    code_u, out_u = _git(workspace, "ls-files", "--others", "--exclude-standard")
    untracked = [ln.strip() for ln in out_u.splitlines() if ln.strip()] if code_u == 0 else []
    return changed, untracked


def normalize_path(path: str, workspace: Path) -> str:
    p = Path(path)
    try:
        if p.is_absolute():
            return str(p.resolve().relative_to(workspace.resolve())).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")
    return str(p).replace("\\", "/")


def path_allowed(
    path: str,
    *,
    allowed_paths: list[str] | None,
    forbidden_paths: list[str] | None,
) -> bool:
    """Return False if path is forbidden or outside allowed prefixes."""
    norm = path.replace("\\", "/")
    for f in forbidden_paths or []:
        f_n = str(f).replace("\\", "/")
        if norm == f_n or norm.startswith(f_n.rstrip("/") + "/"):
            return False
    if not allowed_paths:
        return True
    for a in allowed_paths:
        a_n = str(a).replace("\\", "/")
        if norm == a_n or norm.startswith(a_n.rstrip("/") + "/") or a_n == "*":
            return True
    return False


def collect_evidence(
    workspace: Path,
    *,
    baseline_ref: str | None,
    reported_files: list[str] | None = None,
    role: str | None = None,
    allow_writes: bool = True,
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
) -> EvidencePack:
    """Compare worker-reported files against actual git changes since baseline."""
    ws = workspace.resolve()
    reported = [normalize_path(f, ws) for f in (reported_files or []) if f]
    changed, untracked = list_changed_since(ws, baseline_ref)
    changed_n = [normalize_path(f, ws) for f in changed]
    untracked_n = [normalize_path(f, ws) for f in untracked]
    actual = set(changed_n) | set(untracked_n)

    pack = EvidencePack(
        workspace=str(ws),
        baseline_ref=baseline_ref,
        changed_files=changed_n,
        untracked_files=untracked_n,
        reported_files=reported,
    )

    if not allow_writes and actual:
        pack.write_roles_violated = True
        pack.notes.append(
            f"role={role or '?'} must not write; found changes: {sorted(actual)[:20]}"
        )

    for f in sorted(actual):
        if not path_allowed(f, allowed_paths=allowed_paths, forbidden_paths=forbidden_paths):
            pack.notes.append(f"path_policy_violation:{f}")
            pack.unexpected_files.append(f)
            pack.write_roles_violated = True

    if reported:
        for f in reported:
            if f in actual:
                pack.matched_files.append(f)
            else:
                pack.missing_reported.append(f)
        for f in sorted(actual):
            if f not in reported and f not in pack.unexpected_files:
                pack.unexpected_files.append(f)
    elif actual and allow_writes:
        pack.notes.append("worker reported no files but workspace has changes")
        for f in sorted(actual):
            if f not in pack.unexpected_files:
                pack.unexpected_files.append(f)

    return pack
