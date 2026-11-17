"""T4 sec.audit Medium 1-3: Goal redaction, approval dialog markup, mock thread cleanup."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Approval, AuditEvent, BudgetEntry, Task, Worker
from ai_company.enums import BudgetKind, TaskKind, TaskStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.services.approval_flow import (
    ApprovalChoice,
    choice_result_message,
    describe_approval_prompt,
    run_interactive_approval_session,
)
from ai_company.services.prompt_builder import build_task_prompt
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import start_goal
from ai_company.services.tasks import create_task

SECRET_GOAL = "顧客向けの勤怠管理Webサービスを作って販売したい。DBは password=Hunter2pass を使う"


# --- R-4: Goal / task text is redacted before it reaches the external LLM ------


def test_goal_secret_never_reaches_any_prompt(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, SECRET_GOAL)
    tasks = db_session.scalars(select(Task).where(Task.project_id == result.project.id)).all()
    assert tasks
    # sanity: the Goal (with the secret) really is stored in task text / constraints
    assert any("Hunter2pass" in f"{t.objective}{t.constraints}" for t in tasks)
    for t in tasks:
        prompt = build_task_prompt(t, session=db_session)
        assert "Hunter2pass" not in prompt, (t.kind, t.title)


def test_prompt_redacts_title_objective_acceptance_and_keeps_json(
    db_session: Session, tmp_env: Path
) -> None:
    t = create_task(
        db_session,
        title="接続設定 パスワード: Abc12345",
        role_required="coding_worker",
        objective="APIキー: sk-abcdefghijklmnopqrstuvwxyz を設定",
        acceptance_criteria="token=zzz999top で疎通",
    )
    t.constraints = json.dumps({"goal": "x password=Hunter2pass", "n": 1}, ensure_ascii=False)
    db_session.flush()
    prompt = build_task_prompt(t, session=db_session)
    for leaked in ("Abc12345", "sk-abcdefghijklmnopqrstuvwxyz", "zzz999top", "Hunter2pass"):
        assert leaked not in prompt
    line = next(x for x in prompt.splitlines() if x.startswith("Constraints: "))
    data = json.loads(line.removeprefix("Constraints: "))  # still valid JSON
    assert data["n"] == 1 and "Hunter2pass" not in data["goal"]


def test_head_brief_goal_line_is_redacted(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, "社内ツール")
    hb = create_task(
        db_session,
        title="部長所見: 法務部",
        role_required="legal_director",
        project_id=result.project.id,
        kind=TaskKind.HEAD_BRIEF.value,
        department_id="legal",
    )
    hb.constraints = json.dumps({"goal": SECRET_GOAL, "held_orders": []}, ensure_ascii=False)
    db_session.flush()
    prompt = build_task_prompt(hb, session=db_session)
    assert "Goal: " in prompt
    assert "Hunter2pass" not in prompt


# --- approval dialog: worker text is never rich markup -------------------------


def _blocked_with_bad_title(session: Session) -> Approval:
    t = create_task(
        session, title="監査是正 [/] 対応 [bold]x[/bold]", role_required="coding_worker"
    )
    t.status = TaskStatus.BLOCKED
    a = Approval(
        id=new_id("APR"),
        action="task.unblock",
        requested_by="system",
        status="PENDING",
        reason=f"manager_exhausted; task={t.id}; title={t.title}",
    )
    session.add(a)
    session.flush()
    return a


def test_interactive_approval_survives_markup_in_title(
    db_session: Session, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = _blocked_with_bad_title(db_session)
    text = describe_approval_prompt(approval)
    assert "[/]" in text
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "3")
    buf = io.StringIO()
    console = Console(file=buf, width=200, color_system=None)
    run_interactive_approval_session(db_session, [approval], runner="mock", console=console)
    out = buf.getvalue()
    assert "監査是正 [/] 対応 [bold]x[/bold]" in out  # literal, not parsed / not crashed
    assert "再検討" in out


def test_choice_result_message_escapes_id() -> None:
    a = Approval(id="APR-[/]x", action="a", requested_by="s", status="PENDING", reason="")
    msg = choice_result_message(ApprovalChoice.YES, a)
    buf = io.StringIO()
    Console(file=buf, color_system=None).print(msg)
    assert "APR-[/]x" in buf.getvalue()


# --- mock thread exception: no leaked worker / reservation / RUNNING task -------


def test_mock_thread_exception_is_cleaned_up(
    db_session: Session, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_company.services import workers

    result = start_goal(db_session, "顧客向けの勤怠管理Webサービスを作って販売したい")
    ready = db_session.scalars(
        select(Task).where(Task.project_id == result.project.id, Task.status == TaskStatus.READY)
    ).all()
    assert len(ready) >= 2  # parallel mock batch
    victim = ready[0].id
    real = workers.run_mock_worker

    def boom(session, *, task, worker, succeed=True):  # type: ignore[no-untyped-def]
        if task.id == victim:
            raise RuntimeError("database is locked password=Hunter2pass")
        return real(session, task=task, worker=worker, succeed=succeed)

    monkeypatch.setattr(workers, "run_mock_worker", boom)
    db_session.commit()
    run_once(db_session, runner="mock")
    db_session.expire_all()

    t = db_session.get(Task, victim)
    assert t is not None
    assert t.status == TaskStatus.READY and t.retry_count == 1
    assert t.leased_by is None
    w = db_session.scalars(select(Worker).where(Worker.task_id == victim)).all()
    assert w and all(x.status == WorkerStatus.FAILED for x in w)
    released = db_session.scalars(
        select(BudgetEntry).where(
            BudgetEntry.scope_type == "task",
            BudgetEntry.scope_id == victim,
            BudgetEntry.kind == BudgetKind.RELEASE,
        )
    ).all()
    assert released
    ev = [
        e
        for e in db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "task_failed", AuditEvent.target_id == victim
            )
        ).all()
        if (e.metadata_json or {}).get("reason") == "mock_thread_exception"
    ]
    assert len(ev) == 1
    assert "Hunter2pass" not in json.dumps(ev[0].metadata_json, ensure_ascii=False)
    # the other jobs of the batch completed normally
    others = [db_session.get(Task, x.id) for x in ready[1:]]
    assert all(o is not None and o.retry_count == 0 for o in others)


def test_mock_thread_exception_exhausts_to_blocked(
    db_session: Session, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_company.services import workers

    result = start_goal(db_session, "顧客向けの勤怠管理Webサービスを作って販売したい")
    ready = db_session.scalars(
        select(Task).where(Task.project_id == result.project.id, Task.status == TaskStatus.READY)
    ).all()
    victim = ready[0]
    victim.max_retries = 0
    db_session.commit()
    real = workers.run_mock_worker

    def boom(session, *, task, worker, succeed=True):  # type: ignore[no-untyped-def]
        if task.id == victim.id:
            raise RuntimeError("boom")
        return real(session, task=task, worker=worker, succeed=succeed)

    monkeypatch.setattr(workers, "run_mock_worker", boom)
    run_once(db_session, runner="mock")
    db_session.expire_all()
    t = db_session.get(Task, victim.id)
    assert t is not None and t.status == TaskStatus.BLOCKED
