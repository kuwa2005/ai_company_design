"""Shared fixtures: isolated data dir + config + DB per test."""

from __future__ import annotations

import os
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

    clear_config_cache()
    reset_engine()
    init_db()
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
