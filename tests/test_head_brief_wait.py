"""head_brief stuck READY/WAITING must not hold the closure loops forever.

A brief the scheduler never starts (policy DENY, cost DENY, role capacity…)
stays READY. ``open_head_briefs`` made the closure loops wait on it with no
way out; now it falls back to the template work orders after
``head_llm.max_wait_cycles`` closure-gate checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db.models import AuditEvent
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.services.autonomous import run_autonomous
from ai_company.services.closure_loops import maybe_spawn_closure_loop
from ai_company.services.secretary import start_goal
from tests.test_head_briefs import (
    HEADS,
    SAAS_GOAL,
    _enable_heads,
    _grant_head_permissions,
    _of_kind,
)


def _start_with_impl_done(db_session: Session, tmp_env: Path, **head: object):
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env, **head)
    result = start_goal(db_session, SAAS_GOAL)
    for t in result.tasks:  # implementation finished; briefs never started
        t.status = TaskStatus.DONE
    db_session.flush()
    assert result.head_brief_tasks
    assert all(b.status == TaskStatus.READY for b in result.head_brief_tasks)
    return result


def _fallbacks(db_session: Session) -> list[AuditEvent]:
    return list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "head_brief_fallback")
        ).all()
    )


def test_ready_brief_falls_back_after_max_wait_cycles(
    db_session: Session, tmp_env: Path
) -> None:
    result = _start_with_impl_done(db_session, tmp_env, max_wait_cycles=3)
    pid = result.project.id
    briefs = result.head_brief_tasks
    assert not _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value)  # held

    for cycle in (1, 2, 3):  # N = 3 dispatch chances (B-1: was 2)
        assert maybe_spawn_closure_loop(db_session, project_id=pid) is None
        for b in briefs:
            db_session.refresh(b)
            assert b.status == TaskStatus.READY
            assert json.loads(b.constraints)["wait_cycles"] == cycle
    assert _fallbacks(db_session) == []

    # 4th check: give up on the heads, file the held templates instead
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is None  # dept work now open
    for b in briefs:
        db_session.refresh(b)
        assert b.status == TaskStatus.CANCELLED
        assert json.loads(b.constraints)["fallback_applied"] is True
    events = _fallbacks(db_session)
    assert len(events) == len(briefs)
    assert all(str(e.metadata_json["reason"]).startswith("stalled_waiting:READY") for e in events)
    orders = _of_kind(db_session, pid, TaskKind.DEPARTMENT_WORK.value)
    assert {json.loads(t.constraints)["work_order_key"].split(".")[0] for t in orders} >= {
        "sales",
        "legal",
    }

    # once the template work is done the closure loops start
    for t in orders:
        t.status = TaskStatus.DONE
    db_session.flush()
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is not None


def test_running_brief_is_not_counted(db_session: Session, tmp_env: Path) -> None:
    result = _start_with_impl_done(db_session, tmp_env, max_wait_cycles=1)
    pid = result.project.id
    for b in result.head_brief_tasks:
        b.status = TaskStatus.RUNNING
    db_session.flush()
    for _ in range(3):
        assert maybe_spawn_closure_loop(db_session, project_id=pid) is None
    for b in result.head_brief_tasks:
        db_session.refresh(b)
        assert b.status == TaskStatus.RUNNING
        assert "wait_cycles" not in json.loads(b.constraints)
    assert _fallbacks(db_session) == []


def test_zero_wait_cycles_falls_back_immediately(db_session: Session, tmp_env: Path) -> None:
    result = _start_with_impl_done(db_session, tmp_env, max_wait_cycles=0)
    assert maybe_spawn_closure_loop(db_session, project_id=result.project.id) is None
    for b in result.head_brief_tasks:
        db_session.refresh(b)
        assert b.status == TaskStatus.CANCELLED


def test_briefs_still_wait_while_implementation_runs(
    db_session: Session, tmp_env: Path
) -> None:
    """The counter only runs at the closure gate (after implementation is done)."""
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env, max_wait_cycles=1)
    result = start_goal(db_session, SAAS_GOAL)
    for _ in range(3):
        assert maybe_spawn_closure_loop(db_session, project_id=result.project.id) is None
    for b in result.head_brief_tasks:
        db_session.refresh(b)
        assert b.status == TaskStatus.READY
        assert "wait_cycles" not in json.loads(b.constraints)


def test_autonomous_completes_when_heads_can_never_run(
    db_session: Session, tmp_env: Path
) -> None:
    """Briefs filed, then the heads lose their profiles → policy denies forever."""
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env, max_wait_cycles=2)
    result = start_goal(db_session, SAAS_GOAL)
    assert result.head_brief_tasks
    path = tmp_env / "config" / "permissions.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for role in HEADS:
        data["profiles"].pop(role, None)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    clear_config_cache()

    auto = run_autonomous(
        db_session, project_id=result.project.id, max_cycles=300, runner="mock"
    )
    assert auto.release_candidate_id is not None, auto.stopped_reason
    db_session.refresh(result.project)
    assert result.project.status == ProjectStatus.DONE
    for b in result.head_brief_tasks:
        db_session.refresh(b)
        assert b.status == TaskStatus.CANCELLED
    orders = _of_kind(db_session, result.project.id, TaskKind.DEPARTMENT_WORK.value)
    assert orders and all(t.status == TaskStatus.DONE for t in orders)


def test_one_wait_cycle_gives_one_dispatch_chance(db_session: Session, tmp_env: Path) -> None:
    """B-1: N=1 is not the same as N=0 — the first gate check keeps the brief."""
    result = _start_with_impl_done(db_session, tmp_env, max_wait_cycles=1)
    pid = result.project.id
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is None
    for b in result.head_brief_tasks:
        db_session.refresh(b)
        assert b.status == TaskStatus.READY
        assert json.loads(b.constraints)["wait_cycles"] == 1
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is None
    for b in result.head_brief_tasks:
        db_session.refresh(b)
        assert b.status == TaskStatus.CANCELLED


def test_running_resets_wait_counter(db_session: Session, tmp_env: Path) -> None:
    """B-3: a brief retried back to READY after running gets fresh chances."""
    result = _start_with_impl_done(db_session, tmp_env, max_wait_cycles=2)
    pid = result.project.id
    briefs = result.head_brief_tasks
    for _ in range(2):
        maybe_spawn_closure_loop(db_session, project_id=pid)
    for b in briefs:
        db_session.refresh(b)
        assert json.loads(b.constraints)["wait_cycles"] == 2
        b.status = TaskStatus.RUNNING
    db_session.flush()
    maybe_spawn_closure_loop(db_session, project_id=pid)
    for b in briefs:
        db_session.refresh(b)
        assert "wait_cycles" not in json.loads(b.constraints)
        b.status = TaskStatus.READY  # retry put it back
    db_session.flush()
    maybe_spawn_closure_loop(db_session, project_id=pid)
    for b in briefs:
        db_session.refresh(b)
        assert b.status == TaskStatus.READY  # not cancelled on stale count
        assert json.loads(b.constraints)["wait_cycles"] == 1
