"""Parallel executor acceptance (P0-5)."""

from __future__ import annotations

from ai_company.services.executor import run_sleep_jobs_parallel


def test_parallel_sleep_faster_than_serial() -> None:
    report = run_sleep_jobs_parallel([0.4, 0.4], max_workers=2)
    assert report.ok
    # Parallel wall clock should be well under serial 0.8s
    assert report.wall_time < 0.75
