"""Worker lifecycle: Mock Runner and Cursor Runner via Worker Manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import ReviewVerdict, TaskKind, TaskStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir, project_root
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.cursor_runner import run_cursor_cli
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.progress import emit as progress
from ai_company.services.result_parser import RoleParseKind, parse_for_kind
from ai_company.services.tasks import create_task
from ai_company.services.validation import (
    finalize_judge,
    is_implementation_task,
    is_judge_task,
    is_validation_task,
    record_validation_result,
    start_validation_gate,
)
from ai_company.ui_ja import role_ja

RunnerKind = Literal["mock", "cursor"]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def hire_worker(
    session: Session,
    *,
    task: Task,
    reuse_worker_id: str | None = None,
    config: AppConfig | None = None,
) -> Worker:
    """Hire a new ephemeral worker. Reusing a coding worker for validation is forbidden."""
    cfg = config or load_config()
    if reuse_worker_id and (
        is_validation_task(task) or is_judge_task(task)
    ):
        if cfg.validation.require_independent_execution:
            coding_worker_id = _coding_worker_id_for_task(session, task)
            if coding_worker_id and reuse_worker_id == coding_worker_id:
                record_audit(
                    session,
                    event_type="permission_denied",
                    actor_type="system",
                    actor_id="worker_manager",
                    target_type="task",
                    target_id=task.id,
                    metadata={
                        "reason": "reviewer_independence_violation",
                        "reuse_worker_id": reuse_worker_id,
                        "coding_worker_id": coding_worker_id,
                    },
                )
                raise PermissionError(
                    "Reviewer cannot reuse Coding Worker identity "
                    f"({reuse_worker_id})"
                )

    worker = Worker(
        id=new_id("WORKER"),
        role=task.role_required,
        project_id=task.project_id,
        task_id=task.id,
        status=WorkerStatus.HIRED,
        cost_estimated=task.estimated_cost,
    )
    session.add(worker)
    session.flush()
    record_audit(
        session,
        event_type="worker_hired",
        actor_type="system",
        actor_id="worker_manager",
        target_type="worker",
        target_id=worker.id,
        metadata={"task_id": task.id, "role": worker.role, "kind": task.kind},
    )
    return worker


def _coding_worker_id_for_task(session: Session, task: Task) -> str | None:
    if not task.source_execution_id:
        return None
    from ai_company.db.models import Execution

    execution = session.get(Execution, task.source_execution_id)
    if execution is None:
        return None
    return execution.worker_id


def _mark_starting(session: Session, *, task: Task, worker: Worker) -> None:
    worker.status = WorkerStatus.STARTING
    task.status = TaskStatus.RUNNING
    session.flush()
    record_audit(
        session,
        event_type="worker_started",
        actor_type="system",
        actor_id="worker_manager",
        target_type="worker",
        target_id=worker.id,
        metadata={"task_id": task.id},
    )
    record_audit(
        session,
        event_type="task_started",
        actor_type="worker",
        actor_id=worker.id,
        target_type="task",
        target_id=task.id,
        metadata={},
    )
    worker.status = WorkerStatus.ACTIVE
    session.flush()


def _finalize_execution(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    execution: Execution,
    success: bool,
    timed_out: bool = False,
    config: AppConfig | None = None,
) -> Execution:
    cfg = config or load_config()
    worker.status = WorkerStatus.REPORTING
    session.flush()

    final_status = WorkerStatus.TIMED_OUT if timed_out else WorkerStatus.TERMINATED
    if not success and not timed_out:
        final_status = WorkerStatus.TERMINATED
    terminate_worker(session, worker=worker, status=final_status)

    actual_cost = (
        execution.actual_cost
        if execution.actual_cost is not None
        else task.estimated_cost
    )
    worker.cost_actual = actual_cost

    if success:
        cg.spend_budget(session, task=task, amount=float(actual_cost))

        # Judge: hard rules + decision; only Judge may mark parent DONE
        if is_judge_task(task):
            finalize_judge(session, judge_task=task, config=cfg)
            return execution

        # Validation reviewer: record evidence, mark this task DONE (not parent)
        if is_validation_task(task):
            payload = execution.result_json or {}
            verdict = str(payload.get("verdict") or payload.get("status") or "PASS").upper()
            if verdict in {"COMPLETED", "SUCCESS"}:
                verdict = ReviewVerdict.PASS
            if verdict not in {ReviewVerdict.PASS, ReviewVerdict.FAIL, ReviewVerdict.UNCERTAIN}:
                verdict = ReviewVerdict.PASS if success else ReviewVerdict.FAIL
            record_validation_result(
                session,
                validation_task=task,
                worker_id=worker.id,
                execution_id=execution.id,
                verdict=verdict,
                findings=list(payload.get("findings") or []),
                evidence=list(payload.get("evidence") or []),
                confidence=payload.get("confidence", 0.9),
            )
            task.status = TaskStatus.DONE
            session.flush()
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"execution_id": execution.id, "kind": "validation"},
            )
            from ai_company.services.tasks import refresh_dependents

            refresh_dependents(session, task.id)
            return execution

        from ai_company.services.closure_loops import apply_closure_result, is_closure_task

        if is_closure_task(task):
            payload = execution.result_json or {}
            apply_closure_result(
                session, task=task, payload=payload, config=cfg
            )
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={
                    "execution_id": execution.id,
                    "kind": task.kind,
                    "closure_verdict": (payload.get("closure_verdict") or "CLEAR"),
                },
            )
            return execution

        # Implementation / repair / manager replan
        if is_implementation_task(task):
            if task.kind in {"replan", TaskKind.REPLAN}:
                # A supervisor decision becomes another queued task. The supervisor
                # never starts a worker directly.
                task.status = TaskStatus.DONE
                session.flush()
                record_audit(
                    session,
                    event_type="task_completed",
                    actor_type="worker",
                    actor_id=worker.id,
                    target_type="task",
                    target_id=task.id,
                    metadata={"execution_id": execution.id, "kind": "replan"},
                )
                parent = (
                    session.get(Task, task.parent_task_id) if task.parent_task_id else None
                )
                if parent is not None:
                    payload = execution.result_json or {}
                    decision = str(payload.get("decision") or "RETRY").upper()
                    guidance = (
                        str(payload.get("instructions") or payload.get("summary") or "")
                        or str(payload.get("reason") or "")
                        or f"Manager replan after: {task.objective}"
                    )
                    if decision == "ESCALATE":
                        escalate_blocked_task(
                            session,
                            task=task,
                            reason=(
                                f"supervisor_requested:{task.role_required}:"
                                f"{payload.get('reason') or guidance}"
                            ),
                            config=cfg,
                        )
                        return execution

                    from ai_company.roles import role_def

                    requested_role = str(payload.get("target_role") or "").strip()
                    target_role = parent.role_required or "coding_worker"
                    if decision == "REASSIGN" and requested_role:
                        candidate = role_def(requested_role)
                        if candidate.layer == "worker":
                            target_role = requested_role
                    parent.retry_count = 0
                    repair = create_task(
                        session,
                        title=f"Repair: {parent.title}",
                        priority=min(100, parent.priority + 5),
                        project_id=parent.project_id,
                        objective=(
                            f"Follow engineering_manager guidance.\n{guidance}"
                        ),
                        role_required=target_role,
                        estimated_cost=parent.estimated_cost,
                        parent_task_id=parent.id,
                        kind=TaskKind.REPAIR,
                        risk_level=parent.risk_level,
                        config=cfg,
                    )
                    parent.status = TaskStatus.FAILED
                    session.flush()
                    record_audit(
                        session,
                        event_type="repair_task_created",
                        actor_type="worker",
                        actor_id=worker.id,
                        target_type="task",
                        target_id=repair.id,
                        metadata={
                            "parent": parent.id,
                            "via": "manager_replan",
                            "replan_id": task.id,
                            "handler_role": task.role_required,
                            "supervisor_role": task.role_required,
                            "decision": decision,
                            "executor_role": target_role,
                        },
                    )
                    progress(
                        f"上司対応完了  {role_ja(task.role_required)}が方針決定 → "
                        f"{role_ja(target_role)} へ修正指示  "
                        f"修正={repair.id}  親={parent.id}"
                    )
                return execution

            if task.kind == "repair":
                # Repair completion closes the repair task and opens a NEW validation plan
                # on the original parent — never reuse old ValidationResults for PASS.
                task.status = TaskStatus.DONE
                session.flush()
                record_audit(
                    session,
                    event_type="task_completed",
                    actor_type="worker",
                    actor_id=worker.id,
                    target_type="task",
                    target_id=task.id,
                    metadata={"execution_id": execution.id, "kind": "repair"},
                )
                parent = (
                    session.get(Task, task.parent_task_id) if task.parent_task_id else None
                )
                if parent is not None and cfg.validation.enabled:
                    start_validation_gate(
                        session,
                        parent=parent,
                        source_execution_id=execution.id,
                        config=cfg,
                    )
                return execution

            if cfg.validation.enabled and not cfg.validation.coding_worker_can_self_approve:
                start_validation_gate(
                    session,
                    parent=task,
                    source_execution_id=execution.id,
                    config=cfg,
                )
                return execution
            # Fail-closed: even if disabled, do not silent self-approve in production
            # paths; tests may set enabled=false for concurrency isolation only.
            task.status = TaskStatus.DONE
            session.flush()
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"execution_id": execution.id, "validation_disabled": True},
            )
            from ai_company.services.tasks import refresh_dependents

            refresh_dependents(session, task.id)
            return execution

        task.status = TaskStatus.DONE
        session.flush()
        return execution

    # Failure path
    cg.release_reservation(session, task=task, amount=task.estimated_cost)

    if is_validation_task(task):
        # Failed validation execution → FAIL verdict recorded, task DONE so Judge can run
        record_validation_result(
            session,
            validation_task=task,
            worker_id=worker.id,
            execution_id=execution.id,
            verdict=ReviewVerdict.FAIL,
            findings=[
                {
                    "severity": "high",
                    "category": "execution",
                    "description": "validation worker failed",
                }
            ],
            evidence=[],
            confidence=0.5,
        )
        task.status = TaskStatus.DONE
        session.flush()
        from ai_company.services.tasks import refresh_dependents

        refresh_dependents(session, task.id)
        cg.record_failure_and_maybe_trip(session)
        return execution

    task.retry_count += 1
    if timed_out:
        record_audit(
            session,
            event_type="task_failed",
            actor_type="worker",
            actor_id=worker.id,
            target_type="task",
            target_id=task.id,
            metadata={"reason": "timeout", "retry_count": task.retry_count},
        )
    if task.retry_count > task.max_retries:
        escalate_blocked_task(
            session,
            task=task,
            reason=f"retries_exhausted:{task.retry_count}/{task.max_retries}",
            config=cfg,
        )
    else:
        if not timed_out:
            record_audit(
                session,
                event_type="task_failed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"retry_count": task.retry_count},
            )
        task.status = TaskStatus.READY
        record_audit(
            session,
            event_type="task_ready",
            actor_type="system",
            actor_id="retry",
            target_type="task",
            target_id=task.id,
            metadata={"retry": True, "timed_out": timed_out},
        )
    session.flush()
    cg.record_failure_and_maybe_trip(session)
    return execution


def run_mock_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    succeed: bool = True,
) -> Execution:
    """Synchronous mock runner: STARTING -> ACTIVE -> REPORTING -> TERMINATED."""
    _mark_starting(session, task=task, worker=worker)

    exec_id = new_id("EXEC")
    out_dir = executions_dir() / exec_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"

    result: dict[str, Any]
    if succeed:
        if is_validation_task(task):
            result = {
                "status": "completed",
                "verdict": "PASS",
                "summary": f"mock {task.review_type} pass",
                "findings": [],
                "evidence": [{"kind": "mock"}],
                "confidence": 0.95,
                "mock": True,
                "task_id": task.id,
            }
        elif is_judge_task(task):
            result = {
                "status": "completed",
                "verdict": "PASS",
                "reason": "mock judge defers to hard rules",
                "mock": True,
                "task_id": task.id,
            }
        elif task.kind in {TaskKind.REPLAN, "replan"}:
            result = {
                "status": "completed",
                "decision": "RETRY",
                "reason": "mock supervisor diagnosis",
                "instructions": "Apply the supervisor diagnosis and rerun tests.",
                "target_role": "",
                "confidence": 0.9,
                "mock": True,
                "task_id": task.id,
            }
        elif task.kind in {
            TaskKind.STILL_NEEDED,
            TaskKind.PROJECT_HEALTH,
            TaskKind.PRODUCT_FIT,
            TaskKind.USABILITY_AUDIT,
            TaskKind.CUSTOMER_SUCCESS,
            TaskKind.IT_OPS_AUDIT,
            TaskKind.COMPLIANCE_AUDIT,
            TaskKind.TEST_AUDIT,
            TaskKind.QUALITY_AUDIT,
            TaskKind.SALES_AUDIT,
            TaskKind.MAINTENANCE_AUDIT,
            "still_needed",
            "project_health",
            "product_fit",
            "usability_audit",
            "customer_success",
            "it_ops_audit",
            "compliance_audit",
            "test_audit",
            "quality_audit",
            "sales_audit",
            "maintenance_audit",
        }:
            result = {
                "status": "completed",
                "closure_verdict": "CLEAR",
                "summary": f"mock {task.kind} found nothing remaining",
                "proposed_tasks": [],
                "evidence": [{"kind": "mock_closure"}],
                "mock": True,
                "task_id": task.id,
            }
        else:
            result = {
                "status": "completed",
                "changed_files": [],
                "tests": {"passed": 1, "failed": 0},
                "commit": None,
                "issues": [],
                "mock": True,
                "task_id": task.id,
            }
        stdout_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        exit_code = 0
        success = True
        actual_cost = task.estimated_cost
    else:
        if is_validation_task(task):
            result = {
                "status": "failed",
                "verdict": "FAIL",
                "summary": f"mock {task.review_type} fail",
                "findings": [
                    {
                        "severity": "high",
                        "category": task.review_type or "review",
                        "description": "mock failure",
                        "evidence": [],
                        "recommended_fix": "repair",
                    }
                ],
                "evidence": [],
                "confidence": 0.7,
                "mock": True,
                "task_id": task.id,
            }
        else:
            result = {
                "status": "failed",
                "changed_files": [],
                "tests": {"passed": 0, "failed": 1},
                "commit": None,
                "issues": ["mock failure"],
                "mock": True,
                "task_id": task.id,
            }
        stdout_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        stderr_path.write_text("mock failure\n", encoding="utf-8")
        exit_code = 1
        success = False
        actual_cost = task.estimated_cost * 0.5

    execution = Execution(
        id=exec_id,
        task_id=task.id,
        worker_id=worker.id,
        started_at=_utcnow(),
        finished_at=_utcnow(),
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        success=success,
        estimated_cost=task.estimated_cost,
        actual_cost=actual_cost,
        result_json=result,
    )
    session.add(execution)
    session.flush()
    return _finalize_execution(
        session, task=task, worker=worker, execution=execution, success=success
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
    from ai_company.services.evidence import collect_evidence, snapshot_head
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
    if (Path(work_cwd) / ".git").exists():
        try:
            baseline = snapshot_head(Path(work_cwd))
        except Exception:
            baseline = None

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


def dispatch_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    runner: RunnerKind = "mock",
    mock_succeed: bool = True,
    config: AppConfig | None = None,
    timeout_seconds: float | None = None,
) -> Execution:
    if runner == "cursor":
        return run_cursor_worker(
            session,
            task=task,
            worker=worker,
            config=config,
            timeout_seconds=timeout_seconds,
        )
    return run_mock_worker(session, task=task, worker=worker, succeed=mock_succeed)


def terminate_worker(
    session: Session,
    *,
    worker: Worker,
    status: WorkerStatus = WorkerStatus.TERMINATED,
) -> None:
    worker.status = WorkerStatus.TERMINATING
    session.flush()
    worker.status = status
    worker.terminated_at = _utcnow()
    session.flush()
    record_audit(
        session,
        event_type="worker_terminated",
        actor_type="system",
        actor_id="worker_manager",
        target_type="worker",
        target_id=worker.id,
        metadata={"final_status": status},
    )


def list_workers(session: Session) -> list[Worker]:
    from sqlalchemy import select

    return list(session.scalars(select(Worker).order_by(Worker.hired_at.desc())).all())
