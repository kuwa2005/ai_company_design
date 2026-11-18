"""部長所見（head_brief）: optional department-head workers (default off)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import clear_config_cache, load_config
from ai_company.db.models import AuditEvent, Task, TaskDependency
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.services.closure_loops import maybe_spawn_closure_loop
from ai_company.services.cursor_runner import build_cursor_command
from ai_company.services.head_briefs import (
    apply_head_brief,
    build_mock_head_brief,
    validate_head_brief,
)
from ai_company.services.policy_engine import infer_dispatch_action, role_allows_writes
from ai_company.services.prompt_builder import build_task_prompt
from ai_company.services.result_parser import parse_head_brief_result
from ai_company.services.secretary import start_goal
from ai_company.services.workers import dispatch_worker, hire_worker
from tests.conftest import (
    drop_head_permissions,
    run_until_released,
    write_settings_override,
    write_validation_override,
)

SAAS_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい"
HEADS = ("sales_director", "marketing_director", "legal_director", "operations_director")


def _grant_head_permissions(tmp_env: Path) -> None:
    """Test-only: the repo's permissions.yaml has no head profiles (by design)."""
    path = tmp_env / "config" / "permissions.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for role in HEADS:
        data["profiles"][role] = {"permissions": ["code.read", "validation.read"]}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    clear_config_cache()


def _enable_heads(tmp_env: Path, **head: object) -> None:
    write_settings_override(
        tmp_env, executive_meeting={"head_llm": {"enabled": True, **head}}
    )


def _of_kind(session: Session, project_id: str, kind: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.kind == kind)
            .order_by(Task.created_at.asc())
        ).all()
    )


def _units(session: Session, project_id: str) -> set[tuple[str, str | None, str | None]]:
    return {
        (json.loads(t.constraints)["work_order_key"], t.department_id, t.section_id)
        for t in _of_kind(session, project_id, TaskKind.DEPARTMENT_WORK.value)
    }


def _events(session: Session, event_type: str) -> list[AuditEvent]:
    return list(
        session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)).all()
    )


def _brief(dept: str = "legal", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "completed",
        "department_id": dept,
        "statement": "論点を先に洗い出します",
        "commitments": [],
        "concerns": [],
        "open_questions": [{"question": "個人情報の保存期間", "decider": "specialist"}],
        "handoff_requests": [{"to_department_id": "development", "request": "ログ設計"}],
        "work_orders": [
            {
                "key": "legal.privacy_review",
                "section_id": "compliance",
                "role_required": "compliance_auditor",
                "title": "個人情報の取扱い点検",
                "objective": "保存項目と保存期間を点検",
                "acceptance_criteria": "点検表がある",
                "priority": 95,
                "depends_on_titles": ["存在しないタスク", "Goal分解とスコープ定義"],
            }
        ],
        "confidence": 0.8,
    }
    base.update(over)
    return base


# --- default off ----------------------------------------------------------


def test_default_off_changes_nothing(db_session: Session, tmp_env: Path) -> None:
    cfg = load_config()
    assert cfg.settings.executive_meeting.head_llm.enabled is False
    result = start_goal(db_session, SAAS_GOAL)
    assert result.head_brief_tasks == []
    assert _of_kind(db_session, result.project.id, TaskKind.HEAD_BRIEF.value) == []
    assert len(result.work_order_tasks) == 6
    assert _events(db_session, "head_brief_requested") == []


def test_enabled_without_permission_profile_falls_back_at_filing(
    db_session: Session, tmp_env: Path
) -> None:
    # No head profiles (removed from the test copy) → the scheduler would deny.
    drop_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    result = start_goal(db_session, SAAS_GOAL)
    assert result.head_brief_tasks == []
    assert len(result.work_order_tasks) == 6  # templates issued immediately
    skipped = _events(db_session, "head_brief_skipped")
    assert {e.metadata_json["department_id"] for e in skipped} == {
        "sales",
        "marketing",
        "legal",
        "operations",
    }
    assert all("unknown_or_empty_role_profile" in e.metadata_json["reason"] for e in skipped)


