"""T3S2 audit (agent-c): worker-derived text must never be parsed as rich markup.

Task titles, approval reasons, findings, judge reasons and audit metadata are
seeded with ``[conceal]…[/conceal]`` (spoof: text would vanish) and ``[/]``
(crash: MarkupError → exit 1). Every command must exit 0 and show them literally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_company.db import session_scope
from ai_company.db.models import (
    Approval,
    AuditEvent,
    JudgeDecision,
    Task,
    ValidationPlan,
    ValidationResult,
)
from ai_company.enums import TaskStatus
from ai_company.ids import new_id
from ai_company.services.secretary import start_goal

FIELDS = ["ZZGOAL", "ZZTITLE", "ZZCLOSURE", "ZZREADYT", "ZZAPPR", "ZZFIND", "ZZJUDGE", "ZZDEFER"]


def _p(fid: str, mode: str) -> str:
    return f"[/]{fid}" if mode == "crash" else f"[conceal]{fid}[/conceal]"


def _seed(mode: str) -> tuple[str, str]:
    with session_scope() as s:
        r = start_goal(s, "社内CLIツール" + _p("ZZGOAL", mode))
        pid = r.project.id
        t = r.tasks[0]
        t.title = "BLK" + _p("ZZTITLE", mode)
        t.status = TaskStatus.BLOCKED
        r.tasks[1].title = "RDY" + _p("ZZREADYT", mode)
        r.tasks[1].status = TaskStatus.READY
        r.tasks[2].title = "DEF" + _p("ZZDEFER", mode)
        r.tasks[2].status = TaskStatus.DEFERRED_BUDGET
        s.add(
            Task(
                id=new_id("TASK"),
                project_id=pid,
                title="CL" + _p("ZZCLOSURE", mode),
                kind="sales_audit",
                role_required="sales",
                status=TaskStatus.READY,
                constraints="",
            )
        )
        s.add(
            Approval(
                id=new_id("APR"),
                action="task.unblock",
                requested_by="system",
                status="PENDING",
                reason=f"judge_reject:{t.id} " + _p("ZZAPPR", mode),
            )
        )
        s.add(
            AuditEvent(
                id=new_id("AUD"),
                event_type="task_failed",
                actor_type="worker",
                actor_id="w",
                target_type="task",
                target_id=t.id,
                metadata_json={"x": _p("ZZAUDIT", mode)},
            )
        )
        plan = ValidationPlan(
            id=new_id("VP"), task_id=t.id, risk_level="low", required_review_types=["test"]
        )
        s.add(plan)
        s.flush()
        t.validation_plan_id = plan.id
        s.add(
            ValidationResult(
                id=new_id("VR"),
                plan_id=plan.id,
                task_id=t.id,
                review_type="test",
                verdict="FAIL",
                findings=[{"description": _p("ZZFIND", mode)}],
            )
        )
        s.add(
            JudgeDecision(
                id=new_id("JD"),
                plan_id=plan.id,
                task_id=t.id,
                verdict="REJECT",
                reasons=[_p("ZZJUDGE", mode)],
            )
        )
        return pid, t.id


def _commands(tid: str) -> dict[str, list[str]]:
    return {
        "status": ["status"],
        "status --debug": ["status", "--debug"],
        "debug stuck": ["debug", "stuck"],
        "debug task": ["debug", "task", tid],
        "ask 問題": ["ask", "問題は？"],
        "ask 進捗": ["ask", "進捗"],
        "task list": ["task", "list"],
        "validation status": ["validation", "status", tid],
        "audit list": ["audit", "list"],
        "approval list": ["approval", "list", "--no-interactive"],
    }


@pytest.mark.parametrize("mode", ["spoof", "crash"])
def test_cli_never_parses_worker_text_as_markup(tmp_env: Path, mode: str) -> None:
    from ai_company.cli import app

    _pid, tid = _seed(mode)
    runner = CliRunner()
    seen: dict[str, str] = {}
    for name, args in _commands(tid).items():
        out = runner.invoke(app, args, env={"COLUMNS": "400"})
        assert out.exit_code == 0, f"{name}: exit={out.exit_code} {out.exception!r}"
        seen[name] = out.output
        if mode == "spoof":
            # A field that appears must appear with its literal markup around it.
            for f in FIELDS:
                if f in out.output:
                    assert f"[conceal]{f}" in out.output, f"{name}: {f} was interpreted"
    blob = "\n".join(seen.values())
    marker = "[conceal]" if mode == "spoof" else "[/]"
    # the main worker-derived fields are actually shown (literally) somewhere
    for f in ("ZZTITLE", "ZZAPPR", "ZZFIND", "ZZJUDGE"):
        assert f"{marker}{f}" in blob, f"{f} not shown literally"


def test_approval_list_table_shows_reason_literally(tmp_env: Path) -> None:
    from ai_company.cli import app

    _seed("crash")
    out = CliRunner().invoke(
        app, ["approval", "list", "--no-interactive"], env={"COLUMNS": "400"}
    )
    assert out.exit_code == 0, out.output
    assert "[/]ZZAPPR" in out.output


def test_unknown_dept_message_is_escaped(tmp_env: Path) -> None:
    from ai_company.cli import app

    out = CliRunner().invoke(app, ["task", "list", "--dept", "[/]bad"])
    assert out.exit_code == 2
    assert "[/]bad" in out.output
