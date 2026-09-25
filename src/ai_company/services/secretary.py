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


def _requirement_gap_proposals(
    *, depends_on_titles: list[str] | None = None
) -> list[TaskProposal]:
    """
    Scan workspace markdown for screen/API ids (S5, /api/admin/...) and propose
    follow-up implementation tasks (C-001 / C-009).
    """
    import re

    from ai_company.paths import project_root

    root = project_root()
    texts: list[str] = []
    for pattern in (
        "**/REQUIREMENTS*.md",
        "**/requirements*.md",
        "**/PRD*.md",
        "**/仕様*.md",
        "**/docs/**/*.md",
    ):
        for path in root.glob(pattern):
            if ".ai_company" in path.parts or "node_modules" in path.parts:
                continue
            try:
                texts.append(path.read_text(encoding="utf-8", errors="ignore")[:80_000])
            except OSError:
                continue
    blob = "\n".join(texts)
    if not blob.strip():
        return []

    screens = sorted(set(re.findall(r"\bS([5-9]|\d{2,})\b", blob)))
    admin_apis = sorted(set(re.findall(r"/api/admin/[a-zA-Z0-9_\-/]+", blob)))
    proposals: list[TaskProposal] = []
    deps = depends_on_titles or []
    for sid in screens[:8]:
        label = f"S{sid}"
        proposals.append(
            TaskProposal(
                title=f"画面ギャップ解消: {label}",
                objective=f"要件に登場する画面 {label} を実装し、デモ導線から到達可能にする",
                role_required="frontend_engineer",
                priority=55,
                depends_on_titles=deps,
                risk_level="medium",
                acceptance_criteria=f"{label} がUIから操作でき、受け入れテストがある",
            )
        )
    for api in admin_apis[:6]:
        proposals.append(
            TaskProposal(
                title=f"APIギャップ解消: {api}",
                objective=f"要件に登場する {api} を実装する",
                role_required="backend_engineer",
                priority=54,
                depends_on_titles=deps,
                risk_level="medium",
                acceptance_criteria=f"{api} が動作しテストがある",
            )
        )
    if proposals:
        # Demo gate already in skeleton; only add if not already proposed upstream
        pass
    return proposals


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


def _generic_decompose_skeleton(goal: str, goal_risk: str) -> list[TaskProposal]:
    """Shared decompose → implement → verify → demo skeleton (C-012)."""
    g = goal.strip()
    return [
        TaskProposal(
            title="Goal分解とスコープ定義",
            objective=f"「{g}」を実装可能なスコープ・非目標・成功指標に分解する",
            role_required="coding_worker",
            priority=90,
            risk_level="low",
            acceptance_criteria="スコープと非目標が文書化されている",
        ),
        TaskProposal(
            title="アーキテクチャ／境界整理",
            objective="コンポーネント境界・データ／API境界・主要リスクを定義する",
            role_required="coding_worker",
            priority=85,
            depends_on_titles=["Goal分解とスコープ定義"],
            risk_level="medium",
            acceptance_criteria="境界と主要依存が文書化されている",
        ),
        TaskProposal(
            title="コア実装",
            objective="スコープ内の中核機能を実装する（完璧より先に動く切片）",
            role_required="coding_worker",
            priority=70,
            depends_on_titles=["アーキテクチャ／境界整理"],
            risk_level=goal_risk,
            acceptance_criteria="中核機能が動作しテストがある",
        ),
        TaskProposal(
            title="E2E / 受け入れ確認",
            objective="主要フローのテストと受け入れ条件確認（デモシナリオ含む）",
            role_required="coding_worker",
            priority=60,
            depends_on_titles=["コア実装"],
            risk_level="medium",
            acceptance_criteria="デモシナリオが自動テストで通る",
        ),
        TaskProposal(
            title="管理者デモシナリオ",
            objective="管理者がログインし主要操作を完走できるデモを機械的受け入れにする",
            role_required="coding_worker",
            priority=55,
            depends_on_titles=["E2E / 受け入れ確認"],
            risk_level="medium",
            acceptance_criteria="管理者デモが自動テストで通る（C-009）",
        ),
    ]


