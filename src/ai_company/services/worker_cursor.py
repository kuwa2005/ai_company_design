"""Cursor Runner: run one task with the Cursor agent CLI and finalize the execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import TaskKind
from ai_company.ids import new_id
from ai_company.paths import data_dir, executions_dir, project_root
from ai_company.services.audit import record_audit
from ai_company.services.cursor_runner import run_cursor_cli
from ai_company.services.progress import emit as progress
from ai_company.services.result_parser import RoleParseKind, parse_for_kind
from ai_company.services.validation import (
    is_judge_task,
    is_validation_task,
)
from ai_company.services.worker_finalize import (
    _finalize_execution,
    _is_closure,
    _mark_starting,
    _utcnow,
)


def run_cursor_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    config: AppConfig | None = None,
    timeout_seconds: float | None = None,
) -> Execution:
    """Run task via Cursor CLI adapter. Fail closed on bad structured output."""
    cfg = config or load_config()
    from ai_company.services.evidence import (
        collect_evidence,
        snapshot_head,
        snapshot_worktree,
    )
    from ai_company.services.policy_engine import (
        assert_dispatch_allowed,
        role_allows_writes,
    )
    from ai_company.services.redact import redact_obj
    from ai_company.services.worktree import create_task_worktree, remove_task_worktree

    assert_dispatch_allowed(session, task=task, role=worker.role, config=cfg)
    _mark_starting(session, task=task, worker=worker)

    exec_id = new_id("EXEC")
    out_dir = executions_dir() / exec_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"

    timeout = timeout_seconds
    if timeout is None:
        timeout = float(cfg.cursor_runner.timeout_minutes) * 60.0

    workspace = Path(cfg.cursor_runner.workspace or str(project_root()))
    wt = None
    allow_writes = role_allows_writes(cfg, worker.role, task)
    if allow_writes and getattr(cfg.cursor_runner, "use_worktrees", False):
        try:
            wt = create_task_worktree(task.id, workspace=workspace)
        except Exception:
            wt = None
    work_cwd = str(wt.path) if wt else str(workspace)
    baseline = None
    pre_dirty: dict[str, str] | None = None
    # ai-company's own state (DB, execution logs, minutes) is never the worker's change.
    ignore_prefixes: list[str] = []
    try:
        ignore_prefixes.append(
            data_dir().resolve().relative_to(Path(work_cwd).resolve()).as_posix()
        )
    except ValueError:
        pass
    if (Path(work_cwd) / ".git").exists():
        try:
            baseline = snapshot_head(Path(work_cwd))
            # Uncommitted output of earlier workers: blame only what this run changes.
            pre_dirty = snapshot_worktree(Path(work_cwd), ignore_prefixes=ignore_prefixes)
        except Exception:
            baseline = None
            pre_dirty = None

    progress(
        f"Cursor起動  {task.id}  「{task.title}」  "
        f"制限時間={int(timeout)}秒  作業場所={work_cwd}"
    )

    # Early execution row so PID / heartbeat are observable while running
    execution = Execution(
        id=exec_id,
        task_id=task.id,
        worker_id=worker.id,
        started_at=_utcnow(),
        finished_at=None,
        exit_code=None,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        success=None,
        estimated_cost=task.estimated_cost,
        actual_cost=None,
        result_json=None,
        worktree_path=str(wt.path) if wt else None,
        base_commit=baseline,
        runner_kind="cursor",
    )
    session.add(execution)
    session.flush()

    def _beat(pid: int) -> None:
        execution.pid = pid
        execution.heartbeat_at = _utcnow()
        worker.pid = pid
        session.flush()

    capture = run_cursor_cli(
        task,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        config=cfg,
        timeout_seconds=timeout_seconds,
        cwd=work_cwd,
        session=session,
        on_heartbeat=_beat,
    )

    parse_kind: RoleParseKind
    if is_validation_task(task):
        parse_kind = "reviewer"
    elif is_judge_task(task):
        parse_kind = "judge"
    elif task.kind in {TaskKind.REPLAN, "replan"}:
        parse_kind = "supervisor"
    elif task.kind in {TaskKind.HEAD_BRIEF, "head_brief"}:
        parse_kind = "head"
    elif task.kind in {TaskKind.ATTENDANCE_REVIEW, "attendance_review"}:
        parse_kind = "attendance"
    elif _is_closure(task):
        parse_kind = "closure"
    else:
        parse_kind = "coding"

    parsed = parse_for_kind(capture.stdout, parse_kind)
    success = (
        not capture.timed_out
        and capture.exit_code == 0
        and parsed.ok
        and parsed.result is not None
    )
    progress(
        f"Cursor終了  {task.id}  終了コード={capture.exit_code}  "
        f"タイムアウト={capture.timed_out}  解析成功={parsed.ok}  "
        f"解析エラー={parsed.error}"
    )
    result_json: dict[str, Any] = parsed.result or {
        "status": "failed",
        "parse_error": parsed.error,
        "timed_out": capture.timed_out,
        "exit_code": capture.exit_code,
    }
    if capture.timed_out:
        result_json["timed_out"] = True
    if parsed.error and not parsed.ok:
        # Keep the cause even when the parser returned partial data (e.g.
        # missing_field:status): the supervisor and `status` show it.
        result_json["parse_error"] = parsed.error
    result_json["parse_kind"] = parse_kind

    reported = []
    if isinstance(result_json.get("changed_files"), list):
        reported = [str(x) for x in result_json["changed_files"]]
    if baseline is not None:
        try:
            evidence = collect_evidence(
                Path(work_cwd),
                baseline_ref=baseline,
                reported_files=reported,
                role=worker.role,
                allow_writes=allow_writes,
                allowed_paths=list(task.allowed_paths or []) or None,
                forbidden_paths=list(task.forbidden_paths or []) or None,
                pre_dirty=pre_dirty,
                ignore_prefixes=ignore_prefixes,
            )
        except Exception as exc:
            from ai_company.services.evidence import EvidencePack

            evidence = EvidencePack(
                workspace=work_cwd,
                baseline_ref=baseline,
                reported_files=reported,
                notes=[f"evidence_collect_error:{exc}"],
            )
    else:
        from ai_company.services.evidence import EvidencePack

        evidence = EvidencePack(
            workspace=work_cwd,
            reported_files=reported,
            notes=["evidence_skipped:no_git_baseline"],
        )
    # Only fail closed on evidence when baseline exists and config requires it
    result_json["evidence"] = evidence.to_dict()
    require_match = getattr(cfg.cursor_runner, "require_evidence_match", True)
    if baseline and require_match and not evidence.ok:
        success = False
        result_json["status"] = "failed"
        result_json["evidence_error"] = (
            "write_forbidden"
            if evidence.write_roles_violated
            else "changed_files_mismatch"
        )
        progress(
            f"Evidence不一致  {task.id}  missing={evidence.missing_reported[:5]}  "
            f"write_violated={evidence.write_roles_violated}"
        )

    # System-owned test re-run from project manifest (P0-2)
    if success and allow_writes and parse_kind == "coding":
        try:
            from ai_company.services.manifest import load_manifest, run_manifest_command

            man = load_manifest(Path(work_cwd))
            if man.test_command:
                test_ev = run_manifest_command(man.test_command, cwd=Path(work_cwd))
                result_json["manifest_tests"] = test_ev
                if not test_ev.get("ok"):
                    success = False
                    result_json["status"] = "failed"
                    result_json["evidence_error"] = "manifest_tests_failed"
                    progress(f"Manifestテスト失敗  {task.id}")
        except Exception as exc:
            result_json["manifest_tests"] = {"ok": False, "error": str(exc)}

    result_json = redact_obj(result_json)

    actual_cost = task.estimated_cost if success else task.estimated_cost * 0.5
    # Prefer measured usage when Cursor/result reports it (P0-8)
    for key in ("cost_usd", "actual_cost", "usage_cost_usd"):
        raw = result_json.get(key)
        if isinstance(raw, (int, float)) and float(raw) >= 0:
            actual_cost = float(raw)
            break
    usage = result_json.get("usage")
    if isinstance(usage, dict):
        for key in ("cost_usd", "total_cost", "usd"):
            raw = usage.get(key)
            if isinstance(raw, (int, float)) and float(raw) >= 0:
                actual_cost = float(raw)
                break
    execution.finished_at = _utcnow()
    execution.exit_code = capture.exit_code
    execution.success = success
    execution.actual_cost = actual_cost
    execution.result_json = result_json
    execution.pid = capture.pid
    execution.heartbeat_at = _utcnow()
    session.flush()

    # Persist EvidenceBundle row when we collected objective evidence
    try:
        import hashlib

        from ai_company.db.models import EvidenceBundle

        payload = evidence.to_dict()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:32]
        session.add(
            EvidenceBundle(
                id=new_id("EVB"),
                execution_id=execution.id,
                artifact_hash=digest,
                diff_hash=baseline,
                changed_files=evidence.changed_files,
                commands=[capture.command[:6]] if capture.command else [],
                test_summary=(
                    result_json.get("tests")
                    if isinstance(result_json.get("tests"), dict)
                    else None
                ),
                payload=payload,
                ok=evidence.ok,
            )
        )
        session.flush()
    except Exception:
        pass

    record_audit(
        session,
        event_type="cursor_runner_finished",
        actor_type="system",
        actor_id="cursor_runner",
        target_type="execution",
        target_id=execution.id,
        metadata={
            "success": success,
            "timed_out": capture.timed_out,
            "exit_code": capture.exit_code,
            "parse_error": parsed.error,
            "parse_kind": parse_kind,
            "evidence_ok": evidence.ok,
            "worktree": str(wt.path) if wt else None,
            "command0": capture.command[0] if capture.command else None,
        },
    )

    if wt and not success:
        remove_task_worktree(wt.path, workspace=workspace)

    return _finalize_execution(
        session,
        task=task,
        worker=worker,
        execution=execution,
        success=success,
        timed_out=capture.timed_out,
        config=cfg,
    )
