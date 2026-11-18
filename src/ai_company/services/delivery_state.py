"""Project delivery phase and failure-cause helpers shared by the secretary's reports."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import load_config
from ai_company.db.models import Project, Task
from ai_company.enums import TaskStatus


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




def _task_id_from_reason(reason: str) -> str | None:
    import re

    m = re.search(r"task=(TASK-[A-Z0-9-]+)", reason or "")
    return m.group(1) if m else None


_CAUSE_JA = {
    "write_forbidden": "読み取り専用の担当がファイルを変更した（書き込み禁止違反）",
    "changed_files_mismatch": "申告した変更ファイルと実際の差分が一致しない",
    "manifest_tests_failed": "テストが失敗した",
    "timeout": "時間切れ",
    "unparseable_or_missing_json": "結果が JSON として読めない",
}


def cause_ja(cause: str) -> str:
    """Japanese label for a machine failure cause (``failure_cause``)."""
    c = (cause or "").strip()
    if not c:
        return ""
    if c in _CAUSE_JA:
        return _CAUSE_JA[c]
    if c.startswith("missing_field:"):
        return f"結果 JSON に必須項目 {c.split(':', 1)[1]} がない"
    if c.startswith("invalid_"):
        return f"結果 JSON の値が不正（{c}）"
    if c.startswith("exit_code="):
        return f"Cursor がエラー終了した（{c}）"
    return c


def _policy_denied_ready(ready: list[Task]) -> list[tuple[Task, str]]:
    """READY tasks the policy engine will refuse (not approval-gated) with the reason."""
    if not ready:
        return []
    from ai_company.services.policy_engine import evaluate_dispatch

    cfg = load_config()
    out: list[tuple[Task, str]] = []
    for t in ready:
        d = evaluate_dispatch(task=t, role=t.role_required or "coding_worker", config=cfg)
        if not d.allowed and not d.requires_approval:
            out.append((t, str(d.reason)))
    return out


def _last_failure_cause(session: Session, task: Task) -> str:
    """Cause of the task's most recent failed execution ("" when unknown)."""
    from ai_company.db.models import Execution
    from ai_company.services.result_parser import failure_cause_of

    last = session.scalar(
        select(Execution)
        .where(Execution.task_id == task.id)
        .order_by(Execution.started_at.desc())
        .limit(1)
    )
    if last is None or last.success:
        return ""
    return failure_cause_of(last.result_json, exit_code=last.exit_code)


def _last_error_from_reason(reason: str) -> str:
    import re

    m = re.search(r"last_error=([A-Za-z0-9_:./=-]+)", reason or "")
    return m.group(1) if m else ""


def _explain_block_reason(reason: str) -> str:
    r = reason or ""
    task_id = _task_id_from_reason(r)
    suffix = f"（タスク: {task_id}）" if task_id else ""
    cause = cause_ja(_last_error_from_reason(r))
    if cause:
        # The underlying cause first: "再計画回数を使い切った" alone hid it (live1).
        suffix = f"直接の原因: {cause}。" + suffix
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
