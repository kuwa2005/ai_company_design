"""Optional project.manifest.yaml — system-owned test/lint commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ai_company.paths import project_root


class ProjectManifest(BaseModel):
    name: str = ""
    test_command: list[str] = Field(default_factory=list)
    lint_command: list[str] = Field(default_factory=list)
    typecheck_command: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=lambda: [".env", "secrets/", "*.pem"])
    demo_command: list[str] = Field(default_factory=list)


def manifest_path(workspace: Path | None = None) -> Path:
    root = workspace or project_root()
    for name in ("project.manifest.yaml", "ai-company.manifest.yaml", ".ai-company-manifest.yaml"):
        p = root / name
        if p.is_file():
            return p
    return root / "project.manifest.yaml"


def load_manifest(workspace: Path | None = None) -> ProjectManifest:
    path = manifest_path(workspace)
    if not path.is_file():
        return ProjectManifest()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ProjectManifest()
    return ProjectManifest.model_validate(data)


def run_manifest_command(cmd: list[str], *, cwd: Path) -> dict[str, Any]:
    """Execute a manifest command; never uses shell=True."""
    import subprocess

    if not cmd:
        return {"skipped": True, "ok": True}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            shell=False,
        )
        return {
            "skipped": False,
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "command": cmd,
        }
    except Exception as exc:
        return {"skipped": False, "ok": False, "error": str(exc), "command": cmd}