def _domain_overlay_tasks(goal: str) -> list[TaskProposal]:
    """Thin domain hints layered onto the generic skeleton (not a fixed template)."""
    lower = goal.lower()
    is_attendance = any(k in lower for k in ("勤怠", "attendance", "打刻", "timesheet"))
    is_web = any(k in lower for k in ("web", "saas", "api", "アプリ", "勤怠", "attendance"))
    if not (is_attendance or is_web):
        return []
    # Replace generic core with slightly richer backend/frontend split
    return [
        TaskProposal(
            title="データモデル設計",
            objective="ドメイン最小スキーマを定義する（まず動くAPIのための最小）",
            role_required="backend_engineer",
            priority=78,
            depends_on_titles=["アーキテクチャ／境界整理"],
            risk_level="medium",
            acceptance_criteria="スキーマと制約が定義され、API実装に進める",
        ),
        TaskProposal(
            title="API 実装",
            objective="主要ドメインAPIを実装する（完璧より先に動くエンドポイント）",
            role_required="backend_engineer",
            priority=72,
            depends_on_titles=["データモデル設計"],
            risk_level="medium",
            acceptance_criteria="主要APIが手動または自動で確認できる",
        ),
        TaskProposal(
            title="Web UI 実装",
            objective="主要画面の最小UIを実装する",
            role_required="frontend_engineer",
            priority=68,
            depends_on_titles=["API 実装"],
            risk_level="medium",
            acceptance_criteria="主要画面からコアフローが完走する",
        ),
    ]


def _keyword_plan(goal: str) -> GoalPlan:
    """Deterministic planner: generic skeleton + thin domain overlay + gap scan."""
    g = goal.strip()
    lower = g.lower()
    name = g if len(g) <= 48 else g[:45] + "..."
    goal_risk = classify_risk(g)

    tasks = _generic_decompose_skeleton(g, goal_risk)
    overlay = _domain_overlay_tasks(g)
    if overlay:
        # Swap generic "コア実装" for domain overlay chain ending into E2E
        tasks = [t for t in tasks if t.title != "コア実装"]
        # Point E2E at last overlay title
        last_overlay = overlay[-1].title
        for t in tasks:
            if t.title == "E2E / 受け入れ確認":
                t.depends_on_titles = [last_overlay]
        # Insert overlay after architecture
        arch_idx = next(
            (i for i, t in enumerate(tasks) if t.title == "アーキテクチャ／境界整理"),
            0,
        )
        tasks = tasks[: arch_idx + 1] + overlay + tasks[arch_idx + 1 :]

    tasks.extend(_requirement_gap_proposals(depends_on_titles=["管理者デモシナリオ"]))

    is_webish = any(
        k in lower for k in ("web", "勤怠", "attendance", "saas", "api", "アプリ")
    )
    if is_webish:
        staffing = [
            StaffingProposal("backend_engineer", 2, "API and schema implementation"),
            StaffingProposal("frontend_engineer", 1, "Web UI implementation"),
            StaffingProposal("test_reviewer", 1, "Independent test validation"),
            StaffingProposal("security_reviewer", 1, "Auth/security validation"),
            StaffingProposal("judge", 1, "Validation gate judge"),
        ]
    else:
        staffing = [
            StaffingProposal("coding_worker", 2, "Core implementation"),
            StaffingProposal("test_reviewer", 1, "Independent test review"),
            StaffingProposal("judge", 1, "Validation judge"),
        ]
    notes = [
        "Secretary: generic decompose→implement→verify skeleton (C-012).",
        "Domain hints are thin overlays; gap scan appends missing screens/APIs.",
        "Worker count is decided later by Scheduler + Cost Governor.",
        "Coding completion routes to Validation Gate — never self-DONE.",
        "Demo scenario task is a mechanical acceptance gate (C-009).",
    ]
    validation_requests = [
        propose_validation_request(t.risk_level) for t in tasks if t.role_required != "reviewer"
    ]
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


