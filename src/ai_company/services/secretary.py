"""Secretary v1 — plan goals into Tasks + Staffing Requests. Never spawns workers."""

from __future__ import annotations

import json
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
from ai_company.services.executive_meeting import (
    MeetingMinutes,
    convene_executive_meeting,
    record_executive_meeting,
)
from ai_company.services.redact import single_line
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
    kind: str = "implementation"


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
    meeting: MeetingMinutes | None = None
    # 会議の決議から起票した部門作業（kind=department_work）
    work_order_tasks: list[Task] = field(default_factory=list)
    # 部長所見タスク（executive_meeting.head_llm.enabled のときだけ。既定は空）
    head_brief_tasks: list[Task] = field(default_factory=list)
    # 部長所見を起票しなかった部 → 理由（権限プロファイル無し・人数上限など）
    head_brief_skipped: dict[str, str] = field(default_factory=dict)


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
            role_required="design_engineer",
            priority=90,
            risk_level="low",
            acceptance_criteria="スコープと非目標が文書化されている",
        ),
        TaskProposal(
            title="アーキテクチャ／境界整理",
            objective="コンポーネント境界・データ／API境界・主要リスクを定義する",
            role_required="design_engineer",
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
            role_required="acceptance_engineer",
            priority=60,
            depends_on_titles=["コア実装"],
            risk_level="medium",
            acceptance_criteria="デモシナリオが自動テストで通る",
        ),
        TaskProposal(
            title="管理者デモシナリオ",
            objective="管理者がログインし主要操作を完走できるデモを機械的受け入れにする",
            role_required="acceptance_engineer",
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
            StaffingProposal("design_engineer", 1, "Scope and architecture design"),
            StaffingProposal("backend_engineer", 2, "API and schema implementation"),
            StaffingProposal("frontend_engineer", 1, "Web UI implementation"),
            StaffingProposal("acceptance_engineer", 1, "E2E / acceptance tests"),
            StaffingProposal("test_reviewer", 1, "Independent test validation"),
            StaffingProposal("security_reviewer", 1, "Auth/security validation"),
            StaffingProposal("judge", 1, "Validation gate judge"),
        ]
    else:
        staffing = [
            StaffingProposal("design_engineer", 1, "Scope and architecture design"),
            StaffingProposal("coding_worker", 2, "Core implementation"),
            StaffingProposal("acceptance_engineer", 1, "E2E / acceptance tests"),
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
    minutes: MeetingMinutes | None = None,
) -> StartResult:
    """Persist project, tasks, staffing. Does not start workers.

    When executive-meeting ``minutes`` are given, each task is filed in the
    部/課 the meeting decided (not re-derived), so minutes and DB always agree.
    """
    cfg = config or load_config()
    decided = {a.task_title: a for a in (minutes.assignments if minutes else [])}
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
        unit = decided.get(proposal.title)
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
            kind=proposal.kind,
            department_id=unit.department_id if unit else None,
            section_id=unit.section_id if unit else None,
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
    hold_meeting: bool = True,
    all_departments: bool = False,
) -> StartResult:
    """社長の依頼 → 秘書が計画 → 重役会議で部/課へ割り当て → タスク起票."""
    cfg = config or load_config()
    plan = understand_goal(goal)
    if not hold_meeting:
        return apply_plan(session, plan, config=cfg)
    # 重役会議が先に部/課を決め、秘書はその決議どおりに起票する（M-3）。
    minutes = convene_executive_meeting(plan, config=cfg, all_departments=all_departments)
    result = apply_plan(session, plan, config=cfg, minutes=minutes)
    result.meeting = record_executive_meeting(
        session, minutes, project_id=result.project.id
    )
    orders_now = list(minutes.work_orders)
    if cfg.settings.executive_meeting.head_llm.enabled:
        # 任意機能: 対象部の部長に所見を求め、その部のテンプレート指示は保留する。
        # 部長 Worker の起動は Scheduler 経由（秘書は起動しない）。
        from ai_company.services.head_briefs import issue_head_briefs

        briefs = issue_head_briefs(
            session, project_id=result.project.id, minutes=minutes, config=cfg
        )
        result.head_brief_tasks = briefs.tasks
        result.head_brief_skipped = dict(briefs.skipped)
        orders_now = [o for o in orders_now if o.department_id not in briefs.held]
    if orders_now or minutes.deferred_orders:
        from ai_company.services.department_orders import issue_work_orders

        issued = issue_work_orders(
            session,
            project_id=result.project.id,
            orders=orders_now,
            deferred=minutes.deferred_orders,
            config=cfg,
        )
        result.work_order_tasks = issued.issued
    # 要約は会議の決議（部門作業・部長所見）を起票した後の状態で作り直す
    session.flush()
    result.summary = human_summary(session, project_id=result.project.id)
    return result


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


