"""T3-S6 review H-1 / M-1 / L-1: completion waits for document work, honest stop reason."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Project, Task
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.services.autonomous import (
    _project_complete,
    continue_until_gate,
    run_autonomous,
)
from ai_company.services.secretary import start_goal
from ai_company.services.tasks import create_project
from tests.test_resume_guidance import INTERNAL, SAAS, _caps


def _finish(session: Session, goal: str) -> str:
    pid = start_goal(session, goal).project.id
    run_autonomous(session, project_id=pid, max_cycles=300, runner="mock")
    session.expire_all()
    return pid


def test_project_not_complete_while_department_work_open(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env, 10000)
    pid = _finish(db_session, SAAS)
    assert _project_complete(db_session, pid)
    order = db_session.scalar(
        select(Task).where(Task.project_id == pid, Task.kind == TaskKind.DEPARTMENT_WORK)
    )
    assert order is not None
    for status in (TaskStatus.READY, TaskStatus.WAITING, TaskStatus.RUNNING):
        order.status = status
        db_session.flush()
        assert not _project_complete(db_session, pid), status
    order.status = TaskStatus.CANCELLED
    db_session.flush()
    assert _project_complete(db_session, pid)


def test_open_head_brief_blocks_completion(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, 10000)
    pid = _finish(db_session, INTERNAL)
    db_session.add(
        Task(
            id="TASK-HB-OPEN",
            project_id=pid,
            title="部長所見: 法務部",
            kind=TaskKind.HEAD_BRIEF.value,
            role_required="legal_director",
            status=TaskStatus.READY,
            constraints="{}",
        )
    )
    db_session.flush()
    assert not _project_complete(db_session, pid)


def test_no_project_continue_reports_incomplete_when_other_project_remains(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env, 10000)
    done_pid = start_goal(db_session, INTERNAL).project.id
    # An ACTIVE project with nothing runnable and no closure → can never complete here.
    stuck = create_project(db_session, name="stuck", goal="stuck")
    db_session.flush()

    result = continue_until_gate(db_session, max_cycles=300, runner="mock")
    db_session.expire_all()
    assert result.completed_project_ids == [done_pid]
    assert db_session.get(Project, done_pid).status == ProjectStatus.DONE
    assert db_session.get(Project, stuck.id).status == ProjectStatus.ACTIVE
    assert result.stopped_reason == "idle_incomplete"  # M-1: not "all done"


def test_no_project_continue_all_done_when_nothing_left(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env, 10000)
    pid = start_goal(db_session, INTERNAL).project.id
    result = continue_until_gate(db_session, max_cycles=300, runner="mock")
    assert result.completed_project_ids == [pid]
    assert result.stopped_reason == "all_implementation_done"


def test_all_release_candidates_are_reported(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, 10000)
    a = start_goal(db_session, INTERNAL).project.id
    b = start_goal(db_session, "社内の備品管理ツールを作る").project.id
    result = continue_until_gate(db_session, max_cycles=400, runner="mock")
    assert sorted(result.completed_project_ids) == sorted([a, b])
    assert len(result.release_candidate_ids) == 2
    assert result.release_candidate_id == result.release_candidate_ids[-1]
    assert result.stopped_reason == "all_implementation_done"
    db_session.expire_all()
    assert {db_session.get(Project, p).status for p in (a, b)} == {ProjectStatus.DONE}


def test_cli_continue_lists_all_release_candidates(db_session: Session, tmp_env: Path) -> None:
    from typer.testing import CliRunner

    from ai_company.cli import app

    _caps(tmp_env, 10000)
    start_goal(db_session, INTERNAL)
    start_goal(db_session, "社内の備品管理ツールを作る")
    db_session.commit()
    out = CliRunner().invoke(app, ["scheduler", "continue", "--runner", "mock", "--quiet"])
    assert out.exit_code == 0, out.output
    assert "リリース候補: 2 件" in out.output
