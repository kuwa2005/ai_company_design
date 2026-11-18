"""T3-S4 acceptance findings N-1 / N-2 / N-3 (resume guidance and completion)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_company.db.models import AuditEvent, Project, Task
from ai_company.enums import ProjectStatus, TaskStatus
from ai_company.services.autonomous import continue_until_gate, run_autonomous
from ai_company.services.decisions import apply_latest_decision
from ai_company.services.secretary import (
    human_next_actions,
    infer_runner,
    resolve_meeting_question,
    scheduler_continue_command,
    start_goal,
)
from tests.conftest import write_budget_override

INTERNAL = "社内向けの勤怠Webアプリを作る"
SAAS = "顧客向けの勤怠管理Webサービスを作って販売したい"


def _caps(tmp_env: Path, hourly_runs: int) -> None:
    write_budget_override(
        tmp_env,
        budget={
            "hourly": {"max_runs": hourly_runs, "max_cost_usd": 0},
            "daily": {"max_runs": 10000, "max_cost_usd": 0},
            "monthly": {"soft_limit_usd": 200.0, "hard_limit_usd": 250.0},
        },
    )


# --- N-1 ---------------------------------------------------------------------


def test_continue_command_names_the_project() -> None:
    assert scheduler_continue_command("mock", "PROJECT-1") == (
        "ai-company scheduler continue --runner mock --project PROJECT-1"
    )
    assert scheduler_continue_command("cursor", None) == (
        "ai-company scheduler continue --runner cursor"
    )


def test_budget_wait_guidance_includes_project(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, 3)
    result = start_goal(db_session, INTERNAL)
    auto = run_autonomous(db_session, project_id=result.project.id, max_cycles=300, runner="mock")
    assert auto.stopped_reason == "budget_wait"
    guide = human_next_actions(
        db_session, project_id=result.project.id, stopped_reason="budget_wait", runner="mock"
    )
    assert f"scheduler continue --runner mock --project {result.project.id}" in guide


def test_continue_without_project_completes_finished_projects(
    db_session: Session, tmp_env: Path
) -> None:
    """Resume from 予算待ち exactly as the old guidance said (no --project)."""
    _caps(tmp_env, 3)
    result = start_goal(db_session, INTERNAL)
    pid = result.project.id
    auto = run_autonomous(db_session, project_id=pid, max_cycles=300, runner="mock")
    assert auto.stopped_reason == "budget_wait"

    _caps(tmp_env, 10000)  # the window has "passed"
    cont = continue_until_gate(db_session, project_id=None, max_cycles=300, runner="mock")
    assert cont.stopped_reason == "all_implementation_done", cont.stopped_reason
    assert pid in cont.completed_project_ids
    assert cont.release_candidate_id is not None
    project = db_session.get(Project, pid)
    assert project is not None and project.status == ProjectStatus.DONE

    # idempotent: a second run does not complete it again
    again = continue_until_gate(db_session, project_id=None, max_cycles=5, runner="mock")
    assert again.completed_project_ids == []


# --- N-2 ---------------------------------------------------------------------


def test_decide_after_done_reopens_project_and_rereleases(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env, 10000)
    result = start_goal(db_session, SAAS)
    pid = result.project.id
    first = run_autonomous(db_session, project_id=pid, max_cycles=300, runner="mock")
    assert first.release_candidate_id is not None
    project = db_session.get(Project, pid)
    assert project is not None and project.status == ProjectStatus.DONE

    question = resolve_meeting_question(
        db_session, project_id=pid, number=1, answer="月額300円/ユーザーで開始"
    )
    applied = apply_latest_decision(db_session, project_id=pid, question=question)
    assert applied is not None and applied.revision_task_ids
    assert applied.reopened is True
    db_session.refresh(project)
    assert project.status == ProjectStatus.ACTIVE
    reopened = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "project_reopened")
    ).all()
    assert len(reopened) == 1 and reopened[0].target_id == pid

    second = continue_until_gate(db_session, project_id=pid, max_cycles=300, runner="mock")
    assert second.stopped_reason == "all_implementation_done"
    # H-1: the revisions were actually executed before the project completed again
    assert second.started_total >= len(applied.revision_task_ids)
    db_session.expire_all()
    for tid in applied.revision_task_ids:
        rev = db_session.get(Task, tid)
        assert rev is not None and rev.status == TaskStatus.DONE, tid
    assert second.release_candidate_id is not None
    assert second.release_candidate_id != first.release_candidate_id
    db_session.refresh(project)
    assert project.status == ProjectStatus.DONE


def test_decide_on_active_project_does_not_reopen(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS)
    pid = result.project.id
    question = resolve_meeting_question(db_session, project_id=pid, number=1, answer="承認")
    applied = apply_latest_decision(db_session, project_id=pid, question=question)
    assert applied is not None and applied.reopened is False
    assert not db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "project_reopened")
    ).all()


def test_cli_decide_prints_reopen_and_resume_command(db_session: Session, tmp_env: Path) -> None:
    from ai_company.cli import app

    _caps(tmp_env, 10000)
    result = start_goal(db_session, SAAS)
    pid = result.project.id
    run_autonomous(db_session, project_id=pid, max_cycles=300, runner="mock")
    db_session.commit()
    out = CliRunner().invoke(app, ["decide", "1", "月額300円", "--project", pid])
    assert out.exit_code == 0, out.output
    text = " ".join(out.output.split())  # rich wraps long lines at 80 cols
    assert "案件を再開しました" in text
    assert f"scheduler continue --runner mock --project {pid}" in text


# --- N-3 ---------------------------------------------------------------------


def test_infer_runner_from_last_execution(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, 3)
    result = start_goal(db_session, INTERNAL)
    pid = result.project.id
    assert infer_runner(db_session, pid) == "cursor"  # no executions yet → default
    run_autonomous(db_session, project_id=pid, max_cycles=300, runner="mock")
    assert infer_runner(db_session, pid) == "mock"
    assert infer_runner(db_session, None) == "mock"


def test_cli_status_guidance_uses_inferred_runner_and_project(
    db_session: Session, tmp_env: Path
) -> None:
    from ai_company.cli import app

    _caps(tmp_env, 3)
    result = start_goal(db_session, INTERNAL)
    pid = result.project.id
    run_autonomous(db_session, project_id=pid, max_cycles=300, runner="mock")
    db_session.commit()
    out = CliRunner().invoke(app, ["status"])
    assert out.exit_code == 0, out.output
    text = " ".join(out.output.split())  # rich wraps long lines at 80 cols
    assert f"scheduler continue --runner mock --project {pid}" in text
    assert "--runner cursor" not in text
