"""Load and validate YAML configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ai_company.paths import config_dir


class HourlyBudget(BaseModel):
    max_runs: int = 40
    max_cost_usd: float = 0.0


class DailyBudget(BaseModel):
    max_runs: int = 200
    max_cost_usd: float = 0.0


class MonthlyBudget(BaseModel):
    soft_limit_usd: float = 0.0
    hard_limit_usd: float = 0.0


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
    max_validation_workers: int = 2
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


class StaffingConfig(BaseModel):
    auto_approve: bool = True
    role_capacity: dict[str, int] = Field(
        default_factory=lambda: {
            "coding_worker": 4,
            "backend_engineer": 3,
            "frontend_engineer": 2,
            "reviewer": 4,
            "judge": 2,
            "test_reviewer": 2,
            "spec_reviewer": 2,
            "security_reviewer": 2,
            "edge_case_reviewer": 2,
            "architecture_reviewer": 1,
        }
    )


class SettingsConfig(BaseModel):
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    staffing: StaffingConfig = Field(default_factory=StaffingConfig)


class BudgetValidationLimits(BaseModel):
    default_budget_ratio: float = 0.30
    max_reviewers_per_task: int = 5
    max_parallel_reviewers_per_task: int = 2


class BudgetConfig(BaseModel):
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    usage_policy: dict[str, UsageTier] = Field(default_factory=dict)
    validation: BudgetValidationLimits = Field(default_factory=BudgetValidationLimits)


class CursorRunnerConfig(BaseModel):
    executable: str = "agent"
    print_mode_flag: str = "-p"
    output_format_flag: str = "--output-format"
    output_format: str = "json"
    timeout_minutes: int = 45
    workspace: str | None = None
    auto_review: bool = True
    sandbox: str | None = "enabled"
    read_only_mode: str = "ask"
    supervisor_mode: str = "plan"
    model_by_role: dict[str, str] = Field(default_factory=dict)


class CursorRunnerFile(BaseModel):
    cursor_runner: CursorRunnerConfig = Field(default_factory=CursorRunnerConfig)


class RiskProfile(BaseModel):
    required: list[str] = Field(default_factory=list)


class ValidationSection(BaseModel):
    enabled: bool = True
    max_reviewers_per_task: int = 5
    max_parallel_reviewers_per_task: int = 2
    require_independent_execution: bool = True
    coding_worker_can_self_approve: bool = False
    risk_profiles: dict[str, RiskProfile] = Field(default_factory=dict)


class JudgeSection(BaseModel):
    required: bool = True
    critical_finding_blocks_pass: bool = True
    security_fail_blocks_pass: bool = True
    missing_required_review_blocks_pass: bool = True
    # ship_first: only security FAIL + critical findings block delivery
    # strict: any required review FAIL rejects (legacy perfectionist mode)
    delivery_mode: str = "ship_first"


class RepairLoopSection(BaseModel):
    max_cycles: int = 3
    max_total_worker_runs: int = 12
    # After coding repair cycles are exhausted, engineering_manager may replan
    # this many times before escalating to the human owner.
    manager_max_replans: int = 2
    # Fire pedantic supervisors mid-process (non-security) and mark work DONE.
    auto_fire_stubborn_supervisor: bool = True
    fire_after_non_security_rejects: int = 2


class ValidationFile(BaseModel):
    validation: ValidationSection = Field(default_factory=ValidationSection)
    judge: JudgeSection = Field(default_factory=JudgeSection)
    repair_loop: RepairLoopSection = Field(default_factory=RepairLoopSection)


class AppConfig(BaseModel):
    settings: SettingsConfig
    budget: BudgetConfig
    cursor_runner: CursorRunnerConfig
    permissions: dict[str, Any] = Field(default_factory=dict)
    validation: ValidationSection = Field(default_factory=ValidationSection)
    judge: JudgeSection = Field(default_factory=JudgeSection)
    repair_loop: RepairLoopSection = Field(default_factory=RepairLoopSection)

    @property
    def default_max_retries(self) -> int:
        return self.settings.retry.default_max_retries

    @property
    def default_estimated_cost(self) -> float:
        # $0: runs are assumed covered by Cursor subscription, not extra USD spend.
        return 0.0


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
    validation_file = ValidationFile.model_validate(_read_yaml(root / "validation.yaml"))
    return AppConfig(
        settings=settings,
        budget=budget,
        cursor_runner=cursor.cursor_runner,
        permissions=permissions,
        validation=validation_file.validation,
        judge=validation_file.judge,
        repair_loop=validation_file.repair_loop,
    )


def clear_config_cache() -> None:
    load_config.cache_clear()
