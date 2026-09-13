"""Path helpers for config and local data store."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Package / design-repo root (contains default config/ and prompts/)."""
    # src/ai_company/paths.py -> parents[2] = repo root
    return Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Working project directory (where the user runs ai-company)."""
    override = os.environ.get("AI_COMPANY_PROJECT_DIR")
    if override:
        return Path(override).resolve()
    return Path.cwd().resolve()


def config_dir() -> Path:
    override = os.environ.get("AI_COMPANY_CONFIG_DIR")
    if override:
        return Path(override).resolve()
    # Prefer project-local config if present, else package defaults
    local = project_root() / "config"
    if local.is_dir() and (local / "settings.yaml").exists():
        return local
    return repo_root() / "config"


def data_dir() -> Path:
    """Per-project state lives under <cwd>/.ai_company by default."""
    override = os.environ.get("AI_COMPANY_DATA_DIR")
    if override:
        return Path(override).resolve()
    return project_root() / ".ai_company"


def db_path() -> Path:
    override = os.environ.get("AI_COMPANY_DB_PATH")
    if override:
        return Path(override).resolve()
    return data_dir() / "ai_company.db"


def audit_log_path() -> Path:
    return data_dir() / "audit.jsonl"


def executions_dir() -> Path:
    return data_dir() / "executions"
