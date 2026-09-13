"""Parse structured worker results. Broken JSON is never success."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedResult:
    ok: bool
    result: dict[str, Any] | None
    error: str | None = None


_REQUIRED = ("status",)


def _coerce_dict(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            inner = json.loads(data)
        except json.JSONDecodeError:
            return None
        return inner if isinstance(inner, dict) else None
    return None


def _unwrap_agent_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """
    Cursor `agent --output-format json` may wrap the worker payload:

      {"type":"result","subtype":"success","result":"<json-string-or-object>", ...}

    Prefer the inner payload when present.
    """
    if "status" in data:
        return data
    if data.get("type") == "result" and "result" in data:
        inner = _coerce_dict(data["result"])
        if inner is not None:
            return inner
    return data


def _try_load(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _unwrap_agent_envelope(data)
    except json.JSONDecodeError:
        pass

    # Agent may wrap JSON in prose; take the last {...} block.
    matches = list(re.finditer(r"\{[\s\S]*\}", text))
    for match in reversed(matches):
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return _unwrap_agent_envelope(data)
        except json.JSONDecodeError:
            continue
    return None


def parse_structured_result(stdout: str) -> ParsedResult:
    data = _try_load(stdout)
    if data is None:
        return ParsedResult(ok=False, result=None, error="unparseable_or_missing_json")

    for key in _REQUIRED:
        if key not in data:
            return ParsedResult(ok=False, result=data, error=f"missing_field:{key}")

    status = str(data.get("status", "")).lower()
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
