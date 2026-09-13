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
    estimated_cost: float = 0.0
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
    """人が読む状況要約（日本語）。"""
    from ai_company.ui_ja import status_ja

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
        "【秘書レポート】",
        f"案件: {project.name if project else '（なし）'}",
        f"目標: {project.goal if project else '-'}",
        f"状態: {status_ja(project.status if project else None)}",
        "",
        "進捗:",
        f"  完了       {counts.get(TaskStatus.DONE, 0)}",
        f"  実行中     {counts.get(TaskStatus.RUNNING, 0)}",
        f"  実行待ち   {counts.get(TaskStatus.READY, 0)}",
        f"  依存待ち   {counts.get(TaskStatus.WAITING, 0)}",
        f"  検証待ち   {counts.get(TaskStatus.REVIEW_REQUIRED, 0)}",
        f"  停止       {blocked}",
        f"  予算待ち   {deferred}",
        "",
        f"稼働中ワーカー: {active}",
        f"未承認の人員依頼: {staffing_open}",
        f"本日の内部コスト: ${snap.daily_spent:.2f}（上限 ${snap.soft_limit:.2f}）",
        f"月次使用率: {snap.soft_usage_ratio:.0%} "
        f"（${snap.spent + snap.reserved:.2f} / ${snap.soft_limit:.2f}）",
        f"ハード上限の残り: ${snap.usable:.2f}",
        f"人間の判断が必要: {decisions}",
        "",
        "例外:",
        exception_digest(session, project_id=project.id if project else None),
    ]
    if decisions:
        lines.append("リスク: 停止／予算待ちタスクがあります。判断または再実行が必要です。")
    else:
        lines.append("リスク: いま目立った問題はありません。")
    lines.append("注: 秘書はワーカーを直接起動しません。スケジューラ／自動実行で進めます。")
    lines.append("注: 実装の自己申告だけでは完了になりません。独立検証ゲートが必要です。")
    return "\n".join(lines)


def exception_digest(session: Session, *, project_id: str | None = None) -> str:
    """例外だけを日本語で列挙。"""
    from ai_company.db.models import Approval, JudgeDecision

    lines: list[str] = []
    task_stmt = select(Task).where(Task.status == TaskStatus.BLOCKED)
    if project_id:
        task_stmt = task_stmt.where(Task.project_id == project_id)
    for t in session.scalars(task_stmt.limit(20)).all():
        lines.append(f"- 停止タスク {t.id}: {t.title}")

    approval_stmt = select(Approval).where(Approval.status == "PENDING")
    for a in session.scalars(approval_stmt.limit(20)).all():
        lines.append(f"- 承認待ち {a.id}: {a.action}（{a.reason[:80]}）")

    judge_stmt = select(JudgeDecision).where(JudgeDecision.verdict == "HUMAN_ESCALATION")
    for d in session.scalars(judge_stmt.limit(20)).all():
        if project_id:
            parent = session.get(Task, d.task_id)
            if parent is None or parent.project_id != project_id:
                continue
        lines.append(
            f"- 人間エスカレーション 判定{d.id} タスク={d.task_id} 理由={d.reasons}"
        )

    if not lines:
        return "  （なし）"
    return "\n".join(lines)


def _explain_block_reason(reason: str) -> str:
    r = reason or ""
    if "judge_reject" in r and "review_fail:test" in r:
        return (
            "独立テスト検証が FAIL し、判定（Judge）が REJECT しました。"
            "自動の修正回数の上限に達したため、タスクを停止しています。"
        )
    if "judge_reject" in r and "security_fail" in r:
        return "セキュリティ検証が FAIL したため、判定が REJECT → 停止しました。"
    if "judge_reject" in r and "critical_finding" in r:
        return "重大（critical）指摘があったため、判定が REJECT → 停止しました。"
    if "judge_reject" in r:
        return f"判定が REJECT したため停止しました（詳細: {r[:120]}）。"
    if "judge_human_escalation" in r or "HUMAN_ESCALATION" in r:
        return "判定が人間エスカレーションになったため停止しています。"
    if "retries_exhausted" in r:
        return "ワーカー実行の失敗が続き、再試行上限に達したため停止しました。"
    if "repair_loop_max_cycles" in r:
        return "修正ループの回数上限に達したため、自動続行を止めました。"
    return f"システムが人間判断を要求しています（詳細: {r[:160]}）。"


