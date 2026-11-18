"""Secretary v1 — plan goals into Tasks + Staffing Requests. Never spawns workers."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Project, StaffingRequest, Task
from ai_company.enums import StaffingStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit
from ai_company.services.cost_governor import get_budget_snapshot
from ai_company.services.delivery_state import (
    _CAUSE_JA,
    _explain_block_reason,
    _last_error_from_reason,
    _last_failure_cause,
    _policy_denied_ready,
    _task_counts,
    _task_id_from_reason,
    cause_ja,
    project_delivery_phase,
)
from ai_company.services.executive_meeting import (
    MeetingMinutes,
    convene_executive_meeting,
    record_executive_meeting,
)
from ai_company.services.goal_planning import (
    GoalPlan,
    StaffingProposal,
    TaskProposal,
    ValidationRequestProposal,
    _domain_overlay_tasks,
    _generic_decompose_skeleton,
    _keyword_plan,
    _requirement_gap_proposals,
    classify_risk,
    propose_validation_request,
    understand_goal,
)
from ai_company.services.meeting_questions import (
    MEETING_QUESTION_RESOLVED,
    _all_meeting_questions,
    meeting_open_questions,
    numbered_meeting_questions,
    resolve_meeting_question,
    resolved_meeting_questions,
)
from ai_company.services.next_actions import (
    _cheapest,
    human_next_actions,
    infer_runner,
    scheduler_continue_command,
)
from ai_company.services.status_report import (
    _OPEN_STATUSES,
    _STOPPED_STATUSES,
    department_digest,
    department_workload,
    exception_digest,
    human_summary,
    responsibility_digest,
)
from ai_company.services.tasks import create_project, create_task

# The secretary's public surface. Planning, reports and next actions live in
# goal_planning / delivery_state / meeting_questions / status_report /
# next_actions; they are re-exported here so existing imports keep working.
__all__ = [
    "answer_question",
    "apply_plan",
    "cause_ja",
    "classify_risk",
    "create_staffing_request",
    "department_digest",
    "department_workload",
    "exception_digest",
    "GoalPlan",
    "human_next_actions",
    "human_summary",
    "infer_runner",
    "list_staffing",
    "meeting_open_questions",
    "MEETING_QUESTION_RESOLVED",
    "numbered_meeting_questions",
    "project_delivery_phase",
    "propose_validation_request",
    "resolve_meeting_question",
    "resolved_meeting_questions",
    "responsibility_digest",
    "scheduler_continue_command",
    "StaffingProposal",
    "start_goal",
    "StartResult",
    "TaskProposal",
    "understand_goal",
    "ValidationRequestProposal",
    "_all_meeting_questions",
    "_CAUSE_JA",
    "_cheapest",
    "_domain_overlay_tasks",
    "_explain_block_reason",
    "_generic_decompose_skeleton",
    "_keyword_plan",
    "_last_error_from_reason",
    "_last_failure_cause",
    "_OPEN_STATUSES",
    "_policy_denied_ready",
    "_requirement_gap_proposals",
    "_STOPPED_STATUSES",
    "_task_counts",
    "_task_id_from_reason",
]

# Hard ban: this module must never import process runners or worker dispatch.
# Enforced by tests/test_secretary.py


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
    attendance_review_task: Task | None = None



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
    if cfg.settings.executive_meeting.attendance_review.enabled:
        # キーワード判定の出席者を下限に、欠席した部の追加招集を推進課に判断させる。
        from ai_company.services.attendance_review import issue_attendance_review

        result.attendance_review_task = issue_attendance_review(
            session, project_id=result.project.id, minutes=minutes, config=cfg
        )
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
