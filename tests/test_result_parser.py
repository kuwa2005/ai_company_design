"""Unit tests for prompt builder and structured result parser."""

from __future__ import annotations

import json

from ai_company.services.result_parser import (
    parse_for_kind,
    parse_judge_result,
    parse_reviewer_result,
    parse_structured_result,
    parse_supervisor_result,
)


def test_parse_valid_completed() -> None:
    raw = json.dumps(
        {
            "status": "completed",
            "changed_files": ["a.py"],
            "tests": {"passed": 1, "failed": 0},
            "commit": None,
            "issues": [],
        }
    )
    parsed = parse_structured_result(raw)
    assert parsed.ok is True
    assert parsed.result is not None
    assert parsed.result["status"] == "completed"


def test_parse_broken_json_is_not_success() -> None:
    parsed = parse_structured_result("not json at all")
    assert parsed.ok is False
    assert parsed.error == "unparseable_or_missing_json"


def test_parse_missing_status_is_not_success() -> None:
    parsed = parse_structured_result('{"changed_files":[]}')
    assert parsed.ok is False
    assert parsed.error == "missing_field:status"


def test_parse_failed_status_is_not_success() -> None:
    parsed = parse_structured_result('{"status":"failed","issues":["x"]}')
    assert parsed.ok is False
    assert parsed.error == "status_not_completed"


def test_parse_json_embedded_in_prose() -> None:
    payload = {
        "status": "completed",
        "changed_files": [],
        "tests": {"passed": 0, "failed": 0},
        "commit": None,
        "issues": [],
    }
    raw = f"Done.\n{json.dumps(payload)}\n"
    parsed = parse_structured_result(raw)
    assert parsed.ok is True


def test_parse_cursor_agent_envelope() -> None:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(
            {
                "status": "completed",
                "changed_files": [],
                "tests": {"passed": 0, "failed": 0},
                "commit": None,
                "issues": [],
            }
        ),
    }
    parsed = parse_structured_result(json.dumps(envelope))
    assert parsed.ok is True
    assert parsed.result is not None
    assert parsed.result["status"] == "completed"


def test_parse_cursor_envelope_prose_plus_json() -> None:
    """Real agent often puts Japanese prose then JSON inside envelope.result."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": (
            "PRD草案の作成に着手します。人間レビュー可能なPRDを1文書として作成します。"
            '{"status":"completed","changed_files":["docs/PRD.md"],'
            '"tests":{"passed":0,"failed":0},"commit":null,"issues":[]}'
        ),
    }
    parsed = parse_structured_result(json.dumps(envelope))
    assert parsed.ok is True
    assert parsed.result is not None
    assert parsed.result["status"] == "completed"
    assert parsed.result["changed_files"] == ["docs/PRD.md"]


def test_parse_reviewer_verdict_only() -> None:
    raw = {
        "verdict": "FAIL",
        "summary": "missing auth",
        "findings": [
            {
                "severity": "critical",
                "category": "authn",
                "description": "no auth check",
                "evidence": ["api.py:10"],
                "recommended_fix": "add middleware",
            }
        ],
        "evidence": ["diff"],
        "confidence": 0.91,
    }
    parsed = parse_reviewer_result(json.dumps(raw))
    assert parsed.ok is True
    assert parsed.result is not None
    assert parsed.result["verdict"] == "FAIL"
    assert parsed.result["findings"][0]["severity"] == "critical"


def test_parse_reviewer_envelope() -> None:
    envelope = {
        "type": "result",
        "result": {"verdict": "PASS", "summary": "ok", "findings": [], "confidence": 0.8},
    }
    parsed = parse_reviewer_result(json.dumps(envelope))
    assert parsed.ok is True
    assert parsed.result["verdict"] == "PASS"


def test_parse_reviewer_invalid_verdict() -> None:
    parsed = parse_reviewer_result('{"verdict":"MAYBE"}')
    assert parsed.ok is False
    assert parsed.error and "invalid_verdict" in parsed.error


def test_parse_judge_result_ok() -> None:
    raw = {
        "verdict": "HUMAN_ESCALATION",
        "reason": "conflict",
        "blocking_findings": [],
        "conflicts": [{"a": "PASS", "b": "FAIL"}],
        "repair_tasks": [],
    }
    parsed = parse_judge_result(json.dumps(raw))
    assert parsed.ok is True
    assert parsed.result["verdict"] == "HUMAN_ESCALATION"


def test_parse_judge_fail_maps_to_reject() -> None:
    parsed = parse_judge_result('{"verdict":"FAIL","reason":"x"}')
    assert parsed.ok is True
    assert parsed.result["verdict"] == "REJECT"


def test_parse_for_kind_routes() -> None:
    assert parse_for_kind('{"verdict":"PASS"}', "reviewer").ok is True
    assert parse_for_kind('{"verdict":"PASS"}', "judge").ok is True
    assert parse_for_kind('{"status":"completed"}', "coding").ok is True
    assert parse_for_kind('{"decision":"RETRY"}', "supervisor").ok is True


def test_parse_supervisor_decision_preserves_instructions() -> None:
    parsed = parse_supervisor_result(
        json.dumps(
            {
                "status": "completed",
                "decision": "REASSIGN",
                "reason": "backend ownership",
                "instructions": "Fix the transaction boundary and rerun tests.",
                "target_role": "backend_engineer",
                "confidence": 0.88,
            }
        )
    )
    assert parsed.ok is True
    assert parsed.result is not None
    assert parsed.result["decision"] == "REASSIGN"
    assert parsed.result["target_role"] == "backend_engineer"
    assert "transaction" in parsed.result["instructions"]


def test_parse_status_done_alias() -> None:
    parsed = parse_structured_result(
        '{"status":"done","changed_files":["a.md"],"tests":{},"commit":null,"issues":[]}'
    )
    assert parsed.ok is True
    assert parsed.result is not None
    assert parsed.result["status"] == "completed"
