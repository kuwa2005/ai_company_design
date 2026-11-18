"""秘書の状況報告: ``human_summary`` and the department / responsibility / exception digests."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.db.models import Project, StaffingRequest, Task
from ai_company.enums import StaffingStatus, TaskStatus
from ai_company.services.cost_governor import count_active_workers, get_budget_snapshot
from ai_company.services.delivery_state import (
    _explain_block_reason,
    _last_failure_cause,
    _task_counts,
    cause_ja,
    project_delivery_phase,
)
from ai_company.services.meeting_questions import numbered_meeting_questions


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
        "awaiting_decision": "社長の回答待ち（自問完了・確認事項が未回答）",
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
            *(
                [
                    f"本日の内部コスト: ${snap.daily_spent:.2f}（金額上限なし・購読モード）",
                    f"月間の内部コスト: ${snap.spent + snap.reserved:.2f}",
                ]
                if snap.soft_limit <= 0
                else [
                    f"本日の内部コスト: ${snap.daily_spent:.2f}（上限 ${snap.soft_limit:.2f}）",
                    f"月次使用率: {snap.soft_usage_ratio:.0%} "
                    f"（${snap.spent + snap.reserved:.2f} / ${snap.soft_limit:.2f}）",
                ]
            ),
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
        cause = cause_ja(_last_failure_cause(session, t))
        cause_txt = f"  原因={cause}" if cause else ""
        if replan:
            lines.append(
                f"- 失敗「{t.title}」→ 直近上司 "
                f"{format_role_label(replan.role_required or 'engineering_manager')} "
                f"が対応予定 （{replan.id}）{cause_txt}"
            )
        else:
            lines.append(
                f"- 失敗「{t.title}」({t.id}) "
                f"担当={format_role_label(t.role_required or 'coding_worker')}{cause_txt}"
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