def project_delivery_phase(session: Session, project: Project | None) -> str:
    """
    Separate empty-queue from goal-satisfied (C-002 / C-007).

    Returns one of:
      implementing | impl_queue_empty_closure_running | impl_done_closure_pending
      | release_ready | blocked | idle_incomplete | no_project
    """
    if project is None:
        return "no_project"
    from ai_company.services.closure_loops import (
        CLOSURE_KINDS,
        all_implementation_done,
        closure_loops_finished,
    )

    counts = _task_counts(session, project.id)
    if counts.get(TaskStatus.BLOCKED, 0):
        return "blocked"
    ready = counts.get(TaskStatus.READY, 0)
    running = counts.get(TaskStatus.RUNNING, 0)
    waiting = counts.get(TaskStatus.WAITING, 0)
    review = counts.get(TaskStatus.REVIEW_REQUIRED, 0)
    open_impl = ready + running + waiting + review

    open_closure = list(
        session.scalars(
            select(Task).where(
                Task.project_id == project.id,
                Task.kind.in_(tuple(CLOSURE_KINDS)),
                Task.status.in_(
                    (
                        TaskStatus.READY,
                        TaskStatus.RUNNING,
                        TaskStatus.WAITING,
                        TaskStatus.BACKLOG,
                    )
                ),
            )
        ).all()
    )
    impl_done = all_implementation_done(session, project.id)
    closure_done = closure_loops_finished(session, project.id)

    if open_closure:
        return "impl_queue_empty_closure_running" if impl_done else "implementing"
    if impl_done and not closure_done:
        return "impl_done_closure_pending"
    if impl_done and closure_done:
        return "release_ready"
    if open_impl == 0:
        return "idle_incomplete"
    return "implementing"


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

    phase = project_delivery_phase(session, project) if project else "no_project"
    phase_label = {
        "implementing": "実装消化中",
        "impl_queue_empty_closure_running": "実装キュー空／自問ループ中（Goal未充足）",
        "impl_done_closure_pending": "実装消化済／自問未着手",
        "release_ready": "リリース可（自問完了）",
        "blocked": "停止あり（要判断）",
        "idle_incomplete": "実行待ちなし／未完了あり",
        "no_project": "案件なし",
    }.get(phase, phase)

    lines = [
        "【秘書レポート】",
        f"案件: {project.name if project else '（なし）'}"
        + (f"  id={project.id}" if project else ""),
        f"目標: {project.goal if project else '-'}",
        f"状態: {status_ja(project.status if project else None)}",
        f"進行フェーズ: {phase_label}",
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
    ]
    if project:
        from ai_company.services.closure_loops import (
            CLOSURE_KINDS,
            all_implementation_done,
            closure_loops_finished,
        )

        open_closure = [
            t
            for t in session.scalars(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.kind.in_(tuple(CLOSURE_KINDS)),
                    Task.status.in_(
                        (
                            TaskStatus.READY,
                            TaskStatus.RUNNING,
                            TaskStatus.WAITING,
                            TaskStatus.BACKLOG,
                        )
                    ),
                )
            ).all()
        ]
        if open_closure or (
            all_implementation_done(session, project.id)
            and not closure_loops_finished(session, project.id)
        ):
            lines.append("自問ループ: 実装キュー空でも Goal 完了前の残件／使い勝手／品質監査中")
            for t in open_closure[:5]:
                lines.append(f"  - {t.title}（{t.status}）")
            lines.append("")

    lines.extend(
        [
            f"稼働中ワーカー: {active}",
            f"未承認の人員依頼: {staffing_open}",
            f"本日の内部コスト: ${snap.daily_spent:.2f}（上限 ${snap.soft_limit:.2f}）",
            f"月次使用率: {snap.soft_usage_ratio:.0%} "
            f"（${snap.spent + snap.reserved:.2f} / ${snap.soft_limit:.2f}）",
            (
                "ハード上限の残り: 制限なし（購読モード）"
                if snap.hard_limit <= 0
                else f"ハード上限の残り: ${snap.usable:.2f}"
            ),
            f"人間の判断が必要: {decisions}",
            "",
            "例外:",
            exception_digest(session, project_id=project.id if project else None),
            "",
            "対応状況（誰が扱っているか）:",
            responsibility_digest(session, project_id=project.id if project else None),
        ]
    )
    if decisions:
        lines.append("リスク: 停止／予算待ちタスクがあります。判断または再実行が必要です。")
    else:
        lines.append("リスク: いま目立った問題はありません。")
    lines.append("注: 秘書はワーカーを直接起動しません。スケジューラ／自動実行で進めます。")
    lines.append("注: 実装の自己申告だけでは完了になりません。独立検証ゲートが必要です。")
    lines.append(
        "注: 実装キューが空でも、残件／使い勝手／品質の自問ループが終わるまでリリースしません。"
    )
    return "\n".join(lines)


