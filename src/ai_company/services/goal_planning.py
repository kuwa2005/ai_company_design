"""Goal → plan (秘書の計画): risk classification, task skeletons and domain overlays.

Pure planning: no DB writes. ``secretary.apply_plan`` files the result."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_company.config import AppConfig, load_config


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
