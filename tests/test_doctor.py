"""Doctor CLI / service smoke tests."""

from __future__ import annotations

from pathlib import Path

from ai_company.services.doctor import run_doctor


def test_doctor_runs(tmp_env: Path) -> None:
    report = run_doctor(workspace=tmp_env)
    names = {c.name for c in report.checks}
    assert "python" in names
    assert "git" in names
    assert "config" in names
    # No unexpected crash; FAIL on missing agent is acceptable
    assert report.checks