def _all_meeting_questions(session: Session, project_id: str) -> list[str]:
    """重役会議と部長所見で出た「社長・専門家への確認事項」（全件・伏せ字化済み・重複なし）.

    The list order is stable (meeting first, then briefs in filing order), so the
    1-based position is the number used by ``ai-company decide``.

    Sources: the latest ``executive_meeting_held`` audit event of the project
    (template minutes) and applied ``head_brief`` tasks (``head_open_questions``).
    """
    from ai_company.db.models import AuditEvent
    from ai_company.org import DEPARTMENTS
    from ai_company.services.redact import redact_text

    found: list[str] = []
    event = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.event_type == "executive_meeting_held",
            AuditEvent.target_id == project_id,
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    meta = (event.metadata_json or {}) if event else {}
    for q in meta.get("open_questions") or []:
        if isinstance(q, str) and q.strip():
            found.append(q.strip())

    names = {d.id: d.name_ja for d in DEPARTMENTS}
    briefs = session.scalars(
        select(Task)
        .where(Task.project_id == project_id, Task.kind == "head_brief")
        .order_by(Task.created_at.asc())
    ).all()
    for task in briefs:
        try:
            data = json.loads(task.constraints or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        dept = names.get(str(task.department_id), "部長")
        for q in data.get("head_open_questions") or []:
            text = q.get("question") if isinstance(q, dict) else q
            if not isinstance(text, str) or not text.strip():
                continue
            who = (
                "社長"
                if isinstance(q, dict) and q.get("decider") == "president"
                else "専門家／担当部署"
            )
            found.append(f"{dept}（{who}）: {text.strip()}")

    out: list[str] = []
    for q in found:
        line = single_line(redact_text(q))[:200]
        if line and line not in out:
            out.append(line)
    return out


MEETING_QUESTION_RESOLVED = "meeting_question_resolved"


def resolved_meeting_questions(session: Session, project_id: str) -> dict[str, str]:
    """Resolved question text → (redacted) answer, from audit events."""
    from ai_company.db.models import AuditEvent

    resolved: dict[str, str] = {}
    for event in session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == MEETING_QUESTION_RESOLVED,
            AuditEvent.target_type == "project",
            AuditEvent.target_id == project_id,
        )
    ).all():
        meta = event.metadata_json or {}
        q = meta.get("question")
        if isinstance(q, str) and q:
            resolved[q] = str(meta.get("answer") or "")
    return resolved


def numbered_meeting_questions(
    session: Session, project_id: str, *, include_resolved: bool = False
) -> list[tuple[int, str]]:
    """(番号, 確認事項). Numbers stay the same after other questions are resolved."""
    done = set() if include_resolved else set(resolved_meeting_questions(session, project_id))
    return [
        (n, q)
        for n, q in enumerate(_all_meeting_questions(session, project_id), 1)
        if q not in done
    ]


def meeting_open_questions(session: Session, project_id: str, *, limit: int = 10) -> list[str]:
    """未解決の確認事項（表示用・伏せ字化済み）."""
    return [q for _, q in numbered_meeting_questions(session, project_id)][:limit]


