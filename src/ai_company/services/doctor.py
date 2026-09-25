"""Preflight diagnostics for local runtime (no side effects)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_company.config import load_config
from ai_company.paths import config_dir, data_dir, db_path, repo_root


@dataclass
class DoctorCheck:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == "FAIL" for c in self.checks)

    def add(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.checks.append(DoctorCheck(name=name, status=status, detail=detail, fix=fix))


def _run(cmd: list[str], *, timeout: int = 15) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _prompts_dir() -> Path:
    try:
        from ai_company.services.prompt_builder import prompts_dir

        return prompts_dir()
    except Exception:
        return repo_root() / "prompts"


def run_doctor(*, workspace: Path | None = None) -> DoctorReport:
    """Side-effect free diagnostics. Never mutates the workspace."""
    report = DoctorReport()
    ws = (workspace or Path.cwd()).resolve()

    report.add("python", "PASS", f"{sys.version.split()[0]} ({sys.executable})")

    try:
        import sqlite3

        report.add("sqlite", "PASS", f"sqlite3 module ok ({sqlite3.sqlite_version})")
    except Exception as exc:  # pragma: no cover
        report.add("sqlite", "FAIL", str(exc), "Install Python with sqlite3 support")

    code, out, err = _run(["git", "--version"])
    if code == 0:
        report.add("git", "PASS", out or "git available")
    else:
        report.add("git", "FAIL", err or "git missing", "Install git")

    exe = "agent"
    try:
        cfg = load_config()
        exe = cfg.cursor_runner.executable or "agent"
    except Exception:
        pass
    agent_path = shutil.which(exe)
    if not agent_path:
        report.add(
            "cursor_agent_cli",
            "FAIL",
            f"executable '{exe}' not on PATH",
            "Install Cursor Agent CLI and ensure it is on PATH",
        )
    else:
        code, out, err = _run([exe, "--version"])
        ver = out or err or "unknown"
        report.add("cursor_agent_cli", "PASS", f"{agent_path} ({ver})")
        code_h, help_out, help_err = _run([exe, "--help"])
        help_text = f"{help_out}\n{help_err}"
        needed = ["--print", "--output-format", "--sandbox", "--workspace", "--trust"]
        missing = [f for f in needed if f not in help_text]
        if code_h != 0:
            report.add(
                "cursor_cli_flags",
                "WARN",
                "could not read agent --help",
                "Upgrade Cursor Agent CLI",
            )
        elif missing:
            report.add(
                "cursor_cli_flags",
                "WARN",
                f"help text missing flags: {missing}",
                "Upgrade Cursor Agent CLI to a compatible version",
            )
        else:
            report.add("cursor_cli_flags", "PASS", "required flags mentioned in --help")

        code_a, out_a, err_a = _run([exe, "whoami"], timeout=20)
        auth_text = (out_a or err_a or "").lower()
        if code_a == 0 and auth_text and "not logged in" not in auth_text:
            report.add("cursor_auth", "PASS", "agent whoami succeeded (details redacted)")
        elif code_a == 0:
            report.add("cursor_auth", "WARN", "whoami unclear", "Run: agent login")
        else:
            report.add(
                "cursor_auth",
                "WARN",
                "not logged in or whoami unsupported",
                "Run: agent login",
            )

    if not ws.exists():
        report.add("workspace", "FAIL", f"missing: {ws}", "cd into a project directory")
    elif not ws.is_dir():
        report.add("workspace", "FAIL", f"not a directory: {ws}")
    else:
        writable = os.access(ws, os.W_OK)
        report.add(
            "workspace",
            "PASS" if writable else "FAIL",
            f"{ws} writable={writable}",
            "Fix directory permissions" if not writable else "",
        )
        if (ws / ".git").exists():
            code, out, _ = _run(["git", "-C", str(ws), "status", "--porcelain"])
            dirty = "dirty" if out else "clean"
            report.add("workspace_git", "PASS", f"git repo ({dirty})")
        else:
            report.add(
                "workspace_git",
                "WARN",
                "not a git repository",
                "git init recommended for worktree isolation",
            )

    try:
        cfg = load_config()
        report.add(
            "config",
            "PASS",
            f"loaded from {config_dir()} delivery_mode={cfg.judge.delivery_mode}",
        )
    except Exception as exc:
        report.add("config", "FAIL", str(exc), "Fix YAML under config/")

    pdir = _prompts_dir()
    if pdir.is_dir() and any(pdir.glob("*.md")):
        report.add("prompts", "PASS", str(pdir))
    else:
        report.add("prompts", "FAIL", f"missing prompts in {pdir}", "Reinstall package")

    # Data / DB
    ddir = data_dir()
    report.add("data_dir", "PASS", f"{ddir} (created on init)")
    db = db_path()
    if db.exists():
        report.add("database", "PASS", str(db))
        try:
            from ai_company.db import init_db, session_scope
            from ai_company.db.schema_version import get_schema_version

            init_db()
            schema_ver: int | None
            with session_scope() as db_sess:
                schema_ver = get_schema_version(db_sess)
            report.add(
                "schema_version",
                "PASS" if schema_ver is not None else "WARN",
                f"version={schema_ver}",
            )
        except Exception as exc:
            report.add("schema_version", "WARN", str(exc))
    else:
        report.add("database", "WARN", f"no DB yet at {db}", "ai-company init")

    try:
        usage = shutil.disk_usage(ws)
        free_gb = usage.free / (1024**3)
        if free_gb < 0.5:
            report.add("disk", "FAIL", f"free={free_gb:.2f}GiB", "Free disk space")
        elif free_gb < 2:
            report.add("disk", "WARN", f"free={free_gb:.2f}GiB")
        else:
            report.add("disk", "PASS", f"free={free_gb:.2f}GiB")
    except Exception as exc:
        report.add("disk", "WARN", str(exc))

    return report


def report_to_dict(report: DoctorReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "detail": c.detail,
                "fix": c.fix,
            }
            for c in report.checks
        ],
    }
