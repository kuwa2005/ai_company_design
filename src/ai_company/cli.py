"""ai-company CLI."""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from ai_company.config import clear_config_cache, load_config
from ai_company.db import init_db, reset_engine, session_scope
from ai_company.paths import audit_log_path, data_dir, db_path, executions_dir
from ai_company.services.audit import list_audit_events
from ai_company.services.cost_governor import get_budget_snapshot
from ai_company.services.scheduler import run_once
from ai_company.services.tasks import create_project, create_task, list_tasks
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

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(worker_app, name="worker")
app.add_typer(budget_app, name="budget")
app.add_typer(audit_app, name="audit")

console = Console()


@app.command("init")
def init_cmd() -> None:
    """Initialize local data directory, SQLite schema, and verify config."""
    clear_config_cache()
    cfg = load_config()
    data_dir().mkdir(parents=True, exist_ok=True)
    executions_dir().mkdir(parents=True, exist_ok=True)
    reset_engine()
    init_db()
    console.print(f"[green]Initialized[/green] data dir: {data_dir()}")
    console.print(f"DB: {db_path()}")
    console.print(f"Audit log: {audit_log_path()}")
    console.print(
        f"Scheduler max_concurrent_workers={cfg.settings.scheduler.max_concurrent_workers}"
    )


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
) -> None:
    with session_scope() as session:
        result = run_once(session, mock_succeed=not fail)
        console.print(
            f"attempted={result.attempted} started={result.started} "
            f"denied={result.denied} deferred={result.deferred} throttled={result.throttled}"
        )
        for d in result.details or []:
            console.print(d)


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
        console.print(f"Spent:    ${snap.spent:.2f}")
        console.print(f"Reserved: ${snap.reserved:.2f}")
        console.print(f"Usable:   ${snap.usable:.2f}")
        console.print(f"Soft:     ${snap.soft_limit:.2f}  (usage={snap.soft_usage_ratio:.0%})")
        console.print(f"Hard:     ${snap.hard_limit:.2f}")
        console.print(f"Hourly:   {snap.hourly_runs} runs / ${snap.hourly_spent:.2f}")
        console.print(f"Daily:    {snap.daily_runs} runs / ${snap.daily_spent:.2f}")


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


if __name__ == "__main__":
    app()
