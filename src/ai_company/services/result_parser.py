"""Parse structured worker results. Broken JSON is never success."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from ai_company.services.redact import redact_text, single_line


@dataclass(frozen=True)
class ParsedResult:
    ok: bool
    result: dict[str, Any] | None
    error: str | None = None


_REQUIRED = ("status",)
_REVIEW_VERDICTS = frozenset({"PASS", "FAIL", "UNCERTAIN"})
_JUDGE_VERDICTS = frozenset({"PASS", "REJECT", "HUMAN_ESCALATION"})
_FINDING_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from raw text or from the last {...} embedded in prose."""
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    matches = list(re.finditer(r"\{[\s\S]*\}", text))
    for match in reversed(matches):
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _coerce_dict(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return _extract_json_object(data)
    return None


def _unwrap_agent_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """
    Cursor `agent --output-format json` may wrap the worker payload:

      {"type":"result","subtype":"success","result":"<json-string-or-object>", ...}

    Real agents often put prose + JSON inside `result` (a string). Prefer the
    innermost worker payload when present.
    """
    if data.get("type") == "result" and "result" in data:
        inner = _coerce_dict(data["result"])
        if inner is not None:
            return inner
        # Nested string that itself is an envelope
        if isinstance(data["result"], str):
            nested = _extract_json_object(data["result"])
            if nested is not None:
                return _unwrap_agent_envelope(nested) if nested.get("type") == "result" else nested
    if "status" in data or "verdict" in data:
        return data
    return data


def _try_load(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    data = _extract_json_object(text)
    if data is None:
        return None
    return _unwrap_agent_envelope(data)


def _normalize_finding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity") or "medium").lower()
    if severity not in _FINDING_SEVERITIES:
        severity = "medium"
    evidence = raw.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    return {
        "severity": severity,
        "category": str(raw.get("category") or "general"),
        "description": str(raw.get("description") or raw.get("message") or ""),
        "evidence": [str(e) for e in evidence],
        "recommended_fix": str(raw.get("recommended_fix") or ""),
    }


def parse_structured_result(stdout: str) -> ParsedResult:
    data = _try_load(stdout)
    if data is None:
        return ParsedResult(ok=False, result=None, error="unparseable_or_missing_json")

    for key in _REQUIRED:
        if key not in data:
            return ParsedResult(ok=False, result=data, error=f"missing_field:{key}")

    status = str(data.get("status", "")).lower()
    if status in {"done", "success", "ok", "complete"}:
        status = "completed"
    if status not in {"completed", "failed"}:
        return ParsedResult(ok=False, result=data, error=f"invalid_status:{_err_val(status)}")

    # Normalize optional fields
    normalized = {
        "status": status,
        "changed_files": data.get("changed_files") or [],
        "tests": data.get("tests") or {"passed": 0, "failed": 0},
        "commit": data.get("commit"),
        "issues": normalize_issues(data.get("issues") or data.get("unresolved_issues")),
        # Keep the worker's own summary (department_work stores it as the deliverable trace).
        "summary": str(data.get("summary") or ""),
    }
    if status != "completed":
        return ParsedResult(ok=False, result=normalized, error="status_not_completed")
    return ParsedResult(ok=True, result=normalized, error=None)


def normalize_issues(value: Any, *, max_items: int = 50) -> list[str]:
    """Coerce untrusted worker ``issues`` into a list of strings.

    list/tuple -> each item as str; a single str -> ``[value]``; anything else
    (int, dict, None, ...) -> ``[]``. Never raises.
    """
    if isinstance(value, str):
        items: list[Any] = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    return [str(i) for i in items[:max_items]]


def parse_reviewer_result(stdout: str) -> ParsedResult:
    """
    Reviewer structured output (No-look).

    Accepts either:
      {"verdict":"PASS|FAIL|UNCERTAIN", ...}
    or coding-style status with optional verdict:
      {"status":"completed","verdict":"PASS", ...}
    """
    data = _try_load(stdout)
    if data is None:
        return ParsedResult(ok=False, result=None, error="unparseable_or_missing_json")

    verdict_raw = data.get("verdict")
    status = str(data.get("status", "")).lower() if data.get("status") is not None else ""

    if verdict_raw is None:
        if status == "completed":
            verdict_raw = "PASS"
        elif status == "failed":
            verdict_raw = "FAIL"
        else:
            return ParsedResult(ok=False, result=data, error="missing_field:verdict")

    verdict = str(verdict_raw).upper()
    if verdict in {"COMPLETED", "SUCCESS", "OK"}:
        verdict = "PASS"
    if verdict not in _REVIEW_VERDICTS:
        return ParsedResult(ok=False, result=data, error=f"invalid_verdict:{_err_val(verdict)}")

    # Explicit failed status without FAIL/UNCERTAIN is not a successful review run
    if status == "failed" and verdict == "PASS":
        return ParsedResult(ok=False, result=data, error="status_failed_with_pass_verdict")

    findings_raw = data.get("findings") or []
    findings: list[dict[str, Any]] = []
    if isinstance(findings_raw, list):
        for item in findings_raw:
            normalized_finding = _normalize_finding(item)
            if normalized_finding is not None:
                findings.append(normalized_finding)

    evidence = data.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []

    try:
        confidence = float(data.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))

    normalized = {
        "status": "completed",
        "verdict": verdict,
        "summary": str(data.get("summary") or ""),
        "findings": findings,
        "evidence": evidence,
        "confidence": confidence,
    }
    # FAIL/UNCERTAIN are still successful *executions* (worker completed review)
    return ParsedResult(ok=True, result=normalized, error=None)


def parse_judge_result(stdout: str) -> ParsedResult:
    """Judge structured output. Hard rules remain authoritative; this captures AI proposal."""
    data = _try_load(stdout)
    if data is None:
        return ParsedResult(ok=False, result=None, error="unparseable_or_missing_json")

    verdict_raw = data.get("verdict")
    status = str(data.get("status", "")).lower() if data.get("status") is not None else ""
    if verdict_raw is None:
        if status == "completed":
            verdict_raw = "PASS"
        elif status == "failed":
            verdict_raw = "REJECT"
        else:
            return ParsedResult(ok=False, result=data, error="missing_field:verdict")

    verdict = str(verdict_raw).upper()
    if verdict in {"FAIL", "FAILED"}:
        verdict = "REJECT"
    if verdict not in _JUDGE_VERDICTS:
        return ParsedResult(ok=False, result=data, error=f"invalid_verdict:{_err_val(verdict)}")

    def _list_field(name: str) -> list[Any]:
        value = data.get(name) or []
        return value if isinstance(value, list) else []

    normalized = {
        "status": "completed",
        "verdict": verdict,
        "reason": str(data.get("reason") or data.get("summary") or ""),
        "blocking_findings": _list_field("blocking_findings"),
        "non_blocking_findings": _list_field("non_blocking_findings"),
        "conflicts": _list_field("conflicts"),
        "repair_tasks": _list_field("repair_tasks"),
    }
    return ParsedResult(ok=True, result=normalized, error=None)


def parse_supervisor_result(stdout: str) -> ParsedResult:
    """Parse a supervisor's bounded decision instead of treating it as coding output."""
    data = _try_load(stdout)
    if data is None:
        return ParsedResult(ok=False, result=None, error="unparseable_or_missing_json")
    status = str(data.get("status") or "completed").lower()
    if status in {"done", "success", "ok", "complete"}:
        status = "completed"
    if status != "completed":
        return ParsedResult(ok=False, result=data, error=f"invalid_status:{_err_val(status)}")
    decision = str(data.get("decision") or "RETRY").upper()
    if decision not in {"RETRY", "REASSIGN", "ESCALATE"}:
        return ParsedResult(ok=False, result=data, error=f"invalid_decision:{_err_val(decision)}")
    try:
        confidence = float(data.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    normalized = {
        "status": "completed",
        "decision": decision,
        "reason": str(data.get("reason") or data.get("summary") or ""),
        "instructions": str(
            data.get("instructions") or data.get("guidance") or data.get("summary") or ""
        ),
        "target_role": str(data.get("target_role") or ""),
        "confidence": max(0.0, min(1.0, confidence)),
    }
    return ParsedResult(ok=True, result=normalized, error=None)


# --- HeadBrief (部長所見) ----------------------------------------------------

HEAD_BRIEF_LIMITS = {
    "statement": 400,
    "list_items": 5,
    "list_item_len": 200,
    "open_questions": 3,
    "handoff_requests": 3,
    "work_orders": 2,
    "title": 80,
    "long_text": 600,
    "depends_on_titles": 3,
}
def _known_department_ids() -> set[str]:
    from ai_company.org import DEPARTMENTS

    return {d.id for d in DEPARTMENTS}


def _err_val(value: object, limit: int = 40) -> str:
    """LLM value echoed in an error string: redacted, one line, short.

    Errors flow into ``fallback_reason`` / audit metadata / audit.jsonl (L-1).
    """
    # Truncate first: redaction cost grows with the input, and the value is
    # attacker-sized LLM output (M-1).
    return single_line(redact_text(str(value)[: limit * 5]))[:limit]


_HEAD_BRIEF_STATUSES = frozenset({"completed", "declined"})
_HEAD_DECIDERS = frozenset({"president", "specialist"})
_WORK_ORDER_REQUIRED = (
    "key",
    "section_id",
    "role_required",
    "title",
    "objective",
    "acceptance_criteria",
)
_WORK_ORDER_KEY_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")


def _str_list(value: Any, *, max_items: int, max_len: int) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    return [str(v)[:max_len] for v in value if isinstance(v, (str, int, float))][:max_items]


def parse_head_brief_result(stdout: str) -> ParsedResult:
    """Parse a department head's brief (HeadBrief JSON). Fail closed on shape errors.

    Structure/type errors make the whole brief unparseable (→ template fallback).
    Lengths and list sizes are clamped, never trusted. Semantic checks (own
    department / section / role, known dependencies, caps) are the secretary's
    job in ``head_briefs.validate_head_brief``.
    """
    lim = HEAD_BRIEF_LIMITS
    data = _try_load(stdout)
    if data is None:
        return ParsedResult(ok=False, result=None, error="unparseable_or_missing_json")
    for key in ("status", "department_id", "statement", "work_orders", "confidence"):
        if key not in data:
            return ParsedResult(ok=False, result=data, error=f"missing_field:{key}")
    status = str(data.get("status") or "").lower()
    if status not in _HEAD_BRIEF_STATUSES:
        return ParsedResult(ok=False, result=data, error=f"invalid_status:{_err_val(status)}")
    if not isinstance(data.get("department_id"), str) or not data["department_id"].strip():
        return ParsedResult(ok=False, result=data, error="invalid_department_id")
    if not isinstance(data.get("statement"), str):
        return ParsedResult(ok=False, result=data, error="invalid_statement")
    conf_raw = data.get("confidence")
    if isinstance(conf_raw, bool) or not isinstance(conf_raw, (int, float)):
        return ParsedResult(ok=False, result=data, error="invalid_confidence")
    if not 0.0 <= float(conf_raw) <= 1.0:
        return ParsedResult(ok=False, result=data, error="confidence_out_of_range")

    commitments = _str_list(
        data.get("commitments"), max_items=lim["list_items"], max_len=lim["list_item_len"]
    )
    concerns = _str_list(
        data.get("concerns"), max_items=lim["list_items"], max_len=lim["list_item_len"]
    )
    if commitments is None or concerns is None:
        return ParsedResult(ok=False, result=data, error="invalid_string_list")

    notes: list[str] = []
    questions: list[dict[str, str]] = []
    raw_q = data.get("open_questions") or []
    if not isinstance(raw_q, list):
        return ParsedResult(ok=False, result=data, error="invalid_open_questions")
    for q in raw_q:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str):
            return ParsedResult(ok=False, result=data, error="invalid_open_question")
        decider = str(q.get("decider") or "")
        if decider not in _HEAD_DECIDERS:
            return ParsedResult(ok=False, result=data, error=f"invalid_decider:{_err_val(decider)}")
        questions.append(
            {"question": q["question"][: lim["list_item_len"]], "decider": decider}
        )
    if len(questions) > lim["open_questions"]:
        notes.append("open_questions_truncated")
        questions = questions[: lim["open_questions"]]

    handoffs: list[dict[str, str]] = []
    raw_h = data.get("handoff_requests") or []
    if not isinstance(raw_h, list):
        return ParsedResult(ok=False, result=data, error="invalid_handoff_requests")
    for h in raw_h:
        if (
            not isinstance(h, dict)
            or not isinstance(h.get("to_department_id"), str)
            or not isinstance(h.get("request"), str)
        ):
            return ParsedResult(ok=False, result=data, error="invalid_handoff_request")
        if h["to_department_id"].strip() not in _known_department_ids():
            # Free text from the LLM: only known 部 ids may appear in minutes / constraints.
            notes.append("handoff_unknown_department_dropped")
            continue
        handoffs.append(
            {
                "to_department_id": h["to_department_id"].strip(),
                "request": h["request"][: lim["list_item_len"]],
            }
        )
    if len(handoffs) > lim["handoff_requests"]:
        notes.append("handoff_requests_truncated")
        handoffs = handoffs[: lim["handoff_requests"]]

    raw_orders = data.get("work_orders")
    if not isinstance(raw_orders, list):
        return ParsedResult(ok=False, result=data, error="invalid_work_orders")
    orders: list[dict[str, Any]] = []
    for raw in raw_orders:
        if not isinstance(raw, dict):
            return ParsedResult(ok=False, result=data, error="invalid_work_order")
        for key in _WORK_ORDER_REQUIRED:
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                return ParsedResult(
                    ok=False, result=data, error=f"work_order_missing_field:{key}"
                )
        if not _WORK_ORDER_KEY_RE.match(raw["key"]):
            return ParsedResult(
                ok=False, result=data, error=f"invalid_work_order_key:{_err_val(raw['key'])}"
            )
        priority = raw.get("priority", 50)
        if isinstance(priority, bool) or not isinstance(priority, int):
            return ParsedResult(ok=False, result=data, error="invalid_work_order_priority")
        deps = _str_list(
            raw.get("depends_on_titles"),
            max_items=lim["depends_on_titles"],
            max_len=lim["title"] * 4,
        )
        if deps is None:
            return ParsedResult(ok=False, result=data, error="invalid_depends_on_titles")
        orders.append(
            {
                "key": raw["key"],
                "section_id": raw["section_id"],
                "role_required": raw["role_required"],
                # one line: a newline here would escape the quoted DATA block
                # of later prompts (agent-b T2S4 H-1)
                "title": single_line(raw["title"])[: lim["title"]],
                "objective": single_line(raw["objective"])[: lim["long_text"]],
                "acceptance_criteria": single_line(raw["acceptance_criteria"])[
                    : lim["long_text"]
                ],
                "priority": max(30, min(80, priority)),
                "depends_on_titles": [single_line(d) for d in deps],
            }
        )
    if len(orders) > lim["work_orders"]:
        notes.append("work_orders_truncated")
        orders = orders[: lim["work_orders"]]

    normalized = {
        "status": status,
        "department_id": data["department_id"].strip(),
        "statement": data["statement"][: lim["statement"]],
        "commitments": commitments,
        "concerns": concerns,
        "open_questions": questions,
        "handoff_requests": handoffs,
        "work_orders": orders,
        "confidence": float(conf_raw),
        "parse_notes": notes,
    }
    return ParsedResult(ok=True, result=normalized, error=None)


RoleParseKind = Literal["coding", "reviewer", "judge", "supervisor", "head"]


def parse_for_kind(stdout: str, kind: RoleParseKind) -> ParsedResult:
    if kind == "head":
        return parse_head_brief_result(stdout)
    if kind == "supervisor":
        return parse_supervisor_result(stdout)
    if kind == "reviewer":
        return parse_reviewer_result(stdout)
    if kind == "judge":
        return parse_judge_result(stdout)
    return parse_structured_result(stdout)
