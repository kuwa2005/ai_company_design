"""Human-visible progress lines for long-running CLI loops."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO

_enabled: bool = False
_stream: TextIO = sys.stderr
_printer: Callable[[str], None] | None = None


def set_progress(
    enabled: bool,
    *,
    stream: TextIO | None = None,
    printer: Callable[[str], None] | None = None,
) -> None:
    global _enabled, _stream, _printer
    _enabled = enabled
    if stream is not None:
        _stream = stream
    _printer = printer


def progress_enabled() -> bool:
    return _enabled


def emit(message: str) -> None:
    if not _enabled:
        return
    ts = datetime.now(UTC).strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    if _printer is not None:
        _printer(line)
        return
    print(line, file=_stream, flush=True)
