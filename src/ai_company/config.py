"""Load and validate YAML configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ai_company.paths import config_dir


class HourlyBudget(BaseModel):
    max_runs: int = 20
    max_cost_usd: float = 5.0


class DailyBudget(BaseModel):
    max_runs: int = 100
    max_cost_usd: float = 20.0


class MonthlyBudget(BaseModel):
    soft_limit_usd: float = 200.0
    hard_limit_usd: float = 250.0


class BudgetLimits(BaseModel):
    hourly: HourlyBudget = Field(default_factory=HourlyBudget)
    daily: DailyBudget = Field(default_factory=DailyBudget)
    monthly: MonthlyBudget = Field(default_factory=MonthlyBudget)


class UsageTier(BaseModel):
    threshold: float
    max_workers: int
    priority_minimum: int | None = None


class SchedulerConfig(BaseModel):
    max_concurrent_workers: int = 4
    poll_interval_seconds: int = 2
    max_workers_per_project: int = 3


class WorkerConfig(BaseModel):
    heartbeat_seconds: int = 30
    idle_timeout_minutes: int = 10
    hard_timeout_minutes: int = 45


class RetryConfig(BaseModel):
    default_max_retries: int = 2


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    window_minutes: int = 10
    cooldown_minutes: int = 30


class ReviewConfig(BaseModel):
    enabled: bool = True
    role: str = "reviewer"


class SettingsConfig(BaseModel):
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)


class BudgetConfig(BaseModel):
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    usage_policy: dict[str, UsageTier] = Field(default_factory=dict)


class CursorRunnerConfig(BaseModel):
    executable: str = "agent"
    print_mode_flag: str = "-p"
    output_format_flag: str = "--output-format"
    output_format: str = "json"
    timeout_minutes: int = 45
    workspace: str | None = None


class CursorRunnerFile(BaseModel):
    cursor_runner: CursorRunnerConfig = Field(default_factory=CursorRunnerConfig)


class AppConfig(BaseModel):
    settings: SettingsConfig
    budget: BudgetConfig
    cursor_runner: CursorRunnerConfig
    permissions: dict[str, Any] = Field(default_factory=dict)

    @property
    def default_max_retries(self) -> int:
        return self.settings.retry.default_max_retries

    @property
    def default_estimated_cost(self) -> float:
        """Fallback estimated cost per mock run (USD)."""
        return 0.10


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return data


@lru_cache(maxsize=1)
def load_config(config_root: str | None = None) -> AppConfig:
    root = Path(config_root) if config_root else config_dir()
    settings = SettingsConfig.model_validate(_read_yaml(root / "settings.yaml"))
    budget = BudgetConfig.model_validate(_read_yaml(root / "budget.yaml"))
    cursor = CursorRunnerFile.model_validate(_read_yaml(root / "cursor_runner.yaml"))
    permissions = _read_yaml(root / "permissions.yaml")
    return AppConfig(
        settings=settings,
        budget=budget,
        cursor_runner=cursor.cursor_runner,
        permissions=permissions,
    )


def clear_config_cache() -> None:
    load_config.cache_clear()
