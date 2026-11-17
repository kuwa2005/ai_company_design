"""Company organization: 部（Department）→ 課（Section）→ 担当ロール.

``roles.py`` holds *who reports to whom* for escalation (the failure route).
This module holds *which department / section a role belongs to* (the
organizational home), plus the department heads (部長) who attend the
executive meeting (重役会議).

The two views are intentionally separate:

- Escalation stays on the proven ``reports_to`` chain (worker → 実装マネージャー
  → 技術責任者 → 社長). Department heads are *not* inserted into that chain, so
  adding a department never creates new spawnable replan roles by accident.
- Department membership is used for planning, meeting minutes, and status
  display ("この仕事はどの部のどの課が持っているか").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ai_company.roles import ROLES, format_role_label


@dataclass(frozen=True)
class Section:
    id: str
    name_ja: str
    member_roles: tuple[str, ...]
    handles: str
    chief_role: str | None = None  # 課長（いなければ部長が直接見る）


@dataclass(frozen=True)
class Department:
    id: str
    name_ja: str
    head_role: str  # 部長
    mission: str
    sections: tuple[Section, ...]
    # Goal keywords that make the secretary summon this department head.
    keywords: tuple[str, ...] = ()
    # Ambiguous keywords (サービス / ユーザー / 顧客 …) that also appear in
    # internal-tool requests. They summon the head only when the goal is not
    # internal (see ``INTERNAL_MARKERS``).
    weak_keywords: tuple[str, ...] = ()
    # Always attends the executive meeting (core of any delivery).
    always_attend: bool = False


DEPARTMENTS: tuple[Department, ...] = (
    Department(
        id="secretariat",
        name_ja="秘書室",
        head_role="secretary",
        mission="社長の依頼を受け、重役会議を招集し、計画・進行・残件管理を行う",
        always_attend=True,
        sections=(
            Section(
                id="pmo",
                name_ja="推進課",
                member_roles=("project_coordinator",),
                handles="スコープ・依存・優先度・遅延リスクの点検",
            ),
            Section(
                id="completeness",
                name_ja="残件監査課",
                member_roles=("completeness_auditor",),
                handles="実装キュー空後の残件の再発見",
            ),
        ),
    ),
    Department(
        id="development",
        name_ja="開発部",
        head_role="technical_director",
        mission="ミッションを動くソフトウェアとして設計・実装・検証する",
        always_attend=True,
        keywords=("開発", "実装", "システム", "アプリ", "web", "api", "saas", "ツール"),
        sections=(
            Section(
                id="design",
                name_ja="設計課",
                member_roles=("design_engineer", "coding_worker"),
                chief_role="engineering_manager",
                handles="スコープ分解・アーキテクチャ・データモデル設計",
            ),
            Section(
                id="build",
                name_ja="開発課",
                member_roles=("backend_engineer", "frontend_engineer", "coding_worker"),
                chief_role="engineering_manager",
                handles="API・UI・中核機能の実装と修正",
            ),
            Section(
                id="verification",
                name_ja="検証課",
                member_roles=("acceptance_engineer", "test_reviewer", "edge_case_reviewer"),
                chief_role="engineering_manager",
                handles="E2E・受け入れ・境界条件の検証（実装者とは別人）",
            ),
        ),
    ),
    Department(
        id="quality_assurance",
        name_ja="品質保証部",
        head_role="qa_director",
        mission="独立した検証と判定で、自己申告ではない品質を保証する",
        always_attend=True,
        keywords=("品質", "テスト", "qa", "検証", "セキュリティ"),
        sections=(
            Section(
                id="independent_review",
                name_ja="独立検証課",
                member_roles=(
                    "spec_reviewer",
                    "security_reviewer",
                    "architecture_reviewer",
                    "reviewer",
                ),
                handles="仕様・セキュリティ・設計の独立レビュー",
            ),
            Section(
                id="judgement",
                name_ja="判定課",
                member_roles=("judge",),
                handles="検証結果を統合し PASS/REJECT を判定",
            ),
            Section(
                id="quality_audit",
                name_ja="品質監査課",
                member_roles=("test_auditor", "quality_auditor", "usability_auditor"),
                handles="テスト妥当性・完成品品質・使い勝手の監査",
            ),
        ),
    ),
    Department(
        id="sales",
        name_ja="営業部",
        head_role="sales_director",
        mission="誰に何を売るか、提案・デモ・導入定着までを用意する",
        # Selling intent only. 「売」単体は「売上集計」に誤反応するので使わない。
        keywords=(
            "営業", "販売し", "販売す", "外販", "売って", "売る", "売れる", "売れた",
            "売り出", "商談", "有料", "sales",
        ),
        # 見積・課金は社内ツールの画面にもよく出る（G-2）
        weak_keywords=(
            "販売", "顧客", "提案", "契約", "見積", "課金", "customer", "saas", "サービス",
            # 社外の利用者に出す依頼は、営業部が誰に・どう届けるかを持つ（G-1）
            "他社", "社外", "取引先", "お客様", "外部提供", "外部公開", "一般向け", "一般公開",
        ),
        sections=(
            Section(
                id="sales_proposal",
                name_ja="営業課",
                member_roles=("sales",),
                handles="提案書・デモ手順・価格メモ",
            ),
            Section(
                id="customer_success",
                name_ja="カスタマーサクセス課",
                member_roles=("customer_success",),
                handles="導入・定着・問い合わせ動線",
            ),
        ),
    ),
    Department(
        id="marketing",
        name_ja="マーケティング部",
        head_role="marketing_director",
        mission="市場と利用者の価値を定め、何を作り何を作らないかを決める",
        keywords=(
            "マーケ", "集客", "広報", "ランディング", "商品企画", "marketing",
        ),
        # 市場データ分析・ブランドガイドライン管理などの社内ツールに出る（G-2）
        weak_keywords=("企画", "ユーザー", "市場", "ブランド", "saas", "サービス"),
        sections=(
            Section(
                id="product_planning",
                name_ja="商品企画課",
                member_roles=("product_planner",),
                handles="価値適合・優先機能・非目標の定義",
            ),
        ),
    ),
    Department(
        id="legal",
        name_ja="法務部",
        head_role="legal_director",
        mission="法令・契約・個人情報・監査証跡のリスクを最小化する",
        keywords=(
            "法務", "法令", "法律", "法的", "契約", "規約", "個人情報", "プライバシー", "労働",
            "勤怠", "給与", "医療", "決済", "privacy", "gdpr", "legal",
        ),
        sections=(
            Section(
                id="compliance",
                name_ja="コンプライアンス課",
                member_roles=("compliance_auditor",),
                handles="個人情報・労働法制・監査ログの最小点検",
            ),
        ),
    ),
    Department(
        id="operations",
        name_ja="運用保守部",
        head_role="operations_director",
        mission="作ったものを運用できる状態にし、保守し続ける",
        keywords=(
            "運用", "保守", "サーバ", "インフラ", "ネットワーク", "デプロイ",
            "監視", "バックアップ", "権限", "ops", "infra",
        ),
        sections=(
            Section(
                id="it_systems",
                name_ja="情報システム課",
                member_roles=("it_operations",),
                handles="権限・シード・運用手順",
            ),
            Section(
                id="maintenance",
                name_ja="保守課",
                member_roles=("maintenance",),
                handles="保守手順・永続運用資料",
            ),
        ),
    ),
)


# A goal containing one of these is an internal tool: weak keywords such as
# 「サービス」「ユーザー」「顧客」 do not summon 営業部 / マーケティング部.
# 「内製」は作り方の話（利用者が社内とは限らない）なので目印にしない（G-1）。
INTERNAL_MARKERS: tuple[str, ...] = (
    "社内", "社員向け", "社員用", "自社用", "自社内", "部内", "内部向け",
    "internal", "in-house",
)
# Outside users / customers. Wins over INTERNAL_MARKERS: 「社内勤怠を他社に提供」
# is an external product even though it says 社内 (G-1).
EXTERNAL_MARKERS: tuple[str, ...] = (
    "社外", "他社", "外部公開", "外部提供", "外部に", "顧客向け", "お客様",
    "取引先", "外販", "一般向け", "一般公開", "external",
)


def is_external_goal(goal: str) -> bool:
    lower = goal.lower()
    return any(m in lower for m in EXTERNAL_MARKERS)


def is_internal_goal(goal: str) -> bool:
    lower = goal.lower()
    if is_external_goal(goal):
        return False
    return any(m in lower for m in INTERNAL_MARKERS)


# ASCII stems whose suffixed forms keep the meaning (infrastructure,
# PrivacyPolicy, customers, marketingsite); they need a word start only (S-3).
_STEM_KEYWORDS = frozenset({"infra", "privacy", "customer", "marketing"})


def _keyword_in(keyword: str, lower: str) -> bool:
    """ASCII keywords match at a word start; short/ambiguous ones as whole words.

    sales ≠ Salesforce, ops ≠ devops, api ≠ rapid; infra ⊂ infrastructure.
    """
    if keyword.isascii():
        tail = "" if keyword in _STEM_KEYWORDS else "(?![a-z0-9])"
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}{tail}", lower) is not None
    return keyword in lower


def summon_hits(dept: Department, goal: str) -> list[str]:
    """Keywords in ``goal`` that summon ``dept`` (empty = not summoned)."""
    lower = goal.lower()
    hits = [k for k in dept.keywords if _keyword_in(k, lower)]
    if not is_internal_goal(goal):
        hits += [k for k in dept.weak_keywords if _keyword_in(k, lower)]
    return hits


def department_by_id(dept_id: str) -> Department:
    for dept in DEPARTMENTS:
        if dept.id == dept_id:
            return dept
    raise KeyError(dept_id)


def home_of_role(role_id: str) -> tuple[Department, Section | None] | None:
    """First department/section a role belongs to (heads map to their dept)."""
    for dept in DEPARTMENTS:
        if dept.head_role == role_id:
            return dept, None
        for section in dept.sections:
            if role_id in section.member_roles or role_id == section.chief_role:
                return dept, section
    return None


def department_label(role_id: str) -> str:
    home = home_of_role(role_id)
    if home is None:
        return "（所属未定）"
    dept, section = home
    return f"{dept.name_ja}／{section.name_ja}" if section else f"{dept.name_ja}（部長）"


# --- task → 部/課 ----------------------------------------------------------

_DESIGN_HINTS = ("分解", "スコープ", "アーキテクチャ", "境界", "設計", "データモデル", "要件")
_VERIFY_HINTS = ("e2e", "受け入れ", "テスト", "デモ", "検証", "確認")
# Roles that can do any kind of development work (設計/開発/検証).
_GENERALIST_DEV_ROLES = frozenset({"coding_worker", "backend_engineer", "frontend_engineer"})
# Task kinds that always belong to 開発部／検証課 when done by a dev-generalist.
_VERIFY_KINDS = frozenset({"validation"})


def section_by_id(dept_id: str, section_id: str | None) -> Section | None:
    if not section_id:
        return None
    for section in department_by_id(dept_id).sections:
        if section.id == section_id:
            return section
    return None


def org_unit_for_task(
    title: str, role: str, kind: str | None = None
) -> tuple[Department, Section | None]:
    """Place a task in a 部/課 (the organizational owner of the work).

    - Development generalists (and the dev section chief) are split by *kind
      of work* (設計/開発/検証), because the same role can do any of them.
    - Specialists use their home section (e.g. 法務部／コンプライアンス課).
    - Department heads doing the task directly keep the department with
      section ``None`` (部長直轄).
    - Unknown roles fall back to 開発部 by the same title heuristic.
    """
    dev = department_by_id("development")
    dev_sections = {s.id: s for s in dev.sections}
    home = home_of_role(role)
    if home is not None and role not in _GENERALIST_DEV_ROLES:
        dept, section = home
        is_dev_chief = (
            dept.id == "development" and section is not None and section.chief_role == role
        )
        if not is_dev_chief:
            return dept, section
    lower = title.lower()
    if (kind and str(kind) in _VERIFY_KINDS) or any(h in lower for h in _VERIFY_HINTS):
        return dev, dev_sections["verification"]
    if any(h in lower for h in _DESIGN_HINTS):
        return dev, dev_sections["design"]
    return dev, dev_sections["build"]


def org_unit_ids_for_task(
    title: str, role: str, kind: str | None = None
) -> tuple[str, str | None]:
    dept, section = org_unit_for_task(title, role, kind)
    return dept.id, section.id if section else None


def unit_label(dept_id: str | None, section_id: str | None) -> str:
    """Human label for stored 部/課 ids (tolerates unknown / missing ids)."""
    if not dept_id:
        return "（所属未定）"
    try:
        dept = department_by_id(dept_id)
    except KeyError:
        return dept_id
    if not section_id:
        return f"{dept.name_ja}（部長直轄）"
    section = section_by_id(dept_id, section_id)
    return f"{dept.name_ja}／{section.name_ja if section else section_id}"


def validate_org() -> list[str]:
    """Return structural problems (empty list = healthy org chart)."""
    problems: list[str] = []
    seen_dept: set[str] = set()
    seen_heads: set[str] = set()
    for dept in DEPARTMENTS:
        if dept.id in seen_dept:
            problems.append(f"duplicate department id: {dept.id}")
        seen_dept.add(dept.id)
        if dept.head_role not in ROLES:
            problems.append(f"{dept.id}: head role not in ROLES: {dept.head_role}")
        if dept.head_role in seen_heads:
            problems.append(f"{dept.id}: head role heads two departments: {dept.head_role}")
        seen_heads.add(dept.head_role)
        if not dept.sections:
            problems.append(f"{dept.id}: department has no sections")
        for section in dept.sections:
            if not section.member_roles:
                problems.append(f"{dept.id}/{section.id}: section has no members")
            for role in section.member_roles:
                if role not in ROLES:
                    problems.append(f"{dept.id}/{section.id}: unknown role {role}")
            if section.chief_role and section.chief_role not in ROLES:
                problems.append(f"{dept.id}/{section.id}: unknown chief {section.chief_role}")
    homed = {
        role
        for dept in DEPARTMENTS
        for role in (
            dept.head_role,
            *(r for s in dept.sections for r in s.member_roles),
            *(s.chief_role for s in dept.sections if s.chief_role),
        )
    }
    for role_id in ROLES:
        if role_id == "human_owner":
            continue
        if role_id not in homed:
            problems.append(f"role without department: {role_id}")
    return problems


def org_tree_text() -> str:
    lines = ["【組織図】社長 → 秘書 → 部長（重役会議）→ 課 → 担当", ""]
    lines.append(f"社長: {format_role_label('human_owner')}")
    for dept in DEPARTMENTS:
        attend = "常任" if dept.always_attend else "案件に応じて招集"
        lines.append("")
        lines.append(f"■ {dept.name_ja}  部長: {format_role_label(dept.head_role)}  [{attend}]")
        lines.append(f"    使命: {dept.mission}")
        for section in dept.sections:
            chief = f"  課長: {format_role_label(section.chief_role)}" if section.chief_role else ""
            lines.append(f"    ├ {section.name_ja}{chief}")
            members = "、".join(format_role_label(r) for r in section.member_roles)
            lines.append(f"    │   担当: {members}")
            lines.append(f"    │   仕事: {section.handles}")
    problems = validate_org()
    if problems:
        lines.append("")
        lines.append("⚠ 組織図の不整合:")
        lines.extend(f"  - {p}" for p in problems)
    return "\n".join(lines)