def resolve_meeting_question(
    session: Session, *, project_id: str, number: int, answer: str
) -> str:
    """社長が確認事項に回答し、解決済みにする（監査イベントを記録）.

    Returns the resolved question. Raises ``ValueError`` for an unknown number,
    an already-resolved question, or an empty answer.
    """
    from ai_company.services.decisions import department_of_question, question_key
    from ai_company.services.redact import redact_text

    answer = " ".join((answer or "").split())
    if not answer:
        raise ValueError("回答が空です")
    questions = _all_meeting_questions(session, project_id)
    if not 1 <= number <= len(questions):
        raise ValueError(f"確認事項 #{number} はありません（1〜{len(questions)}）")
    question = questions[number - 1]
    if question in resolved_meeting_questions(session, project_id):
        raise ValueError(f"確認事項 #{number} は解決済みです")
    record_audit(
        session,
        event_type=MEETING_QUESTION_RESOLVED,
        actor_type="human",
        actor_id="human_owner",
        target_type="project",
        target_id=project_id,
        metadata={
            "number": number,
            "question": question,
            "question_key": question_key(question),
            "department_id": department_of_question(question),
            "answer": redact_text(answer)[:500],
        },
    )
    session.flush()
    return question


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
    questions = numbered_meeting_questions(session, project.id) if project else []

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
            f"人間の判断が必要: {decisions + len(questions)}"
            + (
                f"（停止・予算待ち {decisions} / 会議の確認事項 {len(questions)}）"
                if questions
                else ""
            ),
            # plain text: the CLI prints this summary with markup=False (no escape here)
            *[f"  - 会議の確認事項 #{n}: {q}" for n, q in questions[:10]],
            *(
                ['    → 回答: ai-company decide <番号> "回答"（解決済みは表示から外れます）']
                if questions
                else []
            ),
            "",
            "例外:",
            exception_digest(session, project_id=project.id if project else None),
            "",
            "対応状況（誰が扱っているか）:",
            responsibility_digest(session, project_id=project.id if project else None),
            "",
            "部門別の状況（部／課）:",
            department_digest(session, project_id=project.id if project else None),
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


_OPEN_STATUSES = (
    TaskStatus.BACKLOG,
    TaskStatus.READY,
    TaskStatus.WAITING,
    TaskStatus.RUNNING,
    TaskStatus.REVIEW_REQUIRED,
)
_STOPPED_STATUSES = (TaskStatus.BLOCKED, TaskStatus.DEFERRED_BUDGET)


def department_workload(
    session: Session, *, project_id: str | None = None
) -> list[dict[str, object]]:
    """Task counts per 部/課 (stored on Task), in org-chart order.

    Each row: ``{"department_id", "section_id", "label", "done", "open",
    "stopped", "total"}``. Units with no tasks are omitted.
    """
    from ai_company.org import DEPARTMENTS, unit_label

    stmt = select(Task.department_id, Task.section_id, Task.status, func.count()).group_by(
        Task.department_id, Task.section_id, Task.status
    )
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    buckets: dict[tuple[str | None, str | None], Counter[str]] = {}
    for dept_id, section_id, status, count in session.execute(stmt).all():
        buckets.setdefault((dept_id, section_id), Counter())[str(status)] += int(count)

    order: dict[tuple[str | None, str | None], int] = {}
    for d_i, dept in enumerate(DEPARTMENTS):
        order[(dept.id, None)] = d_i * 100
        for s_i, section in enumerate(dept.sections, 1):
            order[(dept.id, section.id)] = d_i * 100 + s_i

    rows: list[dict[str, object]] = []
    for key in sorted(buckets, key=lambda k: (order.get(k, 10_000), str(k))):
        c = buckets[key]
        open_n = sum(c.get(st, 0) for st in _OPEN_STATUSES)
        stopped = sum(c.get(st, 0) for st in _STOPPED_STATUSES)
        rows.append(
            {
                "department_id": key[0],
                "section_id": key[1],
                "label": unit_label(key[0], key[1]),
                "done": c.get(TaskStatus.DONE, 0),
                "open": open_n,
                "stopped": stopped,
                "total": sum(c.values()),
            }
        )
    return rows


