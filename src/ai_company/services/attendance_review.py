"""招集見直し（attendance_review）: キーワード判定の出席者に、部を追加招集する.

重役会議の出席者は依頼文のキーワードで決まる（``executive_meeting.select_attendees``）。
言い回しが違うと必要な部が呼ばれないので、会議の後に秘書室／推進課の短命 Worker が
「依頼文から見て、欠席している部のうち招集すべき部」を判断する。

Flow::

    start_goal → 決定論的な重役会議（キーワード判定＝下限）
      → attendance_review タスクを1件起票（欠席した部が候補）
      → Queue → Scheduler（policy・lease）→ Cost Governor → 推進課 Worker（読み取り専用）
      → JSON（summon[]）→ parse → apply_attendance_review（秘書側の検証）
      → 追加した部: 会議テンプレートの部門作業を起票（部長所見が有効な部は部長所見）
    失敗・解析不能・リトライ上限・停止 → キーワード判定のまま（追加なし）

Invariants:

- 見直しは**追加だけ**できる。会議に出た部を外すことはできない（安全側）。
- 候補は会議で欠席した部だけ。上限は ``attendance_review.max_additions``。
- 秘書・会議は LLM を呼ばず Worker も起動しない。起動は Scheduler 経由。
- 見直し役は ``reports_to`` の上申経路に入らない。失敗しても上申しない。
- mock ランナーは「追加なし」を返すので、mock の結果はキーワード判定と同じ。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ai_company.config import AppConfig, load_config
from ai_company.enums import TaskKind, TaskStatus
from ai_company.org import DEPARTMENTS, department_by_id
from ai_company.paths import meetings_dir
from ai_company.services.redact import redact_text, single_line

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ai_company.db.models import Task
    from ai_company.services.executive_meeting import MeetingMinutes


ATTENDANCE_REVIEW_KIND = TaskKind.ATTENDANCE_REVIEW.value
ATTENDANCE_REVIEW_PRIORITY = 99  # 部長所見（98）より先に動かす
_STALLED = {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.DEFERRED_BUDGET}
_WAITING = {TaskStatus.READY, TaskStatus.WAITING, TaskStatus.BACKLOG}

# 追加招集した部への「社長への確認事項」（会議テンプレートと同じ文言）
_OPEN_QUESTIONS: dict[str, str] = {
    "sales": "営業部: 価格・契約条件の方針（社長判断）",
    "legal": "法務部: 個人情報・法令の扱いの最終確認（専門家／担当部署）",
    "operations": "運用保守部: 本番環境・運用体制の有無（社長判断）",
}


@dataclass
class AttendanceDecision:
    added: dict[str, str] = field(default_factory=dict)  # 部 id → 理由（伏せ字化済み）
    rejected: dict[str, str] = field(default_factory=dict)  # 部 id → 却下理由
    fatal: str | None = None


# --- helpers ---------------------------------------------------------------


def attendance_review_enabled(cfg: AppConfig) -> bool:
    return bool(cfg.settings.executive_meeting.attendance_review.enabled)


def is_attendance_review_task(task: Task) -> bool:
    return str(task.kind) == ATTENDANCE_REVIEW_KIND


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


def _review_tasks(session: Session, project_id: str) -> list[Task]:
    from ai_company.db.models import Task

    return list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.kind == ATTENDANCE_REVIEW_KIND)
            .order_by(Task.created_at.asc())
        ).all()
    )


def open_attendance_reviews(session: Session, project_id: str) -> list[Task]:
    done = {TaskStatus.DONE, TaskStatus.CANCELLED}
    return [t for t in _review_tasks(session, project_id) if t.status not in done]


def attendance_open_questions(session: Session, project_id: str) -> list[str]:
    """Open questions for departments added by the review (filing order)."""
    out: list[str] = []
    for task in _review_tasks(session, project_id):
        for q in _constraints(task).get("added_open_questions") or []:
            if isinstance(q, str) and q.strip():
                out.append(q.strip())
    return out


# --- filing ---------------------------------------------------------------


def issue_attendance_review(
    session: Session,
    *,
    project_id: str,
    minutes: MeetingMinutes,
    config: AppConfig | None = None,
) -> Task | None:
    """File one ``attendance_review`` task for the meeting (idempotent).

    Not filed when disabled, when every department already attends, or when
    the reviewer role would be denied by the scheduler's policy check.
    """
    from ai_company.services.audit import record_audit
    from ai_company.services.department_orders import meeting_tag
    from ai_company.services.head_briefs import head_role_dispatchable
    from ai_company.services.tasks import create_task

    cfg = config or load_config()
    review = cfg.settings.executive_meeting.attendance_review
    if not review.enabled or review.max_additions <= 0:
        return None
    if _review_tasks(session, project_id):
        return None
    candidates = [d.id for d in DEPARTMENTS if d.id not in minutes.attendees]
    if not candidates:
        return None
    tag = meeting_tag(project_id)
    ok, reason = head_role_dispatchable(cfg, review.role)
    if not ok:
        record_audit(
            session,
            event_type="attendance_review_skipped",
            actor_type="secretary",
            actor_id="secretary",
            target_type="project",
            target_id=project_id,
            metadata={"meeting": tag, "role": review.role, "reason": reason},
        )
        return None
    task = create_task(
        session,
        title="招集見直し: 重役会議の出席者",
        priority=ATTENDANCE_REVIEW_PRIORITY,
        project_id=project_id,
        objective=(
            f"Goal「{minutes.goal}」について、重役会議に欠席した部のうち"
            "追加で招集すべき部があるかを判断せよ（追加のみ。外すことはできない）。"
        ),
        role_required=review.role,
        estimated_cost=review.estimated_cost,
        max_retries=review.max_retries,
        kind=ATTENDANCE_REVIEW_KIND,
        risk_level="low",
        acceptance_criteria="AttendanceReview JSON（summon は欠席した部のみ）を返す",
        department_id="secretariat",
        section_id="pmo",
        config=cfg,
    )
    _set_constraints(
        task,
        {
            "meeting": tag,
            "goal": minutes.goal,
            "attendees": list(minutes.attendees),
            "candidates": candidates,
            "absent_reasons": {d: minutes.absentees.get(d, "") for d in candidates},
        },
    )
    session.flush()
    record_audit(
        session,
        event_type="attendance_review_requested",
        actor_type="secretary",
        actor_id="secretary",
        target_type="task",
        target_id=task.id,
        metadata={"meeting": tag, "candidates": candidates, "role": review.role},
    )
    return task


# --- mock -----------------------------------------------------------------


def build_mock_attendance_review(task: Task) -> dict[str, Any]:
    """Deterministic review: nothing to add (= the keyword decision stands)."""
    return {
        "status": "completed",
        "summon": [],
        "summary": "mock: キーワード判定の出席者で足りる",
        "confidence": 0.9,
        "mock": True,
        "task_id": task.id,
    }


# --- validation / apply ----------------------------------------------------


def validate_attendance_review(
    task: Task, review: dict[str, Any], *, config: AppConfig | None = None
) -> AttendanceDecision:
    """Secretary-side checks. ``review`` must be normalized by the parser."""
    cfg = config or load_config()
    limit = cfg.settings.executive_meeting.attendance_review.max_additions
    data = _constraints(task)
    candidates = [str(c) for c in data.get("candidates") or []]
    decision = AttendanceDecision()
    for item in review.get("summon") or []:
        dept_id = str(item.get("department_id") or "")
        reason = single_line(redact_text(str(item.get("reason") or "")))[:200]
        if dept_id not in candidates:
            decision.rejected[dept_id] = "会議に出席済み、または候補外"
            continue
        if len(decision.added) >= limit:
            decision.rejected[dept_id] = f"上限: 追加招集は {limit} 部まで"
            continue
        decision.added[dept_id] = reason or "（理由の記載なし）"
    return decision


def _plan_proposals(session: Session, project_id: str) -> list[Any]:
    """The project's planned tasks as proposals (for template derivation)."""
    from ai_company.db.models import Task
    from ai_company.services.secretary import TaskProposal

    doc_kinds = (
        TaskKind.DEPARTMENT_WORK.value,
        TaskKind.HEAD_BRIEF.value,
        ATTENDANCE_REVIEW_KIND,
    )
    rows = session.scalars(
        select(Task)
        .where(Task.project_id == project_id, Task.kind.not_in(doc_kinds))
        .order_by(Task.created_at.asc())
    ).all()
    return [
        TaskProposal(title=t.title, objective="", role_required=t.role_required) for t in rows
    ]