def responsibility_digest(session: Session, *, project_id: str | None = None) -> str:
    """Show which role currently owns open work / failures."""
    from ai_company.db.models import Approval
    from ai_company.roles import format_role_label

    lines: list[str] = []

    def _scoped(*statuses: str) -> list[Task]:
        stmt = select(Task).where(Task.status.in_(statuses)).order_by(Task.priority.desc())
        if project_id:
            stmt = stmt.where(Task.project_id == project_id)
        return list(session.scalars(stmt.limit(12)).all())

    for t in _scoped(TaskStatus.RUNNING, TaskStatus.READY):
        if t.kind == "replan":
            lines.append(
                f"- {format_role_label(t.role_required or 'engineering_manager')} が再計画中: "
                f"{t.id}「{t.title}」（親={t.parent_task_id}）"
            )
        elif t.kind == "repair":
            lines.append(
                f"- {format_role_label(t.role_required or 'coding_worker')} が修正中: "
                f"{t.id}「{t.title}」（親={t.parent_task_id}）"
            )
        elif t.kind in {"validation", "judge"}:
            lines.append(
                f"- {format_role_label(t.role_required or t.kind)} が検証/判定: "
                f"{t.id}「{t.title}」"
            )
        elif t.status == TaskStatus.RUNNING:
            lines.append(
                f"- {format_role_label(t.role_required or 'coding_worker')} が実行中: "
                f"{t.id}「{t.title}」"
            )

    for t in _scoped(TaskStatus.FAILED):
        replan = session.scalar(
            select(Task)
            .where(
                Task.parent_task_id == t.id,
                Task.kind.in_(("replan",)),
                Task.status.in_((TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.WAITING)),
            )
            .limit(1)
        )
        if replan:
            lines.append(
                f"- 失敗「{t.title}」→ 直近上司 "
                f"{format_role_label('engineering_manager')} が対応予定 "
                f"（{replan.id}）"
            )
        else:
            lines.append(
                f"- 失敗「{t.title}」({t.id}) "
                f"担当={format_role_label(t.role_required or 'coding_worker')}"
            )

    for t in _scoped(TaskStatus.BLOCKED):
        has_pending = any(
            t.id in (a.reason or "")
            for a in session.scalars(
                select(Approval).where(Approval.status == "PENDING").limit(20)
            ).all()
        )
        if has_pending:
            lines.append(
                f"- 停止「{t.title}」→ {format_role_label('human_owner')} の最終判断待ち "
                f"（{t.id}）"
            )
        else:
            lines.append(
                f"- 停止「{t.title}」→ 承認キュー空の孤児停止 "
                f"（次は {format_role_label('engineering_manager')} へ自動移管可 / {t.id}）"
            )

    pending = list(
        session.scalars(
            select(Approval).where(Approval.status == "PENDING").limit(10)
        ).all()
    )
    for a in pending:
        lines.append(
            f"- 承認 {a.id}: {format_role_label('human_owner')} 向け "
            f"（{_explain_block_reason(a.reason)[:80]}）"
        )

    if not lines:
        return "  （開いている役割対応はありません）"
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


def _task_id_from_reason(reason: str) -> str | None:
    import re

    m = re.search(r"task=(TASK-[A-Z0-9-]+)", reason or "")
    return m.group(1) if m else None


def _explain_block_reason(reason: str) -> str:
    r = reason or ""
    task_id = _task_id_from_reason(r)
    suffix = f"（タスク: {task_id}）" if task_id else ""
    if "manager_exhausted" in r:
        return (
            "直近の上司（実装マネージャー）の再計画回数も使い切ったため、"
            "社長（人間）の判断が必要です。"
            + suffix
        )
    if "judge_reject" in r and "review_fail:test" in r:
        return (
            "独立テスト検証が FAIL し、判定（Judge）が REJECT しました。"
            "自動の修正回数の上限に達したため、タスクを停止しています。"
            + suffix
        )
    if "judge_reject" in r and "security_fail" in r:
        return "セキュリティ検証が FAIL したため、判定が REJECT → 停止しました。" + suffix
    if "judge_reject" in r and "critical_finding" in r:
        return "重大（critical）指摘があったため、判定が REJECT → 停止しました。" + suffix
    if "judge_reject" in r:
        return f"判定が REJECT したため停止しました（詳細: {r[:120]}）。" + suffix
    if "judge_human_escalation" in r or "HUMAN_ESCALATION" in r:
        return "判定が人間エスカレーションになったため停止しています。" + suffix
    if "retries_exhausted" in r:
        return "ワーカー実行の失敗が続き、再試行上限に達したため停止しました。" + suffix
    if "repair_loop_max_cycles" in r:
        return (
            "末端の修正ループ上限に達しました。"
            "通常は直近の上司（実装マネージャー）が再計画します。"
            + suffix
        )
    return f"システムが人間判断を要求しています（詳細: {r[:160]}）。" + suffix


