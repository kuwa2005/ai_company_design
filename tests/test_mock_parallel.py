"""Parallel mock worker dispatch."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ai_company.services.executor import run_mock_jobs_parallel
from ai_company.services.tasks import create_project, create_task
from ai_company.services.workers import hire_worker


def test_mock_jobs_parallel(db_session: Session) -> None:
    project = create_project(db_session, name="par", goal="g")
    jobs: list[tuple[str, str]] = []
    for i in range(2):
        t = create_task(
            db_session,
            title=f"t{i}",
            project_id=project.id,
            kind="implementation",
        )
        t.status = "READY"
        w = hire_worker(db_session, task=t)
        jobs.append((t.id, w.id))
    # Parallel workers open new sessions; commit so rows are visible
    db_session.commit()
    report = run_mock_jobs_parallel(jobs, mock_succeed=True, max_workers=2)
    assert report.ok
    assert len(report.results) == 2