# --- filing ---------------------------------------------------------------


def test_enabled_files_briefs_and_holds_templates(db_session: Session, tmp_env: Path) -> None:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    result = start_goal(db_session, SAAS_GOAL)
    briefs = result.head_brief_tasks
    assert [(t.department_id, t.role_required) for t in briefs] == [
        ("sales", "sales_director"),
        ("marketing", "marketing_director"),
        ("legal", "legal_director"),
        ("operations", "operations_director"),
    ]
    for t in briefs:
        assert t.kind == TaskKind.HEAD_BRIEF
        assert t.section_id is None  # 部長直轄
        assert t.status == TaskStatus.READY
        assert t.max_retries == 1
        data = json.loads(t.constraints)
        assert data["brief_for"] == t.department_id
        assert all(o["department_id"] == t.department_id for o in data["held_orders"])
    # held departments' templates are not issued yet
    assert result.work_order_tasks == []
    # second filing is idempotent
    from ai_company.services.head_briefs import issue_head_briefs

    again = issue_head_briefs(
        db_session, project_id=result.project.id, minutes=result.meeting  # type: ignore[arg-type]
    )
    assert again.tasks == []


def test_max_heads_cap_issues_templates_for_the_rest(
    db_session: Session, tmp_env: Path
) -> None:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env, max_heads_per_meeting=1)
    result = start_goal(db_session, SAAS_GOAL)
    assert [t.department_id for t in result.head_brief_tasks] == ["sales"]
    issued_depts = {t.department_id for t in result.work_order_tasks}
    assert issued_depts == {"marketing", "legal", "operations"}


def test_head_brief_is_read_only(db_session: Session, tmp_env: Path) -> None:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    cfg = load_config()
    brief = start_goal(db_session, SAAS_GOAL).head_brief_tasks[0]
    assert infer_dispatch_action(brief) == "code.read"
    assert role_allows_writes(cfg, brief.role_required, brief) is False
    cmd = build_cursor_command("p", task_kind=brief.kind, role=brief.role_required)
    assert "--mode" in cmd
    prompt = build_task_prompt(brief, session=db_session)
    assert "部長所見" in prompt and "Executive Meeting Context" in prompt
    assert "sales_proposal" in prompt  # own sections listed
    assert "HeadBrief" in prompt


# --- mock = template invariant + full run ---------------------------------


def _fast_closure(tmp_env: Path) -> None:
    write_settings_override(
        tmp_env, scheduler={"max_validation_workers": 8, "max_concurrent_workers": 10}
    )
    phases = {
        k: {"max_rounds": 1, "consecutive_clear_to_advance": 1}
        for k in (
            "still_needed", "project_health", "product_fit", "usability",
            "customer_success", "it_operations", "compliance", "test_audit",
            "quality", "sales", "maintenance",
        )
    }
    write_validation_override(tmp_env, closure_loops={"enabled": True, **phases})


def test_on_with_mock_matches_off_and_completes(db_session: Session, tmp_env: Path) -> None:
    _fast_closure(tmp_env)
    off = start_goal(db_session, SAAS_GOAL)
    off_units = _units(db_session, off.project.id)

    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    on = start_goal(db_session, SAAS_GOAL)
    auto = run_until_released(
        db_session, on.project.id, max_cycles=300, runner="mock", mock_succeed=True
    )
    assert auto.release_candidate_id is not None, auto.stopped_reason
    db_session.refresh(on.project)
    assert on.project.status == ProjectStatus.DONE

    assert _units(db_session, on.project.id) == off_units  # invariant
    briefs = _of_kind(db_session, on.project.id, TaskKind.HEAD_BRIEF.value)
    assert briefs and all(t.status == TaskStatus.DONE for t in briefs)
    assert all(json.loads(t.constraints)["brief_applied"] for t in briefs)
    assert len(_events(db_session, "head_brief_applied")) == len(briefs)
    # sales.onboarding still waits for sales.proposal
    orders = {
        json.loads(t.constraints)["work_order_key"]: t
        for t in _of_kind(db_session, on.project.id, TaskKind.DEPARTMENT_WORK.value)
    }
    dep_ids = set(
        db_session.scalars(
            select(TaskDependency.depends_on_task_id).where(
                TaskDependency.task_id == orders["sales.onboarding"].id
            )
        ).all()
    )
    assert orders["sales.proposal"].id in dep_ids
    minutes = Path(on.meeting.path).with_name(f"{on.project.id}_kickoff_heads.md")  # type: ignore[union-attr,arg-type]
    assert minutes.exists() and "営業部長" in minutes.read_text(encoding="utf-8")


