"""重役会議・部長所見・招集見直しで出た「社長への確認事項」の番号付けと回答."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Task
from ai_company.services.audit import record_audit
from ai_company.services.redact import single_line


def _all_meeting_questions(session: Session, project_id: str) -> list[str]:
    """重役会議と部長所見で出た「社長・専門家への確認事項」（全件・伏せ字化済み・重複なし）.

    The list order is stable (meeting first, then briefs in filing order), so the
    1-based position is the number used by ``ai-company decide``.

    Sources: the latest ``executive_meeting_held`` audit event of the project
    (template minutes) and applied ``head_brief`` tasks (``head_open_questions``).
    """
    from ai_company.db.models import AuditEvent
    from ai_company.org import DEPARTMENTS
    from ai_company.services.redact import redact_text

    found: list[str] = []
    event = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.event_type == "executive_meeting_held",
            AuditEvent.target_id == project_id,
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    meta = (event.metadata_json or {}) if event else {}
    for q in meta.get("open_questions") or []:
        if isinstance(q, str) and q.strip():
            found.append(q.strip())

    names = {d.id: d.name_ja for d in DEPARTMENTS}
    briefs = session.scalars(
        select(Task)
        .where(Task.project_id == project_id, Task.kind == "head_brief")
        .order_by(Task.created_at.asc())
    ).all()
    for task in briefs:
        try:
            data = json.loads(task.constraints or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        dept = names.get(str(task.department_id), "部長")
        for q in data.get("head_open_questions") or []:
            text = q.get("question") if isinstance(q, dict) else q
            if not isinstance(text, str) or not text.strip():
                continue
            who = (
                "社長"
                if isinstance(q, dict) and q.get("decider") == "president"
                else "専門家／担当部署"
            )
            found.append(f"{dept}（{who}）: {text.strip()}")

    out: list[str] = []
    for q in found:
        line = single_line(redact_text(q))[:200]
        if line and line not in out:
            out.append(line)
    return out


MEETING_QUESTION_RESOLVED = "meeting_question_resolved"


def resolved_meeting_questions(session: Session, project_id: str) -> dict[str, str]:
    """Resolved question text → (redacted) answer, from audit events."""
    from ai_company.db.models import AuditEvent

    resolved: dict[str, str] = {}
    for event in session.scalars(
        select(AuditEvent).where(
            AuditEvent.event_type == MEETING_QUESTION_RESOLVED,
            AuditEvent.target_type == "project",
            AuditEvent.target_id == project_id,
        )
    ).all():
        meta = event.metadata_json or {}
        q = meta.get("question")
        if isinstance(q, str) and q:
            resolved[q] = str(meta.get("answer") or "")
    return resolved


def numbered_meeting_questions(
    session: Session, project_id: str, *, include_resolved: bool = False
) -> list[tuple[int, str]]:
    """(番号, 確認事項). Numbers stay the same after other questions are resolved."""
    done = set() if include_resolved else set(resolved_meeting_questions(session, project_id))
    return [
        (n, q)
        for n, q in enumerate(_all_meeting_questions(session, project_id), 1)
        if q not in done
    ]


def meeting_open_questions(session: Session, project_id: str, *, limit: int = 10) -> list[str]:
    """未解決の確認事項（表示用・伏せ字化済み）."""
    return [q for _, q in numbered_meeting_questions(session, project_id)][:limit]


def resolve_meeting_question(
    session: Session, *, project_id: str, number: int, answer: str
) -> str:
    """社長が確認事項に回答し、解決済みにする（監査イベントを記録）.

    Returns the resolved question. Raises ``ValueError`` for an unknown number,
    an already-resolved question, or an empty answer.
    """
    from ai_company.services.decisions import department_of_question, question_key
    from ai_company.services.redact import redact_text

    answer = " ".join((answer or "").split())
    if not answer:
        raise ValueError("回答が空です")
    questions = _all_meeting_questions(session, project_id)
    if not 1 <= number <= len(questions):
        raise ValueError(f"確認事項 #{number} はありません（1〜{len(questions)}）")
    question = questions[number - 1]
    if question in resolved_meeting_questions(session, project_id):
        raise ValueError(f"確認事項 #{number} は解決済みです")
    record_audit(
        session,
        event_type=MEETING_QUESTION_RESOLVED,
        actor_type="human",
        actor_id="human_owner",
        target_type="project",
        target_id=project_id,
        metadata={
            "number": number,
            "question": question,
            "question_key": question_key(question),
            "department_id": department_of_question(question),
            "answer": redact_text(answer)[:500],
        },
    )
    session.flush()
    return question
