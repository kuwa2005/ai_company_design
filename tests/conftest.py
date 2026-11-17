"""Shared fixtures: isolated data dir + config + DB per test."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache
from ai_company.db import init_db, reset_engine, session_scope


@pytest.fixture()
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data = tmp_path / "data"
    cfg = tmp_path / "config"
    data.mkdir()
    cfg.mkdir()

    # Copy repo config with optional overrides applied by individual tests
    repo_cfg = Path(__file__).resolve().parents[1] / "config"
    for name in (
        "settings.yaml",
        "budget.yaml",
        "permissions.yaml",
        "cursor_runner.yaml",
        "validation.yaml",
    ):
        (cfg / name).write_text((repo_cfg / name).read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("AI_COMPANY_DATA_DIR", str(data))
    monkeypatch.setenv("AI_COMPANY_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AI_COMPANY_DB_PATH", str(data / "test.db"))
    monkeypatch.setenv("AI_COMPANY_PROJECT_DIR", str(tmp_path))

    # The shipped default turns head briefs on. Most tests exercise the
    # deterministic template flow, so the test baseline pins it off; tests of
    # head briefs enable it explicitly (see test_head_briefs.py and
    # test_heads_default_on.py for the shipped default).
    settings_path = cfg / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    settings["executive_meeting"]["head_llm"]["enabled"] = False
    settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")

    clear_config_cache()
    reset_engine()
    init_db()
    # Tests need headroom; default product budget is $0 (Cursor subscription mode).
    write_budget_override(
        tmp_path,
        budget={
            "hourly": {"max_runs": 1000, "max_cost_usd": 1000.0},
            "daily": {"max_runs": 1000, "max_cost_usd": 1000.0},
            "monthly": {"soft_limit_usd": 200.0, "hard_limit_usd": 250.0},
        },
    )
    yield tmp_path
    clear_config_cache()
    reset_engine()


@pytest.fixture()
def db_session(tmp_env: Path) -> Iterator[Session]:
    with session_scope() as session:
        yield session


def write_budget_override(tmp_env: Path, **budget_updates: object) -> None:
    path = tmp_env / "config" / "budget.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "budget" in budget_updates and isinstance(budget_updates["budget"], dict):
        data["budget"] = {**data.get("budget", {}), **budget_updates["budget"]}
        budget_updates = {k: v for k, v in budget_updates.items() if k != "budget"}
    for k, v in budget_updates.items():
        data[k] = v
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    clear_config_cache()


def write_settings_override(tmp_env: Path, **updates: object) -> None:
    path = tmp_env / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    clear_config_cache()


def write_validation_override(tmp_env: Path, **updates: object) -> None:
    path = tmp_env / "config" / "validation.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    clear_config_cache()


HEAD_ROLES = (
    "qa_director",
    "sales_director",
    "marketing_director",
    "legal_director",
    "operations_director",
)


def drop_head_permissions(tmp_env: Path) -> None:
    """Remove the department-head profiles from the test copy of permissions.yaml.

    Used by tests of the fallback path (heads skipped for lack of a profile).
    """
    path = tmp_env / "config" / "permissions.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for role in HEAD_ROLES:
        data["profiles"].pop(role, None)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    clear_config_cache()