def test_mock_brief_is_deterministic(db_session: Session, tmp_env: Path) -> None:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    brief = start_goal(db_session, SAAS_GOAL).head_brief_tasks[0]
    a = build_mock_head_brief(brief)
    b = build_mock_head_brief(brief)
    assert a == b
    assert parse_head_brief_result(json.dumps(a)).ok


# --- failure → fallback (no escalation) -----------------------------------


def test_failed_brief_falls_back_after_retries_without_escalation(
    db_session: Session, tmp_env: Path
) -> None:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    result = start_goal(db_session, SAAS_GOAL)
    legal = next(t for t in result.head_brief_tasks if t.department_id == "legal")
    for _ in range(legal.max_retries + 1):
        worker = hire_worker(db_session, task=legal)
        dispatch_worker(db_session, task=legal, worker=worker, runner="mock", mock_succeed=False)
    db_session.refresh(legal)
    assert legal.status == TaskStatus.CANCELLED
    assert json.loads(legal.constraints)["fallback_applied"] is True
    assert ("legal.precheck", "legal", "compliance") in _units(db_session, result.project.id)
    assert _of_kind(db_session, result.project.id, TaskKind.REPLAN.value) == []
    assert _events(db_session, "head_brief_fallback")


def test_stalled_brief_is_settled_before_closure(db_session: Session, tmp_env: Path) -> None:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    result = start_goal(db_session, SAAS_GOAL)
    pid = result.project.id
    for t in result.tasks:  # pretend implementation is finished
        t.status = TaskStatus.DONE
    briefs = result.head_brief_tasks
    briefs[0].status = TaskStatus.BLOCKED
    db_session.flush()
    # other briefs still open → closure waits
    assert maybe_spawn_closure_loop(db_session, project_id=pid) is None
    db_session.refresh(briefs[0])
    assert briefs[0].status == TaskStatus.CANCELLED
    assert any(k.startswith("sales.") for k, _, _ in _units(db_session, pid))


# --- secretary-side validation --------------------------------------------


def _legal_brief_task(db_session: Session, tmp_env: Path) -> Task:
    _grant_head_permissions(tmp_env)
    _enable_heads(tmp_env)
    result = start_goal(db_session, SAAS_GOAL)
    return next(t for t in result.head_brief_tasks if t.department_id == "legal")


def _parsed(payload: dict[str, object]) -> dict[str, object]:
    parsed = parse_head_brief_result(json.dumps(payload))
    assert parsed.ok and parsed.result is not None, parsed.error
    return parsed.result


def test_validate_accepts_own_department_and_clamps(db_session: Session, tmp_env: Path) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    decision = validate_head_brief(db_session, task=task, brief=_parsed(_brief()))
    assert decision.fatal is None
    (order,) = decision.orders
    assert order.department_id == "legal" and order.section_id == "compliance"
    assert order.priority == load_config().settings.meeting_resolutions.priority_ceiling
    assert order.depends_on_titles == ["Goal分解とスコープ定義"]  # unknown title dropped


