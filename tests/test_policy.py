"""Policy engine and release policy defaults."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ai_company.config import load_config
from ai_company.services.policy import resolve_policy_name
from ai_company.services.policy_engine import evaluate_dispatch
from ai_company.services.tasks import create_project, create_task
from tests.conftest import write_validation_override


def test_default_delivery_is_strict(tmp_env: Path) -> None:
    cfg = load_config()
    assert cfg.judge.delivery_mode == "strict"
    assert cfg.repair_loop.auto_fire_stubborn_supervisor is False
    assert resolve_policy_name(cfg) == "strict"


def test_reviewer_cannot_edit(db_session: Session) -> None:
    project = create_project(db_session, name="p", goal="g")
    task = create_task(
        db_session,
        title="review",
        project_id=project.id,
        kind="validation",
        role_required="test_reviewer",
        review_type="test",
    )
    d = evaluate_dispatch(task=task, role="test_reviewer", action="code.edit")
    assert d.allowed is False


def test_coding_can_edit(db_session: Session) -> None:
    project = create_project(db_session, name="p", goal="g")
    task = create_task(
        db_session,
        title="impl",
        project_id=project.id,
        kind="implementation",
        role_required="coding_worker",
    )
    d = evaluate_dispatch(task=task, role="coding_worker")
    assert d.allowed is True


def test_undefined_role_denied(db_session: Session) -> None:
    project = create_project(db_session, name="p", goal="g")
    task = create_task(
        db_session,
        title="x",
        project_id=project.id,
        kind="implementation",
        role_required="totally_unknown_role",
    )
    d = evaluate_dispatch(task=task, role="totally_unknown_role")
    assert d.allowed is False


def test_prototype_override(tmp_env: Path) -> None:
    write_validation_override(
        tmp_env,
        judge={"delivery_mode": "ship_first", "release_policy": "prototype"},
        repair_loop={"auto_fire_stubborn_supervisor": True},
    )
    cfg = load_config()
    assert cfg.judge.delivery_mode == "ship_first"
    assert resolve_policy_name(cfg) == "prototype"
