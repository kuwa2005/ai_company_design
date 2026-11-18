"""Tasks remember their 部/課 (department / section) ownership."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_company.db import migrate_additive_columns
from ai_company.db.models import Task
from ai_company.org import org_unit_ids_for_task, unit_label
from ai_company.services.secretary import department_workload, human_summary, start_goal
from ai_company.services.tasks import create_task, list_tasks


def test_org_unit_rules() -> None:
    assert org_unit_ids_for_task("データモデル設計", "backend_engineer") == (
        "development",
        "design",
    )
    assert org_unit_ids_for_task("API 実装", "coding_worker") == ("development", "build")
    # generalist doing a validation task belongs to 検証課 even without hint words
    assert org_unit_ids_for_task("認証まわり", "coding_worker", "validation") == (
        "development",
        "verification",
    )
    # 合否判定に関わる検証は品質保証部（実装部門から独立）
    assert org_unit_ids_for_task("x", "test_reviewer") == (
        "quality_assurance",
        "independent_review",
    )
    assert org_unit_ids_for_task("x", "edge_case_reviewer") == (
        "quality_assurance",
        "independent_review",
    )
    assert org_unit_ids_for_task("x", "security_reviewer") == (
        "quality_assurance",
        "independent_review",
    )
    assert org_unit_ids_for_task("x", "judge") == ("quality_assurance", "judgement")
    assert org_unit_ids_for_task("x", "compliance_auditor") == ("legal", "compliance")
    # department head doing work directly → 部長直轄
    assert org_unit_ids_for_task("方針", "legal_director") == ("legal", None)
    # dev section chief doing a replan is still split by work kind
    assert org_unit_ids_for_task("再計画: スコープ分解", "engineering_manager") == (
        "development",
        "design",
    )
    assert unit_label("legal", None) == "法務部（部長直轄）"
    assert unit_label("development", "verification") == "開発部／検証課"
    assert unit_label(None, None) == "（所属未定）"
    assert unit_label("unknown_dept", "s") == "unknown_dept"


def test_create_task_stores_unit_and_allows_override(db_session: Session, tmp_env) -> None:
    t = create_task(db_session, title="個人情報の点検", role_required="compliance_auditor")
    assert (t.department_id, t.section_id) == ("legal", "compliance")
    o = create_task(
        db_session,
        title="提案書",
        role_required="coding_worker",
        department_id="sales",
        section_id="sales_proposal",
    )
    assert (o.department_id, o.section_id) == ("sales", "sales_proposal")
    assert [x.id for x in list_tasks(db_session, department_id="legal")] == [t.id]


def test_start_goal_tasks_match_meeting_assignments(db_session: Session, tmp_env) -> None:
    result = start_goal(db_session, "顧客向けの勤怠管理Webサービスを作って販売したい")
    assert result.meeting is not None
    by_title = {a.task_title: a for a in result.meeting.assignments}
    assert result.tasks
    for task in result.tasks:
        assert task.department_id, task.title
        a = by_title[task.title]
        assert task.department_id == a.department_id
        # minutes show the same 課 name (heads' direct work is shown under 課1)
        if task.section_id:
            assert unit_label(task.department_id, task.section_id).endswith(a.section_ja)

    rows = department_workload(db_session, project_id=result.project.id)
    assert sum(int(r["total"]) for r in rows) == len(
        list_tasks(db_session, project_id=result.project.id)
    )
    labels = [r["label"] for r in rows]
    assert any(str(label).startswith("開発部／") for label in labels)
    summary = human_summary(db_session, project_id=result.project.id)
    assert "部門別の状況（部／課）" in summary


def test_migrate_adds_columns_and_backfills_old_db(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE tasks (id VARCHAR(64) PRIMARY KEY, title VARCHAR(512), "
                "role_required VARCHAR(64), kind VARCHAR(32))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks VALUES "
                "('T1', 'E2E 確認', 'coding_worker', 'implementation'), "
                "('T2', '規約点検', 'compliance_auditor', 'compliance_audit')"
            )
        )
    added = migrate_additive_columns(engine)
    assert added == ["tasks.department_id", "tasks.section_id"]
    cols = {c["name"] for c in inspect(engine).get_columns("tasks")}
    assert {"department_id", "section_id"} <= cols
    with engine.connect() as conn:
        rows = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute(text("SELECT id, department_id, section_id FROM tasks"))
        )
    assert rows == {"T1": ("development", "verification"), "T2": ("legal", "compliance")}
    # idempotent
    assert migrate_additive_columns(engine) == []
    engine.dispose()


def test_migrate_skips_missing_table(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert migrate_additive_columns(engine) == []
    engine.dispose()


def test_cli_task_list_dept_filter(db_session: Session, tmp_env) -> None:
    from ai_company.cli import app

    create_task(db_session, title="規約点検", role_required="compliance_auditor")
    create_task(db_session, title="API 実装", role_required="coding_worker")
    db_session.commit()

    runner = CliRunner()
    out = runner.invoke(app, ["task", "list", "--dept", "法務部"])
    assert out.exit_code == 0, out.output
    assert "規約点検" in out.output and "API 実装" not in out.output
    out = runner.invoke(app, ["task", "list"])
    assert out.exit_code == 0, out.output
    assert "法務部" in out.output  # 部／課 column (rich may wrap the 課 name)
    out = runner.invoke(app, ["task", "list", "--dept", "存在しない部"])
    assert out.exit_code == 2
    out = runner.invoke(app, ["status"])
    assert out.exit_code == 0, out.output
    assert "部門別の状況" in out.output


def test_task_model_has_columns() -> None:
    cols = {c.name for c in Task.__table__.columns}
    assert {"department_id", "section_id"} <= cols
