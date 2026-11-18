"""部長所見（head_brief）: department heads as short-lived, read-only workers.

On by default since 2026-11-18 (``settings.executive_meeting.head_llm``).

Flow when enabled::

    start_goal → 決定論的な重役会議（テンプレート）
      → 対象部ごとに head_brief タスクを起票し、その部のテンプレート Work Order を保留
      → Queue → Scheduler（lease・role_capacity・policy）→ Cost Governor → 部長 Worker
      → 部長の JSON（HeadBrief）→ parse → validate_head_brief（秘書側の厳格検証）
      → issue_work_orders（会議テンプレートと同じ上限・冪等 key）
    失敗・解析不能・全件却下・リトライ上限・停止 → 保留テンプレートを起票（fallback）

Invariants:

- The meeting / secretary never calls an LLM or starts a worker directly.
- Heads are read-only (``head_brief`` is a read-only kind) and are not in any
  worker's ``reports_to`` chain; a failed brief never escalates to a manager.
- A head can only file work for its own department, inside its own sections,
  with roles of that section, under the same ``meeting_resolutions`` caps.
  Requests to other departments are text only (``handoff_requests``).
- The mock runner answers with the template orders, so "on + mock" files the
  same work orders (keys / 部 / 課) as "off".
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ai_company.config import AppConfig, load_config
from ai_company.enums import TaskKind, TaskStatus
from ai_company.org import DEPARTMENTS, department_by_id, org_unit_ids_for_task, section_by_id
from ai_company.paths import meetings_dir
from ai_company.roles import format_role_label
from ai_company.services.redact import redact_obj as _redact_obj
from ai_company.services.redact import redact_text as _redact_text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ai_company.db.models import Task
    from ai_company.services.department_orders import DeferredOrder, WorkOrder
    from ai_company.services.executive_meeting import MeetingMinutes


HEAD_BRIEF_KIND = TaskKind.HEAD_BRIEF.value
HEAD_BRIEF_PRIORITY = 98  # 部長所見は計画タスクより先に動かす（並行で実行される）
_STALLED = {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.DEFERRED_BUDGET}
# Not started yet. If the scheduler keeps denying / throttling the head
# (no permission profile, cost DENY, role capacity…) the brief would sit here
# forever and the closure loops would wait on it forever.
_WAITING = {TaskStatus.READY, TaskStatus.WAITING, TaskStatus.BACKLOG}


@dataclass
class HeadBriefIssue:
    tasks: list[Task] = field(default_factory=list)
    # department id → template orders held until the head answers
    held: dict[str, list[WorkOrder]] = field(default_factory=dict)
    # department id → reason it was not briefed (its templates are issued now)
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass
class HeadBriefDecision:
    orders: list[WorkOrder] = field(default_factory=list)
    rejected: list[DeferredOrder] = field(default_factory=list)
    fatal: str | None = None  # whole brief rejected → fallback


# --- helpers ---------------------------------------------------------------


def head_llm_enabled(cfg: AppConfig) -> bool:
    return bool(cfg.settings.executive_meeting.head_llm.enabled)


def is_head_brief_task(task: Task) -> bool:
    return str(task.kind) == HEAD_BRIEF_KIND


def _constraints(task: Task) -> dict[str, Any]:
    raw = task.constraints
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _set_constraints(task: Task, data: dict[str, Any]) -> None:
    task.constraints = json.dumps(data, ensure_ascii=False)


def head_role_dispatchable(cfg: AppConfig, role: str) -> tuple[bool, str]:
    """Would the scheduler's policy check allow this head to run a brief?

    Checked *before* filing, so a head without a permission profile never
    leaves a task that the scheduler denies forever.
    """
    from ai_company.db.models import Task
    from ai_company.services.policy_engine import evaluate_dispatch

    probe = Task(id="PROBE", title="probe", kind=HEAD_BRIEF_KIND, role_required=role)
    decision = evaluate_dispatch(task=probe, role=role, config=cfg)
    return decision.allowed, decision.reason


def _order_from_dict(data: dict[str, Any]) -> WorkOrder:
    from ai_company.services.department_orders import WorkOrder

    return WorkOrder(
        key=str(data["key"]),
        department_id=str(data["department_id"]),
        section_id=str(data["section_id"]),
        role_required=str(data["role_required"]),
        title=str(data["title"]),
        objective=str(data.get("objective") or ""),
        acceptance_criteria=str(data.get("acceptance_criteria") or ""),
        priority=int(data.get("priority") or 50),
        depends_on_titles=[str(t) for t in data.get("depends_on_titles") or []],
        depends_on_keys=[str(k) for k in data.get("depends_on_keys") or []],
        estimated_cost=float(data.get("estimated_cost") or 0.0),
    )


def held_orders(task: Task) -> list[WorkOrder]:
    return [
        _order_from_dict(d)
        for d in _constraints(task).get("held_orders") or []
        if isinstance(d, dict)
    ]


def _brief_tasks(session: Session, project_id: str) -> list[Task]:
    from ai_company.db.models import Task

    return list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.kind == HEAD_BRIEF_KIND)
            .order_by(Task.created_at.asc())
        ).all()
    )


def open_head_briefs(session: Session, project_id: str) -> list[Task]:
    done = {TaskStatus.DONE, TaskStatus.CANCELLED}
    return [t for t in _brief_tasks(session, project_id) if t.status not in done]


# --- filing ---------------------------------------------------------------


def plan_head_briefs(
    minutes: MeetingMinutes, cfg: AppConfig
) -> tuple[list[str], dict[str, str]]:
    """Which summoned departments get a head brief (pure, deterministic)."""
    head = cfg.settings.executive_meeting.head_llm
    if not head.enabled:
        return [], {}
    wanted = set(head.departments) & set(minutes.attendees)
    briefed: list[str] = []
    skipped: dict[str, str] = {}
    for dept in DEPARTMENTS:  # deterministic order
        if dept.id not in wanted:
            continue
        ok, reason = head_role_dispatchable(cfg, dept.head_role)
        if not ok:
            skipped[dept.id] = f"部長を起動できない（{reason}）→ テンプレートで起票"
            continue
        if len(briefed) >= head.max_heads_per_meeting:
            skipped[dept.id] = f"上限: 1会議あたり部長 {head.max_heads_per_meeting} 名"
            continue
        briefed.append(dept.id)
    return briefed, skipped


def issue_head_briefs(
    session: Session,
    *,
    project_id: str,
    minutes: MeetingMinutes,
    config: AppConfig | None = None,
) -> HeadBriefIssue:
    """File one ``head_brief`` task per briefed department (idempotent).

    The department's template work orders are held in the task's
    ``constraints`` so that fallback can file exactly what "off" would have.
    """
    cfg = config or load_config()
    briefed, skipped = plan_head_briefs(minutes, cfg)
    held = {
        d: [o for o in minutes.work_orders if o.department_id == d] for d in briefed
    }
    return _file_head_briefs(
        session,
        project_id=project_id,
        goal=minutes.goal,
        briefed=briefed,
        skipped=skipped,
        held=held,
        config=cfg,
    )


def issue_head_briefs_for_added(
    session: Session,
    *,
    project_id: str,
    goal: str,
    departments: list[str],
    orders: list[WorkOrder],
    config: AppConfig | None = None,
) -> HeadBriefIssue:
    """Head briefs for departments summoned *after* the meeting (招集見直し).

    Same rules as the meeting: only ``head_llm.departments``, only heads the
    scheduler would dispatch, and ``max_heads_per_meeting`` counts the briefs
    already filed for this project.
    """
    cfg = config or load_config()
    head = cfg.settings.executive_meeting.head_llm
    if not head.enabled:
        return HeadBriefIssue()
    already = {
        _constraints(t).get("brief_for") for t in _brief_tasks(session, project_id)
    }
    room = max(0, head.max_heads_per_meeting - len(already))
    briefed: list[str] = []
    skipped: dict[str, str] = {}
    for dept in DEPARTMENTS:
        if dept.id not in departments or dept.id not in head.departments:
            continue
        if dept.id in already:
            continue
        ok, reason = head_role_dispatchable(cfg, dept.head_role)
        if not ok:
            skipped[dept.id] = f"部長を起動できない（{reason}）→ テンプレートで起票"
            continue
        if len(briefed) >= room:
            skipped[dept.id] = f"上限: 1会議あたり部長 {head.max_heads_per_meeting} 名"
            continue
        briefed.append(dept.id)
    held = {d: [o for o in orders if o.department_id == d] for d in briefed}
    return _file_head_briefs(
        session,
        project_id=project_id,
        goal=goal,
        briefed=briefed,
        skipped=skipped,
        held=held,
        config=cfg,
    )


def _file_head_briefs(
    session: Session,
    *,
    project_id: str,
    goal: str,
    briefed: list[str],
    skipped: dict[str, str],
    held: dict[str, list[WorkOrder]],
    config: AppConfig,
) -> HeadBriefIssue:
    from ai_company.services.audit import record_audit
    from ai_company.services.department_orders import meeting_tag
    from ai_company.services.tasks import create_task

    cfg = config
    head = cfg.settings.executive_meeting.head_llm
    result = HeadBriefIssue()
    result.skipped = dict(skipped)
    tag = meeting_tag(project_id)
    existing = {
        _constraints(t).get("brief_for"): t for t in _brief_tasks(session, project_id)
    }
    for dept_id in briefed:
        dept = department_by_id(dept_id)
        dept_held = held.get(dept_id, [])
        result.held[dept_id] = dept_held
        if dept_id in existing:
            continue
        task = create_task(
            session,
            title=f"部長所見: {dept.name_ja}",
            priority=HEAD_BRIEF_PRIORITY,
            project_id=project_id,
            objective=(
                f"Goal「{goal}」について、{dept.name_ja}の部長として"
                "自部門が今回やるべき作業（最大2件）と懸念・社長への確認事項を判断せよ。"
            ),
            role_required=dept.head_role,
            estimated_cost=head.estimated_cost_per_head,
            max_retries=head.max_retries,
            kind=HEAD_BRIEF_KIND,
            risk_level="low",
            acceptance_criteria="HeadBrief JSON（自部門の課・担当のみ）を返す",
            department_id=dept_id,
            section_id=None,
            config=cfg,
        )
        _set_constraints(
            task,
            {
                "meeting": tag,
                "brief_for": dept_id,
                "goal": goal,
                "held_orders": [asdict(o) for o in dept_held],
            },
        )
        session.flush()
        result.tasks.append(task)
        record_audit(
            session,
            event_type="head_brief_requested",
            actor_type="secretary",
            actor_id="secretary",
            target_type="task",
            target_id=task.id,
            metadata={
                "meeting": tag,
                "department_id": dept_id,
                "head_role": dept.head_role,
                "held_orders": [o.key for o in dept_held],
            },
        )
    for dept_id, reason in skipped.items():
        record_audit(
            session,
            event_type="head_brief_skipped",
            actor_type="secretary",
            actor_id="secretary",
            target_type="project",
            target_id=project_id,
            metadata={"meeting": tag, "department_id": dept_id, "reason": reason},
        )
    return result


# --- mock -----------------------------------------------------------------


def build_mock_head_brief(task: Task) -> dict[str, Any]:
    """Deterministic brief = the template statement + the held template orders."""
    from ai_company.services.executive_meeting import _statement_for

    dept_id = str(task.department_id or _constraints(task).get("brief_for") or "")
    dept = department_by_id(dept_id)
    goal = str(_constraints(task).get("goal") or "")
    st = _statement_for(dept, goal, [])
    held = held_orders(task)
    title_of = {o.key: o.title for o in held}
    orders = [
        {
            "key": o.key,
            "section_id": o.section_id,
            "role_required": o.role_required,
            "title": o.title,
            "objective": o.objective,
            "acceptance_criteria": o.acceptance_criteria,
            "priority": o.priority,
            "depends_on_titles": [
                *o.depends_on_titles,
                *(title_of[k] for k in o.depends_on_keys if k in title_of),
            ],
        }
        for o in held
    ]
    return {
        "status": "completed",
        "department_id": dept_id,
        "statement": st.statement,
        "commitments": st.commitments[:5],
        "concerns": st.concerns[:5],
        "open_questions": [],
        "handoff_requests": [],
        "work_orders": orders,
        "confidence": 0.9,
        "mock": True,
        "task_id": task.id,
    }


# --- validation ------------------------------------------------------------


def _planned_units(session: Session, project_id: str) -> set[tuple[str, str | None]]:
    from ai_company.db.models import Task

    rows = session.scalars(
        select(Task).where(
            Task.project_id == project_id,
            Task.kind.not_in((HEAD_BRIEF_KIND, TaskKind.DEPARTMENT_WORK.value)),
        )
    ).all()
    units: set[tuple[str, str | None]] = set()
    for t in rows:
        if t.department_id:
            units.add((t.department_id, t.section_id))
        else:
            units.add(org_unit_ids_for_task(t.title, t.role_required, t.kind))
    return units


def validate_head_brief(
    session: Session,
    *,
    task: Task,
    brief: dict[str, Any],
    config: AppConfig | None = None,
) -> HeadBriefDecision:
    """Secretary-side checks, stricter than the JSON schema.

    ``brief`` must already be normalized by ``parse_head_brief_result``.
    """
    from ai_company.db.models import Task as TaskModel
    from ai_company.services.department_orders import (
        DOC_RULE,
        DeferredOrder,
        WorkOrder,
        existing_order_tasks,
        validate_work_order,
    )

    cfg = config or load_config()
    res = cfg.settings.meeting_resolutions
    decision = HeadBriefDecision()
    dept_id = str(task.department_id or "")
    if brief.get("department_id") != dept_id:
        decision.fatal = f"department_mismatch:{brief.get('department_id')}!={dept_id}"
        return decision
    if brief.get("status") == "declined":
        return decision  # the head's judgement: nothing to add

    project_id = task.project_id
    titles_in_project = set(
        session.scalars(select(TaskModel.title).where(TaskModel.project_id == project_id)).all()
    )
    existing = existing_order_tasks(session, project_id)
    already_issued = len(existing)
    already_cost = sum(float(t.estimated_cost or 0.0) for t in existing.values())
    dept_issued = sum(1 for t in existing.values() if t.department_id == dept_id)
    planned = _planned_units(session, project_id)
    accepted_titles: list[str] = []
    raw_orders = list(brief.get("work_orders") or [])

    for raw in raw_orders:
        order = WorkOrder(
            key=str(raw["key"]),
            department_id=dept_id,
            section_id=str(raw["section_id"]),
            role_required=str(raw["role_required"]),
            title=str(raw["title"]),
            # Same document-only rule as template orders (L-3).
            objective=f"{raw['objective']}\n{DOC_RULE}",
            acceptance_criteria=str(raw["acceptance_criteria"]),
            priority=min(int(raw.get("priority") or 50), res.priority_ceiling),
            depends_on_titles=[
                t
                for t in raw.get("depends_on_titles") or []
                if t in titles_in_project or t in accepted_titles
            ],
            estimated_cost=cfg.default_estimated_cost,
        )

        def reject(reason: str, order: WorkOrder = order) -> None:
            decision.rejected.append(DeferredOrder(order, f"部長指示を却下: {reason}"))

        if order.key.split(".", 1)[0] != dept_id:
            reject(f"他部のkey（{order.key}）")
            continue
        if section_by_id(dept_id, order.section_id) is None:
            reject(f"自部門の課ではない（{order.section_id}）")
            continue
        problems = validate_work_order(order)
        if problems:
            reject(",".join(problems))
            continue
        if (dept_id, order.section_id) in planned:
            reject("計画に同じ部/課のタスクがある")
            continue
        if order.key in existing:
            reject("起票済み（冪等）")
            continue
        n_new = len(decision.orders)
        if already_issued + n_new >= res.max_orders_per_meeting:
            reject(f"上限: 1会議あたり {res.max_orders_per_meeting} 件")
            continue
        if dept_issued + n_new >= res.max_orders_per_department:
            reject(f"上限: 1部あたり {res.max_orders_per_department} 件")
            continue
        new_cost = sum(o.estimated_cost for o in decision.orders) + order.estimated_cost
        cap = res.max_estimated_cost_per_meeting_usd
        if cap > 0 and already_cost + new_cost > cap:
            reject(f"上限: 見積コスト ${cap:.2f}/会議")
            continue
        decision.orders.append(order)
        accepted_titles.append(order.title)

    if raw_orders and not decision.orders:
        # Every instruction was invalid → trust nothing, file the template instead.
        # Cap / idempotency rejections are not "invalid": the fallback would file
        # the held template *without* the caps and exceed them (H-1). Such a
        # brief simply ends as 「上限で見送り」.
        if all(not _is_non_fatal_rejection(r.reason) for r in decision.rejected):
            decision.fatal = "all_work_orders_rejected"
    return decision


_NON_FATAL_REJECTIONS = ("起票済み（冪等）", "上限: ")


def _is_non_fatal_rejection(reason: str) -> bool:
    body = reason.removeprefix("部長指示を却下: ")
    return any(body.startswith(m) for m in _NON_FATAL_REJECTIONS)


# --- apply / fallback ------------------------------------------------------


_MD_SPECIAL = re.compile(r"([\\`*_#|<>\[\]~])")


def _md_inline(value: object) -> str:
    """LLM text → one inert Markdown line: redacted, newlines collapsed, specials escaped.

    Head output is untrusted; without this a statement such as
    ``"x\\n## 決定事項"`` forges sections of the minutes and secrets are stored
    in plain text (M-1, same policy as ``executive_meeting.quote_goal_block``).
    """
    from ai_company.services.redact import redact_text, single_line

    text = single_line(redact_text(str(value if value is not None else "")))
    return _MD_SPECIAL.sub(r"\\\1", text)


def _append_heads_minutes(
    task: Task, brief: dict[str, Any], decision: HeadBriefDecision, *, fallback: str | None
) -> None:
    dept = department_by_id(str(task.department_id))
    path = meetings_dir() / f"{task.project_id}_kickoff_heads.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if not path.exists():
        lines += ["# 重役会議 部長所見（追記）", ""]
    lines += [f"## {dept.name_ja} — {format_role_label(dept.head_role)}", ""]
    if fallback:
        lines += [f"- 所見は採用せず、会議テンプレートで起票（理由: {_md_inline(fallback)}）", ""]
    else:
        known = {d.id: d.name_ja for d in DEPARTMENTS}
        # fixed prefix: a statement starting with "- 起票:" must not look like a real line
        lines += [f"- 発言: {_md_inline(brief.get('statement') or '（発言なし）')}", ""]
        lines += [f"- 担当: {_md_inline(c)}" for c in brief.get("commitments") or []]
        lines += [f"- 懸念: {_md_inline(c)}" for c in brief.get("concerns") or []]
        for q in brief.get("open_questions") or []:
            who = "社長" if q.get("decider") == "president" else "専門家／担当部署"
            lines.append(f"- 確認事項（{who}）: {_md_inline(q.get('question'))}")
        for h in brief.get("handoff_requests") or []:
            to = known.get(str(h.get("to_department_id")))
            if to is None:  # parser already drops these; never echo an unknown id
                continue
            lines.append(f"- 他部への依頼（{to}）: {_md_inline(h.get('request'))}")
        lines += [
            f"- 起票: {_md_inline(o.key)}「{_md_inline(o.title)}」" for o in decision.orders
        ]
        lines += [
            f"- 見送り: {_md_inline(r.order.key)}（{_md_inline(r.reason)}）"
            for r in decision.rejected
        ]
        lines.append("")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def apply_head_brief(
    session: Session,
    *,
    task: Task,
    payload: dict[str, Any],
    config: AppConfig | None = None,
) -> HeadBriefDecision:
    """Validate a finished brief and file its work orders (or fall back)."""
    from ai_company.services.audit import record_audit
    from ai_company.services.department_orders import issue_work_orders
    from ai_company.services.result_parser import parse_head_brief_result

    cfg = config or load_config()
    data = _constraints(task)
    if data.get("brief_applied") or data.get("fallback_applied"):
        return HeadBriefDecision()  # idempotent

    parsed = parse_head_brief_result(json.dumps(payload, ensure_ascii=False))
    if not parsed.ok or parsed.result is None:
        fallback_head_brief(session, task=task, reason=f"parse:{parsed.error}", config=cfg)
        return HeadBriefDecision(fatal=f"parse:{parsed.error}")
    brief = parsed.result
    decision = validate_head_brief(session, task=task, brief=brief, config=cfg)
    if decision.fatal:
        fallback_head_brief(session, task=task, reason=decision.fatal, config=cfg)
        return decision

    issued = issue_work_orders(
        session,
        project_id=task.project_id,
        orders=decision.orders,
        deferred=decision.rejected,
        config=cfg,
    )
    data = _constraints(task)
    data.update(
        {
            "brief_applied": True,
            # Head output is stored redacted, same policy as the minutes (L-2).
            "head_statement": _redact_text(str(brief.get("statement") or ""))[:400],
            "head_open_questions": _redact_obj(brief.get("open_questions") or []),
            "head_handoff_requests": _redact_obj(brief.get("handoff_requests") or []),
            "head_issued": [o.key for o in decision.orders],
            "head_rejected": [r.order.key for r in decision.rejected],
        }
    )
    _set_constraints(task, data)
    session.flush()
    _append_heads_minutes(task, brief, decision, fallback=None)
    record_audit(
        session,
        event_type="head_brief_applied",
        actor_type="secretary",
        actor_id="secretary",
        target_type="task",
        target_id=task.id,
        metadata={
            "department_id": task.department_id,
            "issued": [t.id for t in issued.issued],
            "keys": [o.key for o in decision.orders],
            "rejected": [r.order.key for r in decision.rejected],
            "confidence": brief.get("confidence"),
        },
    )
    return decision


def fallback_head_brief(
    session: Session,
    *,
    task: Task,
    reason: str,
    config: AppConfig | None = None,
) -> list[Task]:
    """File the held template orders for this department (idempotent)."""
    from ai_company.services.audit import record_audit
    from ai_company.services.department_orders import issue_work_orders

    cfg = config or load_config()
    data = _constraints(task)
    if data.get("brief_applied") or data.get("fallback_applied"):
        return []
    issued: list[Task] = []
    if cfg.settings.executive_meeting.head_llm.fallback == "template":
        issued = issue_work_orders(
            session, project_id=task.project_id, orders=held_orders(task), config=cfg
        ).issued
    # Reasons can carry LLM values (parse errors etc.): redact before storing (L-1).
    reason = _redact_text(reason)[:300]
    data["fallback_applied"] = True
    data["fallback_reason"] = reason
    _set_constraints(task, data)
    session.flush()
    _append_heads_minutes(task, {}, HeadBriefDecision(), fallback=reason)
    record_audit(
        session,
        event_type="head_brief_fallback",
        actor_type="secretary",
        actor_id="secretary",
        target_type="task",
        target_id=task.id,
        metadata={
            "department_id": task.department_id,
            "reason": reason,
            "mode": cfg.settings.executive_meeting.head_llm.fallback,
            "issued": [t.id for t in issued],
        },
    )
    return issued


def close_failed_head_brief(
    session: Session, *, task: Task, reason: str, config: AppConfig | None = None
) -> None:
    """Retries exhausted / stalled: fall back and close without escalation."""
    fallback_head_brief(session, task=task, reason=reason, config=config)
    task.status = TaskStatus.CANCELLED
    session.flush()


def settle_stalled_head_briefs(
    session: Session, *, project_id: str, config: AppConfig | None = None
) -> list[Task]:
    """Briefs that cannot finish fall back to templates and close.

    Called before the closure loops start (i.e. once per scheduler cycle after
    implementation is done), so a head that never ran cannot hold up the
    release (the department still gets its template work):

    - BLOCKED / FAILED / DEFERRED_BUDGET → fall back at once;
    - READY / WAITING / BACKLOG → count the cycle in ``constraints.wait_cycles``
      and fall back once it exceeds ``head_llm.max_wait_cycles``. This runs
      before the scheduler dispatches, so N gives the brief N dispatch chances
      (0 = fall back at once);
    - RUNNING is left alone (a worker owns it; leases / zombies are handled by
      the scheduler's cleanup) and its wait counter is reset, so a brief put
      back to READY by a retry gets a fresh N chances.
    """
    cfg = config or load_config()
    limit = cfg.settings.executive_meeting.head_llm.max_wait_cycles
    settled: list[Task] = []
    for task in _brief_tasks(session, project_id):
        if task.status in _STALLED:
            close_failed_head_brief(
                session, task=task, reason=f"stalled:{task.status}", config=cfg
            )
            settled.append(task)
            continue
        if task.status == TaskStatus.RUNNING:
            data = _constraints(task)
            if "wait_cycles" in data:
                data.pop("wait_cycles")
                _set_constraints(task, data)
                session.flush()
            continue
        if task.status not in _WAITING:
            continue
        data = _constraints(task)
        waited = int(data.get("wait_cycles") or 0) + 1
        if waited > limit:
            close_failed_head_brief(
                session,
                task=task,
                reason=f"stalled_waiting:{task.status}:{waited}cycles",
                config=cfg,
            )
            settled.append(task)
            continue
        data["wait_cycles"] = waited
        _set_constraints(task, data)
        session.flush()
    return settled
