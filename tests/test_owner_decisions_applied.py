"""社長判断 3件の反映.

①部長の権限プロファイル ②hourly max_runs 60 ③部門成果物を docs/<部>/ に出力.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import BudgetConfig, clear_config_cache, load_config
from ai_company.db.models import Task
from ai_company.enums import TaskKind
from ai_company.services.department_orders import (
    deliverable_path,
    write_department_deliverable,
)
from ai_company.services.policy_engine import evaluate_dispatch, permissions_for_role
from ai_company.services.result_parser import parse_structured_result
from ai_company.services.secretary import start_goal
from tests.conftest import write_settings_override

SAAS_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい"
REPO = Path(__file__).resolve().parents[1]
HEADS = (
    "qa_director",
    "sales_director",
    "marketing_director",
    "legal_director",
    "operations_director",
)


def _orders(session: Session, project_id: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task).where(
                Task.project_id == project_id, Task.kind == TaskKind.DEPARTMENT_WORK
            )
        ).all()
    )


# --- ① permissions ---------------------------------------------------------


def test_department_heads_have_read_only_profiles(tmp_env: Path) -> None:
    cfg = load_config()
    for role in HEADS:
        perms = permissions_for_role(cfg, role)
        assert perms == {"code.read", "validation.read", "project.report"}, role
    assert permissions_for_role(cfg, "test_reviewer") == {
        "code.read",
        "test.execute",
        "validation.report",
    }


def test_head_briefs_are_no_longer_skipped(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(
        tmp_env, executive_meeting={"head_llm": {"enabled": True}}
    )
    result = start_goal(db_session, SAAS_GOAL)
    assert result.head_brief_tasks, result.head_brief_skipped
    assert not result.head_brief_skipped
    cfg = load_config()
    for t in result.head_brief_tasks:
        decision = evaluate_dispatch(task=t, role=t.role_required, config=cfg)
        assert decision.allowed, decision.reason
        # head_brief stays read-only even with the new profile
        write = evaluate_dispatch(task=t, role=t.role_required, config=cfg, action="code.edit")
        assert not write.allowed


# --- ② budget --------------------------------------------------------------


def test_hourly_max_runs_is_60() -> None:
    raw = yaml.safe_load((REPO / "config" / "budget.yaml").read_text(encoding="utf-8"))
    assert raw["budget"]["hourly"]["max_runs"] == 60
    assert BudgetConfig().budget.hourly.max_runs == 60


# --- ③ deliverables --------------------------------------------------------


def test_parser_keeps_document_field() -> None:
    out = json.dumps(
        {"status": "completed", "changed_files": [], "summary": "s", "document": "# 提案書\n本文"}
    )
    parsed = parse_structured_result(out)
    assert parsed.ok
    assert parsed.result is not None
    assert parsed.result["document"] == "# 提案書\n本文"


def test_deliverable_written_under_docs_dept(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    task = _orders(db_session, result.project.id)[0]
    payload = {
        "summary": "要点",
        "document": "## 提案\n価格は月額\x1b[31m 500円\r\nパスワード: hunter2secret",
        "issues": ["価格未定"],
    }
    path = write_department_deliverable(task, payload)
    assert path == tmp_env / "docs" / str(task.department_id) / f"{task.id}.md"
    text = path.read_text(encoding="utf-8")
    assert "## 提案" in text
    assert "\x1b" not in text and "\r" not in text
    assert "hunter2secret" not in text
    assert "## 未決事項" in text and "価格未定" in text
    data = json.loads(task.constraints)
    assert data["work_order_document_path"] == f"docs/{task.department_id}/{task.id}.md"


def test_deliverable_falls_back_to_summary(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    task = _orders(db_session, result.project.id)[0]
    path = write_department_deliverable(task, {"summary": "要約だけ"})
    assert path is not None
    assert "要約だけ" in path.read_text(encoding="utf-8")


def test_deliverable_disabled(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(tmp_env, meeting_resolutions={"write_deliverables": False})
    result = start_goal(db_session, SAAS_GOAL)
    task = _orders(db_session, result.project.id)[0]
    assert write_department_deliverable(task, {"summary": "x"}) is None
    assert not (tmp_env / "docs").exists()


def test_deliverable_path_refuses_escape(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    task = _orders(db_session, result.project.id)[0]
    cfg = load_config()

    for bad in ("../outside", "/etc", "docs/../../x"):
        write_settings_override(tmp_env, meeting_resolutions={"deliverables_dir": bad})
        assert deliverable_path(task, load_config()) is None, bad
    write_settings_override(tmp_env, meeting_resolutions={"deliverables_dir": "docs"})
    clear_config_cache()

    original = task.department_id
    task.department_id = "../../etc"
    assert deliverable_path(task, cfg) is None
    task.department_id = original

    outside = tmp_env.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    (tmp_env / "docs").symlink_to(outside, target_is_directory=True)
    assert deliverable_path(task, load_config()) is None
    assert write_department_deliverable(task, {"summary": "x"}) is None
    assert json.loads(task.constraints)["work_order_document_error"] == "path_refused"
    assert not any(outside.iterdir())


def test_department_work_worker_stays_read_only(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    cfg = load_config()
    for t in _orders(db_session, result.project.id):
        d = evaluate_dispatch(task=t, role=t.role_required, config=cfg, action="code.edit")
        assert not d.allowed
