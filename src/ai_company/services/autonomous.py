"""Product-level autonomous / no-look run loop (M10)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Artifact, Project, Task
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir
from ai_company.services.audit import record_audit
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
    return closure_loops_finished(session, project_id)


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
    checklist = [
        f"- {phase_mark} 進行フェーズ: {phase}",
        f"- {'[x]' if closure_ok else '[ ]'} 自問ループ完了",
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
                result.release_candidate_id = art.id
            result.stopped_reason = "all_implementation_done"
            progress("継続実行完了  理由=実装タスクすべて完了")
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

        if sched.started == 0 and not _has_runnable_work(session, project_id):
            if project_id and _project_complete(session, project_id):
                project = session.get(Project, project_id)
                if project is not None:
                    project.status = ProjectStatus.DONE
                    session.flush()
                    art = create_release_candidate(session, project=project)
                    result.release_candidate_id = art.id
                result.stopped_reason = "all_implementation_done"
                progress("継続実行完了  理由=実装タスクすべて完了")
            else:
                result.stopped_reason = "idle_incomplete"
                progress("継続実行停止  理由=実行できるタスクがない")
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
            result.release_candidate_id = art.id
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

        if sched.started == 0 and _idle(session, project_id):
            if _project_complete(session, project_id):
                project.status = ProjectStatus.DONE
                session.flush()
                art = create_release_candidate(session, project=project)
                result.release_candidate_id = art.id
                result.stopped_reason = "all_implementation_done"
                progress(f"自動実行完了  理由=実装タスクすべて完了  候補={art.id}")
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
        },
    )
    return result