def human_next_actions(
    session: Session,
    *,
    project_id: str | None = None,
    stopped_reason: str | None = None,
    runner: str = "cursor",
    throttled: int = 0,
    started: int | None = None,
) -> str:
    """『なぜ止まった／一区切りしたか』と『いま人が打つコマンド』を案内する。"""
    from ai_company.db.models import Approval
    from ai_company.ui_ja import kind_ja, role_ja, status_ja

    def _tasks(*statuses: str) -> list[Task]:
        stmt = select(Task).where(Task.status.in_(statuses)).order_by(Task.priority.desc())
        if project_id:
            stmt = stmt.where(Task.project_id == project_id)
        return list(session.scalars(stmt.limit(15)).all())

    ready = _tasks(TaskStatus.READY)
    waiting = _tasks(TaskStatus.WAITING)
    review = _tasks(TaskStatus.REVIEW_REQUIRED)
    blocked = _tasks(TaskStatus.BLOCKED)
    running = _tasks(TaskStatus.RUNNING)
    deferred = _tasks(TaskStatus.DEFERRED_BUDGET)
    pending = list(
        session.scalars(
            select(Approval)
            .where(Approval.status == "PENDING")
            .order_by(Approval.created_at.desc())
            .limit(10)
        ).all()
    )

    lines: list[str] = ["【いまの状態】"]
    reason_map = {
        "blocked_exception": "停止タスクがあり、自動実行を安全のため中断しました。",
        "idle_incomplete": (
            "実行できるREADYがありませんが、Goal充足／自問完了とは限りません。"
        ),
        "max_cycles": "設定した最大サイクル数に達したため停止しました。",
        "all_implementation_done": (
            "実装タスクは消化済みです。自問ループが残っていれば continue で進めてください。"
        ),
        "scheduler_pause": "スケジューラが1サイクル終了しました（ここは正常な一区切りです）。",
        "closure_running": "実装キューは空ですが、Goal完了前の自問ループが実行中です。",
        "release_ready": "自問まで完了し、リリース候補の状態です。",
    }
    if project_id:
        proj = session.get(Project, project_id)
        phase = project_delivery_phase(session, proj)
        lines.append(f"- 対象案件: {proj.name if proj else project_id} ({project_id})")
        lines.append(f"- 進行フェーズ: {phase}")
        if phase == "impl_queue_empty_closure_running" and not stopped_reason:
            stopped_reason = "closure_running"
        elif phase == "release_ready" and not stopped_reason:
            stopped_reason = "release_ready"
    if stopped_reason:
        lines.append(f"- {reason_map.get(stopped_reason, stopped_reason)}")
    if throttled > 0:
        lines.append(
            f"- 同時起動上限のため {throttled} 件が待ちになりました"
            "（検証ワーカー数の上限など）。続きは次のサイクルで実行できます。"
        )
    if started is not None:
        lines.append(f"- このサイクルで開始したタスク: {started} 件")

    if blocked:
        lines.append("")
        lines.append("【停止中のタスク】")
        for t in blocked[:8]:
            matched = next((a for a in pending if t.id in (a.reason or "")), None)
            lines.append(f"- {t.id}  「{t.title}」")
            if matched:
                lines.append(f"  原因: {_explain_block_reason(matched.reason)}")
            else:
                last = session.scalar(
                    select(Approval)
                    .where(Approval.reason.contains(t.id))
                    .order_by(Approval.created_at.desc())
                    .limit(1)
                )
                if last:
                    lines.append(f"  原因: {_explain_block_reason(last.reason)}")
                    lines.append(
                        "  状態: 承認キュー空 → 実装マネージャーへ移管可"
                    )
                else:
                    lines.append(
                        "  原因: 停止中（承認なし）。実装マネージャーへ移管可"
                    )
    if deferred:
        lines.append("")
        lines.append("【予算待ち】")
        for t in deferred[:8]:
            lines.append(f"- {t.id}  「{t.title}」")
    if ready:
        lines.append("")
        lines.append("【実行待ち】")
        for t in ready[:8]:
            lines.append(
                f"- {t.id}  「{t.title}」  "
                f"({kind_ja(t.kind)}/{role_ja(t.role_required)})"
            )
    if review:
        lines.append("")
        lines.append("【検証待ちの親タスク】")
        for t in review[:8]:
            lines.append(f"- {t.id}  「{t.title}」  状態={status_ja(t.status)}")
    if waiting:
        lines.append("")
        lines.append(f"【依存待ち】 {len(waiting)} 件（前のタスク完了待ち）")
    if running:
        lines.append("")
        lines.append("【実行中】")
        for t in running[:5]:
            lines.append(f"- {t.id}  「{t.title}」")

    lines.append("")
    lines.append("【いまやること】")

    # Priority 1: human approval
    if pending:
        lines.append("人間の承認が必要です。次を実行してください。")
        lines.append("")
        lines.append("1) 対話形式で承認（おすすめ）:")
        lines.append("   ai-company approval list --status PENDING")
        lines.append("   （1=はい / 2=いいえ / 3=再度検討せよ）")
        lines.append("")
        lines.append("   または個別に:")
        for a in pending:
            lines.append("")
            lines.append(f"2) 承認 ID: {a.id}")
            lines.append(f"   内容: {_explain_block_reason(a.reason)}")
            lines.append("   ・続けてよい場合:")
            lines.append(f"     ai-company approval resolve {a.id} --approve")
            lines.append("   ・打ち切る場合:")
            lines.append(
                f"     ai-company approval resolve {a.id} --reject --note \"理由\""
            )
        lines.append("")
        lines.append("3) 承認後、同じディレクトリで継続実行（人が毎回回す必要なし）:")
        lines.append(f"   ai-company scheduler continue --runner {runner}")
        return "\n".join(lines)

    if blocked and not pending:
        lines.append(
            "停止タスクがありますが、社長向け承認キューは空です"
            "（「再度検討／却下」済み、または旧ルートの孤児停止）。"
        )
        lines.append("")
        lines.append("いまやることは、次のいずれかです。")
        lines.append("")
        lines.append("A) 直近上司（実装マネージャー）に戻して直させる:")
        lines.append(f"   ai-company scheduler continue --runner {runner}")
        for t in blocked:
            lines.append(f"   ai-company task handoff-manager {t.id}")
        lines.append("")
        lines.append("B) わからず屋を解雇して出荷する（セキュリティ以外の膠着向け）:")
        for t in blocked:
            lines.append(f"   ai-company task fire-supervisor {t.id}")
        lines.append("")
        lines.append("調査だけするなら:")
        lines.append("   ai-company status --debug")
        for t in blocked:
            lines.append(f"   ai-company debug task {t.id}")
        return "\n".join(lines)

    # Priority 2: more work ready / throttled / deferred → machine continues, not human
    if ready or throttled > 0 or review or deferred:
        lines.append("人の操作は不要です。機械側が続ける作業です。")
        lines.append("一度だけ次を実行すれば、承認が要るまで自動で回します:")
        lines.append("")
        lines.append(f"   ai-company scheduler continue --runner {runner}")
        lines.append("")
        lines.append(
            "（`run-once` を毎回打たないでください。THROTTLE は異常ではなく"
            "次サイクルで消化します）"
        )
        lines.append("")
        lines.append("状況確認だけするなら:")
        lines.append("   ai-company status")
        return "\n".join(lines)

    if waiting and not ready and not blocked:
        lines.append("実行待ちはありません。依存待ちだけが残っています。")
        lines.append("前段タスクの完了／検証を待つか、status で詰まりを確認してください。")
        lines.append("   ai-company status")
        return "\n".join(lines)

    if stopped_reason == "all_implementation_done":
        lines.append("追加作業は不要です。リリース候補を確認してください。")
        return "\n".join(lines)

    lines.append("いま承認待ちも実行待ちもありません。")
    lines.append("   ai-company status")
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
                "SecretaryはWorkerを起動しません。scheduler と Cost Governor を"
                "確認してください。\n\n"
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