def test_validate_rejects_cross_department_instructions(
    db_session: Session, tmp_env: Path
) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    good = _brief()["work_orders"][0]  # type: ignore[index]
    bad = [
        {**good, "key": "sales.pitch"},  # other department's key
        {**good, "key": "legal.x", "section_id": "sales_proposal"},  # other dept section
        {**good, "key": "legal.y", "role_required": "coding_worker"},  # role not in section
    ]
    for order in bad:
        d = validate_head_brief(
            db_session, task=task, brief=_parsed(_brief(work_orders=[order]))
        )
        assert d.orders == [] and d.fatal == "all_work_orders_rejected"
    d = validate_head_brief(
        db_session, task=task, brief=_parsed(_brief(work_orders=[bad[0], good]))
    )
    assert [o.key for o in d.orders] == ["legal.privacy_review"]
    assert d.fatal is None and len(d.rejected) == 1


def test_department_mismatch_and_declined(db_session: Session, tmp_env: Path) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    d = validate_head_brief(db_session, task=task, brief=_parsed(_brief(dept="sales")))
    assert d.fatal and d.fatal.startswith("department_mismatch")
    d = validate_head_brief(
        db_session, task=task, brief=_parsed(_brief(status="declined", work_orders=[]))
    )
    assert d.fatal is None and d.orders == []


def test_apply_files_orders_once_and_mismatch_falls_back(
    db_session: Session, tmp_env: Path
) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    apply_head_brief(db_session, task=task, payload=_brief())
    apply_head_brief(db_session, task=task, payload=_brief())  # idempotent
    units = _units(db_session, task.project_id)
    assert ("legal.privacy_review", "legal", "compliance") in units
    assert not any(k == "legal.precheck" for k, _, _ in units)  # head replaced template
    assert len(_events(db_session, "head_brief_applied")) == 1

    sales = next(
        t
        for t in _of_kind(db_session, task.project_id, TaskKind.HEAD_BRIEF.value)
        if t.department_id == "sales"
    )
    apply_head_brief(db_session, task=sales, payload=_brief(dept="legal"))
    assert json.loads(sales.constraints)["fallback_applied"] is True
    assert any(k == "sales.proposal" for k, _, _ in _units(db_session, task.project_id))


def test_unparseable_payload_falls_back(db_session: Session, tmp_env: Path) -> None:
    task = _legal_brief_task(db_session, tmp_env)
    apply_head_brief(db_session, task=task, payload={"status": "completed"})
    data = json.loads(task.constraints)
    assert data["fallback_applied"] and data["fallback_reason"].startswith("parse:")
    assert any(k == "legal.precheck" for k, _, _ in _units(db_session, task.project_id))


# --- parser ----------------------------------------------------------------


def test_parse_head_brief_shapes() -> None:
    ok = parse_head_brief_result(json.dumps(_brief()))
    assert ok.ok and ok.result is not None
    assert ok.result["work_orders"][0]["priority"] == 80  # clamped to schema max
    assert not parse_head_brief_result("not json").ok
    assert parse_head_brief_result(json.dumps({"status": "completed"})).error == (
        "missing_field:department_id"
    )
    bad_decider = _brief(open_questions=[{"question": "q", "decider": "ceo"}])
    assert parse_head_brief_result(json.dumps(bad_decider)).error == "invalid_decider:ceo"
    assert parse_head_brief_result(json.dumps(_brief(confidence=True))).error == (
        "invalid_confidence"
    )
    assert parse_head_brief_result(json.dumps(_brief(confidence=1.5))).error == (
        "confidence_out_of_range"
    )
    order = _brief()["work_orders"][0]  # type: ignore[index]
    bad_key = _brief(work_orders=[{**order, "key": "Legal-X"}])
    assert parse_head_brief_result(json.dumps(bad_key)).error == (
        "invalid_work_order_key:Legal-X"
    )
    many = _brief(work_orders=[order, order, order], statement="x" * 999)
    parsed = parse_head_brief_result(json.dumps(many))
    assert parsed.ok and parsed.result is not None
    assert len(parsed.result["work_orders"]) == 2
    assert len(parsed.result["statement"]) == 400
    assert "work_orders_truncated" in parsed.result["parse_notes"]
