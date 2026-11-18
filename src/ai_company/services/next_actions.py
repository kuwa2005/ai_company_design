"""秘書が人に示す「次の一手」（``human_next_actions``）と runner の推定."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Project, Task
from ai_company.enums import ProjectStatus, TaskStatus
from ai_company.services.delivery_state import (
    _explain_block_reason,
    _policy_denied_ready,
    project_delivery_phase,
)


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
        "awaiting_decision": (
            "作業と自問はすべて終わりました。会議の「社長への確認事項」が未回答のため、"
            "リリース可にしていません。"
        ),
        "budget_wait": (
            "予算待ち（時間枠の上限）のため停止しました。"
            "同じ時間枠の中で再試行しても進まないため、空回りせずに止めています。"
        ),
        "policy_denied": (
            "権限（permissions.yaml）で起動できないタスクだけが残ったため、"
            "空回りせずに止めました。担当役割の見直しか権限の判断が必要です。"
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
        elif phase == "awaiting_decision" and stopped_reason in (None, "idle_incomplete"):
            stopped_reason = "awaiting_decision"
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
                        "  状態: 承認キュー空 → 直属の上司（課長・部長）へ移管可"
                    )
                else:
                    lines.append(
                        "  原因: 停止中（承認なし）。直属の上司（課長・部長）へ移管可"
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
        lines.append("A) 直属の上司（課長・部長）に戻して直させる:")
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

    # Priority 1.6: every READY task is denied by policy → a person must act.
    # Telling the owner to "just continue" here only re-ran the spin (live2).
    denied = _policy_denied_ready(ready)
    if denied and len(denied) == len(ready) and not (throttled or review or deferred):
        from ai_company.roles import format_role_label

        lines.append(
            "人の判断が必要です。次のタスクは担当役割に必要な権限が無く、起動できません:"
        )
        for t, why in denied[:5]:
            lines.append(
                f"   - {t.id}「{t.title}」 担当={format_role_label(t.role_required or '-')}"
                f"  理由={why}"
            )
        lines.append("")
        lines.append("担当を見直す（直近の上司に再計画させる）なら:")
        for t, _ in denied[:5]:
            lines.append(f"   ai-company task handoff-manager {t.id} --runner {runner}")
        lines.append("")
        lines.append(
            "権限を広げる場合は config/permissions.yaml の変更になるため、"
            "社内の承認手続きを経てください。"
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

    if stopped_reason == "awaiting_decision":
        from ai_company.services.meeting_questions import decisions_pending

        if project_id:
            pids = [project_id]
        else:
            pids = list(
                session.scalars(
                    select(Project.id)
                    .where(Project.status == ProjectStatus.ACTIVE)
                    .order_by(Project.created_at.asc())
                ).all()
            )
        lines.append("社長の回答が必要です（回答するまでリリース可になりません）:")
        multi = len(pids) > 1
        for pid in pids:
            pend = decisions_pending(session, pid)
            if not pend:
                continue
            if multi:
                lines.append(f"  案件 {pid}:")
            for n, q in pend[:10]:
                lines.append(f"   #{n} {q}")
        lines.append("")
        lines.append(
            '   ai-company decide <番号> "回答"'
            + ("  --project <案件id>" if multi else "")
        )
        lines.append("")
        lines.append("回答した部の文書は必要に応じて改訂されます。回答後に続きを実行:")
        lines.append(f"   {continue_cmd}")
        lines.append(
            "（確認事項を待たずに完了させる運用にするなら config/settings.yaml の "
            "executive_meeting.require_decisions_before_release: false）"
        )
        return "\n".join(lines)

    if stopped_reason == "all_implementation_done":
        lines.append("追加作業は不要です。リリース候補を確認してください。")
        return "\n".join(lines)

    lines.append("いま承認待ちも実行待ちもありません。")
    lines.append("   ai-company status")
    return "\n".join(lines)
