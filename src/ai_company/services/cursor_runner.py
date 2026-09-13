"""Cursor CLI Runner — config-driven subprocess adapter (no shell=True)."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_company.config import AppConfig, CursorRunnerConfig, load_config
from ai_company.db.models import Task
from ai_company.paths import project_root
from ai_company.services.prompt_builder import build_task_prompt
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RunnerCapture:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    command: list[str]
    pid: int | None = None


def build_cursor_command(
    prompt: str,
    *,
    config: CursorRunnerConfig | None = None,
) -> list[str]:
    cfg = config or load_config().cursor_runner
    executable = cfg.executable
    resolved = shutil.which(executable) or executable
    cmd: list[str] = [resolved]
    if cfg.print_mode_flag:
        cmd.append(cfg.print_mode_flag)
    # Prefer explicit flag from config if present on model
    output_format_flag = getattr(cfg, "output_format_flag", "--output-format")
    if cfg.output_format:
        cmd.extend([output_format_flag, cfg.output_format])
    workspace = getattr(cfg, "workspace", None) or str(project_root())
    if workspace:
        cmd.extend(["--workspace", str(workspace)])
    # Prompt last as positional arg
    cmd.append(prompt)
    return cmd


def run_cursor_cli(
    task: Task,
    *,
    stdout_path: Path,
    stderr_path: Path,
    config: AppConfig | None = None,
    timeout_seconds: float | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    session: Session | None = None,
) -> RunnerCapture:
    """
    Invoke Cursor agent CLI. Never uses shell=True.
    Writes full stdout/stderr to paths even on timeout.
    """
    cfg = config or load_config()
    prompt = build_task_prompt(task, session=session)
    cmd = build_cursor_command(prompt, config=cfg.cursor_runner)
    timeout = timeout_seconds
    if timeout is None:
        timeout = float(cfg.cursor_runner.timeout_minutes) * 60.0

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            cwd=str(cwd) if cwd else None,
            env=run_env,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        return RunnerCapture(
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            command=cmd,
            pid=None,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(
            (stderr + "\n[ai-company] hard timeout\n").lstrip("\n"),
            encoding="utf-8",
        )
        return RunnerCapture(
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            command=cmd,
            pid=None,
        )
    except FileNotFoundError as exc:
        msg = f"cursor executable not found: {cmd[0]} ({exc})"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(msg + "\n", encoding="utf-8")
        return RunnerCapture(
            exit_code=127,
            stdout="",
            stderr=msg,
            timed_out=False,
            command=cmd,
            pid=None,
        )
