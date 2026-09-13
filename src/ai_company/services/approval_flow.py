"""Interactive human approval flow."""

from __future__ import annotations

import sys
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from ai_company.db.models import Approval, Task
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.services.escalation import resolve_approval
from ai_company.services.secretary import _explain_block_reason, _task_id_from_reason, human_next_actions

if TYPE_CHECKING:
    from rich.console import Console


class ApprovalChoice(str, Enum):
    YES = "yes"
    NO = "no"
    RECONSIDER = "reconsider"


CHOICE_LABELS = {
    ApprovalChoice.YES: "はい",
    ApprovalChoice.NO: "いいえ",
    ApprovalChoice.RECONSIDER: "再度検討せよ",
}

PROMPT_CHOICES = [CHOICE_LABELS[c] for c in ApprovalChoice]


def parse_approval_choice(text: str) -> ApprovalChoice | None:
    t = text.strip()
    if not t:
        return None
    lowered = t.lower()
    aliases = {
        "y": ApprovalChoice.YES,
        "yes": ApprovalChoice.YES,
        "はい": ApprovalChoice.YES,
        "1": ApprovalChoice.YES,
        "n": ApprovalChoice.NO,
        "no": ApprovalChoice.NO,
        "いいえ": ApprovalChoice.NO,
        "2": ApprovalChoice.NO,
        "r": ApprovalChoice.RECONSIDER,
        "reconsider": ApprovalChoice.RECONSIDER,
        "再度検討": ApprovalChoice.RECONSIDER,
        "再度検討せよ": ApprovalChoice.RECONSIDER,
        "3": ApprovalChoice.RECONSIDER,
    }
    if lowered in aliases:
        return aliases[lowered]
    if t in aliases:
        return aliases[t]
    for choice in ApprovalChoice:
        if t == CHOICE_LABELS[choice]:
            return choice
    return None


def describe_approval_prompt(approval: Approval, *, task: Task | None = None) -> str:
    """What this approval is asking the human to decide."""
    explanation = _explain_block_reason(approval.reason)
    task_id = _task_id_from_reason(approval.reason)
    title = task.title if task else ""
    if not title and "title=" in (approval.reason or ""):
        part = approval.reason.split("title=", 1)[1]
        title = part.split(";", 1)[0].strip()

    lines = [
        f"【承認確認】 {approval.id}",
        f"種別: {approval.action}",
        f"内容: {explanation}",
    ]
    if task_id or title:
        lines.append(f"対象タスク: {task_id or '（不明）'}  「{title or '（不明）'}」")
    lines.extend(
        [
            "",
            "この停止は、自動の修正回数上限に達したあとの人間判断です。",
            "質問: このまま自動修正を続けてよいですか？",
            "",
            "  はい          … 続行を承認し、タスクを再キューします",
            "  いいえ        … 打ち切り（自動続行しません）",
            "  再度検討せよ  … 自動続行せず、方針の再検討が必要です",
        ]
    )
    return "\n".join(lines)


def resolve_approval_choice(
    session: Session,
    *,
    approval_id: str,
    choice: ApprovalChoice,
    note: str = "",
    resolved_by: str = "owner",
) -> Approval:
    if choice == ApprovalChoice.YES:
        return resolve_approval(
            session,
            approval_id=approval_id,
            approve=True,
            note=note,
            resolved_by=resolved_by,
        )
    resolution_note = note
    if choice == ApprovalChoice.NO:
        resolution_note = note or "人間判断: 打ち切り"
    elif choice == ApprovalChoice.RECONSIDER:
        resolution_note = note or "人間判断: 再度検討"
    return resolve_approval(
        session,
        approval_id=approval_id,
        approve=False,
        note=resolution_note,
        resolved_by=resolved_by,
    )


def choice_result_message(choice: ApprovalChoice, approval: Approval) -> str:
    if choice == ApprovalChoice.YES:
        return f"[green]承認[/green] {approval.id} → 続行 OK（タスクを再キューしました）"
    if choice == ApprovalChoice.NO:
        return f"[yellow]却下[/yellow] {approval.id} → 打ち切り（自動続行しません）"
    return f"[yellow]却下[/yellow] {approval.id} → 再度検討（自動続行しません）"


def should_run_interactive(
    *,
    status: str | None,
    row_count: int,
    interactive: bool | None,
) -> bool:
    if interactive is not None:
        return interactive
    return (
        sys.stdin.isatty()
        and (status or "").upper() == ApprovalStatus.PENDING
        and row_count > 0
    )


def run_interactive_approval_session(
    session: Session,
    approvals: list[Approval],
    *,
    runner: str = "cursor",
    console: Console,
) -> bool:
    """Prompt for each pending approval. Returns True if any was approved."""
    from rich.prompt import Prompt

    pending = [a for a in approvals if a.status == ApprovalStatus.PENDING]
    if not pending:
        console.print("[dim]未承認の項目はありません。[/dim]")
        return False

    approved_any = False
    for approval in pending:
        task_id = _task_id_from_reason(approval.reason)
        task = session.get(Task, task_id) if task_id else None
        console.print("")
        console.print(describe_approval_prompt(approval, task=task))
        console.print("")
        while True:
            raw = Prompt.ask(
                "どうしますか",
                choices=PROMPT_CHOICES,
                default=CHOICE_LABELS[ApprovalChoice.RECONSIDER],
                show_choices=True,
            )
            choice = parse_approval_choice(raw)
            if choice is not None:
                break
            console.print("[red]はい / いいえ / 再度検討せよ のいずれかを選んでください。[/red]")

        row = resolve_approval_choice(session, approval_id=approval.id, choice=choice)
        console.print(choice_result_message(choice, row))
        if choice == ApprovalChoice.YES:
            approved_any = True

    console.print("")
    console.print(
        human_next_actions(
            session,
            stopped_reason="scheduler_pause" if approved_any else None,
            runner=runner,
        )
    )
    return approved_any
