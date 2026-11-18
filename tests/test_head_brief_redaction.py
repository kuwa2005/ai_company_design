"""agent-b T2-S4 L-1 / L-2 / L-3 fixes (head briefs / department work).

L-1: LLM values echoed in parser errors are redacted (and kept short/one line)
     before they reach fallback_reason / audit metadata / audit.jsonl.
L-2: head output and department-work results are stored redacted.
L-3: head-authored work orders carry the same document-only rule as templates.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent
from ai_company.paths import audit_log_path
from ai_company.services.department_orders import DOC_RULE, record_department_work_result
from ai_company.services.head_briefs import apply_head_brief, validate_head_brief
from ai_company.services.result_parser import parse_head_brief_result
from tests.test_head_briefs import _brief, _legal_brief_task, _parsed

SECRET = "hunter2secret"


# --- L-1 -------------------------------------------------------------------


def test_parser_errors_redact_and_shorten_llm_values() -> None:
    order = _brief()["work_orders"][0]  # type: ignore[index]
    bad_key = _brief(work_orders=[{**order, "key": f"password={SECRET}"}])
    err = parse_head_brief_result(json.dumps(bad_key)).error or ""
    assert err.startswith("invalid_work_order_key:") and SECRET not in err

    bad_decider = _brief(open_questions=[{"question": "q", "decider": f"token={SECRET}\nX"}])
    err = parse_head_brief_result(json.dumps(bad_decider)).error or ""
    assert err.startswith("invalid_decider:") and SECRET not in err and "\n" not in err

    long_status = _brief(status="x" * 500)
    err = parse_head_brief_result(json.dumps(long_status)).error or ""
    assert len(err) <= len("invalid_status:") + 40


def test_fallback_reason_is_redacted_in_db_and_audit(db_session: Session, tmp_env: Path) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    order = _brief()["work_orders"][0]  # type: ignore[index]
    apply_head_brief(
        db_session,
        task=task,
        payload=_brief(work_orders=[{**order, "key": f"password={SECRET}"}]),
    )
    data = json.loads(task.constraints)
    assert data["fallback_applied"] is True
    assert data["fallback_reason"].startswith("parse:invalid_work_order_key:")
    assert SECRET not in task.constraints
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "head_brief_fallback")
    ).all()
    assert events and all(SECRET not in json.dumps(e.metadata_json) for e in events)
    assert SECRET not in audit_log_path().read_text(encoding="utf-8")


# --- L-2 -------------------------------------------------------------------


def test_head_output_is_stored_redacted(db_session: Session, tmp_env: Path) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    apply_head_brief(
        db_session,
        task=task,
        payload=_brief(
            statement=f"了解 password={SECRET}",
            open_questions=[{"question": f"api_key: {SECRET}", "decider": "president"}],
            handoff_requests=[
                {"to_department_id": "development", "request": f"secret={SECRET}"}
            ],
        ),
    )
    data = json.loads(task.constraints)
    assert data["brief_applied"] is True
    assert SECRET not in task.constraints
    assert data["head_open_questions"][0]["decider"] == "president"  # structure kept
    assert data["head_handoff_requests"][0]["to_department_id"] == "development"


def test_department_work_result_is_stored_redacted() -> None:
    class _T:
        constraints = "{}"

    t = _T()
    record_department_work_result(
        t,  # type: ignore[arg-type]
        {"summary": f"要点 password={SECRET}", "issues": [f"token: {SECRET}", "未定"]},
    )
    assert SECRET not in t.constraints
    data = json.loads(t.constraints)
    assert data["work_order_issues"][1] == "未定"


# --- L-3 -------------------------------------------------------------------


def test_head_orders_carry_document_only_rule(db_session: Session, tmp_env: Path) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    decision = validate_head_brief(db_session, task=task, brief=_parsed(_brief()))
    (order,) = decision.orders
    assert order.objective.startswith("保存項目と保存期間を点検")
    assert order.objective.endswith(DOC_RULE)
