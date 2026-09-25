"""Scheduler: lease READY tasks, consult Cost Governor, hire workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Project, Task, Worker
from ai_company.enums import CostDecision, ProjectStatus
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.cleanup import run_cleanup
from ai_company.services.progress import emit as progress
from ai_company.services.staffing import (
    auto_approve_open_requests,
    note_staffing_hire,
)
from ai_company.services.tasks import list_ready_tasks
from ai_company.services.validation import is_judge_task, is_validation_task
from ai_company.services.workers import RunnerKind, dispatch_worker, hire_worker


@dataclass
class SchedulerResult:
    attempted: int = 0
    started: int = 0
    denied: int = 0
    deferred: int = 0
    throttled: int = 0
    leases_reclaimed: int = 0
    zombies_killed: int = 0
    details: list[dict[str, str]] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def acquire_lease(session: Session, task: Task, *, holder: str, minutes: int = 10) -> bool:
    """Compare-and-swap lease so two schedulers cannot both hold the same task."""
    from sqlalchemy import or_, update

    now = _utcnow()
    expires = now + timedelta(minutes=minutes)
    result = session.execute(
        update(Task)
        .where(Task.id == task.id)
        .where(
            or_(
                Task.leased_by.is_(None),
                Task.lease_expires_at.is_(None),
                Task.lease_expires_at <= now,
                Task.leased_by == holder,
            )
        )
        .values(leased_by=holder, lease_expires_at=expires)
    )
    session.flush()
    rowcount = getattr(result, "rowcount", None)
    if int(rowcount or 0) != 1:
        return False
    session.refresh(task)
    return True


def release_lease(session: Session, task: Task) -> None:
    task.leased_by = None
    task.lease_expires_at = None
    session.flush()


def _try_prepare(
    session: Session,
    *,
    task: Task,
    cfg: AppConfig,
    holder: str,
    result: SchedulerResult,
    started: int,
    started_validation: int,
    started_validation_by_plan: dict[str, int],
    started_by_role: dict[str, int],
    seen_blocked_lease: set[str],
) -> Worker | None:
    """Lease + capacity + cost + policy + hire. None if skipped."""
    result.attempted += 1
    if not acquire_lease(session, task, holder=holder):
        seen_blocked_lease.add(task.id)
        result.details.append({"task_id": task.id, "result": "lease_held"})
        return None

    from ai_company.services.staffing import (
        count_active_workers_for_role,
        role_capacity_for,
    )

    role_cap = role_capacity_for(task.role_required, cfg)
    if role_cap is not None:
        active_role = count_active_workers_for_role(
            session, role=task.role_required, project_id=task.project_id
        )
        cycle_role = started_by_role.get(task.role_required, 0)
        if active_role + cycle_role >= role_cap:
            result.throttled += 1
            release_lease(session, task)
            seen_blocked_lease.add(task.id)
            result.details.append(
                {
                    "task_id": task.id,
                    "result": "THROTTLE",
                    "reason": (
                        f"role_capacity:{task.role_required}:"
                        f"{active_role + cycle_role}/{role_cap}"
                    ),
                }
            )
            return None

    if is_validation_task(task) or is_judge_task(task):
        val_cap = cfg.settings.scheduler.max_validation_workers
        if cg.count_active_validation_workers(session) + started_validation >= val_cap:
            result.throttled += 1
            release_lease(session, task)
            seen_blocked_lease.add(task.id)
            result.details.append(
                {
                    "task_id": task.id,
                    "result": "THROTTLE",
                    "reason": f"max_validation_workers_cycle:{val_cap}",
                }
            )
            return None
        if is_validation_task(task) and task.validation_plan_id:
            parallel_cap = min(
                cfg.budget.validation.max_parallel_reviewers_per_task,
                cfg.validation.max_parallel_reviewers_per_task,
            )
            plan_n = started_validation_by_plan.get(task.validation_plan_id, 0)
            plan_n += cg.count_active_reviewers_for_plan(session, task.validation_plan_id)
            if plan_n >= parallel_cap:
                result.throttled += 1
                release_lease(session, task)
                seen_blocked_lease.add(task.id)
                result.details.append(
                    {
                        "task_id": task.id,
                        "result": "THROTTLE",
                        "reason": f"max_parallel_reviewers:{parallel_cap}",
                    }
                )
                return None

    verdict = cg.evaluate(session, task=task, config=cfg)
    if verdict.decision == CostDecision.DENY:
        result.denied += 1
        cg.deny_and_defer(session, task=task, verdict=verdict)
        release_lease(session, task)
        seen_blocked_lease.add(task.id)
        result.details.append(
            {"task_id": task.id, "result": "DENY", "reason": verdict.reason}
        )
        return None
    if verdict.decision in {CostDecision.DEFER, CostDecision.REQUIRE_APPROVAL}:
        result.deferred += 1
        cg.deny_and_defer(session, task=task, verdict=verdict)
        release_lease(session, task)
        seen_blocked_lease.add(task.id)
        result.details.append(
            {"task_id": task.id, "result": verdict.decision, "reason": verdict.reason}
        )
        return None

    active = cg.count_active_workers(session) + started
    if active >= verdict.effective_max_workers:
        result.throttled += 1
        release_lease(session, task)
        seen_blocked_lease.add(task.id)
        result.details.append(
            {
                "task_id": task.id,
                "result": "THROTTLE",
                "reason": f"at_capacity:{active}/{verdict.effective_max_workers}",
            }
        )
        return None

    cg.reserve_budget(session, task=task)
    try:
        from ai_company.services.policy_engine import assert_dispatch_allowed

        assert_dispatch_allowed(session, task=task, role=task.role_required, config=cfg)
    except PermissionError as exc:
        result.denied += 1
        release_lease(session, task)
        seen_blocked_lease.add(task.id)
        record_audit(
            session,
            event_type="policy_denied_dispatch",
            actor_type="system",
            actor_id="scheduler",
            target_type="task",
            target_id=task.id,
            metadata={"reason": str(exc)},
        )
        result.details.append(
            {"task_id": task.id, "result": "DENY", "reason": f"policy:{exc}"}
        )
        return None
    except Exception as exc:
        record_audit(
            session,
            event_type="policy_check_error",
            actor_type="system",
            actor_id="scheduler",
            target_type="task",
            target_id=task.id,
            metadata={"error": str(exc)},
        )

    worker = hire_worker(session, task=task)
    note_staffing_hire(session, project_id=task.project_id, role=task.role_required)
    record_audit(
        session,
        event_type="scheduler_dispatched",
        actor_type="system",
        actor_id="scheduler",
        target_type="task",
        target_id=task.id,
        metadata={
            "worker_id": worker.id,
            "decision": verdict.decision,
        },
    )
    return worker


def run_once(
    session: Session,
    *,
    config: AppConfig | None = None,
    mock_succeed: bool = True,
    max_to_start: int | None = None,
    runner: RunnerKind = "mock",
    timeout_seconds: float | None = None,
) -> SchedulerResult:
    """
    One scheduling cycle:
    cleanup -> READY -> Cost check -> reserve -> hire -> runner
    Re-fetches READY after each batch so review tasks enqueued mid-cycle can run.
    Mock runner batches currently-READY tasks and executes them in parallel.
    """
    cfg = config or load_config()
    result = SchedulerResult()
    holder = "scheduler"

    cleanup = run_cleanup(session, config=cfg)
    result.leases_reclaimed = cleanup.leases_reclaimed
    result.zombies_killed = cleanup.zombies_killed
    auto_approve_open_requests(session, config=cfg)
    cg.revive_deferred_budget_tasks(session, config=cfg)
    from ai_company.services.escalation import recover_orphaned_blocked_to_manager

    recovered = recover_orphaned_blocked_to_manager(session, config=cfg)
    if recovered:
        progress(f"孤児停止をマネージャーへ移管: {len(recovered)} 件")

    from ai_company.services.closure_loops import maybe_spawn_closure_loop

    for proj in session.scalars(
        select(Project).where(Project.status == ProjectStatus.ACTIVE)
    ).all():
        maybe_spawn_closure_loop(session, project_id=proj.id, config=cfg)

    slots_budget = max_to_start
    if slots_budget is None:
        slots_budget = cfg.settings.scheduler.max_concurrent_workers

    started = 0
    started_validation = 0
    seen_blocked_lease: set[str] = set()
    started_ids: set[str] = set()
    started_validation_by_plan: dict[str, int] = {}
    started_by_role: dict[str, int] = {}

    from ai_company.ui_ja import kind_ja, role_ja, runner_ja, status_ja

    while started < slots_budget:
        # Snapshot currently READY (independent of work that will enqueue mid-cycle)
        batch: list[tuple[Task, Worker]] = []
        while started + len(batch) < slots_budget:
            ready = list_ready_tasks(session)
            task = next(
                (
                    t
                    for t in ready
                    if t.id not in seen_blocked_lease and t.id not in started_ids
                ),
                None,
            )
            if task is None:
                break
            # Capacity uses DB-active (HIRED+) plus cycle-completed counts.
            # Do NOT add len(batch): batch workers are already HIRED in DB.
            worker = _try_prepare(
                session,
                task=task,
                cfg=cfg,
                holder=holder,
                result=result,
                started=started,
                started_validation=started_validation,
                started_validation_by_plan=started_validation_by_plan,
                started_by_role=started_by_role,
                seen_blocked_lease=seen_blocked_lease,
            )
            if worker is None:
                continue
            batch.append((task, worker))
            started_ids.add(task.id)
            # Cursor stays one-at-a-time in the outer sense; mock can fill a batch
            if runner != "mock":
                break

        if not batch:
            break

        if runner == "mock" and len(batch) > 1:
            from ai_company.services.executor import run_mock_jobs_parallel

            for task, worker in batch:
                progress(
                    f"開始  {kind_ja(task.kind)}/{role_ja(task.role_required)}  "
                    f"{task.id}  「{task.title}」  方式={runner_ja(runner)}  "
                    f"ワーカー={worker.id}"
                )
            session.flush()
            session.commit()
            session.expire_all()
            jobs = [(t.id, w.id) for t, w in batch]
            report = run_mock_jobs_parallel(
                jobs,
                mock_succeed=mock_succeed,
                max_workers=len(jobs),
            )
            by_id = {j.job_id: j for j in report.results}
            for task, worker in batch:
                job = by_id.get(task.id)
                t = session.get(Task, task.id)
                if t is not None:
                    release_lease(session, t)
                    progress(
                        f"終了  {'成功' if (job and job.ok) else '失敗'}  {task.id}  "
                        f"状態={status_ja(t.status if t else None)}  "
                        f"{job.detail if job else ''}"
                    )
                result.started += 1
                started += 1
                # After completion workers leave ACTIVE_*; count against cycle caps.
                t_done = session.get(Task, task.id)
                if t_done is not None:
                    started_by_role[t_done.role_required] = (
                        started_by_role.get(t_done.role_required, 0) + 1
                    )
                    if is_validation_task(t_done) or is_judge_task(t_done):
                        started_validation += 1
                        if is_validation_task(t_done) and t_done.validation_plan_id:
                            started_validation_by_plan[t_done.validation_plan_id] = (
                                started_validation_by_plan.get(
                                    t_done.validation_plan_id, 0
                                )
                                + 1
                            )
                result.details.append(
                    {
                        "task_id": task.id,
                        "result": "STARTED",
                        "worker_id": worker.id,
                        "runner": "mock",
                        "success": str(bool(job and job.ok)),
                        "parallel": "true",
                    }
                )
            # Continue outer while — newly READY validation/judge appear next
            continue

        # Sequential path (cursor, or single mock)
        task, worker = batch[0]
        progress(
            f"開始  {kind_ja(task.kind)}/{role_ja(task.role_required)}  {task.id}  "
            f"「{task.title}」  方式={runner_ja(runner)}  ワーカー={worker.id}"
            + (
                f"  親={task.parent_task_id}"
                if task.parent_task_id
                and task.kind in {"repair", "replan", "validation", "judge"}
                else ""
            )
        )
        execution = dispatch_worker(
            session,
            task=task,
            worker=worker,
            runner=runner,
            mock_succeed=mock_succeed,
            config=cfg,
            timeout_seconds=timeout_seconds,
        )
        session.refresh(task)
        outcome = "成功" if execution.success else "失敗"
        extra = ""
        if execution.result_json:
            err = execution.result_json.get("parse_error")
            if err:
                extra = f"  解析エラー={err}"
            v = execution.result_json.get("verdict")
            if v:
                extra += f"  判定={v}"
        progress(
            f"終了  {outcome}  {task.id}  状態={status_ja(task.status)}  "
            f"実行={execution.id}{extra}"
        )
        release_lease(session, task)
        result.started += 1
        started += 1
        started_by_role[task.role_required] = (
            started_by_role.get(task.role_required, 0) + 1
        )
        if is_validation_task(task) or is_judge_task(task):
            started_validation += 1
            if is_validation_task(task) and task.validation_plan_id:
                started_validation_by_plan[task.validation_plan_id] = (
                    started_validation_by_plan.get(task.validation_plan_id, 0) + 1
                )
        result.details.append(
            {
                "task_id": task.id,
                "result": "STARTED",
                "worker_id": worker.id,
                "runner": runner,
                "execution_id": execution.id,
                "success": str(bool(execution.success)),
            }
        )

    return result
