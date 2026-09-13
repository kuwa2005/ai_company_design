"""Unit tests for prompt builder and structured result parser."""

from __future__ import annotations

import json

from ai_company.services.result_parser import parse_structured_result


def test_parse_valid_completed() -> None:
    raw = '{"status":"completed","changed_files":["a.py"],"tests":{"passed":1,"failed":0},"commit":null,"issues":[]}'
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
    raw = 'Done.\n{"status":"completed","changed_files":[],"tests":{"passed":0,"failed":0},"commit":null,"issues":[]}\n'
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