def department_digest(session: Session, *, project_id: str | None = None) -> str:
    rows = department_workload(session, project_id=project_id)
    if not rows:
        return "  （タスクなし）"
    lines = []
    for r in rows:
        stop = f"  停止 {r['stopped']}" if r["stopped"] else ""
        lines.append(
            f"  {r['label']}: 完了 {r['done']} / 進行中・待ち {r['open']}{stop}"
            f"（計 {r['total']}）"
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


def scheduler_continue_command(runner: str, project_id: str | None) -> str:
    """The resume command shown to the human (with ``--project`` when known).

    Without ``--project`` the continue loop cannot attribute completion to one
    project, so the guidance always names it when the caller knows it.
    """
    cmd = f"ai-company scheduler continue --runner {runner}"
    return f"{cmd} --project {project_id}" if project_id else cmd


def infer_runner(
    session: Session, project_id: str | None = None, *, default: str = "cursor"
) -> str:
    """Runner of the most recent execution (``mock`` / ``cursor``), else ``default``."""
    from ai_company.db.models import Execution

    stmt = select(Execution.runner_kind).join(Task, Task.id == Execution.task_id)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    kind = session.scalar(
        stmt.order_by(Execution.started_at.desc(), Execution.id.desc()).limit(1)
    )
    return kind if kind in {"mock", "cursor"} else default


def _cheapest(tasks: list[Task]) -> float:
    """Smallest estimate among held tasks (DEFERRED first) — mirrors ``autonomous`` (L-2)."""
    costs = [float(t.estimated_cost or 0.0) for t in tasks]
    return min(costs) if costs else 0.0


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

    continue_cmd = scheduler_continue_command(runner, project_id)
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
        "budget_wait": (
            "予算待ち（時間枠の上限）のため停止しました。"
            "同じ時間枠の中で再試行しても進まないため、空回りせずに止めています。"
        ),
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
    # 自問ループ中（closure_running）でも、READY が時間枠の上限で止まっていれば
    # 「待てば進む」は誤りなので予算待ちを優先して案内する（R-1）。
    if (
        stopped_reason in (None, "closure_running")
        and (ready or deferred)
        and not pending
        and not blocked
    ):
        # 単独の `status` でも、時間枠の上限に達していれば予算待ちとして案内する
        from ai_company.services.cost_governor import budget_window_wait

        if budget_window_wait(session, next_cost=_cheapest(deferred or ready)) is not None:
            stopped_reason = "budget_wait"
            reason_map["budget_wait"] = (
                "予算待ち（時間枠の上限）です。"
                "同じ時間枠の中で再試行しても進まないため、時間枠が進むのを待ちます。"
            )
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
        lines.append(f"   {continue_cmd}")
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
        lines.append(f"   {continue_cmd}")
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

    # Priority 1.5: rolling-window budget cap → wait, then continue (not a spin)
    if stopped_reason == "budget_wait":
        from ai_company.services.cost_governor import budget_window_wait

        wait = budget_window_wait(session, next_cost=_cheapest(deferred or ready))
        if wait is None:
            lines.append("時間枠の上限はすでに解除されています。続きを実行できます:")
        else:
            mins = wait.wait_minutes()
            eta = (
                f"約{mins}分後（{wait.resume_at_local_text()} 頃）"
                if mins is not None
                else "時間枠が進んだ後"
            )
            lines.append(
                f"{wait.reason_ja}（{wait.used:g} / {wait.limit:g}）に達しています。"
                "これは暴走防止の仕組みで、故障ではありません。"
            )
            lines.append(f"{eta}に、次を一度実行すれば続きから再開します:")
        lines.append("")
        lines.append(f"   {continue_cmd}")
        lines.append("")
        lines.append(
            "すぐに進めたい場合は、上限を見直してください"
            "（config/budget.yaml の budget.hourly / budget.daily）。"
        )
        lines.append("状況確認だけするなら:")
        lines.append("   ai-company status")
        return "\n".join(lines)

    # Priority 2: more work ready / throttled / deferred → machine continues, not human
    if ready or throttled > 0 or review or deferred:
        lines.append("人の操作は不要です。機械側が続ける作業です。")
        lines.append("一度だけ次を実行すれば、承認が要るまで自動で回します:")
        lines.append("")
        lines.append(f"   {continue_cmd}")
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
