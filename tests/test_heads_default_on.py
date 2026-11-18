"""部長所見の既定オン + 設計担当 / 受け入れテスト担当（2026-11-18 社長判断）."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import HeadLLMConfig, clear_config_cache, load_config
from ai_company.db.models import Task
from ai_company.enums import TaskKind
from ai_company.org import org_unit_ids_for_task
from ai_company.roles import supervisor_of
from ai_company.services.policy_engine import evaluate_dispatch
from ai_company.services.prompt_builder import load_role_prompt
from ai_company.services.secretary import start_goal, understand_goal
from tests.conftest import run_until_released, write_validation_override

REPO = Path(__file__).resolve().parents[1]
SAAS_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい"


def _use_shipped_config(tmp_env: Path) -> None:
    """Undo the test baseline overrides: shipped settings and budget as is."""
    for name in ("settings.yaml", "budget.yaml"):
        shutil.copy(REPO / "config" / name, tmp_env / "config" / name)
    clear_config_cache()


# --- head_llm default on ------------------------------------------------------


def test_head_llm_is_on_by_default() -> None:
    raw = yaml.safe_load((REPO / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert raw["executive_meeting"]["head_llm"]["enabled"] is True
    assert HeadLLMConfig().enabled is True


def test_shipped_defaults_complete_saas_goal_within_hourly_cap(
    db_session: Session, tmp_env: Path
) -> None:
    _use_shipped_config(tmp_env)
    phases = {
        k: {"max_rounds": 1, "consecutive_clear_to_advance": 1}
        for k in (
            "still_needed",
            "project_health",
            "product_fit",
            "usability",
            "customer_success",
            "it_operations",
            "compliance",
            "test_audit",
            "quality",
            "sales",
            "maintenance",
        )
    }
    write_validation_override(tmp_env, closure_loops={"enabled": True, **phases})
    assert load_config().budget.budget.hourly.max_runs == 60

    result = start_goal(db_session, SAAS_GOAL)
    assert result.head_brief_tasks, result.head_brief_skipped
    assert not result.head_brief_skipped

    # 社長への確認事項（価格・法務・本番体制）に回答してから完了まで流す
    auto = run_until_released(
        db_session, result.project.id, max_cycles=300, runner="mock", mock_succeed=True
    )
    assert auto.budget_wait is None
    assert auto.release_candidate_id is not None, auto.stopped_reason
    docs = sorted(p.relative_to(tmp_env).as_posix() for p in (tmp_env / "docs").rglob("*.md"))
    assert docs, "department deliverables should be written"


# --- design_engineer / acceptance_engineer -------------------------------------


def test_plan_assigns_design_and_acceptance_roles(tmp_env: Path) -> None:
    roles = {t.title: t.role_required for t in understand_goal(SAAS_GOAL).tasks}
    assert roles["Goal分解とスコープ定義"] == "design_engineer"
    assert roles["アーキテクチャ／境界整理"] == "design_engineer"
    assert roles["E2E / 受け入れ確認"] == "acceptance_engineer"
    assert roles["管理者デモシナリオ"] == "acceptance_engineer"
    # core implementation stays with the build section
    assert roles["API 実装"] == "backend_engineer"


def test_new_roles_belong_to_dev_sections(tmp_env: Path) -> None:
    assert org_unit_ids_for_task("Goal分解とスコープ定義", "design_engineer") == (
        "development",
        "design",
    )
    assert org_unit_ids_for_task("E2E / 受け入れ確認", "acceptance_engineer") == (
        "development",
        "verification",
    )


def test_new_roles_escalate_to_engineering_manager() -> None:
    # Not test_reviewer (which reports to judge): a failed E2E task must be
    # re-planned by the engineering manager, not by the validation judge.
    assert supervisor_of("design_engineer") == "engineering_manager"
    assert supervisor_of("acceptance_engineer") == "engineering_manager"


def test_new_roles_can_write_their_implementation_tasks(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, SAAS_GOAL)
    cfg = load_config()
    tasks = list(
        db_session.scalars(
            select(Task).where(
                Task.project_id == result.project.id,
                Task.role_required.in_(("design_engineer", "acceptance_engineer")),
            )
        ).all()
    )
    assert {t.role_required for t in tasks} == {"design_engineer", "acceptance_engineer"}
    for t in tasks:
        assert t.kind == TaskKind.IMPLEMENTATION
        d = evaluate_dispatch(task=t, role=t.role_required, config=cfg)
        assert d.allowed, (t.title, d.reason)


def test_new_role_prompts_exist() -> None:
    assert "設計担当" in load_role_prompt("design_engineer")
    assert "受け入れテスト担当" in load_role_prompt("acceptance_engineer")
