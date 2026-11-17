"""Executive meeting (重役会議): secretary summons department heads for a goal.

Flow (matches the company concept):

    社長がお願い → 秘書が部長を招集 → 重役会議 → 各部→課→担当に割り当て

The meeting is deterministic and side-effect-light:

- ``convene_executive_meeting`` is a pure function over a ``GoalPlan``.
  It decides attendees, each head's statement, the task→部/課 assignment,
  cross-department hand-offs, and open questions for the president.
- ``record_executive_meeting`` persists minutes as Markdown under
  ``<data_dir>/meetings/`` and writes an audit event.

It never spawns workers and never changes task routing; workers are still
dispatched only via Queue → Scheduler → Cost Governor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ai_company.config import AppConfig, load_config
from ai_company.org import (
    DEPARTMENTS,
    Department,
    Section,
    department_by_id,
    home_of_role,
    is_internal_goal,
    org_unit_for_task,
    summon_hits,
)
from ai_company.paths import meetings_dir
from ai_company.roles import format_role_label

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ai_company.services.department_orders import DeferredOrder, WorkOrder
    from ai_company.services.secretary import GoalPlan


@dataclass
class Assignment:
    task_title: str
    role: str
    department_id: str
    department_ja: str
    section_ja: str
    depends_on: list[str] = field(default_factory=list)
    # Stored 課 id (None = 部長直轄). This is the decision handed to create_task.
    section_id: str | None = None
    kind: str = "implementation"


@dataclass
class HeadStatement:
    department_id: str
    head_role: str
    statement: str
    commitments: list[str]
    concerns: list[str]


@dataclass
class MeetingMinutes:
    goal: str
    attendees: list[str]  # department ids
    absentees: dict[str, str]  # department id -> reason
    statements: list[HeadStatement]
    assignments: list[Assignment]
    handoffs: list[str]
    closure_reviews: list[str]
    decisions: list[str]
    open_questions: list[str]
    # 決議 → 部門作業指示（起票は department_orders.issue_work_orders）
    work_orders: list[WorkOrder] = field(default_factory=list)
    deferred_orders: list[DeferredOrder] = field(default_factory=list)
    project_id: str | None = None
    path: Path | None = None

    def attendee_labels(self) -> list[str]:
        return [department_by_id(d).name_ja for d in self.attendees]


# --- task → section --------------------------------------------------------


def section_for_task(
    title: str, role: str, kind: str | None = None
) -> tuple[Department, Section]:
    """Place a planned task in a 部/課 (same rule as the stored ``Task`` unit).

    The minutes table always needs a 課, so 部長直轄 work is shown under the
    department's first section here.
    """
    dept, section = org_unit_for_task(title, role, kind)
    return dept, section or dept.sections[0]


# --- attendance ------------------------------------------------------------


def select_attendees(
    goal: str, *, all_departments: bool = False
) -> tuple[list[str], dict[str, str]]:
    internal = is_internal_goal(goal)
    attendees: list[str] = []
    absentees: dict[str, str] = {}
    for dept in DEPARTMENTS:
        if all_departments or dept.always_attend:
            attendees.append(dept.id)
            continue
        if summon_hits(dept, goal):
            attendees.append(dept.id)
        elif internal and dept.weak_keywords and any(
            k in goal.lower() for k in dept.weak_keywords
        ):
            absentees[dept.id] = (
                "社内向けの依頼のため招集せず（完了前の自問ループでは点検する）"
            )
        else:
            absentees[dept.id] = "依頼文に該当論点なし（完了前の自問ループでは点検する）"
    return attendees, absentees


# --- statements ------------------------------------------------------------


def _statement_for(dept: Department, goal: str, assigned: list[Assignment]) -> HeadStatement:
    titles = [a.task_title for a in assigned]
    commitments: list[str] = []
    concerns: list[str] = []
    if dept.id == "secretariat":
        statement = "社長の依頼を各部に割り振り、進行と残件を管理します。"
        commitments = ["議事録と割り当てを記録し、社長には例外と最終判断だけを上げる"]
        concerns = ["部をまたぐ依存（設計→開発→検証→判定）の遅延"]
    elif dept.id == "development":
        statement = "設計課・開発課・検証課で、動く切片を先に出します。"
        by_section: dict[str, list[str]] = {}
        for a in assigned:
            by_section.setdefault(a.section_ja, []).append(a.task_title)
        commitments = [f"{sec}: {'、'.join(ts)}" for sec, ts in by_section.items()]
        concerns = ["実装者の自己申告では完了にしない（品質保証部の判定待ち）"]
    elif dept.id == "quality_assurance":
        statement = "実装とは独立した検証と判定で品質を担保します。"
        commitments = [
            "独立検証課: 各実装タスクにリスク別のレビューを付ける",
            "判定課: PASS 無しの DONE を認めない",
            "品質監査課: 完了前にテスト妥当性・品質・使い勝手を監査",
        ]
        concerns = ["テストが緑でも妥当とは限らない"]
    elif dept.id == "sales":
        statement = "誰に何を売るかを一文にし、デモと導入動線を用意します。"
        commitments = [
            "営業課: 提案・デモ手順・価格メモ",
            "カスタマーサクセス課: 導入・定着・問い合わせ動線",
        ]
        concerns = ["価格・契約条件は社長判断が必要"]
    elif dept.id == "marketing":
        statement = "利用者価値と優先機能・非目標を明確にします。"
        commitments = ["商品企画課: 価値適合・優先機能・非目標を定義"]
        concerns = ["スコープ膨張（作らないものを先に決める）"]
    elif dept.id == "legal":
        statement = "法令・契約・個人情報のリスクを先に洗い出します。"
        commitments = ["コンプライアンス課: 個人情報・労働法制・監査ログの最小点検"]
        concerns = ["最終的な法的判断は専門家・担当部署の確認が必要"]
    elif dept.id == "operations":
        statement = "運用できる・保守し続けられる状態を完了条件に含めます。"
        commitments = ["情報システム課: 権限・初期データ・運用手順", "保守課: 保守手順・資料"]
        concerns = ["『動くが運用不能』のまま出荷すること"]
    else:  # pragma: no cover - future departments
        statement = dept.mission
    if titles and dept.id not in {"development"}:
        commitments.append("今回の担当タスク: " + "、".join(titles))
    return HeadStatement(
        department_id=dept.id,
        head_role=dept.head_role,
        statement=statement,
        commitments=commitments,
        concerns=concerns,
    )


# --- main ------------------------------------------------------------------


def convene_executive_meeting(
    plan: GoalPlan,
    *,
    config: AppConfig | None = None,
    all_departments: bool = False,
) -> MeetingMinutes:
    from ai_company.services.closure_loops import _PHASE_ORDER
    from ai_company.services.department_orders import derive_work_orders

    cfg = config or load_config()
    attendees, absentees = select_attendees(plan.goal, all_departments=all_departments)

    assignments: list[Assignment] = []
    for t in plan.tasks:
        kind = str(getattr(t, "kind", None) or "implementation")
        dept, org_section = org_unit_for_task(t.title, t.role_required, kind)
        shown = org_section or dept.sections[0]  # 部長直轄 is shown under the first 課
        assignments.append(
            Assignment(
                task_title=t.title,
                role=t.role_required,
                department_id=dept.id,
                department_ja=dept.name_ja,
                section_ja=shown.name_ja,
                depends_on=list(t.depends_on_titles),
                section_id=org_section.id if org_section else None,
                kind=kind,
            )
        )
        # A department that owns work must be in the room.
        if dept.id not in attendees:
            attendees.append(dept.id)
            absentees.pop(dept.id, None)
    order = {d.id: i for i, d in enumerate(DEPARTMENTS)}
    attendees.sort(key=lambda d: order[d])

    statements = [
        _statement_for(
            department_by_id(d),
            plan.goal,
            [a for a in assignments if a.department_id == d],
        )
        for d in attendees
    ]

    # Hand-offs between sections (only where the owning section changes).
    by_title = {a.task_title: a for a in assignments}
    handoffs: list[str] = []
    for a in assignments:
        for dep in a.depends_on:
            src = by_title.get(dep)
            if src is None:
                continue
            src_unit = f"{src.department_ja}／{src.section_ja}"
            dst_unit = f"{a.department_ja}／{a.section_ja}"
            if src_unit != dst_unit:
                handoffs.append(f"{src_unit}「{src.task_title}」→ {dst_unit}「{a.task_title}」")
    handoffs.append("開発部の各成果 → 品質保証部／独立検証課 → 判定課（PASS で完了）")

    skip = set(getattr(cfg.closure_loops, "skip_phases", []) or [])
    closure_reviews: list[str] = []
    if getattr(cfg.closure_loops, "enabled", True):
        for key, _kind, role in _PHASE_ORDER:
            home = home_of_role(role)
            unit = (
                f"{home[0].name_ja}／{home[1].name_ja}" if home and home[1] else role
            )
            mark = "（設定で省略）" if key in skip else ""
            closure_reviews.append(f"{unit}: {format_role_label(role)}{mark}")

    decisions = [
        "秘書室が本議事録の割り当てどおりにタスクを起票する（Worker 起動は Scheduler 経由）",
        "開発部の成果は品質保証部の判定 PASS をもって完了とする",
        "実装キューが空になったら、各部の完了前自問ループを順に回してからリリース候補とする",
    ]
    work_orders, deferred_orders = derive_work_orders(
        plan.goal, attendees, plan.tasks, config=cfg
    )
    if work_orders:
        decisions.append(
            "招集した部に部門作業指示を起票する（"
            + "、".join(o.key for o in work_orders)
            + "）。成果は文書のみで、完了前の自問ループで点検する"
        )

    open_questions: list[str] = []
    if "sales" in attendees:
        open_questions.append("営業部: 価格・契約条件の方針（社長判断）")
    if "legal" in attendees:
        open_questions.append("法務部: 個人情報・法令の扱いの最終確認（専門家／担当部署）")
    if "operations" in attendees:
        open_questions.append("運用保守部: 本番環境・運用体制の有無（社長判断）")

    return MeetingMinutes(
        goal=plan.goal,
        attendees=attendees,
        absentees=absentees,
        statements=statements,
        assignments=assignments,
        handoffs=handoffs,
        closure_reviews=closure_reviews,
        decisions=decisions,
        open_questions=open_questions,
        work_orders=work_orders,
        deferred_orders=deferred_orders,
    )


def quote_goal_block(goal: str) -> list[str]:
    """Render the president's request as an inert, redacted fenced block.

    The goal is free text. Embedding it raw lets a line such as ``## 決定事項``
    forge sections of the minutes (L-1), and secrets typed into it would be
    stored in plain text (L-2). The fence is longer than any backtick run in
    the text, so the content cannot close it early.
    """
    from ai_company.services.redact import redact_text

    text = redact_text(goal or "").replace("\r\n", "\n").replace("\r", "\n")
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", *(text.split("\n") if text else [""]), fence]


def render_minutes_markdown(minutes: MeetingMinutes) -> str:
    lines = ["# 重役会議 議事録", ""]
    if minutes.project_id:
        lines.append(f"- 案件: `{minutes.project_id}`")
    lines.append(f"- 依頼者: {format_role_label('human_owner')}")
    lines.append(f"- 進行: {format_role_label('secretary')}")
    heads = "、".join(
        f"{department_by_id(d).name_ja}（{format_role_label(department_by_id(d).head_role)}）"
        for d in minutes.attendees
    )
    lines.append(f"- 出席: {heads}")
    if minutes.absentees:
        lines.append(
            "- 今回招集せず: "
            + "、".join(
                f"{department_by_id(d).name_ja}（{why}）" for d, why in minutes.absentees.items()
            )
        )
    lines += ["", "## 社長からの依頼", ""]
    lines += quote_goal_block(minutes.goal)
    lines += ["", "## 各部長の発言", ""]
    for st in minutes.statements:
        dept = department_by_id(st.department_id)
        lines.append(f"### {dept.name_ja} — {format_role_label(st.head_role)}")
        lines.append("")
        lines.append(st.statement)
        for c in st.commitments:
            lines.append(f"- 担当: {c}")
        for c in st.concerns:
            lines.append(f"- 懸念: {c}")
        lines.append("")
    lines += [
        "## 決定事項：割り当て",
        "",
        "| タスク | 部 | 課 | 担当ロール | 前提 |",
        "|---|---|---|---|---|",
    ]
    for a in minutes.assignments:
        dep = "、".join(a.depends_on) or "-"
        lines.append(
            f"| {a.task_title} | {a.department_ja} | {a.section_ja} | "
            f"{format_role_label(a.role)} | {dep} |"
        )
    lines += ["", "## 部門間の引き継ぎ", ""]
    lines += [f"- {h}" for h in minutes.handoffs]
    if minutes.closure_reviews:
        lines += ["", "## 完了前に各部が行う自問（順番）", ""]
        lines += [f"{i}. {c}" for i, c in enumerate(minutes.closure_reviews, 1)]
    lines += ["", "## 決定事項", ""]
    lines += [f"- {d}" for d in minutes.decisions]
    if minutes.work_orders or minutes.deferred_orders:
        lines += [
            "",
            "## 決定事項：部門別追加タスク",
            "",
            "| key | タスク | 部 | 課 | 担当ロール | 優先度 | 前提 |",
            "|---|---|---|---|---|---|---|",
        ]
        for o in minutes.work_orders:
            dept = department_by_id(o.department_id)
            sec = next((s for s in dept.sections if s.id == o.section_id), None)
            dep = "、".join([*o.depends_on_titles, *o.depends_on_keys]) or "-"
            lines.append(
                f"| {o.key} | {o.title} | {dept.name_ja} | "
                f"{sec.name_ja if sec else o.section_id} | "
                f"{format_role_label(o.role_required)} | {o.priority} | {dep} |"
            )
        if not minutes.work_orders:
            lines.append("| - | なし | - | - | - | - | - |")
        if minutes.deferred_orders:
            lines += ["", "### 見送り", ""]
            lines += [
                f"- {d.order.key}「{d.order.title}」: {d.reason}"
                for d in minutes.deferred_orders
            ]
    lines += ["", "## 社長への確認事項", ""]
    lines += [f"- {q}" for q in minutes.open_questions] or ["- なし"]
    return "\n".join(lines) + "\n"


def minutes_brief(minutes: MeetingMinutes) -> str:
    """Short on-screen summary for the CLI."""
    attend = "、".join(minutes.attendee_labels())
    lines = [f"【重役会議】出席: {attend}"]
    counts: dict[str, int] = {}
    for a in minutes.assignments:
        key = f"{a.department_ja}／{a.section_ja}"
        counts[key] = counts.get(key, 0) + 1
    lines.append("  割り当て: " + "、".join(f"{k} {v}件" for k, v in counts.items()))
    if minutes.work_orders:
        lines.append(
            "  部門別追加タスク: " + "、".join(o.title for o in minutes.work_orders)
        )
    if minutes.deferred_orders:
        lines.append(f"  見送り: {len(minutes.deferred_orders)}件（議事録参照）")
    if minutes.open_questions:
        lines.append("  社長への確認: " + " / ".join(minutes.open_questions))
    if minutes.path:
        lines.append(f"  議事録: {minutes.path}")
    return "\n".join(lines)


def record_executive_meeting(
    session: Session,
    minutes: MeetingMinutes,
    *,
    project_id: str,
) -> MeetingMinutes:
    from ai_company.services.audit import record_audit

    minutes.project_id = project_id
    out_dir = meetings_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{project_id}_kickoff.md"
    path.write_text(render_minutes_markdown(minutes), encoding="utf-8")
    minutes.path = path
    record_audit(
        session,
        event_type="executive_meeting_held",
        actor_type="secretary",
        actor_id="secretary",
        target_type="project",
        target_id=project_id,
        metadata={
            "attendees": minutes.attendees,
            "absentees": sorted(minutes.absentees),
            "assignment_count": len(minutes.assignments),
            "work_orders": [o.key for o in minutes.work_orders],
            "deferred_orders": [d.order.key for d in minutes.deferred_orders],
            "open_questions": minutes.open_questions,
            "minutes_path": str(path),
        },
    )
    return minutes