def apply_attendance_review(
    session: Session,
    *,
    task: Task,
    payload: dict[str, Any],
    config: AppConfig | None = None,
) -> AttendanceDecision:
    """Validate a finished review and summon the added departments."""
    from ai_company.services.audit import record_audit
    from ai_company.services.department_orders import (
        DeferredOrder,
        derive_work_orders,
        existing_order_tasks,
        issue_work_orders,
    )
    from ai_company.services.head_briefs import issue_head_briefs_for_added
    from ai_company.services.result_parser import parse_attendance_review_result

    cfg = config or load_config()
    data = _constraints(task)
    if data.get("review_applied") or data.get("review_closed"):
        return AttendanceDecision()  # idempotent

    parsed = parse_attendance_review_result(json.dumps(payload, ensure_ascii=False))
    if not parsed.ok or parsed.result is None:
        reason = f"parse:{parsed.error}"
        close_attendance_review(session, task=task, reason=reason, config=cfg)
        return AttendanceDecision(fatal=reason)
    decision = validate_attendance_review(task, parsed.result, config=cfg)

    project_id = str(task.project_id)
    goal = str(data.get("goal") or "")
    added = list(decision.added)
    issued_ids: list[str] = []
    brief_ids: list[str] = []
    if added:
        orders, deferred = derive_work_orders(
            goal, added, _plan_proposals(session, project_id), config=cfg
        )
        # The meeting already filed orders: the caps count them too.
        res = cfg.settings.meeting_resolutions
        room = max(0, res.max_orders_per_meeting - len(existing_order_tasks(session, project_id)))
        over = orders[room:]
        orders = orders[:room]
        deferred = [
            *deferred,
            *(
                DeferredOrder(o, f"上限: 1会議あたり {res.max_orders_per_meeting} 件")
                for o in over
            ),
        ]
        briefs = issue_head_briefs_for_added(
            session,
            project_id=project_id,
            goal=goal,
            departments=added,
            orders=orders,
            config=cfg,
        )
        brief_ids = [t.id for t in briefs.tasks]
        orders_now = [o for o in orders if o.department_id not in briefs.held]
        if orders_now or deferred:
            issued = issue_work_orders(
                session, project_id=project_id, orders=orders_now, deferred=deferred, config=cfg
            )
            issued_ids = [t.id for t in issued.issued]

    data.update(
        {
            "review_applied": True,
            "added": decision.added,
            "rejected": decision.rejected,
            "review_summary": single_line(
                redact_text(str(parsed.result.get("summary") or ""))
            )[:400],
            "added_open_questions": [
                _OPEN_QUESTIONS[d] for d in added if d in _OPEN_QUESTIONS
            ],
        }
    )
    _set_constraints(task, data)
    session.flush()
    _append_minutes(task, decision, summary=str(data["review_summary"]), fallback=None)
    record_audit(
        session,
        event_type="attendance_review_applied",
        actor_type="secretary",
        actor_id="secretary",
        target_type="task",
        target_id=task.id,
        metadata={
            "added": added,
            "rejected": sorted(decision.rejected),
            "work_order_tasks": issued_ids,
            "head_brief_tasks": brief_ids,
            "confidence": parsed.result.get("confidence"),
        },
    )
    return decision


