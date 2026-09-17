"""Cursor runner integration via mocked subprocess (no real Cursor required)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent, Execution, Task, Worker
from ai_company.enums import TaskStatus, WorkerStatus
from ai_company.services.cursor_runner import build_cursor_command
from ai_company.services.scheduler import run_once
from ai_company.services.tasks import create_task


def test_build_cursor_command_from_config(tmp_env: Path) -> None:
    from ai_company.config import load_config

    cfg = load_config()
    cmd = build_cursor_command("hello world", config=cfg.cursor_runner)
    assert cmd[0].endswith("agent") or cmd[0] == "agent" or "agent" in cmd[0]
    assert "-p" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert cmd[-1] == "hello world"
    # Never a single shell string
    assert all(isinstance(part, str) for part in cmd)


def test_cursor_command_uses_role_safe_noninteractive_modes(tmp_env: Path) -> None:
    from ai_company.config import load_config

    cfg = load_config().cursor_runner
    coding = build_cursor_command(
        "code", config=cfg, role="coding_worker", task_kind="implementation"
    )
    reviewer = build_cursor_command(
        "review", config=cfg, role="security_reviewer", task_kind="validation"
    )
    supervisor = build_cursor_command(
        "decide", config=cfg, role="engineering_manager", task_kind="replan"
    )
    assert "--auto-review" in coding
    assert "--force" not in coding
    assert reviewer[reviewer.index("--mode") + 1] == "ask"
    assert supervisor[supervisor.index("--mode") + 1] == "plan"
    assert coding[coding.index("--sandbox") + 1] == "enabled"


def test_cursor_runner_success_saves_execution(
    db_session: Session, tmp_env: Path
) -> None:
    from ai_company.config import clear_config_cache
    from tests.conftest import write_validation_override

    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    payload = {
        "status": "completed",
        "changed_files": ["src/x.py"],
        "tests": {"passed": 3, "failed": 0},
        "commit": "abc",
        "issues": [],
    }
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = json.dumps(payload)
    completed.stderr = ""

    task = create_task(db_session, title="cursor-sample", priority=60)
    with patch("ai_company.services.cursor_runner.subprocess.run", return_value=completed) as run:
        result = run_once(db_session, runner="cursor")
        assert result.started >= 1
        run.assert_called()
        kwargs = run.call_args.kwargs
        assert kwargs.get("shell") is False
        assert isinstance(run.call_args.args[0], list)

    db_session.refresh(task)
    assert task.status == TaskStatus.DONE

    workers = list(db_session.scalars(select(Worker)).all())
    assert len(workers) >= 1
    assert all(w.status == WorkerStatus.TERMINATED for w in workers)

    executions = list(db_session.scalars(select(Execution)).all())
    assert len(executions) >= 1
    assert any(e.success is True for e in executions)
    assert any(e.result_json and e.result_json.get("status") == "completed" for e in executions)

    events = {e.event_type for e in db_session.scalars(select(AuditEvent)).all()}
    assert "cursor_runner_finished" in events
    assert "worker_hired" in events
    assert "task_completed" in events


def test_cursor_runner_broken_json_not_success(
    db_session: Session, tmp_env: Path
) -> None:
    from ai_company.config import clear_config_cache
    from tests.conftest import write_validation_override

    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = "I finished but forgot JSON"
    completed.stderr = ""

    task = create_task(db_session, title="bad-json", priority=50, max_retries=0)
    with patch("ai_company.services.cursor_runner.subprocess.run", return_value=completed):
        run_once(db_session, runner="cursor")

    db_session.refresh(task)
    # max_retries=0 => escalate to nearest manager (not human yet)
    assert task.status == TaskStatus.FAILED
    replan = db_session.scalar(
        select(Task).where(Task.parent_task_id == task.id, Task.kind == "replan")
    )
    assert replan is not None
    executions = list(db_session.scalars(select(Execution)).all())
    assert executions[0].success is False


def test_cursor_runner_timeout_terminates(
    db_session: Session, tmp_env: Path
) -> None:
    from ai_company.config import clear_config_cache
    from tests.conftest import write_validation_override

    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    task = create_task(db_session, title="slow", priority=50, max_retries=2)

    def _raise(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["agent"], timeout=1, output=b"", stderr=b"")

    with patch("ai_company.services.cursor_runner.subprocess.run", side_effect=_raise):
        run_once(db_session, runner="cursor", timeout_seconds=1)

    db_session.refresh(task)
    assert task.status == TaskStatus.READY  # requeued for retry
    workers = list(db_session.scalars(select(Worker)).all())
    assert workers[0].status == WorkerStatus.TIMED_OUT
    executions = list(db_session.scalars(select(Execution)).all())
    assert executions[0].success is False
    assert executions[0].result_json.get("timed_out") is True


def test_cursor_reviewer_saves_validation_result(
    db_session: Session, tmp_env: Path
) -> None:
    """M5: Cursor Review Task -> PASS/FAIL + evidence in ValidationResult."""
    from ai_company.config import clear_config_cache
    from ai_company.db.models import Artifact, ValidationResult
    from ai_company.enums import TaskStatus
    from tests.conftest import write_settings_override

    write_settings_override(
        tmp_env,
        scheduler={"max_validation_workers": 8, "max_concurrent_workers": 12},
    )
    clear_config_cache()

    coding_payload = {
        "status": "completed",
        "changed_files": ["src/app.py"],
        "tests": {"passed": 1, "failed": 0},
        "commit": None,
        "issues": [],
    }
    review_payload = {
        "verdict": "PASS",
        "summary": "tests look independent",
        "findings": [],
        "evidence": ["pytest -q"],
        "confidence": 0.93,
    }
    judge_payload = {
        "verdict": "PASS",
        "reason": "all required reviews passed",
        "blocking_findings": [],
        "repair_tasks": [],
    }

    responses = [
        MagicMock(returncode=0, stdout=json.dumps(coding_payload), stderr=""),
        MagicMock(returncode=0, stdout=json.dumps(review_payload), stderr=""),
        MagicMock(returncode=0, stdout=json.dumps(judge_payload), stderr=""),
    ]

    task = create_task(
        db_session, title="cursor-nolook", priority=70, risk_level="low"
    )
    with patch(
        "ai_company.services.cursor_runner.subprocess.run", side_effect=responses
    ) as run:
        # coding -> validation -> judge (low risk = test only)
        for _ in range(3):
            run_once(db_session, runner="cursor", max_to_start=4)
            db_session.refresh(task)
            if task.status == TaskStatus.DONE:
                break
        assert run.call_count >= 3

    db_session.refresh(task)
    assert task.status == TaskStatus.DONE
    results = list(db_session.scalars(select(ValidationResult)).all())
    assert results
    assert results[0].verdict == "PASS"
    assert results[0].evidence
    arts = list(db_session.scalars(select(Artifact)).all())
    assert arts
    executions = list(db_session.scalars(select(Execution)).all())
    kinds = {e.result_json.get("parse_kind") for e in executions if e.result_json}
    assert "reviewer" in kinds
    assert "judge" in kinds
