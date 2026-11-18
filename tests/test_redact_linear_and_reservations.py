"""agent-e T4-S3: M-1, L-1, L-2, L-3 and the Info (reservation leak) fixes.

M-1: the credential-URI pattern is linear (no O(n²) on "a.a.a.…"), and
     ``_err_val`` truncates before redacting.
L-1: only URIs with credentials are touched; the host / path / following text
     (quotes, commas) stay.
L-2: ``status`` uses the held task's estimate for cost-cap budget waits.
L-3: a mock thread crash counts towards the circuit breaker.
Info: a policy denial after ``reserve_budget`` releases the reservation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent, BudgetEntry, Task
from ai_company.enums import BudgetKind, TaskStatus
from ai_company.services import cost_governor, policy_engine
from ai_company.services.redact import redact_text
from ai_company.services.result_parser import parse_structured_result
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import human_next_actions, start_goal
from tests.test_budget_cost_gap import _cost_cap
from tests.test_budget_cost_gap import _spend as _spend_cost

SAAS = "顧客向けの勤怠管理Webサービスを作って販売したい"


# --- M-1 -------------------------------------------------------------------


@pytest.mark.parametrize("evil", ["a." * 20000, "aa-bb-" * 7000, "a+b." * 10000, "x://" * 10000])
def test_redact_is_linear_on_adversarial_input(evil: str) -> None:
    t0 = time.perf_counter()
    redact_text(evil)
    assert time.perf_counter() - t0 < 1.0


def test_err_val_truncates_before_redacting() -> None:
    t0 = time.perf_counter()
    err = parse_structured_result('{"status": "' + "a." * 20000 + '"}').error or ""
    assert time.perf_counter() - t0 < 1.0
    assert err.startswith("invalid_status:") and len(err) <= len("invalid_status:") + 40


# --- L-1 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Redis は redis://localhost:6379/0 を使う",
        "LDAP: ldap://dc.example.local",
        "DB は postgresql://db.internal:5432/main（資格情報なし）",
        "参照 https://host:8080/path と mailto:a@example.com",
    ],
)
def test_uris_without_credentials_are_kept(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"postgresql://u:pw1234@h/d"', '"postgresql://***:***@h/d"'),
        ("postgresql://u:pw1234@h/d,次へ", "postgresql://***:***@h/d,次へ"),
        ("redis://:pw1234@cache:6379/0 を使う", "redis://***:***@cache:6379/0 を使う"),
        ("<mongodb+srv://app:pw1234@c0.example.net/x>", "<mongodb+srv://***:***@c0.example.net/x>"),
    ],
)
def test_credentials_hidden_but_host_and_tail_kept(text: str, expected: str) -> None:
    assert redact_text(text) == expected


# --- L-2 -------------------------------------------------------------------


def test_status_budget_wait_uses_held_task_estimate(db_session: Session, tmp_env: Path) -> None:
    pid = start_goal(db_session, "社内向けの勤怠Webアプリを作る", hold_meeting=False).project.id
    _cost_cap(tmp_env, 1.0)
    _spend_cost(db_session, at=datetime.now(UTC).replace(tzinfo=None), amount=0.8)
    held = db_session.scalars(
        select(Task).where(Task.project_id == pid, Task.status == TaskStatus.READY)
    ).first()
    assert held is not None
    held.status = TaskStatus.DEFERRED_BUDGET
    held.estimated_cost = 0.3  # 0.8 + 0.3 > 1.0 → cannot run in this window
    db_session.flush()
    text = human_next_actions(db_session, project_id=pid, runner="mock")
    assert "予算待ち（時間枠の上限）です" in text


# --- L-3 -------------------------------------------------------------------


def test_mock_thread_crash_counts_for_circuit_breaker(
    db_session: Session, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_company.services import workers

    pid = start_goal(db_session, SAAS).project.id
    victim = db_session.scalars(
        select(Task).where(Task.project_id == pid, Task.status == TaskStatus.READY)
    ).first()
    assert victim is not None
    real = workers.run_mock_worker

    def boom(session, *, task, worker, succeed=True):  # type: ignore[no-untyped-def]
        if task.id == victim.id:
            raise RuntimeError("boom")
        return real(session, task=task, worker=worker, succeed=succeed)

    calls: list[int] = []
    orig = cost_governor.record_failure_and_maybe_trip

    def spy(session, config=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        return orig(session, config)

    monkeypatch.setattr(workers, "run_mock_worker", boom)
    monkeypatch.setattr(cost_governor, "record_failure_and_maybe_trip", spy)
    db_session.commit()
    run_once(db_session, runner="mock")
    assert calls  # the crash was reported to the breaker


# --- Info: reservation leak on policy denial -----------------------------------


def test_policy_denial_releases_reservation(
    db_session: Session, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = start_goal(db_session, "社内向けの勤怠Webアプリを作る", hold_meeting=False).project.id
    for t in db_session.scalars(select(Task).where(Task.project_id == pid)).all():
        t.estimated_cost = 0.5  # non-zero so a leaked reservation is visible

    def deny(*_a, **_k):  # type: ignore[no-untyped-def]
        raise PermissionError("policy denied (test)")

    monkeypatch.setattr(policy_engine, "assert_dispatch_allowed", deny)
    db_session.commit()
    run_once(db_session, runner="mock")
    reserved: dict[str, float] = {}
    for e in db_session.scalars(
        select(BudgetEntry).where(BudgetEntry.scope_type == "task")
    ).all():
        sign = {BudgetKind.RESERVE: 1, BudgetKind.RELEASE: -1}.get(e.kind, 0)
        reserved[e.scope_id] = reserved.get(e.scope_id, 0.0) + sign * float(e.amount)
    assert reserved, "no reservation happened — test setup is wrong"
    denied = db_session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "policy_denied_dispatch")
    ).all()
    assert denied, "policy denial path not reached — test setup is wrong"
    assert all(abs(v) < 1e-9 for v in reserved.values()), reserved