def close_attendance_review(
    session: Session, *, task: Task, reason: str, config: AppConfig | None = None
) -> None:
    """The review could not be used: keep the keyword decision (no additions)."""
    from ai_company.services.audit import record_audit

    data = _constraints(task)
    if data.get("review_applied") or data.get("review_closed"):
        return
    reason = single_line(redact_text(reason))[:300]
    data["review_closed"] = True
    data["close_reason"] = reason
    _set_constraints(task, data)
    session.flush()
    _append_minutes(task, AttendanceDecision(), summary="", fallback=reason)
    record_audit(
        session,
        event_type="attendance_review_closed",
        actor_type="secretary",
        actor_id="secretary",
        target_type="task",
        target_id=task.id,
        metadata={"reason": reason},
    )


def close_failed_attendance_review(
    session: Session, *, task: Task, reason: str, config: AppConfig | None = None
) -> None:
    """Retries exhausted / stalled: close without escalation."""
    close_attendance_review(session, task=task, reason=reason, config=config)
    task.status = TaskStatus.CANCELLED
    session.flush()


def settle_stalled_attendance_reviews(
    session: Session, *, project_id: str, config: AppConfig | None = None
) -> list[Task]:
    """Same rule as ``head_briefs.settle_stalled_head_briefs``.

    A review that cannot run never holds up the release: BLOCKED / FAILED /
    DEFERRED_BUDGET close at once; READY / WAITING / BACKLOG close after
    ``attendance_review.max_wait_cycles`` scheduler cycles.
    """
    cfg = config or load_config()
    limit = cfg.settings.executive_meeting.attendance_review.max_wait_cycles
    settled: list[Task] = []
    for task in _review_tasks(session, project_id):
        if task.status in _STALLED:
            close_failed_attendance_review(
                session, task=task, reason=f"stalled:{task.status}", config=cfg
            )
            settled.append(task)
            continue
        data = _constraints(task)
        if task.status == TaskStatus.RUNNING:
            if "wait_cycles" in data:
                data.pop("wait_cycles")
                _set_constraints(task, data)
                session.flush()
            continue
        if task.status not in _WAITING:
            continue
        waited = int(data.get("wait_cycles") or 0) + 1
        if waited > limit:
            close_failed_attendance_review(
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


# --- minutes ---------------------------------------------------------------


def _append_minutes(
    task: Task, decision: AttendanceDecision, *, summary: str, fallback: str | None
) -> None:
    from ai_company.services.head_briefs import _md_inline

    path = meetings_dir() / f"{task.project_id}_kickoff_attendance.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 重役会議 招集見直し（追記）", ""]
    if fallback:
        lines.append(
            "- 見直しは採用せず、キーワード判定の出席者のまま"
            f"（理由: {_md_inline(fallback)}）"
        )
    else:
        if summary:
            lines.append(f"- 所見: {_md_inline(summary)}")
        for dept_id, why in decision.added.items():
            lines.append(
                f"- 追加招集: {department_by_id(dept_id).name_ja}（{_md_inline(why)}）"
            )
        for dept_id, why in decision.rejected.items():
            known = {d.id: d.name_ja for d in DEPARTMENTS}
            name = known.get(dept_id, "不明な部")
            lines.append(f"- 見送り: {name}（{_md_inline(why)}）")
        if not decision.added and not decision.rejected:
            lines.append("- 追加招集なし（キーワード判定の出席者で足りる）")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
