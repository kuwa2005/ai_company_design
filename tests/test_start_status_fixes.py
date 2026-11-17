"""T2 STEP7: status の予算待ち案内 / start 画面の部門作業・部長所見見送り / mock の部門作業要約."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_company.db.models import Task
from ai_company.enums import TaskKind
from ai_company.services.autonomous import run_autonomous
from ai_company.services.prompt_builder import build_task_prompt
from ai_company.services.secretary import human_next_actions, start_goal
from ai_company.services.workers import mock_department_work_summary
from tests.conftest import write_settings_override
from tests.test_budget_wait import _caps, _spend, _utcnow

SAAS_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい"


# (1) standalone status explains the budget wait --------------------------------


def test_standalone_status_reports_budget_wait(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, hourly_runs=2)
    result = start_goal(db_session, SAAS_GOAL)
    now = _utcnow()
    _spend(db_session, at=now - timedelta(minutes=20))
    _spend(db_session, at=now - timedelta(minutes=10))
    text = human_next_actions(db_session, project_id=result.project.id)
    assert "予算待ち" in text
    assert "人の操作は不要" not in text
    assert "config/budget.yaml" in text


def test_standalone_status_below_cap_unchanged(db_session: Session, tmp_env: Path) -> None:
    _caps(tmp_env, hourly_runs=100)
    result = start_goal(db_session, SAAS_GOAL)
    _spend(db_session, at=_utcnow() - timedelta(minutes=5))
    text = human_next_actions(db_session, project_id=result.project.id)
    assert "人の操作は不要" in text
    assert "予算待ち（時間枠の上限）" not in text


# (2) start summary includes work orders -----------------------------------------


def test_start_summary_counts_work_orders(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    assert result.work_order_tasks
    for t in result.work_order_tasks:
        assert t.department_id
    # the department digest is rebuilt after the work orders were filed
    assert "営業部／" in result.summary
    assert "法務部／" in result.summary


def test_cli_start_lists_work_orders(tmp_env: Path) -> None:
    from ai_company.cli import app

    out = CliRunner().invoke(app, ["start", SAAS_GOAL])
    assert out.exit_code == 0, out.output
    assert "部門作業指示: 4" in out.output
    assert "営業部／" in out.output


# (3) start shows skipped head briefs --------------------------------------------


def test_cli_start_shows_all_heads_skipped(tmp_env: Path) -> None:
    from ai_company.cli import app

    write_settings_override(tmp_env, executive_meeting={"head_llm": {"enabled": True}})
    out = CliRunner().invoke(app, ["start", SAAS_GOAL])
    assert out.exit_code == 0, out.output
    assert "部長所見を見送り: 3部" in out.output
    assert "営業部:" in out.output and "法務部:" in out.output
    assert "部長が全員見送り" in out.output
    assert "permissions.yaml" in out.output


def test_start_result_records_skipped_heads(db_session: Session, tmp_env: Path) -> None:
    write_settings_override(tmp_env, executive_meeting={"head_llm": {"enabled": True}})
    result = start_goal(db_session, SAAS_GOAL)
    assert result.head_brief_tasks == []
    assert set(result.head_brief_skipped) == {"sales", "marketing", "legal"}


def test_cli_start_partial_skip_has_no_all_skipped_hint(tmp_env: Path) -> None:
    from ai_company.cli import app

    path = tmp_env / "config" / "permissions.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["profiles"]["sales_director"] = {"permissions": ["code.read"]}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    write_settings_override(tmp_env, executive_meeting={"head_llm": {"enabled": True}})
    out = CliRunner().invoke(app, ["start", SAAS_GOAL])
    assert out.exit_code == 0, out.output
    assert "部長所見: 1件" in out.output
    assert "部長所見を見送り: 2部" in out.output
    assert "部長が全員見送り" not in out.output


def test_default_off_shows_no_head_lines(tmp_env: Path) -> None:
    from ai_company.cli import app

    out = CliRunner().invoke(app, ["start", SAAS_GOAL])
    assert out.exit_code == 0, out.output
    assert "部長所見" not in out.output


# (4) mock department_work returns a deterministic summary -----------------------


def test_mock_department_work_summary_reaches_closure(
    db_session: Session, tmp_env: Path
) -> None:
    _caps(tmp_env)
    result = start_goal(db_session, SAAS_GOAL)
    orders = result.work_order_tasks
    assert orders
    expected = {t.id: mock_department_work_summary(t) for t in orders}
    # deterministic: same input → same text, no time / random ids
    assert expected == {t.id: mock_department_work_summary(t) for t in orders}
    for t, text in zip(orders, expected.values(), strict=True):
        key = json.loads(t.constraints)["work_order_key"]
        assert f"key={key}" in text and t.title in text

    run_autonomous(
        db_session, project_id=result.project.id, max_cycles=300, runner="mock",
        mock_succeed=True,
    )
    stored = {}
    for t in orders:
        db_session.refresh(t)
        stored[t.id] = json.loads(t.constraints).get("work_order_summary")
    assert stored == expected

    # every closure audit sees the mock summaries as data
    closure = db_session.scalar(
        select(Task)
        .where(Task.project_id == result.project.id, Task.kind == TaskKind.SALES_AUDIT)
        .limit(1)
    )
    assert closure is not None
    prompt = build_task_prompt(closure, session=db_session)
    for text in expected.values():
        assert text in prompt
