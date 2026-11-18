"""Product-level autonomous / no-look run loop (M10)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Artifact, Project, Task
from ai_company.enums import CostDecision, ProjectStatus, TaskKind, TaskStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir
from ai_company.services.audit import record_audit
from ai_company.services.cost_governor import (
    TIME_WINDOW_DEFER_REASONS,
    BudgetWindowWait,
    budget_window_wait,
    count_active_workers,
)
from ai_company.services.progress import emit as progress
from ai_company.services.scheduler import SchedulerResult, run_once
from ai_company.services.secretary import exception_digest, human_summary
from ai_company.services.staffing import auto_approve_open_requests
from ai_company.services.workers import RunnerKind

TERMINAL_TASK = {
    TaskStatus.DONE,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
}


@dataclass
class AutonomousResult:
    cycles: int = 0
    started_total: int = 0
    project_status: str = ""
    stopped_reason: str = ""
    release_candidate_id: str | None = None
    summaries: list[str] = field(default_factory=list)
    last_scheduler: SchedulerResult | None = None
    # Set when stopped_reason == "budget_wait" (hourly/daily window cap hit).
    budget_wait: BudgetWindowWait | None = None
    # Projects completed by a ``scheduler continue`` without ``--project``.
    completed_project_ids: list[str] = field(default_factory=list)
    # Every release candidate created in this run (``release_candidate_id`` is the last).
    release_candidate_ids: list[str] = field(default_factory=list)
    # Set when stopped_reason == "policy_denied": "<task id>: <reason>" lines.
    policy_denials: list[str] = field(default_factory=list)


# Cycles in a row where every candidate was denied by policy and nothing ran.
# Nothing can change without a person (or a supervisor) acting, so looping on is
# pure spin (live Cursor 2026-11-18: 64 cycles in ~2 s on one denied repair task).
POLICY_DEADLOCK_CYCLES = 3


def _policy_denials(session: Session, sched: SchedulerResult) -> list[str] | None:
    """Denied-only cycle with no live worker → the denial lines, else None."""
    if (
        sched.started
        or not sched.denied
        or sched.deferred
        or sched.throttled
        or count_active_workers(session)
    ):
        return None
    lines = [
        f"{d.get('task_id', '?')}: {d.get('reason', '')}"
        for d in sched.details
        if d.get("result") == "DENY"
    ]
    return lines or ["(policy denied)"]


def _policy_deadlock_progress(lines: list[str]) -> str:
    shown = "  /  ".join(lines[:3])
    return (
        f"停止  理由=権限で起動できないタスクだけが残っています（{POLICY_DEADLOCK_CYCLES}"
        f"サイクル連続）  {shown}"
    )


def _time_window_stall(
    session: Session, sched: SchedulerResult, cfg: AppConfig
) -> BudgetWindowWait | None:
    """Nothing started and every deferral was a rolling-window cap.

    Retrying in the same window only re-defers (DEFERRED_BUDGET is revived to
    READY every cycle), so the loop would spin until max_cycles. Detect it and
    let the caller stop with "budget_wait".
    """
    if sched.started > 0 or sched.deferred == 0:
        return None
    reasons = [
        str(d.get("reason") or "")
        for d in sched.details
        if d.get("result") in {CostDecision.DEFER, CostDecision.DEFER.value}
    ]
    if not reasons or any(r not in TIME_WINDOW_DEFER_REASONS for r in reasons):
        return None
    # Mirror evaluate's cost check (spent + estimate > cap) with the cheapest
    # deferred task, so a cost-cap DEFER below the raw cap is still detected.
    deferred_ids = [
        str(d.get("task_id"))
        for d in sched.details
        if d.get("result") in {CostDecision.DEFER, CostDecision.DEFER.value}
        and d.get("task_id")
    ]
    costs = [
        float(t.estimated_cost or 0.0)
        for t in (session.get(Task, tid) for tid in deferred_ids)
        if t is not None
    ]
    wait = budget_window_wait(session, config=cfg, next_cost=min(costs) if costs else 0.0)
    if wait is None:  # window moved on between evaluate and now — keep going
        return None
    return wait


def _budget_wait_progress(wait: BudgetWindowWait) -> str:
    mins = wait.wait_minutes()
    eta = (
        f"約{mins}分後（{wait.resume_at_local_text()} 頃）に再開可"
        if mins is not None
        else "再開時刻は不明"
    )
    return (
        f"停止  理由=予算待ち・{wait.reason_ja}（{wait.used:g}/{wait.limit:g}）  {eta}"
    )


def _implementation_tasks(session: Session, project_id: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task).where(
                Task.project_id == project_id,
                Task.kind.in_((TaskKind.IMPLEMENTATION, "implementation")),
            )
        ).all()
    )


def _project_complete(session: Session, project_id: str) -> bool:
    from ai_company.services.closure_loops import (
        all_implementation_done,
        closure_loops_finished,
    )

    if not all_implementation_done(session, project_id):
        return False
    if _open_document_work(session, project_id):
        # 部門作業（社長回答による改訂を含む）・部長所見が残っていれば未完了 (H-1)
        return False
    if not closure_loops_finished(session, project_id):
        return False
    # 社長への確認事項（価格・法務の最終確認など）が未回答ならリリース可にしない
    from ai_company.services.meeting_questions import decisions_pending

    return not decisions_pending(session, project_id)


def _awaiting_decision(session: Session, project_id: str | None) -> bool:
    """Nothing left to run; only the president's answers hold the release."""
    if not project_id:
        return any(
            _awaiting_decision(session, p.id)
            for p in session.scalars(
                select(Project).where(Project.status == ProjectStatus.ACTIVE)
            ).all()
        )
    from ai_company.services.delivery_state import project_delivery_phase

    return project_delivery_phase(session, session.get(Project, project_id)) == (
        "awaiting_decision"
    )