def human_next_actions(
    session: Session, *, project_id: str | None = None, stopped_reason: str | None = None
) -> str:
    """停止時に『なぜ止まったか』『人は何をすればよいか』を案内する。"""
    from ai_company.db.models import Approval

    lines: list[str] = ["【いま止まっている理由】"]

    reason_map = {
        "blocked_exception": "停止タスクがあり、自動実行を安全のため中断しました。",
        "idle_incomplete": "実行できるタスクがなくなりましたが、まだ未完了の作業が残っています。",
        "max_cycles": "設定した最大サイクル数に達したため停止しました。",
        "all_implementation_done": "実装タスクはすべて完了しています。",
    }
    if stopped_reason:
        lines.append(f"- {reason_map.get(stopped_reason, stopped_reason)}")

    blocked_stmt = select(Task).where(Task.status == TaskStatus.BLOCKED)
    if project_id:
        blocked_stmt = blocked_stmt.where(Task.project_id == project_id)
    blocked = list(session.scalars(blocked_stmt.limit(10)).all())

    pending = list(
        session.scalars(
            select(Approval)
            .where(Approval.status == "PENDING")
            .order_by(Approval.created_at.desc())
            .limit(10)
        ).all()
    )

    if blocked:
        lines.append("")
        lines.append("【停止中のタスク】")
        for t in blocked:
            lines.append(f"- {t.id}  「{t.title}」")
            # Find matching pending approval for this task
            matched = next((a for a in pending if t.id in (a.reason or "")), None)
            if matched:
                lines.append(f"  原因: {_explain_block_reason(matched.reason)}")
            else:
                lines.append("  原因: 再試行上限または判定により停止（詳細は承認一覧を確認）")

    if pending:
        lines.append("")
        lines.append("【人がやること】")
        lines.append("1. 状況確認:")
        lines.append("   ai-company approval list --status PENDING")
        lines.append("   ai-company status")
        for a in pending:
            lines.append("")
            lines.append(f"2. 承認 ID: {a.id}")
            lines.append(f"   内容: {_explain_block_reason(a.reason)}")
            lines.append("   ・再実行してよい（修正を続ける）場合:")
            lines.append(f"     ai-company approval resolve {a.id} --approve")
            lines.append("   ・打ち切る／方針を変える場合:")
            lines.append(f"     ai-company approval resolve {a.id} --reject --note \"理由\"")
        lines.append("")
        lines.append("3. 承認して再キューした場合、同じディレクトリで自動実行を再開:")
        lines.append(
            '   ai-company scheduler run-once --runner cursor'
        )
        lines.append(
            "   # または案件を続けて回すなら、必要に応じて start の --autonomous を再実行"
        )
        lines.append(
            "   # （既存DBを使うので、新しい計画を増やしたくない場合は scheduler 側が安全）"
        )
    elif blocked:
        lines.append("")
        lines.append("【人がやること】")
        lines.append("- 停止タスクはあるが承認キューが空です。ログと validation status を確認してください。")
        for t in blocked:
            lines.append(f"  ai-company validation status {t.id}")
    else:
        lines.append("")
        lines.append("【人がやること】")
        lines.append("- いま承認待ちはありません。ai-company status で進捗を確認してください。")

    lines.append("")
    lines.append("補足: 自動実行は『実装の自己申告』だけでは完了にしません。")
    lines.append("検証 FAIL → 判定 REJECT → 修正上限超過、のとき人間判断に上がります。")
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
