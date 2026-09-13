"""Progress emitter for live CLI feedback."""

from __future__ import annotations

from ai_company.services.progress import emit, progress_enabled, set_progress


def test_progress_disabled_by_default() -> None:
    set_progress(False)
    assert progress_enabled() is False
    # should not raise
    emit("should be silent")


def test_progress_emits_when_enabled() -> None:
    lines: list[str] = []
    set_progress(True, printer=lines.append)
    emit("hello cycle")
    assert progress_enabled() is True
    assert len(lines) == 1
    assert "hello cycle" in lines[0]
    set_progress(False)
