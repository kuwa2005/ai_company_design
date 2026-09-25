"""Cursor CLI Runner — config-driven subprocess adapter (no shell=True)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, CursorRunnerConfig, load_config
from ai_company.db.models import Task
from ai_company.paths import project_root
from ai_company.services.prompt_builder import build_task_prompt


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
    role: str | None = None,
    task_kind: str | None = None,
    workspace: str | Path | None = None,
) -> list[str]:
    cfg = config or load_config().cursor_runner
    executable = cfg.executable
    resolved = shutil.which(executable) or executable
    cmd: list[str] = [resolved]
    if cfg.print_mode_flag:
        cmd.append(cfg.print_mode_flag)
    output_format_flag = getattr(cfg, "output_format_flag", "--output-format")
    if cfg.output_format:
        cmd.extend([output_format_flag, cfg.output_format])
    if task_kind == "replan":
        cmd.extend(["--mode", cfg.supervisor_mode])
    elif task_kind in {"validation", "judge"}:
        cmd.extend(["--mode", cfg.read_only_mode])
    elif cfg.auto_review:
        cmd.append("--auto-review")
    if cfg.sandbox:
        cmd.extend(["--sandbox", cfg.sandbox])
    if getattr(cfg, "trust_workspace", True):
        cmd.append("--trust")
    model = cfg.model_by_role.get(role or "")
    if model:
        cmd.extend(["--model", model])
    ws = workspace or getattr(cfg, "workspace", None) or str(project_root())
    if ws:
        cmd.extend(["--workspace", str(ws)])
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
    on_heartbeat: Callable[[int], None] | None = None,
) -> RunnerCapture:
    """
    Invoke Cursor agent CLI via Popen (parallel-capable). Never uses shell=True.
    Writes full stdout/stderr to paths even on timeout.
    """
    cfg = config or load_config()
    prompt = build_task_prompt(task, session=session)
    cmd = build_cursor_command(
        prompt,
        config=cfg.cursor_runner,
        role=task.role_required,
        task_kind=str(task.kind),
        workspace=cwd,
    )
    timeout = timeout_seconds
    if timeout is None:
        timeout = float(cfg.cursor_runner.timeout_minutes) * 60.0

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            cwd=str(cwd) if cwd else None,
            env=run_env,
            stdin=subprocess.DEVNULL,
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

    pid = proc.pid
    if on_heartbeat is not None:
        try:
            on_heartbeat(pid)
        except Exception:
            pass

    heartbeat_every = max(5.0, float(getattr(cfg.settings.worker, "heartbeat_seconds", 30) or 30))
    deadline = time.monotonic() + float(timeout)
    next_beat = time.monotonic() + heartbeat_every
    timed_out = False

    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            break
        if on_heartbeat is not None and now >= next_beat:
            try:
                on_heartbeat(pid)
            except Exception:
                pass
            next_beat = now + heartbeat_every
        time.sleep(0.2)

    stdout, stderr = proc.communicate() if proc.stdout else ("", "")
    stdout = stdout or ""
    stderr = stderr or ""
    if timed_out:
        stderr = (stderr + "\n[ai-company] hard timeout\n").lstrip("\n")
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return RunnerCapture(
        exit_code=None if timed_out else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        command=cmd,
        pid=pid,
    )
