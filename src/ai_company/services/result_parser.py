"""Parse structured worker results. Broken JSON is never success."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


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
        return ParsedResult(ok=False, result=data, error=f"invalid_status:{status}")

    # Normalize optional fields
    normalized = {
        "status": status,
        "changed_files": data.get("changed_files") or [],
        "tests": data.get("tests") or {"passed": 0, "failed": 0},
        "commit": data.get("commit"),
        "issues": data.get("issues") or data.get("unresolved_issues") or [],
    }
    if status != "completed":
        return ParsedResult(ok=False, result=normalized, error="status_not_completed")
    return ParsedResult(ok=True, result=normalized, error=None)


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
        return ParsedResult(ok=False, result=data, error=f"invalid_verdict:{verdict}")

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
        return ParsedResult(ok=False, result=data, error=f"invalid_verdict:{verdict}")

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
        return ParsedResult(ok=False, result=data, error=f"invalid_status:{status}")
    decision = str(data.get("decision") or "RETRY").upper()
    if decision not in {"RETRY", "REASSIGN", "ESCALATE"}:
        return ParsedResult(ok=False, result=data, error=f"invalid_decision:{decision}")
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


RoleParseKind = Literal["coding", "reviewer", "judge", "supervisor"]


def parse_for_kind(stdout: str, kind: RoleParseKind) -> ParsedResult:
    if kind == "supervisor":
        return parse_supervisor_result(stdout)
    if kind == "reviewer":
        return parse_reviewer_result(stdout)
    if kind == "judge":
        return parse_judge_result(stdout)
    return parse_structured_result(stdout)
