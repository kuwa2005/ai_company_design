"""Cost Governor: limits, reservation, throttle, circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import BudgetEntry, CircuitBreakerState, Project, Task, Worker
from ai_company.enums import BudgetKind, CostDecision, TaskStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit

ACTIVE_WORKER_STATUSES = {
    WorkerStatus.HIRED,
    WorkerStatus.STARTING,
    WorkerStatus.ACTIVE,
    WorkerStatus.REPORTING,
}


@dataclass(frozen=True)
class CostVerdict:
    decision: CostDecision
    reason: str
    effective_max_workers: int
    usage_ratio: float
    priority_minimum: int | None = None


@dataclass(frozen=True)
class BudgetSnapshot:
    spent: float
    reserved: float
    hourly_spent: float
    hourly_runs: int
    daily_spent: float
    daily_runs: int
    monthly_spent: float
    soft_limit: float
    hard_limit: float

    @property
    def usable(self) -> float:
        # hard_limit <= 0: subscription mode (no USD hard cap)
        if self.hard_limit <= 0:
            return float("inf")
        return max(0.0, self.hard_limit - self.spent - self.reserved)

    @property
    def soft_usage_ratio(self) -> float:
        # $0 soft limit = subscription mode (no USD throttle via usage tiers).
        if self.soft_limit <= 0:
            return 0.0
        return (self.spent + self.reserved) / self.soft_limit

    @property
    def subscription_mode(self) -> bool:
        return self.hard_limit <= 0 and self.soft_limit <= 0


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sum_budget(
    session: Session,
    *,
    kinds: set[str],
    since: datetime | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
) -> float:
    stmt = select(func.coalesce(func.sum(BudgetEntry.amount), 0.0)).where(
        BudgetEntry.kind.in_(kinds)
    )
    if since is not None:
        stmt = stmt.where(BudgetEntry.created_at >= since)
    if scope_type is not None:
        stmt = stmt.where(BudgetEntry.scope_type == scope_type)
    if scope_id is not None:
        stmt = stmt.where(BudgetEntry.scope_id == scope_id)
    return float(session.scalar(stmt) or 0.0)


def _count_runs(session: Session, *, since: datetime) -> int:
    # task-scoped spend entries count as runs (avoid double-count with project mirrors)
    stmt = select(func.count()).select_from(BudgetEntry).where(
        BudgetEntry.kind == BudgetKind.SPEND,
        BudgetEntry.scope_type == "task",
        BudgetEntry.created_at >= since,
    )
    return int(session.scalar(stmt) or 0)


def get_budget_snapshot(session: Session, config: AppConfig | None = None) -> BudgetSnapshot:
    cfg = config or load_config()
    now = _utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Global ledger uses task-scoped entries only (project-scoped are mirrors for project caps).
    spent = _sum_budget(session, kinds={BudgetKind.SPEND}, scope_type="task")
    reserved = _sum_budget(session, kinds={BudgetKind.RESERVE}, scope_type="task")
    released = _sum_budget(session, kinds={BudgetKind.RELEASE}, scope_type="task")
    net_reserved = max(0.0, reserved - released)

    monthly_spent = _sum_budget(
        session, kinds={BudgetKind.SPEND}, since=month_start, scope_type="task"
    )

    return BudgetSnapshot(
        spent=spent,
        reserved=net_reserved,
        hourly_spent=_sum_budget(
            session, kinds={BudgetKind.SPEND}, since=hour_ago, scope_type="task"
        ),
        hourly_runs=_count_runs(session, since=hour_ago),
        daily_spent=_sum_budget(
            session, kinds={BudgetKind.SPEND}, since=day_ago, scope_type="task"
        ),
        daily_runs=_count_runs(session, since=day_ago),
        monthly_spent=monthly_spent + net_reserved,
        soft_limit=cfg.budget.budget.monthly.soft_limit_usd,
        hard_limit=cfg.budget.budget.monthly.hard_limit_usd,
    )


def count_active_workers(session: Session, project_id: str | None = None) -> int:
    stmt = select(func.count()).select_from(Worker).where(
        Worker.status.in_(list(ACTIVE_WORKER_STATUSES))
    )
    if project_id:
        stmt = stmt.where(Worker.project_id == project_id)
    return int(session.scalar(stmt) or 0)


def count_active_validation_workers(session: Session) -> int:
    """Active workers whose task.kind is validation or judge."""
    stmt = (
        select(func.count())
        .select_from(Worker)
        .join(Task, Task.id == Worker.task_id)
        .where(
            Worker.status.in_(list(ACTIVE_WORKER_STATUSES)),
            Task.kind.in_(["validation", "judge"]),
        )
    )
    return int(session.scalar(stmt) or 0)


def count_active_reviewers_for_plan(session: Session, plan_id: str | None) -> int:
    if not plan_id:
        return 0
    stmt = (
        select(func.count())
        .select_from(Worker)
        .join(Task, Task.id == Worker.task_id)
        .where(
            Worker.status.in_(list(ACTIVE_WORKER_STATUSES)),
            Task.kind == "validation",
            Task.validation_plan_id == plan_id,
        )
    )
    return int(session.scalar(stmt) or 0)


def is_circuit_open(session: Session, config: AppConfig | None = None) -> tuple[bool, str]:
    cfg = config or load_config()
    state = session.scalar(
        select(CircuitBreakerState).order_by(CircuitBreakerState.id.desc()).limit(1)
    )
    if state and state.opened_at:
        cooldown = timedelta(minutes=cfg.settings.circuit_breaker.cooldown_minutes)
        if _utcnow() < state.opened_at + cooldown:
            return True, state.reason or "circuit_open"
        # auto-close after cooldown
        state.opened_at = None
        state.reason = None
        session.flush()
    return False, ""


def record_failure_and_maybe_trip(
    session: Session, config: AppConfig | None = None
) -> bool:
    """Count recent FAILED executions via audit/task failures; open circuit if needed."""
    cfg = config or load_config()
    window = timedelta(minutes=cfg.settings.circuit_breaker.window_minutes)
    since = _utcnow() - window

    # Count recent task_failed audits
    from ai_company.db.models import AuditEvent

    failures = int(
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.event_type == "task_failed",
                AuditEvent.created_at >= since,
            )
        )
        or 0
    )
    if failures >= cfg.settings.circuit_breaker.failure_threshold:
        open_circuit(session, reason=f"failure_threshold:{failures}")
        return True
    return False


def open_circuit(session: Session, *, reason: str) -> None:
    state = session.scalar(
        select(CircuitBreakerState).order_by(CircuitBreakerState.id.desc()).limit(1)
    )
    if state is None:
        state = CircuitBreakerState()
        session.add(state)
    state.opened_at = _utcnow()
    state.reason = reason
    session.flush()
    record_audit(
        session,
        event_type="circuit_opened",
        actor_type="system",
        actor_id="cost_governor",
        target_type="circuit",
        target_id="global",
        metadata={"reason": reason},
    )


def _usage_tier(config: AppConfig, usage_ratio: float) -> tuple[str, object]:
    from ai_company.config import UsageTier

    policy = config.budget.usage_policy
    best_name = "normal"
    best: object = policy.get("normal") or UsageTier(
        threshold=0.0, max_workers=config.settings.scheduler.max_concurrent_workers
    )
    best_th = -1.0
    for name, tier in policy.items():
        if tier.threshold <= usage_ratio and tier.threshold >= best_th:
            best_name = name
            best = tier
            best_th = tier.threshold
    return best_name, best


def evaluate(
    session: Session,
    *,
    task: Task,
    config: AppConfig | None = None,
) -> CostVerdict:
    cfg = config or load_config()
    snap = get_budget_snapshot(session, cfg)

    open_, reason = is_circuit_open(session, cfg)
    if open_:
        return CostVerdict(
            decision=CostDecision.DENY,
            reason=f"circuit_breaker:{reason}",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )

    # hard monthly ($0 hard = subscription mode: do not block on internal USD ledger)
    hard = cfg.budget.budget.monthly.hard_limit_usd
    if hard > 0 and snap.spent + snap.reserved + task.estimated_cost > hard:
        return CostVerdict(
            decision=CostDecision.DENY,
            reason="monthly_hard_limit",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )

    # hourly / daily run and cost caps
    b = cfg.budget.budget
    if snap.hourly_runs >= b.hourly.max_runs:
        return CostVerdict(
            decision=CostDecision.DEFER,
            reason="hourly_max_runs",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )
    if b.hourly.max_cost_usd > 0 and snap.hourly_spent + task.estimated_cost > b.hourly.max_cost_usd:
        return CostVerdict(
            decision=CostDecision.DEFER,
            reason="hourly_max_cost",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )
    if snap.daily_runs >= b.daily.max_runs:
        return CostVerdict(
            decision=CostDecision.DEFER,
            reason="daily_max_runs",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )
    if b.daily.max_cost_usd > 0 and snap.daily_spent + task.estimated_cost > b.daily.max_cost_usd:
        return CostVerdict(
            decision=CostDecision.DEFER,
            reason="daily_max_cost",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )

    # project hard budget if set
    project = session.get(Project, task.project_id)
    if project and project.budget_hard is not None:
        project_spent = _sum_budget(
            session,
            kinds={BudgetKind.SPEND},
            scope_type="project",
            scope_id=project.id,
        )
        project_reserved = max(
            0.0,
            _sum_budget(
                session,
                kinds={BudgetKind.RESERVE},
                scope_type="project",
                scope_id=project.id,
            )
            - _sum_budget(
                session,
                kinds={BudgetKind.RELEASE},
                scope_type="project",
                scope_id=project.id,
            ),
        )
        if project_spent + project_reserved + task.estimated_cost > project.budget_hard:
            return CostVerdict(
                decision=CostDecision.DENY,
                reason="project_hard_limit",
                effective_max_workers=0,
                usage_ratio=snap.soft_usage_ratio,
            )

    tier_name, tier = _usage_tier(cfg, snap.soft_usage_ratio)
    sched_max = cfg.settings.scheduler.max_concurrent_workers
    effective = min(sched_max, tier.max_workers)
    pmin = getattr(tier, "priority_minimum", None)

    # Validation / Judge fan-out caps (Cost Governor owns quality fan-out limits)
    is_validation_like = task.kind in {"validation", "judge"}
    if is_validation_like:
        val_cap = cfg.settings.scheduler.max_validation_workers
        parallel_cap = min(
            cfg.budget.validation.max_parallel_reviewers_per_task,
            cfg.validation.max_parallel_reviewers_per_task,
        )
        active_val = count_active_validation_workers(session)
        if active_val >= val_cap:
            return CostVerdict(
                decision=CostDecision.THROTTLE,
                reason=f"max_validation_workers:{active_val}/{val_cap}",
                effective_max_workers=min(effective, val_cap),
                usage_ratio=snap.soft_usage_ratio,
                priority_minimum=pmin,
            )
        if task.kind == "validation":
            plan_active = count_active_reviewers_for_plan(session, task.validation_plan_id)
            if plan_active >= parallel_cap:
                return CostVerdict(
                    decision=CostDecision.THROTTLE,
                    reason=f"max_parallel_reviewers_per_task:{plan_active}/{parallel_cap}",
                    effective_max_workers=min(effective, parallel_cap),
                    usage_ratio=snap.soft_usage_ratio,
                    priority_minimum=pmin,
                )
        # Soft validation budget: validation spend should not exceed ratio of monthly soft
        ratio = cfg.budget.validation.default_budget_ratio
        val_spent = _sum_budget(
            session,
            kinds={BudgetKind.SPEND},
            scope_type="task",
        )
        # Approximate: count spend on validation tasks via joining is heavy; use ratio of soft as ceiling for reserved+spent on validation kinds via metadata — simpler: deny if total soft usage already high AND this is validation beyond ratio of soft
        validation_ceiling = snap.soft_limit * ratio
        # Use reserved+spent already attributed; for MVP use concurrent throttle primarily.
        # Additional soft gate: if soft usage > ratio and task is validation-only fan-out beyond needed — skip.
        _ = (val_spent, validation_ceiling)  # reserved for accounting extension

    if effective <= 0:
        return CostVerdict(
            decision=CostDecision.DENY,
            reason=f"usage_exhausted:{tier_name}",
            effective_max_workers=0,
            usage_ratio=snap.soft_usage_ratio,
        )

    if pmin is not None and task.priority < pmin:
        return CostVerdict(
            decision=CostDecision.DEFER,
            reason=f"priority_below_minimum:{pmin}",
            effective_max_workers=effective,
            usage_ratio=snap.soft_usage_ratio,
            priority_minimum=pmin,
        )

    active = count_active_workers(session)
    if active >= effective:
        return CostVerdict(
            decision=CostDecision.THROTTLE,
            reason=f"at_capacity:{active}/{effective}",
            effective_max_workers=effective,
            usage_ratio=snap.soft_usage_ratio,
            priority_minimum=pmin,
        )

    per_project = cfg.settings.scheduler.max_workers_per_project
    if count_active_workers(session, project_id=task.project_id) >= per_project:
        return CostVerdict(
            decision=CostDecision.THROTTLE,
            reason=f"project_capacity:{per_project}",
            effective_max_workers=effective,
            usage_ratio=snap.soft_usage_ratio,
            priority_minimum=pmin,
        )

    return CostVerdict(
        decision=CostDecision.ALLOW if active < effective else CostDecision.THROTTLE,
        reason=f"ok:{tier_name}",
        effective_max_workers=effective,
        usage_ratio=snap.soft_usage_ratio,
        priority_minimum=pmin,
    )


def reserve_budget(
    session: Session,
    *,
    task: Task,
    amount: float | None = None,
) -> BudgetEntry:
    amt = amount if amount is not None else task.estimated_cost
    entry = BudgetEntry(
        id=new_id("BUD"),
        scope_type="task",
        scope_id=task.id,
        amount=amt,
        kind=BudgetKind.RESERVE,
    )
    session.add(entry)
    # also mirror project scope for project limits
    session.add(
        BudgetEntry(
            id=new_id("BUD"),
            scope_type="project",
            scope_id=task.project_id,
            amount=amt,
            kind=BudgetKind.RESERVE,
        )
    )
    session.flush()
    record_audit(
        session,
        event_type="budget_reserved",
        actor_type="system",
        actor_id="cost_governor",
        target_type="task",
        target_id=task.id,
        metadata={"amount": amt},
    )
    return entry


def release_reservation(session: Session, *, task: Task, amount: float | None = None) -> None:
    amt = amount if amount is not None else task.estimated_cost
    for scope_type, scope_id in (("task", task.id), ("project", task.project_id)):
        session.add(
            BudgetEntry(
                id=new_id("BUD"),
                scope_type=scope_type,
                scope_id=scope_id,
                amount=amt,
                kind=BudgetKind.RELEASE,
            )
        )
    session.flush()


def spend_budget(
    session: Session,
    *,
    task: Task,
    amount: float,
) -> None:
    # release reservation then spend actual
    release_reservation(session, task=task, amount=task.estimated_cost)
    for scope_type, scope_id in (("task", task.id), ("project", task.project_id)):
        session.add(
            BudgetEntry(
                id=new_id("BUD"),
                scope_type=scope_type,
                scope_id=scope_id,
                amount=amount,
                kind=BudgetKind.SPEND,
            )
        )
    session.flush()
    record_audit(
        session,
        event_type="budget_spent",
        actor_type="system",
        actor_id="cost_governor",
        target_type="task",
        target_id=task.id,
        metadata={"amount": amount},
    )


def deny_and_defer(session: Session, *, task: Task, verdict: CostVerdict) -> None:
    if verdict.decision == CostDecision.DENY and verdict.reason.startswith(
        ("monthly_hard", "project_hard", "usage_exhausted", "circuit")
    ):
        task.status = TaskStatus.DEFERRED_BUDGET
    # THROTTLE/DEFER leave as READY for later
    session.flush()
    record_audit(
        session,
        event_type="budget_denied",
        actor_type="system",
        actor_id="cost_governor",
        target_type="task",
        target_id=task.id,
        metadata={"decision": verdict.decision, "reason": verdict.reason},
    )


def revive_deferred_budget_tasks(
    session: Session, *, config: AppConfig | None = None
) -> int:
    """Move DEFERRED_BUDGET back to READY when Cost Governor would no longer hard-deny."""
    cfg = config or load_config()
    deferred = list(
        session.scalars(
            select(Task).where(Task.status == TaskStatus.DEFERRED_BUDGET)
        ).all()
    )
    revived = 0
    for task in deferred:
        verdict = evaluate(session, task=task, config=cfg)
        sticky = verdict.decision == CostDecision.DENY and verdict.reason.startswith(
            ("monthly_hard", "project_hard", "usage_exhausted", "circuit")
        )
        if sticky:
            continue
        task.status = TaskStatus.READY
        revived += 1
        record_audit(
            session,
            event_type="task_ready",
            actor_type="system",
            actor_id="cost_governor",
            target_type="task",
            target_id=task.id,
            metadata={"from": TaskStatus.DEFERRED_BUDGET, "reason": "budget_revived"},
        )
    if revived:
        session.flush()
    return revived
