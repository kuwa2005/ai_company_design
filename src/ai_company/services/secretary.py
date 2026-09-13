"""Secretary v1 — plan goals into Tasks + Staffing Requests. Never spawns workers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Project, StaffingRequest, Task
from ai_company.enums import StaffingStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit
from ai_company.services.cost_governor import count_active_workers, get_budget_snapshot
from ai_company.services.tasks import create_project, create_task

# Hard ban: this module must never import process runners or worker dispatch.
# Enforced by tests/test_secretary.py


@dataclass
class TaskProposal:
    title: str
    objective: str
    role_required: str = "coding_worker"
    priority: int = 50
    depends_on_titles: list[str] = field(default_factory=list)
    estimated_cost: float = 0.10
    risk_level: str = "medium"
    acceptance_criteria: str = ""


@dataclass
class StaffingProposal:
    role: str
    requested_count: int
    reason: str


@dataclass
class ValidationRequestProposal:
    """Secretary proposes validation; does not spawn reviewers."""

    risk_level: str
    required_review_types: list[str]
    judge_required: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class GoalPlan:
    project_name: str
    goal: str
    tasks: list[TaskProposal]
    staffing: list[StaffingProposal]
    notes: list[str] = field(default_factory=list)
    validation_requests: list[ValidationRequestProposal] = field(default_factory=list)


@dataclass
class StartResult:
    project: Project
    tasks: list[Task]
    staffing: list[StaffingRequest]
    summary: str
    validation_requests: list[ValidationRequestProposal] = field(default_factory=list)


def classify_risk(text: str, *, default: str = "medium") -> str:
    """Deterministic risk classification for Secretary v1 (no LLM)."""
    lower = (text or "").lower()
    critical_kw = (
        "payment",
        "決済",
        "auth",
        "認証",
        "セキュリティ",
        "security",
        "pii",
        "個人情報",
        "production",
        "本番",
        "crypto",
        "権限",
    )
    high_kw = ("api", "database", "db", "migration", "勤怠", "attendance", "saas")
    low_kw = ("docs", "document", "readme", "typo", "文言", "コメント")
    if any(k in lower for k in critical_kw):
        return "critical"
    if any(k in lower for k in high_kw):
        return "high"
    if any(k in lower for k in low_kw):
        return "low"
    return default


def propose_validation_request(
    risk_level: str, *, config: AppConfig | None = None
) -> ValidationRequestProposal:
    cfg = config or load_config()
    profile = cfg.validation.risk_profiles.get(risk_level)
    if profile is None:
        profile = cfg.validation.risk_profiles.get("medium")
    required = list(profile.required) if profile else ["test"]
    cap = cfg.validation.max_reviewers_per_task
    return ValidationRequestProposal(
        risk_level=risk_level,
        required_review_types=required[:cap],
        judge_required=cfg.judge.required,
        notes=[
            "Coding Worker self-report must not mark DONE.",
            "Validation Tasks must enter Queue via Scheduler/Cost Governor.",
        ],
    )


def _keyword_plan(goal: str) -> GoalPlan:
    """Deterministic v1 planner (no LLM, no process spawn)."""
    g = goal.strip()
    lower = g.lower()
    name = g if len(g) <= 48 else g[:45] + "..."
    goal_risk = classify_risk(g)

    # Domain hints
    is_web = any(k in lower for k in ("web", "勤怠", "attendance", "saas", "api", "アプリ"))
    is_attendance = any(k in lower for k in ("勤怠", "attendance", "打刻", "timesheet"))

    if is_attendance or is_web:
        tasks = [
            TaskProposal(
                title="PRD草案",
                objective=f"Goal「{g}」のPRD（問題・利用者・成功指標・非目標）を定義する",
                role_required="coding_worker",
                priority=90,
                risk_level="low",
                acceptance_criteria="PRDが人間レビュー可能な1文書になっている",
            ),
            TaskProposal(
                title="アーキテクチャ概要",
                objective="コンポーネント境界・データフロー・主要リスクを定義する",
                role_required="coding_worker",
                priority=85,
                depends_on_titles=["PRD草案"],
                risk_level="medium",
                acceptance_criteria="層構造と主要依存が図示または文書化されている",
            ),
            TaskProposal(
                title="要件・画面/API境界の整理",
                objective=f"Goal「{g}」の要件を整理し、APIと画面の境界を定義する",
                role_required="coding_worker",
                priority=80,
                depends_on_titles=["アーキテクチャ概要"],
                risk_level=classify_risk(g + " requirements", default="medium"),
                acceptance_criteria="境界と主要ユースケースが文書化されている",
            ),
            TaskProposal(
                title="データモデル設計",
                objective="従業員・勤怠記録・承認フローのスキーマを定義する",
                role_required="backend_engineer",
                priority=75,
                depends_on_titles=["要件・画面/API境界の整理"],
                risk_level="high",
                acceptance_criteria="スキーマと制約が定義されている",
            ),
            TaskProposal(
                title="Attendance API 実装",
                objective="打刻・一覧・修正APIを実装する",
                role_required="backend_engineer",
                priority=70,
                depends_on_titles=["データモデル設計"],
                risk_level=goal_risk if goal_risk in {"high", "critical"} else "high",
                acceptance_criteria="打刻・一覧・修正がテスト可能",
            ),
            TaskProposal(
                title="Web UI 実装",
                objective="勤怠入力・一覧の最小UIを実装する",
                role_required="frontend_engineer",
                priority=65,
                depends_on_titles=["Attendance API 実装"],
                risk_level="medium",
                acceptance_criteria="主要画面から打刻フローが完走する",
            ),
            TaskProposal(
                title="E2E / 受け入れ確認",
                objective="主要フローのテストと受け入れ条件確認",
                role_required="coding_worker",
                priority=60,
                depends_on_titles=["Web UI 実装"],
                risk_level="medium",
                acceptance_criteria="受け入れ条件を満たす自動テストがある",
            ),
        ]
        staffing = [
            StaffingProposal("backend_engineer", 2, "API and schema implementation"),
            StaffingProposal("frontend_engineer", 1, "Web UI implementation"),
            StaffingProposal("test_reviewer", 1, "Independent test validation"),
            StaffingProposal("security_reviewer", 1, "Auth/security validation"),
            StaffingProposal("judge", 1, "Validation gate judge"),
        ]
        notes = [
            "Secretary created Task/Staffing/Validation proposals only.",
            "Worker count is decided later by Scheduler + Cost Governor.",
            "Coding completion routes to Validation Gate — never self-DONE.",
        ]
        validation_requests = [
            propose_validation_request(t.risk_level) for t in tasks if t.role_required != "reviewer"
        ]
        # Deduplicate by risk_level for plan-level summary
        seen: set[str] = set()
        unique_vr: list[ValidationRequestProposal] = []
        for vr in validation_requests:
            if vr.risk_level not in seen:
                seen.add(vr.risk_level)
                unique_vr.append(vr)
        return GoalPlan(
            project_name=name,
            goal=g,
            tasks=tasks,
            staffing=staffing,
            notes=notes,
            validation_requests=unique_vr,
        )

    # Generic software goal
    tasks = [
        TaskProposal(
            title="Goal分解とスコープ定義",
            objective=f"「{g}」を実装可能なスコープに分解する",
            role_required="coding_worker",
            priority=80,
            risk_level="low",
            acceptance_criteria="スコープと非目標が明確",
        ),
        TaskProposal(
            title="コア実装",
            objective="スコープ内の中核機能を実装する",
            role_required="coding_worker",
            priority=70,
            depends_on_titles=["Goal分解とスコープ定義"],
            risk_level=goal_risk,
            acceptance_criteria="中核機能が動作しテストがある",
        ),
        TaskProposal(
            title="検証準備タスク",
            objective="独立Validation Gate向けの受け入れ条件を確定する",
            role_required="coding_worker",
            priority=60,
            depends_on_titles=["コア実装"],
            risk_level=classify_risk(g, default="medium"),
            acceptance_criteria="Acceptance Criteriaが検証可能な形である",
        ),
    ]
    staffing = [
        StaffingProposal("coding_worker", 2, "Core implementation"),
        StaffingProposal("test_reviewer", 1, "Independent test review"),
        StaffingProposal("judge", 1, "Validation judge"),
    ]
    notes = [
        "Generic plan generated by Secretary v1 rules.",
        "No workers were spawned by Secretary.",
        "Self-reported coding success is not DONE; Validation Gate required.",
    ]
    return GoalPlan(
        project_name=name,
        goal=g,
        tasks=tasks,
        staffing=staffing,
        notes=notes,
        validation_requests=[propose_validation_request(goal_risk)],
    )


def understand_goal(goal: str) -> GoalPlan:
    if not goal or not goal.strip():
        raise ValueError("goal must be non-empty")
    return _keyword_plan(goal)


def create_staffing_request(
    session: Session,
    *,
    project_id: str,
    role: str,
    requested_count: int,
    reason: str,
) -> StaffingRequest:
    req = StaffingRequest(
        id=new_id("STAFF"),
        project_id=project_id,
        role=role,
        requested_count=requested_count,
        approved_count=0,
        reason=reason,
        status=StaffingStatus.REQUESTED,
    )
    session.add(req)
    session.flush()
    record_audit(
        session,
        event_type="staffing_requested",
        actor_type="secretary",
        actor_id="secretary",
        target_type="staffing_request",
        target_id=req.id,
        metadata={
            "role": role,
            "requested_count": requested_count,
            "reason": reason,
        },
    )
    return req


def apply_plan(
    session: Session,
    plan: GoalPlan,
    *,
    config: AppConfig | None = None,
) -> StartResult:
    """Persist project, tasks, staffing. Does not start workers."""
    cfg = config or load_config()
    project = create_project(session, name=plan.project_name, goal=plan.goal)
    record_audit(
        session,
        event_type="secretary_plan_applied",
        actor_type="secretary",
        actor_id="secretary",
        target_type="project",
        target_id=project.id,
        metadata={
            "task_count": len(plan.tasks),
            "staffing_count": len(plan.staffing),
            "validation_request_count": len(plan.validation_requests),
            "notes": plan.notes,
        },
    )

    title_to_id: dict[str, str] = {}
    tasks: list[Task] = []
    for proposal in plan.tasks:
        depends = [title_to_id[t] for t in proposal.depends_on_titles if t in title_to_id]
        task = create_task(
            session,
            title=proposal.title,
            priority=proposal.priority,
            project_id=project.id,
            objective=proposal.objective,
            role_required=proposal.role_required,
            estimated_cost=proposal.estimated_cost,
            depends_on=depends or None,
            risk_level=proposal.risk_level,
            acceptance_criteria=proposal.acceptance_criteria or None,
            config=cfg,
        )
        record_audit(
            session,
            event_type="task_proposed",
            actor_type="secretary",
            actor_id="secretary",
            target_type="task",
            target_id=task.id,
            metadata={
                "title": task.title,
                "role_required": task.role_required,
                "risk_level": task.risk_level,
            },
        )
        # Validation Request proposal (arrangement only — gate still opens after coding)
        vr = propose_validation_request(task.risk_level, config=cfg)
        record_audit(
            session,
            event_type="validation_request_proposed",
            actor_type="secretary",
            actor_id="secretary",
            target_type="task",
            target_id=task.id,
            metadata={
                "risk_level": vr.risk_level,
                "required_review_types": vr.required_review_types,
                "judge_required": vr.judge_required,
            },
        )
        title_to_id[proposal.title] = task.id
        tasks.append(task)

    staffing: list[StaffingRequest] = []
    for sp in plan.staffing:
        staffing.append(
            create_staffing_request(
                session,
                project_id=project.id,
                role=sp.role,
                requested_count=sp.requested_count,
                reason=sp.reason,
            )
        )

    summary = human_summary(session, project_id=project.id)
    return StartResult(
        project=project,
        tasks=tasks,
        staffing=staffing,
        summary=summary,
        validation_requests=list(plan.validation_requests),
    )


def start_goal(
    session: Session,
    goal: str,
    *,
    config: AppConfig | None = None,
) -> StartResult:
    plan = understand_goal(goal)
    return apply_plan(session, plan, config=config)


def _task_counts(session: Session, project_id: str | None = None) -> Counter[str]:
    stmt = select(Task.status, func.count()).group_by(Task.status)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    rows = session.execute(stmt).all()
    return Counter({str(status): int(count) for status, count in rows})


def human_summary(session: Session, *, project_id: str | None = None) -> str:
    """Human-facing summary: Goal/Progress/Cost/Risk/Blocked — not a raw task dump."""
    project: Project | None
    if project_id:
        project = session.get(Project, project_id)
    else:
        project = session.scalar(select(Project).order_by(Project.created_at.desc()).limit(1))

    counts = _task_counts(session, project.id if project else None)
    snap = get_budget_snapshot(session)
    active = count_active_workers(session, project_id=project.id if project else None)
    blocked = counts.get(TaskStatus.BLOCKED, 0)
    deferred = counts.get(TaskStatus.DEFERRED_BUDGET, 0)
    decisions = blocked + deferred

    staffing_stmt = (
        select(func.count())
        .select_from(StaffingRequest)
        .where(StaffingRequest.status == StaffingStatus.REQUESTED)
    )
    if project:
        staffing_stmt = staffing_stmt.where(StaffingRequest.project_id == project.id)
    staffing_open = int(session.scalar(staffing_stmt) or 0)

    lines = [
        "SECRETARY REPORT",
        f"Project: {project.name if project else '(none)'}",
        f"Goal: {project.goal if project else '-'}",
        f"Status: {project.status if project else '-'}",
        "",
        "Progress:",
        f"  DONE     {counts.get(TaskStatus.DONE, 0)}",
        f"  RUNNING  {counts.get(TaskStatus.RUNNING, 0)}",
        f"  READY    {counts.get(TaskStatus.READY, 0)}",
        f"  WAITING  {counts.get(TaskStatus.WAITING, 0)}",
        f"  REVIEW   {counts.get(TaskStatus.REVIEW_REQUIRED, 0)}",
        f"  BLOCKED  {blocked}",
        f"  DEFERRED {deferred}",
        "",
        f"Workers active: {active}",
        f"Staffing requests open: {staffing_open}",
        f"Budget today: ${snap.daily_spent:.2f} / ${snap.soft_limit:.2f} soft-month",
        f"Budget month usage: {snap.soft_usage_ratio:.0%} of soft "
        f"(${snap.spent + snap.reserved:.2f} / ${snap.soft_limit:.2f})",
        f"Hard remaining: ${snap.usable:.2f}",
        f"Human decisions required: {decisions}",
        "",
        "Exceptions:",
        exception_digest(session, project_id=project.id if project else None),
    ]
    if decisions:
        lines.append("Risk: blocked/deferred tasks need human or retry policy review.")
    else:
        lines.append("Risk: none flagged by Secretary v1.")
    lines.append("Note: Secretary cannot spawn workers; use scheduler / autonomous to dispatch.")
    lines.append("Note: Coding self-report never equals DONE — Validation Gate required.")
    return "\n".join(lines)


def exception_digest(session: Session, *, project_id: str | None = None) -> str:
    """Exception-only view for no-look operation (M10 human interface)."""
    from ai_company.db.models import Approval, JudgeDecision

    lines: list[str] = []
    task_stmt = select(Task).where(Task.status == TaskStatus.BLOCKED)
    if project_id:
        task_stmt = task_stmt.where(Task.project_id == project_id)
    for t in session.scalars(task_stmt.limit(20)).all():
        lines.append(f"- BLOCKED task {t.id}: {t.title}")

    approval_stmt = select(Approval).where(Approval.status == "PENDING")
    for a in session.scalars(approval_stmt.limit(20)).all():
        lines.append(f"- PENDING approval {a.id}: {a.action} ({a.reason[:80]})")

    judge_stmt = select(JudgeDecision).where(JudgeDecision.verdict == "HUMAN_ESCALATION")
    for d in session.scalars(judge_stmt.limit(20)).all():
        if project_id:
            parent = session.get(Task, d.task_id)
            if parent is None or parent.project_id != project_id:
                continue
        lines.append(
            f"- HUMAN_ESCALATION judge {d.id} task={d.task_id} reasons={d.reasons}"
        )

    if not lines:
        return "  (none)"
    return "\n".join(lines)


def answer_question(session: Session, question: str, *, project_id: str | None = None) -> str:
    """Lightweight ask over DB state — no process spawn."""
    q = question.strip().lower()
    summary = human_summary(session, project_id=project_id)
    if any(k in q for k in ("問題", "block", "blocked", "risk", "困")):
        counts = _task_counts(session, project_id)
        blocked = counts.get(TaskStatus.BLOCKED, 0)
        deferred = counts.get(TaskStatus.DEFERRED_BUDGET, 0)
        if blocked or deferred:
            return (
                f"最優先の問題: BLOCKED={blocked}, DEFERRED_BUDGET={deferred}.\n"
                "SecretaryはWorkerを起動しません。scheduler と Cost Governor を確認してください。\n\n"
                + summary
            )
        return "現時点で BLOCKED/DEFERRED はありません。\n\n" + summary
    if any(k in q for k in ("予算", "budget", "cost", "費用")):
        snap = get_budget_snapshot(session)
        return (
            f"Spent ${snap.spent:.2f}, reserved ${snap.reserved:.2f}, "
            f"usable ${snap.usable:.2f}, soft usage {snap.soft_usage_ratio:.0%}.\n\n"
            + summary
        )
    if any(k in q for k in ("進捗", "status", "progress", "今")):
        return summary
    return "Secretary v1 は状況要約のみ答えます。\n\n" + summary


def list_staffing(session: Session, *, project_id: str | None = None) -> list[StaffingRequest]:
    stmt = select(StaffingRequest).order_by(StaffingRequest.created_at.desc())
    if project_id:
        stmt = stmt.where(StaffingRequest.project_id == project_id)
    return list(session.scalars(stmt).all())
