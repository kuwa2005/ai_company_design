"""重役会議の決議 → 部門作業指示（Work Order）.

The executive meeting decides *which departments* attend. This module turns
that decision into a small, bounded set of department tasks
(``kind = department_work``) for the summoned departments only:

- ``derive_work_orders`` is a pure function (meeting attendees + plan →
  orders + deferred orders). It applies the de-duplication rule and the
  ``meeting_resolutions`` caps deterministically.
- ``issue_work_orders`` writes the tasks to the DB with explicit
  ``department_id`` / ``section_id`` and an idempotency key in
  ``constraints`` so re-running the meeting never duplicates work.

Boundaries (same as the rest of the meeting):

- Never spawns workers; tasks go through Queue → Scheduler → Cost Governor.
- ``department_work`` is a read-only kind (deliverable = the result JSON).
  It is not part of the code validation gate; the closure loops
  (sales / compliance / product_fit / it_ops audits) check it afterwards.
- Results of ``department_work`` never create child tasks (no chained growth).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ai_company.config import AppConfig, load_config
from ai_company.enums import TaskKind
from ai_company.org import DEPARTMENTS, department_by_id, org_unit_ids_for_task, section_by_id
from ai_company.services.redact import redact_text
from ai_company.services.result_parser import normalize_issues

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ai_company.db.models import Task
    from ai_company.services.secretary import TaskProposal


WORK_ORDER_KIND = TaskKind.DEPARTMENT_WORK.value


@dataclass(frozen=True)
class OrderTemplate:
    key: str
    department_id: str
    section_id: str
    role_required: str
    title: str
    objective: str
    acceptance_criteria: str
    priority: int
    # First planned task title found in the plan becomes a dependency.
    depends_on_plan_titles: tuple[str, ...] = ()
    # Other work-order keys this one waits for.
    depends_on_keys: tuple[str, ...] = ()


@dataclass
class WorkOrder:
    key: str
    department_id: str
    section_id: str
    role_required: str
    title: str
    objective: str
    acceptance_criteria: str
    priority: int
    depends_on_titles: list[str] = field(default_factory=list)
    depends_on_keys: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0


@dataclass
class DeferredOrder:
    order: WorkOrder
    reason: str


@dataclass
class IssueResult:
    issued: list[Task] = field(default_factory=list)
    skipped: list[DeferredOrder] = field(default_factory=list)


_CORE_IMPL_TITLES = ("コア実装", "API 実装", "Web UI 実装")
_DESIGN_TITLES = ("アーキテクチャ／境界整理", "データモデル設計", "Goal分解とスコープ定義")

DOC_RULE = (
    "成果物は文書のみ。コードやリポジトリは変更しない。"
    "結果 JSON の summary に要点、issues に未確定事項を書く。"
)

# v1 deterministic templates. Order inside a department = issue order.
ORDER_TEMPLATES: tuple[OrderTemplate, ...] = (
    OrderTemplate(
        key="sales.proposal",
        department_id="sales",
        section_id="sales_proposal",
        role_required="sales",
        title="提案資料・デモ手順・価格メモ（案）",
        objective=(
            "誰に何を売るかを一文にし、提案の骨子・デモ手順・価格の考え方（案）をまとめよ。"
        ),
        acceptance_criteria="対象顧客・提供価値・デモ手順・価格メモ（社長判断事項を明記）がある",
        priority=55,
        depends_on_plan_titles=_CORE_IMPL_TITLES,
    ),
    OrderTemplate(
        key="sales.onboarding",
        department_id="sales",
        section_id="customer_success",
        role_required="customer_success",
        title="導入・問い合わせ動線メモ",
        objective="導入手順・初期設定・問い合わせ窓口と一次回答の動線をまとめよ。",
        acceptance_criteria="導入手順・問い合わせ動線・よくある質問の初版がある",
        priority=50,
        depends_on_keys=("sales.proposal",),
    ),
    OrderTemplate(
        key="marketing.value",
        department_id="marketing",
        section_id="product_planning",
        role_required="product_planner",
        title="価値仮説・優先機能・非目標",
        objective="利用者価値の仮説、優先する機能、今回は作らないもの（非目標）を定義せよ。",
        acceptance_criteria="価値仮説・優先機能（順位つき）・非目標が列挙されている",
        priority=75,
    ),
    OrderTemplate(
        key="legal.precheck",
        department_id="legal",
        section_id="compliance",
        role_required="compliance_auditor",
        title="法務事前点検（個人情報・労働法制・監査ログ）",
        objective=(
            "個人情報・労働法制・監査ログの観点で、設計前に押さえるべき論点を洗い出せ。"
            "最終的な法的判断は専門家・担当部署の確認事項として残すこと。"
        ),
        acceptance_criteria="論点一覧と、専門家・担当部署に確認すべき事項が分かれている",
        priority=80,
    ),
    OrderTemplate(
        key="operations.premise",
        department_id="operations",
        section_id="it_systems",
        role_required="it_operations",
        title="運用前提・権限・初期データ方針",
        objective="運用体制の前提、権限設計の方針、初期データ（シード）の扱いをまとめよ。",
        acceptance_criteria="運用前提・権限方針・初期データ方針と、社長判断事項がある",
        priority=60,
        depends_on_plan_titles=_DESIGN_TITLES,
    ),
)


def _dept_order() -> dict[str, int]:
    return {d.id: i for i, d in enumerate(DEPARTMENTS)}


def validate_work_order(order: WorkOrder) -> list[str]:
    """Structural problems of a work order (empty list = valid)."""
    problems: list[str] = []
    try:
        department_by_id(order.department_id)
    except KeyError:
        return [f"unknown_department:{order.department_id}"]
    prefix = order.key.split(".", 1)[0]
    if "." not in order.key or prefix != order.department_id:
        problems.append(f"key_prefix_mismatch:{order.key}")
    section = section_by_id(order.department_id, order.section_id)
    if section is None:
        problems.append(f"unknown_section:{order.department_id}/{order.section_id}")
    elif order.role_required not in section.member_roles:
        problems.append(f"role_not_in_section:{order.role_required}")
    if not order.title.strip():
        problems.append("empty_title")
    return problems


def _from_template(
    tpl: OrderTemplate, goal: str, plan_titles: list[str], cfg: AppConfig
) -> WorkOrder:
    deps = [t for t in tpl.depends_on_plan_titles if t in plan_titles][:1]
    ceiling = cfg.settings.meeting_resolutions.priority_ceiling
    return WorkOrder(
        key=tpl.key,
        department_id=tpl.department_id,
        section_id=tpl.section_id,
        role_required=tpl.role_required,
        title=tpl.title,
        objective=f"Goal「{goal}」について: {tpl.objective}\n{DOC_RULE}",
        acceptance_criteria=tpl.acceptance_criteria,
        priority=min(tpl.priority, ceiling),
        depends_on_titles=deps,
        depends_on_keys=list(tpl.depends_on_keys),
        estimated_cost=cfg.default_estimated_cost,
    )


def derive_work_orders(
    goal: str,
    attendees: list[str],
    plan_tasks: list[TaskProposal],
    *,
    config: AppConfig | None = None,
) -> tuple[list[WorkOrder], list[DeferredOrder]]:
    """Meeting resolution → work orders for summoned departments (pure).

    Deterministic order: department order (``DEPARTMENTS``) then template order.
    Orders over the caps are returned as deferred with a reason.
    """
    cfg = config or load_config()
    res = cfg.settings.meeting_resolutions
    if not res.enabled:
        return [], []

    allowed = set(res.departments) & set(attendees)
    plan_titles = [t.title for t in plan_tasks]
    planned_units = {
        org_unit_ids_for_task(t.title, t.role_required) for t in plan_tasks
    }
    order_idx = _dept_order()
    templates = sorted(
        (t for t in ORDER_TEMPLATES if t.department_id in allowed),
        key=lambda t: order_idx.get(t.department_id, len(order_idx)),
    )

    orders: list[WorkOrder] = []
    deferred: list[DeferredOrder] = []
    per_dept: dict[str, int] = {}
    total_cost = 0.0
    for tpl in templates:
        order = _from_template(tpl, goal, plan_titles, cfg)
        if (tpl.department_id, tpl.section_id) in planned_units:
            deferred.append(DeferredOrder(order, "計画に同じ部/課のタスクがあるため起票しない"))
            continue
        problems = validate_work_order(order)
        if problems:
            deferred.append(DeferredOrder(order, "不正な指示: " + ",".join(problems)))
            continue
        if len(orders) >= res.max_orders_per_meeting:
            deferred.append(
                DeferredOrder(order, f"上限: 1会議あたり {res.max_orders_per_meeting} 件")
            )
            continue
        if per_dept.get(tpl.department_id, 0) >= res.max_orders_per_department:
            deferred.append(
                DeferredOrder(order, f"上限: 1部あたり {res.max_orders_per_department} 件")
            )
            continue
        cost_cap = res.max_estimated_cost_per_meeting_usd
        if cost_cap > 0 and total_cost + order.estimated_cost > cost_cap:
            deferred.append(DeferredOrder(order, f"上限: 見積コスト ${cost_cap:.2f}/会議"))
            continue
        orders.append(order)
        per_dept[tpl.department_id] = per_dept.get(tpl.department_id, 0) + 1
        total_cost += order.estimated_cost

    # A dependency on a deferred order is dropped (the order still runs).
    issued_keys = {o.key for o in orders}
    orders = [
        replace(o, depends_on_keys=[k for k in o.depends_on_keys if k in issued_keys])
        for o in orders
    ]
    return orders, deferred


# --- DB --------------------------------------------------------------------


def _constraints_of(task: Task) -> dict[str, Any]:
    raw = task.constraints
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def meeting_tag(project_id: str) -> str:
    return f"{project_id}_kickoff"


def existing_order_tasks(session: Session, project_id: str) -> dict[str, Task]:
    """work_order_key → task for orders already issued in this project."""
    from ai_company.db.models import Task

    found: dict[str, Task] = {}
    rows = session.scalars(
        select(Task)
        .where(Task.project_id == project_id, Task.kind == WORK_ORDER_KIND)
        .order_by(Task.created_at.asc())
    ).all()
    for task in rows:
        key = _constraints_of(task).get("work_order_key")
        if isinstance(key, str) and key not in found:
            found[key] = task
    return found


def issue_work_orders(
    session: Session,
    *,
    project_id: str,
    orders: list[WorkOrder],
    deferred: list[DeferredOrder] | None = None,
    config: AppConfig | None = None,
) -> IssueResult:
    """Create ``department_work`` tasks (idempotent per project + key)."""
    from ai_company.db.models import Task
    from ai_company.services.audit import record_audit
    from ai_company.services.tasks import create_task

    cfg = config or load_config()
    tag = meeting_tag(project_id)
    result = IssueResult()
    key_to_task = existing_order_tasks(session, project_id)

    for order in orders:
        problems = validate_work_order(order)
        if problems:
            result.skipped.append(DeferredOrder(order, "不正な指示: " + ",".join(problems)))
            continue
        if order.key in key_to_task:
            result.skipped.append(DeferredOrder(order, "起票済み（冪等）"))
            continue
        depends: list[str] = []
        for title in order.depends_on_titles:
            dep = session.scalar(
                select(Task)
                .where(Task.project_id == project_id, Task.title == title)
                .order_by(Task.created_at.asc())
                .limit(1)
            )
            if dep is not None:
                depends.append(dep.id)
        for key in order.depends_on_keys:
            dep_task = key_to_task.get(key)
            if dep_task is not None:
                depends.append(dep_task.id)
        task = create_task(
            session,
            title=order.title,
            priority=min(order.priority, cfg.settings.meeting_resolutions.priority_ceiling),
            project_id=project_id,
            objective=order.objective,
            role_required=order.role_required,
            estimated_cost=order.estimated_cost,
            depends_on=depends or None,
            kind=WORK_ORDER_KIND,
            risk_level="low",
            acceptance_criteria=order.acceptance_criteria,
            department_id=order.department_id,
            section_id=order.section_id,
            config=cfg,
        )
        task.constraints = json.dumps(
            {"work_order_key": order.key, "meeting": tag}, ensure_ascii=False
        )
        session.flush()
        key_to_task[order.key] = task
        result.issued.append(task)
        record_audit(
            session,
            event_type="meeting_work_order_issued",
            actor_type="secretary",
            actor_id="secretary",
            target_type="task",
            target_id=task.id,
            metadata={
                "key": order.key,
                "meeting": tag,
                "department_id": order.department_id,
                "section_id": order.section_id,
                "role_required": order.role_required,
                "depends_on": depends,
            },
        )

    for d in [*(deferred or []), *result.skipped]:
        record_audit(
            session,
            event_type="meeting_work_order_deferred",
            actor_type="secretary",
            actor_id="secretary",
            target_type="project",
            target_id=project_id,
            metadata={
                "key": d.order.key,
                "meeting": tag,
                "department_id": d.order.department_id,
                "reason": d.reason,
            },
        )
    return result


def record_department_work_result(task: Task, payload: dict[str, Any]) -> None:
    """Keep a short trace of the deliverable; ``proposed_tasks`` are ignored."""
    data = _constraints_of(task)
    # Stored redacted (L-2); readers still redact again on output.
    summary = redact_text(str(payload.get("summary") or ""))[:2000]
    issues = normalize_issues(payload.get("issues"), max_items=10)
    data["work_order_summary"] = summary
    data["work_order_issues"] = [redact_text(i)[:300] for i in issues]
    if payload.get("proposed_tasks"):
        data["work_order_ignored_proposals"] = len(payload.get("proposed_tasks") or [])
    task.constraints = json.dumps(data, ensure_ascii=False)