_DOCUMENT_WORK_KINDS = ("department_work", "head_brief", "attendance_review")


def _open_document_work(session: Session, project_id: str) -> int:
    """Open (not DONE / CANCELLED) department_work / head_brief / attendance_review tasks."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.kind.in_(_DOCUMENT_WORK_KINDS),
                Task.status.not_in((TaskStatus.DONE, TaskStatus.CANCELLED)),
            )
        )
        or 0
    )


def _active_project_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.status == ProjectStatus.ACTIVE)
        )
        or 0
    )


def _record_release(result: AutonomousResult, art_id: str) -> None:
    """Keep every release candidate made in this run (L-1), latest last."""
    result.release_candidate_id = art_id
    if art_id not in result.release_candidate_ids:
        result.release_candidate_ids.append(art_id)


def _has_blocking_exception(session: Session, project_id: str) -> bool:
    blocked = int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.project_id == project_id, Task.status == TaskStatus.BLOCKED)
        )
        or 0
    )
    return blocked > 0


def _idle(session: Session, project_id: str) -> bool:
    """No READY/RUNNING work left for the project (and dependents)."""
    active = int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.status.in_(
                    (
                        TaskStatus.READY,
                        TaskStatus.RUNNING,
                        TaskStatus.WAITING,
                        TaskStatus.REVIEW_REQUIRED,
                        TaskStatus.FAILED,
                    )
                ),
            )
        )
        or 0
    )
    return active == 0


def create_release_candidate(
    session: Session, *, project: Project
) -> Artifact:
    from ai_company.config import load_config
    from ai_company.services.closure_loops import CLOSURE_KINDS, closure_loops_finished
    from ai_company.services.policy import policy_snapshot
    from ai_company.services.secretary import project_delivery_phase

    out_dir = executions_dir() / "release_candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{project.id}.md"
    summary = human_summary(session, project_id=project.id)
    digest = exception_digest(session, project_id=project.id)
    cfg = load_config()
    phase = project_delivery_phase(session, project)
    closure_ok = closure_loops_finished(session, project.id, config=cfg)
    closure_rows = list(
        session.scalars(
            select(Task).where(
                Task.project_id == project.id,
                Task.kind.in_(tuple(CLOSURE_KINDS)),
            )
        ).all()
    )
    clear_list = [
        f"- [{t.status}] {t.kind}: {t.title}"
        for t in closure_rows
        if t.status == TaskStatus.DONE
    ]
    demo_tasks = list(
        session.scalars(
            select(Task).where(
                Task.project_id == project.id,
                Task.title.like("%デモ%"),
            )
        ).all()
    )
    demo_ok = bool(demo_tasks) and all(t.status == TaskStatus.DONE for t in demo_tasks)
    phase_mark = "[x]" if phase == "release_ready" else "[ ]"
    from ai_company.services.meeting_questions import (
        numbered_meeting_questions,
        resolved_meeting_questions,
    )

    open_q = numbered_meeting_questions(session, project.id)
    answered = len(resolved_meeting_questions(session, project.id))
    checklist = [
        f"- {phase_mark} 進行フェーズ: {phase}",
        f"- {'[x]' if closure_ok else '[ ]'} 自問ループ完了",
        f"- {'[ ]' if open_q else '[x]'} 社長への確認事項: 回答済み {answered} 件"
        + (f"・未回答 {len(open_q)} 件" if open_q else ""),
        f"- [x] 出荷ポリシー: {policy_snapshot(cfg)}",
        f"- {'[x]' if demo_ok else '[ ]'} 管理者デモシナリオ通過"
        + (f"（{len(demo_tasks)} tasks）" if demo_tasks else "（デモタスク未作成）"),
        "- [ ] 営業／保守資料パス（あれば）",
    ]
    # Attach recent validation evidence paths / execution ids
    from ai_company.db.models import Execution, ValidationResult

    evidence_lines: list[str] = []
    for vr in session.scalars(
        select(ValidationResult)
        .join(Task, Task.id == ValidationResult.task_id)
        .where(Task.project_id == project.id)
        .limit(30)
    ).all():
        n_findings = len(vr.findings or [])
        evidence_lines.append(
            f"- {vr.review_type}:{vr.verdict} exec={vr.execution_id} "
            f"findings={n_findings}"
        )
    execs = list(
        session.scalars(
            select(Execution)
            .join(Task, Task.id == Execution.task_id)
            .where(Task.project_id == project.id)
            .order_by(Execution.started_at.desc())
            .limit(10)
        ).all()
    )
    commit_lines = []
    for e in execs:
        rj = e.result_json or {}
        if rj.get("commit"):
            commit_lines.append(f"- {e.id}: {rj.get('commit')}")
        ev = rj.get("evidence") or {}
        if isinstance(ev, dict) and ev.get("baseline_ref"):
            commit_lines.append(f"- {e.id} baseline={ev.get('baseline_ref')}")
    body = (
        f"# Release Candidate\n\n"
        f"Project: {project.id} — {project.name}\n\n"
        f"Goal: {project.goal}\n\n"
        f"## Checklist\n\n" + "\n".join(checklist) + "\n\n"
        "## Closure CLEAR\n\n"
        + ("\n".join(clear_list) if clear_list else "(none)")
        + "\n\n"
        "## Validation Evidence\n\n"
        + ("\n".join(evidence_lines) if evidence_lines else "(none)")
        + "\n\n"
        "## Commits / baselines\n\n"
        + ("\n".join(commit_lines) if commit_lines else "(none)")
        + "\n\n"
        f"## Summary\n\n{summary}\n\n"
        f"## Exceptions\n\n{digest}\n"
    )
    path.write_text(body, encoding="utf-8")
    art = Artifact(
        id=new_id("ART"),
        project_id=project.id,
        task_id=None,
        kind="release_candidate",
        path=str(path),
        metadata_json={
            "goal": project.goal,
            "phase": phase,
            "closure_finished": closure_ok,
            "policy": policy_snapshot(cfg),
            "checklist": checklist,
        },
    )
    session.add(art)
    session.flush()
    record_audit(
        session,
        event_type="release_candidate_created",
        actor_type="system",
        actor_id="autonomous",
        target_type="artifact",
        target_id=art.id,
        metadata={"project_id": project.id, "path": str(path), "phase": phase},
    )
    return art


def _progress_snapshot(session: Session, project_id: str) -> str:
    counts: dict[str, int] = {str(s): 0 for s in (
        TaskStatus.DONE,
        TaskStatus.READY,
        TaskStatus.WAITING,
        TaskStatus.RUNNING,
        TaskStatus.REVIEW_REQUIRED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    )}
    rows = session.execute(
        select(Task.status, func.count())
        .where(Task.project_id == project_id)
        .group_by(Task.status)
    ).all()
    for status, n in rows:
        counts[str(status)] = int(n)
    return (
        f"完了={counts.get(str(TaskStatus.DONE), 0)} "
        f"実行待ち={counts.get(str(TaskStatus.READY), 0)} "
        f"依存待ち={counts.get(str(TaskStatus.WAITING), 0)} "
        f"実行中={counts.get(str(TaskStatus.RUNNING), 0)} "
        f"検証待ち={counts.get(str(TaskStatus.REVIEW_REQUIRED), 0)} "
        f"停止={counts.get(str(TaskStatus.BLOCKED), 0)} "
        f"失敗={counts.get(str(TaskStatus.FAILED), 0)}"
    )


def _pending_approvals(session: Session) -> int:
    from ai_company.db.models import Approval

    return int(
        session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.status == "PENDING")
        )
        or 0
    )


def _has_runnable_work(session: Session, project_id: str | None = None) -> bool:
    """READY / RUNNING / DEFERRED_BUDGET (reviveable) remain."""
    stmt = select(func.count()).select_from(Task).where(
        Task.status.in_(
            (
                TaskStatus.READY,
                TaskStatus.RUNNING,
                TaskStatus.DEFERRED_BUDGET,
            )
        )
    )
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    return int(session.scalar(stmt) or 0) > 0


def _complete_finished_projects(session: Session, result: AutonomousResult) -> list[str]:
    """Mark every ACTIVE project whose completion conditions hold as DONE.

    Used when ``scheduler continue`` runs without ``--project`` (all projects):
    previously only the ``--project`` path created the release candidate, so a
    project resumed from "予算待ち" by the guidance stayed ACTIVE forever.
    Returns the ids of the projects completed in this call.
    """
    done: list[str] = []
    for project in session.scalars(
        select(Project)
        .where(Project.status == ProjectStatus.ACTIVE)
        .order_by(Project.created_at.asc())
    ).all():
        if not _project_complete(session, project.id):
            continue
        project.status = ProjectStatus.DONE
        session.flush()
        art = create_release_candidate(session, project=project)
        _record_release(result, art.id)
        result.completed_project_ids.append(project.id)
        done.append(project.id)
    return done


def _no_project_stop_reason(session: Session) -> str:
    """After completing projects without ``--project`` and nothing is runnable:
    "all done" only if no ACTIVE project is left, else idle_incomplete (M-1)."""
    if not _active_project_count(session):
        return "all_implementation_done"
    return "awaiting_decision" if _awaiting_decision(session, None) else "idle_incomplete"


def _no_project_stop_progress(session: Session, result: AutonomousResult) -> str:
    remaining = _active_project_count(session)
    done = len(result.completed_project_ids)
    if remaining:
        return (
            f"継続実行停止  理由=完了 {done} 件、未完了 {remaining} 件"
            "（実行できるタスクがない案件あり）"
        )
    return f"継続実行完了  理由=完了条件を満たした案件を完了処理（{done} 件）"


def continue_until_gate(
    session: Session,
    *,
    project_id: str | None = None,
    max_cycles: int = 200,
    runner: RunnerKind = "mock",
    mock_succeed: bool = True,
    config: AppConfig | None = None,
) -> AutonomousResult:
    """
    Keep calling Scheduler until a human gate, idle, or max cycles.

    Humans are NOT asked to re-run after THROTTLE / partial cycles —
    only when BLOCKED / pending approval / truly idle.
    """
    cfg = config or load_config()
    result = AutonomousResult()
    auto_approve_open_requests(session, project_id=project_id, config=cfg)
    from ai_company.services.escalation import recover_orphaned_blocked_to_manager
    from ai_company.ui_ja import runner_ja

    recovered = recover_orphaned_blocked_to_manager(
        session, project_id=project_id, config=cfg
    )
    progress(
        f"継続実行を開始  実行方式={runner_ja(runner)}  最大サイクル={max_cycles}"
        + (f"  案件={project_id}" if project_id else "")
        + (f"  孤児回収={len(recovered)}" if recovered else "")
    )

    denied_streak = 0
    for cycle in range(1, max_cycles + 1):
        result.cycles = cycle

        if _pending_approvals(session) > 0 or (
            project_id and _has_blocking_exception(session, project_id)
        ) or (
            not project_id
            and int(
                session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.status == TaskStatus.BLOCKED)
                )
                or 0
            )
            > 0
        ):
            result.stopped_reason = "blocked_exception"
            progress("継続実行停止  理由=人間の承認／停止タスクあり")
            break

        if project_id and _project_complete(session, project_id):
            project = session.get(Project, project_id)
            if project is not None:
                project.status = ProjectStatus.DONE
                session.flush()
                art = create_release_candidate(session, project=project)
                _record_release(result, art.id)
            result.stopped_reason = "all_implementation_done"
            progress("継続実行完了  理由=実装タスクすべて完了")
            break

        # No --project: finish every ACTIVE project that is complete (N-1);
        # stop only when nothing runnable is left anywhere.
        if not project_id and _complete_finished_projects(session, result):
            # Stop here only when no ACTIVE project is left. Otherwise run the
            # scheduler: it spawns the next closure phase of the other projects
            # (M-1: stopping early left them ACTIVE with "実装完了").
            if not _has_runnable_work(session, None) and not _active_project_count(session):
                result.stopped_reason = _no_project_stop_reason(session)
                progress(_no_project_stop_progress(session, result))
                break

        snap = (
            _progress_snapshot(session, project_id)
            if project_id
            else "（全案件）"
        )
        progress(f"サイクル {cycle}/{max_cycles}  {snap}")
        sched = run_once(
            session,
            config=cfg,
            mock_succeed=mock_succeed,
            runner=runner,
            max_to_start=cfg.settings.scheduler.max_concurrent_workers,
        )
        result.last_scheduler = sched
        result.started_total += sched.started
        progress(
            f"サイクル {cycle} 結果  開始={sched.started} 拒否={sched.denied} "
            f"延期={sched.deferred} 抑制={sched.throttled}"
        )

        # After a cycle: stop only for human gates or no runnable work
        if _pending_approvals(session) > 0:
            result.stopped_reason = "blocked_exception"
            progress("継続実行停止  理由=人間の承認が必要")
            break

        if project_id and _has_blocking_exception(session, project_id):
            result.stopped_reason = "blocked_exception"
            progress("継続実行停止  理由=停止タスクあり（要判断）")
            break

        if not project_id:
            blocked_n = int(
                session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.status == TaskStatus.BLOCKED)
                )
                or 0
            )
            if blocked_n > 0:
                result.stopped_reason = "blocked_exception"
                progress("継続実行停止  理由=停止タスクあり（要判断）")
                break

        wait = _time_window_stall(session, sched, cfg)
        if wait is not None:
            result.stopped_reason = "budget_wait"
            result.budget_wait = wait
            progress("継続実行" + _budget_wait_progress(wait))
            break

        if sched.started == 0 and not _has_runnable_work(session, project_id):
            if project_id and _project_complete(session, project_id):
                project = session.get(Project, project_id)
                if project is not None:
                    project.status = ProjectStatus.DONE
                    session.flush()
                    art = create_release_candidate(session, project=project)
                    _record_release(result, art.id)
                result.stopped_reason = "all_implementation_done"
                progress("継続実行完了  理由=実装タスクすべて完了")
            elif not project_id and _complete_finished_projects(session, result):
                result.stopped_reason = _no_project_stop_reason(session)
                progress(_no_project_stop_progress(session, result))
            elif _awaiting_decision(session, project_id):
                result.stopped_reason = "awaiting_decision"
                progress("継続実行停止  理由=社長の回答待ち（会議の確認事項が未回答）")
            else:
                result.stopped_reason = "idle_incomplete"
                progress("継続実行停止  理由=実行できるタスクがない")
            break

        denials = _policy_denials(session, sched)
        denied_streak = denied_streak + 1 if denials else 0
        if denials and denied_streak >= POLICY_DEADLOCK_CYCLES:
            result.stopped_reason = "policy_denied"
            result.policy_denials = denials
            progress("継続実行" + _policy_deadlock_progress(denials))
            break

        # started==0 but READY remains (e.g. all THROTTLE) → keep looping
        if sched.started == 0 and sched.throttled > 0:
            progress("同時起動上限のため次サイクルへ（人の操作は不要）")
            continue

        if cycle == max_cycles:
            result.stopped_reason = "max_cycles"
            progress("継続実行停止  理由=最大サイクル到達")
    else:
        result.stopped_reason = result.stopped_reason or "max_cycles"

    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            result.project_status = project.status
        result.summaries.append(human_summary(session, project_id=project_id))

    record_audit(
        session,
        event_type="scheduler_continue_finished",
        actor_type="system",
        actor_id="scheduler",
        target_type="project" if project_id else "system",
        target_id=project_id or "all",
        metadata={
            "cycles": result.cycles,
            "started_total": result.started_total,
            "stopped_reason": result.stopped_reason,
            "budget_wait": (
                {
                    "reason": result.budget_wait.reason,
                    "resume_at": (
                        result.budget_wait.resume_at.isoformat()
                        if result.budget_wait.resume_at
                        else None
                    ),
                }
                if result.budget_wait
                else None
            ),
        },
    )
    return result


def run_autonomous(
    session: Session,
    *,
    project_id: str,
    max_cycles: int = 40,
    runner: RunnerKind = "mock",
    mock_succeed: bool = True,
    config: AppConfig | None = None,
) -> AutonomousResult:
    """
    Drive Scheduler until implementation tasks are DONE, blocked, or idle.

    Humans are not required to open each Task; they get summaries + exceptions.
    """
    cfg = config or load_config()
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    result = AutonomousResult(project_status=project.status)
    auto_approve_open_requests(session, project_id=project_id, config=cfg)
    from ai_company.services.escalation import recover_orphaned_blocked_to_manager
    from ai_company.ui_ja import runner_ja

    recovered = recover_orphaned_blocked_to_manager(
        session, project_id=project_id, config=cfg
    )

    progress(
        f"自動実行を開始  案件={project_id}  実行方式={runner_ja(runner)}  "
        f"最大サイクル={max_cycles}  {_progress_snapshot(session, project_id)}"
        + (f"  孤児回収={len(recovered)}" if recovered else "")
    )

    denied_streak = 0
    for cycle in range(1, max_cycles + 1):
        result.cycles = cycle
        # A rejected/reconsidered old approval must not leave the company idle.
        # Give such work back to its nearest AI supervisor before considering a stop.
        recover_orphaned_blocked_to_manager(
            session, project_id=project_id, config=cfg
        )
        if _project_complete(session, project_id):
            project.status = ProjectStatus.DONE
            session.flush()
            art = create_release_candidate(session, project=project)
            _record_release(result, art.id)
            result.project_status = project.status
            result.stopped_reason = "all_implementation_done"
            result.summaries.append(human_summary(session, project_id=project_id))
            progress(f"自動実行完了  理由=実装タスクすべて完了  候補={art.id}")
            break

        if _has_blocking_exception(session, project_id):
            result.project_status = project.status
            result.stopped_reason = "blocked_exception"
            result.summaries.append(exception_digest(session, project_id=project_id))
            progress("自動実行停止  理由=停止タスクあり（要判断）")
            progress(
                "次は `ai-company status` か、表示された承認 resolve 手順を確認してください"
            )
            break

        progress(
            f"サイクル {cycle}/{max_cycles}  {_progress_snapshot(session, project_id)}"
        )
        sched = run_once(
            session,
            config=cfg,
            mock_succeed=mock_succeed,
            runner=runner,
            max_to_start=cfg.settings.scheduler.max_concurrent_workers,
        )
        result.last_scheduler = sched
        result.started_total += sched.started
        progress(
            f"サイクル {cycle} 結果  開始={sched.started} 拒否={sched.denied} "
            f"延期={sched.deferred} 抑制={sched.throttled}  "
            f"{_progress_snapshot(session, project_id)}"
        )

        wait = _time_window_stall(session, sched, cfg)
        if wait is not None:
            result.stopped_reason = "budget_wait"
            result.budget_wait = wait
            result.project_status = project.status
            result.summaries.append(human_summary(session, project_id=project_id))
            progress("自動実行" + _budget_wait_progress(wait))
            break

        denials = _policy_denials(session, sched)
        denied_streak = denied_streak + 1 if denials else 0
        if denials and denied_streak >= POLICY_DEADLOCK_CYCLES:
            result.stopped_reason = "policy_denied"
            result.policy_denials = denials
            result.project_status = project.status
            result.summaries.append(exception_digest(session, project_id=project_id))
            progress("自動実行" + _policy_deadlock_progress(denials))
            break

        if sched.started == 0 and _idle(session, project_id):
            if _project_complete(session, project_id):
                project.status = ProjectStatus.DONE
                session.flush()
                art = create_release_candidate(session, project=project)
                _record_release(result, art.id)
                result.stopped_reason = "all_implementation_done"
                progress(f"自動実行完了  理由=実装タスクすべて完了  候補={art.id}")
            elif _awaiting_decision(session, project_id):
                result.stopped_reason = "awaiting_decision"
                progress("自動実行停止  理由=社長の回答待ち（会議の確認事項が未回答）")
            else:
                result.stopped_reason = "idle_incomplete"
                progress("自動実行停止  理由=実行できるタスクがない（未完了）")
            result.project_status = project.status
            result.summaries.append(human_summary(session, project_id=project_id))
            break

        if cycle == max_cycles:
            result.stopped_reason = "max_cycles"
            result.project_status = project.status
            result.summaries.append(exception_digest(session, project_id=project_id))
            progress("自動実行停止  理由=最大サイクル到達")
    else:
        result.stopped_reason = result.stopped_reason or "max_cycles"

    record_audit(
        session,
        event_type="autonomous_run_finished",
        actor_type="system",
        actor_id="autonomous",
        target_type="project",
        target_id=project_id,
        metadata={
            "cycles": result.cycles,
            "started_total": result.started_total,
            "stopped_reason": result.stopped_reason,
            "release_candidate_id": result.release_candidate_id,
            "release_candidate_ids": result.release_candidate_ids,
            "completed_project_ids": result.completed_project_ids,
            "budget_wait_reason": result.budget_wait.reason if result.budget_wait else None,
        },
    )
    return result
