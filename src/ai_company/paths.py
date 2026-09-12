"""Path helpers for config and local data store."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Repository root (contains config/ and prompts/)."""
    # src/ai_company/paths.py -> parents[2] = repo root
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    override = os.environ.get("AI_COMPANY_CONFIG_DIR")
    if override:
        return Path(override).resolve()
    return repo_root() / "config"


def data_dir() -> Path:
    override = os.environ.get("AI_COMPANY_DATA_DIR")
    if override:
        return Path(override).resolve()
    return repo_root() / ".ai_company"


def db_path() -> Path:
    override = os.environ.get("AI_COMPANY_DB_PATH")
    if override:
        return Path(override).resolve()
    return data_dir() / "ai_company.db"


def audit_log_path() -> Path:
    return data_dir() / "audit.jsonl"


def executions_dir() -> Path:
    return data_dir() / "executions"
