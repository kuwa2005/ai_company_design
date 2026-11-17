"""Load and validate YAML configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ai_company.paths import config_dir


class HourlyBudget(BaseModel):
    max_runs: int = 60
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
            "design_engineer": 1,
            "acceptance_engineer": 2,
            "reviewer": 4,
            "judge": 2,
            "test_reviewer": 2,
            "spec_reviewer": 2,
            "security_reviewer": 2,
            "edge_case_reviewer": 2,
            "architecture_reviewer": 1,
        }
    )


class MeetingResolutionsConfig(BaseModel):
    """重役会議の決議 → 部門作業指示（Work Order）の起票上限."""

    enabled: bool = True
    max_orders_per_meeting: int = Field(default=4, ge=0)
    max_orders_per_department: int = Field(default=2, ge=0)
    # 0 = 件数上限のみ（定額サブスク運用）
    max_estimated_cost_per_meeting_usd: float = Field(default=0.0, ge=0.0)
    # 計画の中核タスク（〜85）を追い越さない
    priority_ceiling: int = Field(default=80, ge=0, le=100)
    # 起票を許す部（ホワイトリスト）
    departments: list[str] = Field(
        default_factory=lambda: ["legal", "sales", "marketing", "operations"]
    )
    # 社長の回答（decide）を後続の Worker プロンプトへ DATA として渡す
    inject_decisions: bool = True
    # 回答を受けて「完了済みの部門作業の改訂」を起票する上限（案件あたり）。0 = 起票しない
    max_decision_revisions_per_project: int = Field(default=3, ge=0)
    # 部門作業の成果物を <project>/<deliverables_dir>/<部 id>/<task id>.md に書き出す。
    # 書き込むのは秘書側（決定論的なコード）で、部門作業の Worker は読み取り専用のまま。
    write_deliverables: bool = True
    deliverables_dir: str = "docs"


class HeadLLMConfig(BaseModel):
    """部長を短命 Worker（head_brief）として呼ぶ機能. 既定オン（2026-11-18 社長判断）.

    オフのときは会議テンプレートの Work Order だけを起票する（従来どおり）。
    オンのときは対象部の Work Order を保留し、部長所見の検証済み指示で置き換える。
    失敗・却下・リトライ上限では保留したテンプレートを起票する（fallback=template）。
    """

    enabled: bool = True
    departments: list[str] = Field(
        default_factory=lambda: ["sales", "marketing", "legal", "operations"]
    )
    max_heads_per_meeting: int = Field(default=4, ge=0)
    estimated_cost_per_head: float = Field(default=0.05, ge=0.0)
    max_retries: int = Field(default=1, ge=0)
    fallback: Literal["template", "none"] = "template"
    # 実装完了後（自問ループ前）に、未着手（READY/WAITING/BACKLOG）のまま
    # この回数だけ待たされた所見はテンプレートへフォールバックする。
    max_wait_cycles: int = Field(default=3, ge=0)


class ExecutiveMeetingConfig(BaseModel):
    head_llm: HeadLLMConfig = Field(default_factory=HeadLLMConfig)


class SettingsConfig(BaseModel):
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    staffing: StaffingConfig = Field(default_factory=StaffingConfig)
    meeting_resolutions: MeetingResolutionsConfig = Field(
        default_factory=MeetingResolutionsConfig
    )
    executive_meeting: ExecutiveMeetingConfig = Field(default_factory=ExecutiveMeetingConfig)


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
    # Isolate coding workers in git worktrees (requires git workspace)
    use_worktrees: bool = False
    # Fail coding runs when changed_files mismatch git (needs baseline HEAD)
    require_evidence_match: bool = True
    # Non-interactive workspace trust (--trust). Not the same as --force/--yolo.
    trust_workspace: bool = True


class CursorRunnerFile(BaseModel):
    cursor_runner: CursorRunnerConfig = Field(default_factory=CursorRunnerConfig)


class RiskProfile(BaseModel):
    required: list[str] = Field(default_factory=list)


class PhaseGateProfile(BaseModel):
    """C-005: lighter review sets for early phases; full risk_profiles at release."""

    required: list[str] = Field(default_factory=list)
    # When true, ignore this override and use risk_profiles only
    use_risk_profile: bool = False


class ValidationSection(BaseModel):
    enabled: bool = True
    max_reviewers_per_task: int = 5
    max_parallel_reviewers_per_task: int = 2
    require_independent_execution: bool = True
    coding_worker_can_self_approve: bool = False
    risk_profiles: dict[str, RiskProfile] = Field(default_factory=dict)
    # Phase -> gate override (mvp / implementing / release)
    phase_gates: dict[str, PhaseGateProfile] = Field(default_factory=dict)
    # Default phase key when project phase is early
    default_phase_gate: str = "mvp"


class JudgeSection(BaseModel):
    required: bool = True
    critical_finding_blocks_pass: bool = True
    security_fail_blocks_pass: bool = True
    missing_required_review_blocks_pass: bool = True
    # strict (default): any required review FAIL rejects
    # ship_first / prototype: only security FAIL + critical findings block delivery
    delivery_mode: str = "strict"
    # Recorded policy name: strict | balanced | prototype
    release_policy: str = "strict"


class RepairLoopSection(BaseModel):
    max_cycles: int = 3
    max_total_worker_runs: int = 12
    # After coding repair cycles are exhausted, engineering_manager may replan
    # this many times before escalating to the human owner.
    manager_max_replans: int = 2
    # Fire pedantic supervisors mid-process (non-security) and mark work DONE.
    # Default false — must be explicitly enabled (prototype / CLI).
    auto_fire_stubborn_supervisor: bool = False
    fire_after_non_security_rejects: int = 2


class ClosureLoopPhase(BaseModel):
    max_rounds: int = 3
    # Advance to next phase after this many consecutive CLEAR verdicts
    consecutive_clear_to_advance: int = 1


class ClosureLoopsSection(BaseModel):
    """Post-implementation self-questioning before declaring the goal done."""

    enabled: bool = True
    # C-010: omit phases by key (e.g. sales, maintenance for internal tools)
    skip_phases: list[str] = Field(default_factory=list)
    still_needed: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=3, consecutive_clear_to_advance=2)
    )
    project_health: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    product_fit: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    usability: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=2, consecutive_clear_to_advance=1)
    )
    customer_success: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    it_operations: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    compliance: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    test_audit: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    quality: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=2, consecutive_clear_to_advance=1)
    )
    sales: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )
    maintenance: ClosureLoopPhase = Field(
        default_factory=lambda: ClosureLoopPhase(max_rounds=1, consecutive_clear_to_advance=1)
    )


class ValidationFile(BaseModel):
    validation: ValidationSection = Field(default_factory=ValidationSection)
    judge: JudgeSection = Field(default_factory=JudgeSection)
    repair_loop: RepairLoopSection = Field(default_factory=RepairLoopSection)
    closure_loops: ClosureLoopsSection = Field(default_factory=ClosureLoopsSection)


class AppConfig(BaseModel):
    settings: SettingsConfig
    budget: BudgetConfig
    cursor_runner: CursorRunnerConfig
    permissions: dict[str, Any] = Field(default_factory=dict)
    validation: ValidationSection = Field(default_factory=ValidationSection)
    judge: JudgeSection = Field(default_factory=JudgeSection)
    repair_loop: RepairLoopSection = Field(default_factory=RepairLoopSection)
    closure_loops: ClosureLoopsSection = Field(default_factory=ClosureLoopsSection)

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
        closure_loops=validation_file.closure_loops,
    )


def clear_config_cache() -> None:
    load_config.cache_clear()
