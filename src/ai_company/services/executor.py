"""Worker executor: support concurrent mock/cursor jobs via threads + Popen."""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field


@dataclass
class JobResult:
    job_id: str
    ok: bool
    elapsed: float
    detail: str = ""


@dataclass
class ParallelRunReport:
    results: list[JobResult] = field(default_factory=list)

    @property
    def wall_time(self) -> float:
        if not self.results:
            return 0.0
        return max(r.elapsed for r in self.results)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


def run_sleep_jobs_parallel(
    durations: list[float],
    *,
    max_workers: int | None = None,
) -> ParallelRunReport:
    """Fixture helper: run N sleep subprocesses in parallel (P0-5 acceptance)."""
    report = ParallelRunReport()
    workers = max_workers or len(durations)

    def _one(i: int, seconds: float) -> JobResult:
        t0 = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603
            ["sleep", str(seconds)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        elapsed = time.monotonic() - t0
        return JobResult(
            job_id=f"sleep-{i}",
            ok=proc.returncode == 0,
            elapsed=elapsed,
            detail=f"pid={proc.pid}",
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, i, d) for i, d in enumerate(durations)]
        for fut in as_completed(futs):
            report.results.append(fut.result())
    return report


def run_mock_jobs_parallel(
    jobs: list[tuple[str, str]],
    *,
    mock_succeed: bool = True,
    max_workers: int = 4,
) -> ParallelRunReport:
    """
    Run mock workers for (task_id, worker_id) pairs in parallel threads.
    Each thread opens its own DB session (SQLite-safe under WAL).
    """
    from ai_company.db import session_scope
    from ai_company.db.models import Task, Worker
    from ai_company.services.workers import run_mock_worker

    report = ParallelRunReport()

    def _one(task_id: str, worker_id: str) -> JobResult:
        t0 = time.monotonic()
        try:
            with session_scope() as session:
                task = session.get(Task, task_id)
                worker = session.get(Worker, worker_id)
                if task is None or worker is None:
                    return JobResult(task_id, False, time.monotonic() - t0, "missing")
                ex = run_mock_worker(
                    session, task=task, worker=worker, succeed=mock_succeed
                )
                return JobResult(
                    task_id,
                    bool(ex.success),
                    time.monotonic() - t0,
                    f"exec={ex.id}",
                )
        except Exception as exc:
            return JobResult(task_id, False, time.monotonic() - t0, str(exc))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_one, tid, wid) for tid, wid in jobs]
        for fut in as_completed(futs):
            report.results.append(fut.result())
    return report
