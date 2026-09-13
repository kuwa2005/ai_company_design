"""ai-company CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ai_company.config import clear_config_cache, load_config
from ai_company.db import init_db, reset_engine, session_scope
from ai_company.paths import audit_log_path, data_dir, db_path, executions_dir
from ai_company.services.audit import list_audit_events
from ai_company.services.cleanup import run_cleanup
from ai_company.services.cost_governor import get_budget_snapshot
from ai_company.services.escalation import list_approvals, resolve_approval
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import (
    answer_question,
    human_summary,
    list_staffing,
    start_goal,
)
from ai_company.services.tasks import create_project, create_task, list_tasks
from ai_company.services.validation import validation_status
from ai_company.services.workers import list_workers

app = typer.Typer(
    name="ai-company",
    help="Safe Cursor Workforce Orchestrator",
    no_args_is_help=True,
)
project_app = typer.Typer(help="Project commands")
task_app = typer.Typer(help="Task commands")
scheduler_app = typer.Typer(help="Scheduler commands")
worker_app = typer.Typer(help="Worker commands")
budget_app = typer.Typer(help="Budget commands")
audit_app = typer.Typer(help="Audit commands")
staffing_app = typer.Typer(help="Staffing request commands")
approval_app = typer.Typer(help="Human approval / escalation commands")

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(worker_app, name="worker")
app.add_typer(budget_app, name="budget")
app.add_typer(audit_app, name="audit")
app.add_typer(staffing_app, name="staffing")
app.add_typer(approval_app, name="approval")

console = Console()


def ensure_runtime_ready() -> None:
    """Create local data/DB if missing so `start` works without a prior `init`."""
    clear_config_cache()
    data_dir().mkdir(parents=True, exist_ok=True)
    executions_dir().mkdir(parents=True, exist_ok=True)
    reset_engine()
    init_db()


@app.command("init")
def init_cmd() -> None:
    """Initialize local data directory, SQLite schema, and verify config."""
    ensure_runtime_ready()
    cfg = load_config()
    console.print(f"[green]Initialized[/green] project: {Path.cwd().resolve()}")
    console.print(f"data dir: {data_dir()}")
    console.print(f"DB: {db_path()}")
    console.print(f"Audit log: {audit_log_path()}")
    console.print(
        f"Scheduler max_concurrent_workers={cfg.settings.scheduler.max_concurrent_workers}"
    )


@app.command("start")
def start_cmd(
    goal: str = typer.Argument(..., help="Human goal for Secretary to plan"),
    dispatch: bool = typer.Option(
        False,
        "--dispatch",
        help="After planning, run scheduler once (CLI layer — not Secretary spawn)",
    ),
    autonomous: bool = typer.Option(
        False,
        "--autonomous",
        help="Run Scheduler until DONE / blocked / idle (M10 no-look loop)",
    ),
    max_cycles: int = typer.Option(40, "--max-cycles", help="Autonomous cycle cap"),
    runner: str = typer.Option("mock", "--runner", help="mock | cursor when dispatching"),
    quiet: bool = typer.Option(
        False, "--quiet", help="Disable live progress lines on stderr"
    ),
) -> None:
    """Secretary plans Tasks + Staffing Requests. Never spawns workers itself."""
    if runner not in {"mock", "cursor"}:
        raise typer.BadParameter("runner must be 'mock' or 'cursor'")
    ensure_runtime_ready()
    from ai_company.services.progress import set_progress
    from ai_company.ui_ja import role_ja, runner_ja, status_ja

    if autonomous or dispatch:
        set_progress(not quiet)
    with session_scope() as session:
        result = start_goal(session, goal)
        console.print(f"[green]案件[/green] {result.project.id}  {result.project.name}")
        console.print(f"作成したタスク: {len(result.tasks)}")
        console.print(f"人員依頼: {len(result.staffing)}")
        console.print(f"検証提案: {len(result.validation_requests)}")
        for t in result.tasks:
            console.print(
                f"  - [{status_ja(t.status)}] {t.id}  {t.title}  "
                f"役割={role_ja(t.role_required)}  リスク={t.risk_level}"
            )
        for s in result.staffing:
            console.print(
                f"  - 人員 {s.id}  役割={role_ja(s.role)}  x{s.requested_count}  "
                f"（{s.status}）"
            )
        for vr in result.validation_requests:
            console.print(
                f"  - 検証提案 リスク={vr.risk_level} 必須={vr.required_review_types}"
            )
        console.print("")
        console.print(result.summary)
        if autonomous:
            from ai_company.services.autonomous import run_autonomous

            console.print("")
            console.print(
                "[cyan]自動実行中…[/cyan] 進捗は随時表示されます "
                "（Ctrl+C で停止）"
            )
            auto = run_autonomous(
                session,
                project_id=result.project.id,
                max_cycles=max_cycles,
                runner=runner,  # type: ignore[arg-type]
            )
            console.print("")
            console.print(
                f"[cyan]自動実行[/cyan] サイクル={auto.cycles} "
                f"開始数={auto.started_total} 停止理由={auto.stopped_reason} "
                f"リリース候補={auto.release_candidate_id}"
            )
            console.print(human_summary(session, project_id=result.project.id))
        elif dispatch:
            # Dispatch is owned by CLI → Scheduler, not Secretary.
            sched = run_once(session, runner=runner)  # type: ignore[arg-type]
            console.print("")
            console.print(
                f"[cyan]スケジューラ実行[/cyan] 方式={runner_ja(runner)} "
                f"開始={sched.started} 拒否={sched.denied} 抑制={sched.throttled}"
            )
            console.print(human_summary(session, project_id=result.project.id))


@app.command("status")
def status_cmd(
    project: Optional[str] = typer.Option(None, "--project", help="Project id"),
) -> None:
    with session_scope() as session:
        console.print(human_summary(session, project_id=project))


@app.command("ask")
def ask_cmd(
    question: str = typer.Argument(..., help="Question for Secretary about current state"),
    project: Optional[str] = typer.Option(None, "--project"),
) -> None:
    with session_scope() as session:
        console.print(answer_question(session, question, project_id=project))


validation_app = typer.Typer(help="Validation / Judge status")
app.add_typer(validation_app, name="validation")


@validation_app.command("status")
def validation_status_cmd(
    task_id: str = typer.Argument(..., help="Implementation or related task id"),
) -> None:
    with session_scope() as session:
        info = validation_status(session, task_id)
        console.print(info)


@project_app.command("create")
def project_create(
    name: str = typer.Argument(..., help="Project name"),
    goal: str = typer.Option("", "--goal", help="Project goal"),
) -> None:
    with session_scope() as session:
        project = create_project(session, name=name, goal=goal)
        console.print(f"[green]Created[/green] {project.id}  {project.name}")


@task_app.command("create")
def task_create(
    title: str = typer.Option(..., "--title", help="Task title"),
    priority: int = typer.Option(50, "--priority", help="Priority 1-100"),
    project: Optional[str] = typer.Option(None, "--project", help="Project id"),
    depends_on: Optional[list[str]] = typer.Option(
        None, "--depends-on", help="Dependency task ids"
    ),
    estimated_cost: Optional[float] = typer.Option(
        None, "--estimated-cost", help="Estimated USD cost"
    ),
) -> None:
    with session_scope() as session:
        task = create_task(
            session,
            title=title,
            priority=priority,
            project_id=project,
            depends_on=depends_on,
            estimated_cost=estimated_cost,
        )
        console.print(
            f"[green]Created[/green] {task.id}  status={task.status}  priority={task.priority}"
        )


@task_app.command("list")
def task_list(
    status: Optional[str] = typer.Option(None, "--status"),
) -> None:
    with session_scope() as session:
        tasks = list_tasks(session, status=status)
        table = Table(title="Tasks")
        table.add_column("ID")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Priority")
        table.add_column("Retries")
        for t in tasks:
            table.add_row(
                t.id,
                t.title,
                t.status,
                str(t.priority),
                f"{t.retry_count}/{t.max_retries}",
            )
        console.print(table)


@scheduler_app.command("run-once")
def scheduler_run_once(
    fail: bool = typer.Option(False, "--fail", help="Force mock worker failure"),
    runner: str = typer.Option(
        "mock",
        "--runner",
        help="Worker runner: mock | cursor (always via Scheduler/Cost Guard)",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Disable live progress lines"),
) -> None:
    if runner not in {"mock", "cursor"}:
        raise typer.BadParameter("runner must be 'mock' or 'cursor'")
    from ai_company.services.progress import set_progress

    set_progress(not quiet)
    with session_scope() as session:
        result = run_once(session, mock_succeed=not fail, runner=runner)  # type: ignore[arg-type]
        console.print(
            f"runner={runner} attempted={result.attempted} started={result.started} "
            f"denied={result.denied} deferred={result.deferred} throttled={result.throttled} "
            f"leases_reclaimed={result.leases_reclaimed} zombies_killed={result.zombies_killed}"
        )
        for d in result.details or []:
            console.print(d)


@scheduler_app.command("cleanup")
def scheduler_cleanup() -> None:
    """Reclaim expired leases and kill zombie workers."""
    with session_scope() as session:
        result = run_cleanup(session)
        console.print(
            f"leases_reclaimed={result.leases_reclaimed} zombies_killed={result.zombies_killed}"
        )


@worker_app.command("list")
def worker_list() -> None:
    with session_scope() as session:
        workers = list_workers(session)
        table = Table(title="Workers")
        table.add_column("ID")
        table.add_column("Role")
        table.add_column("Task")
        table.add_column("Status")
        table.add_column("Cost est")
        table.add_column("Cost actual")
        for w in workers:
            table.add_row(
                w.id,
                w.role,
                w.task_id,
                w.status,
                f"{w.cost_estimated:.2f}",
                "" if w.cost_actual is None else f"{w.cost_actual:.2f}",
            )
        console.print(table)


@budget_app.command("status")
def budget_status() -> None:
    with session_scope() as session:
        snap = get_budget_snapshot(session)
        console.print(f"使用済み:   ${snap.spent:.2f}")
        console.print(f"予約中:     ${snap.reserved:.2f}")
        console.print(f"利用可能:   ${snap.usable:.2f}")
        console.print(f"ソフト上限: ${snap.soft_limit:.2f}  （使用率={snap.soft_usage_ratio:.0%}）")
        console.print(f"ハード上限: ${snap.hard_limit:.2f}")
        console.print(f"直近1時間:  {snap.hourly_runs} 回 / ${snap.hourly_spent:.2f}")
        console.print(f"本日:       {snap.daily_runs} 回 / ${snap.daily_spent:.2f}")


@audit_app.command("list")
def audit_list(limit: int = typer.Option(20, "--limit")) -> None:
    with session_scope() as session:
        events = list_audit_events(session, limit=limit)
        table = Table(title="Audit")
        table.add_column("Time")
        table.add_column("Event")
        table.add_column("Actor")
        table.add_column("Target")
        for e in events:
            table.add_row(
                str(e.created_at),
                e.event_type,
                f"{e.actor_type}:{e.actor_id}",
                f"{e.target_type}:{e.target_id}",
            )
        console.print(table)


@staffing_app.command("list")
def staffing_list(
    project: Optional[str] = typer.Option(None, "--project"),
) -> None:
    with session_scope() as session:
        rows = list_staffing(session, project_id=project)
        table = Table(title="Staffing Requests")
        table.add_column("ID")
        table.add_column("Role")
        table.add_column("Requested")
        table.add_column("Approved")
        table.add_column("Status")
        table.add_column("Reason")
        for r in rows:
            table.add_row(
                r.id,
                r.role,
                str(r.requested_count),
                str(r.approved_count),
                r.status,
                r.reason[:60],
            )
        console.print(table)


@staffing_app.command("approve")
def staffing_approve(
    request_id: str = typer.Argument(..., help="Staffing request id"),
    count: Optional[int] = typer.Option(None, "--count", help="Approved headcount"),
) -> None:
    from ai_company.services.staffing import approve_staffing

    with session_scope() as session:
        req = approve_staffing(session, request_id=request_id, approved_count=count)
        console.print(
            f"[green]Approved[/green] {req.id} role={req.role} x{req.approved_count}"
        )


@staffing_app.command("reject")
def staffing_reject(
    request_id: str = typer.Argument(..., help="Staffing request id"),
    reason: str = typer.Option("", "--reason"),
) -> None:
    from ai_company.services.staffing import reject_staffing

    with session_scope() as session:
        req = reject_staffing(session, request_id=request_id, reason=reason)
        console.print(f"[yellow]Rejected[/yellow] {req.id}")


@approval_app.command("list")
def approval_list(
    status: Optional[str] = typer.Option(None, "--status"),
) -> None:
    with session_scope() as session:
        rows = list_approvals(session, status=status)
        table = Table(title="Approvals")
        table.add_column("ID")
        table.add_column("Action")
        table.add_column("Status")
        table.add_column("Reason")
        for r in rows:
            table.add_row(r.id, r.action, r.status, r.reason[:80])
        console.print(table)


@approval_app.command("resolve")
def approval_resolve(
    approval_id: str = typer.Argument(..., help="Approval id"),
    approve: bool = typer.Option(False, "--approve", help="Approve and requeue task"),
    reject: bool = typer.Option(False, "--reject", help="Reject approval"),
    note: str = typer.Option("", "--note"),
) -> None:
    if approve == reject:
        raise typer.BadParameter("Specify exactly one of --approve or --reject")
    with session_scope() as session:
        row = resolve_approval(
            session,
            approval_id=approval_id,
            approve=approve,
            note=note,
        )
        console.print(f"[green]Resolved[/green] {row.id} -> {row.status}")


if __name__ == "__main__":
    app()
