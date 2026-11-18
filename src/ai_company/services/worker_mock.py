"""Mock Runner: deterministic worker results for tests and dry runs (no LLM)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import TaskKind
from ai_company.ids import new_id
from ai_company.paths import executions_dir
from ai_company.services.validation import (
    is_judge_task,
    is_validation_task,
)
from ai_company.services.worker_finalize import _finalize_execution, _mark_starting, _utcnow


def mock_department_work_summary(task: Task) -> str:
    """Deterministic deliverable summary for a mock ``department_work`` task."""
    from ai_company.org import unit_label

    try:
        key = str(json.loads(task.constraints or "{}").get("work_order_key") or "-")
    except (json.JSONDecodeError, AttributeError):
        key = "-"
    unit = unit_label(task.department_id, task.section_id)
    return f"[mock] {unit}「{task.title}」の成果物（key={key}）: 受け入れ条件を満たす文書を作成"


def run_mock_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    succeed: bool = True,
) -> Execution:
    """Synchronous mock runner: STARTING -> ACTIVE -> REPORTING -> TERMINATED."""
    _mark_starting(session, task=task, worker=worker)

    exec_id = new_id("EXEC")
    out_dir = executions_dir() / exec_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"

    result: dict[str, Any]
    if succeed:
        if is_validation_task(task):
            result = {
                "status": "completed",
                "verdict": "PASS",
                "summary": f"mock {task.review_type} pass",
                "findings": [],
                "evidence": [{"kind": "mock"}],
                "confidence": 0.95,
                "mock": True,
                "task_id": task.id,
            }
        elif is_judge_task(task):
            result = {
                "status": "completed",
                "verdict": "PASS",
                "reason": "mock judge defers to hard rules",
                "mock": True,
                "task_id": task.id,
            }
        elif task.kind in {TaskKind.HEAD_BRIEF, "head_brief"}:
            from ai_company.services.head_briefs import build_mock_head_brief

            result = build_mock_head_brief(task)
        elif task.kind in {TaskKind.REPLAN, "replan"}:
            result = {
                "status": "completed",
                "decision": "RETRY",
                "reason": "mock supervisor diagnosis",
                "instructions": "Apply the supervisor diagnosis and rerun tests.",
                "target_role": "",
                "confidence": 0.9,
                "mock": True,
                "task_id": task.id,
            }
        elif task.kind in {
            TaskKind.STILL_NEEDED,
            TaskKind.PROJECT_HEALTH,
            TaskKind.PRODUCT_FIT,
            TaskKind.USABILITY_AUDIT,
            TaskKind.CUSTOMER_SUCCESS,
            TaskKind.IT_OPS_AUDIT,
            TaskKind.COMPLIANCE_AUDIT,
            TaskKind.TEST_AUDIT,
            TaskKind.QUALITY_AUDIT,
            TaskKind.SALES_AUDIT,
            TaskKind.MAINTENANCE_AUDIT,
            "still_needed",
            "project_health",
            "product_fit",
            "usability_audit",
            "customer_success",
            "it_ops_audit",
            "compliance_audit",
            "test_audit",
            "quality_audit",
            "sales_audit",
            "maintenance_audit",
        }:
            result = {
                "status": "completed",
                "closure_verdict": "CLEAR",
                "summary": f"mock {task.kind} found nothing remaining",
                "proposed_tasks": [],
                "evidence": [{"kind": "mock_closure"}],
                "mock": True,
                "task_id": task.id,
            }
        elif task.kind in {TaskKind.DEPARTMENT_WORK, "department_work"}:
            # 決定論的な成果要約（乱数・時刻なし）: 自問監査へ要約が渡る流れを mock で確認できる
            result = {
                "status": "completed",
                "summary": mock_department_work_summary(task),
                "issues": [],
                "changed_files": [],
                "mock": True,
                "task_id": task.id,
            }
        else:
            result = {
                "status": "completed",
                "changed_files": [],
                "tests": {"passed": 1, "failed": 0},
                "commit": None,
                "issues": [],
                "mock": True,
                "task_id": task.id,
            }
        stdout_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        exit_code = 0
        success = True
        actual_cost = task.estimated_cost
    else:
        if is_validation_task(task):
            result = {
                "status": "failed",
                "verdict": "FAIL",
                "summary": f"mock {task.review_type} fail",
                "findings": [
                    {
                        "severity": "high",
                        "category": task.review_type or "review",
                        "description": "mock failure",
                        "evidence": [],
                        "recommended_fix": "repair",
                    }
                ],
                "evidence": [],
                "confidence": 0.7,
                "mock": True,
                "task_id": task.id,
            }
        else:
            result = {
                "status": "failed",
                "changed_files": [],
                "tests": {"passed": 0, "failed": 1},
                "commit": None,
                "issues": ["mock failure"],
                "mock": True,
                "task_id": task.id,
            }
        stdout_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        stderr_path.write_text("mock failure\n", encoding="utf-8")
        exit_code = 1
        success = False
        actual_cost = task.estimated_cost * 0.5

    execution = Execution(
        id=exec_id,
        task_id=task.id,
        worker_id=worker.id,
        started_at=_utcnow(),
        finished_at=_utcnow(),
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        success=success,
        estimated_cost=task.estimated_cost,
        actual_cost=actual_cost,
        result_json=result,
        runner_kind="mock",
    )
    session.add(execution)
    session.flush()
    return _finalize_execution(
        session, task=task, worker=worker, execution=execution, success=success
    )
