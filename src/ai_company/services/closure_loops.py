"""Closure loops: still-needed / usability / quality self-questioning before release."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, ClosureLoopPhase, load_config
from ai_company.db.models import Project, Task
from ai_company.enums import TaskKind, TaskStatus
from ai_company.services.audit import record_audit
from ai_company.services.progress import emit as progress
from ai_company.services.staffing import ensure_staffing_request
from ai_company.services.tasks import create_task

CLOSURE_KINDS = {
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
}

# Order: … → compliance → test audit → QA → sales → maintenance → release
_PHASE_ORDER = (
    ("still_needed", TaskKind.STILL_NEEDED, "completeness_auditor"),
    ("project_health", TaskKind.PROJECT_HEALTH, "project_coordinator"),
    ("product_fit", TaskKind.PRODUCT_FIT, "product_planner"),
    ("usability", TaskKind.USABILITY_AUDIT, "usability_auditor"),
    ("customer_success", TaskKind.CUSTOMER_SUCCESS, "customer_success"),
    ("it_operations", TaskKind.IT_OPS_AUDIT, "it_operations"),
    ("compliance", TaskKind.COMPLIANCE_AUDIT, "compliance_auditor"),
    ("test_audit", TaskKind.TEST_AUDIT, "test_auditor"),
    ("quality", TaskKind.QUALITY_AUDIT, "quality_auditor"),
    ("sales", TaskKind.SALES_AUDIT, "sales"),
    ("maintenance", TaskKind.MAINTENANCE_AUDIT, "maintenance"),
)

_OPEN = {
    TaskStatus.BACKLOG,
    TaskStatus.WAITING,
    TaskStatus.READY,
    TaskStatus.RUNNING,
    TaskStatus.REVIEW_REQUIRED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.DEFERRED_BUDGET,
}


def is_closure_task(task: Task) -> bool:
    return task.kind in CLOSURE_KINDS


def _constraints_dict(task: Task) -> dict[str, Any]:
    raw = task.constraints
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _set_constraints(task: Task, data: dict[str, Any]) -> None:
    task.constraints = json.dumps(data, ensure_ascii=False)


def _phase_cfg(cfg: AppConfig, phase_key: str) -> ClosureLoopPhase:
    phase = getattr(cfg.closure_loops, phase_key, None)
    if not isinstance(phase, ClosureLoopPhase):
        raise KeyError(f"unknown closure phase: {phase_key}")
    return phase


def _tasks_of_kind(session: Session, project_id: str, kind: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project_id, Task.kind == kind)
            .order_by(Task.created_at.asc())
        ).all()
    )


def _open_closure_tasks(session: Session, project_id: str) -> list[Task]:
    return [
        t
        for t in session.scalars(
            select(Task).where(
                Task.project_id == project_id,
                Task.kind.in_(tuple(CLOSURE_KINDS)),
            )
        ).all()
        if t.status in _OPEN
    ]


def _verdict_of(task: Task) -> str:
    v = str(_constraints_dict(task).get("closure_verdict") or "").upper()
    return v if v in {"CLEAR", "GAPS"} else ""


def _consecutive_clears_at_end(tasks: list[Task]) -> int:
    done = [t for t in tasks if t.status == TaskStatus.DONE]
    n = 0
    for t in reversed(done):
        if _verdict_of(t) == "CLEAR":
            n += 1
        else:
            break
    return n


def _phase_objectives(phase_key: str, goal: str, round_n: int) -> tuple[str, str, str]:
    if phase_key == "still_needed":
        title = f"本当にもう無いか？ #{round_n}"
        objective = (
            f"Goal「{goal}」に対し、実装キューが空でも残件が無いか執拗に自問せよ。\n"
            "繰り返し問え: まだ必要なものは何か？ 他に無いか？ 本当に無いか？\n"
            "docs/PRD.md・REQUIREMENTS・ARCHITECTURE・実装・テスト・デモ導線を照合し、\n"
            "未実装の画面・API・権限・シード・運用手順があれば proposed_tasks に出せ。\n"
            "無ければ closure_verdict=CLEAR。あれば closure_verdict=GAPS。"
        )
        acceptance = "残件をタスク化した、または CLEAR と根拠を出した"
    elif phase_key == "project_health":
        title = f"プロジェクト推進チェック #{round_n}"
        objective = (
            f"Goal「{goal}」の計画健全性をプロジェクト推進部として点検せよ。\n"
            "スコープ膨張・依存のねじれ・優先度の狂い・遅延リスクはないか？\n"
            "タスク分割や順序変更が必要なら proposed_tasks に具体化せよ。\n"
            "問題なければ CLEAR。"
        )
        acceptance = "計画是正タスクを出した、または CLEAR"
    elif phase_key == "product_fit":
        title = f"商品企画レビュー #{round_n}"
        objective = (
            f"Goal「{goal}」について商品企画として価値適合を自問せよ。\n"
            "誰のどんな課題を解いているか？ 優先機能は合っているか？ 非目標は守れているか？\n"
            "足りない価値・削るべきものを proposed_tasks にせよ。問題なければ CLEAR。"
        )
        acceptance = "企画改善タスクを出した、または CLEAR"
    elif phase_key == "usability":
        title = f"使い勝手の自問 #{round_n}"
        objective = (
            f"Goal「{goal}」の動く完成品について使い勝手を自問せよ。\n"
            "使い勝手向上のために何かできることは無いか？ 工夫できるところは無いか？\n"
            "初回ログイン、管理者導線、エラーメッセージ、モバイル、操作数などを見よ。\n"
            "改善があれば proposed_tasks。無ければ CLEAR。"
        )
        acceptance = "UX改善タスクを出した、または CLEAR と根拠を出した"
    elif phase_key == "customer_success":
        title = f"カスタマーサクセス点検 #{round_n}"
        objective = (
            f"Goal「{goal}」を現場導入・定着の観点で見よ（カスタマーサクセス）。\n"
            "オンボーディング、管理者の日常運用、詰まりどころ、サポートしやすさは足りるか？\n"
            "不足があれば proposed_tasks。無ければ CLEAR。"
        )
        acceptance = "CS改善タスクを出した、または CLEAR"
    elif phase_key == "it_operations":
        title = f"情報システム運用点検 #{round_n}"
        objective = (
            f"Goal「{goal}」の運用・権限・シード・環境を情報システム部として点検せよ。\n"
            "管理者アカウント、デモデータ、権限ロール、バックアップ／設定手順は足りるか？\n"
            "『動くが運用不能』を防ぐタスクがあれば proposed_tasks。無ければ CLEAR。"
        )
        acceptance = "運用・権限タスクを出した、または CLEAR"
    elif phase_key == "compliance":
        title = f"法務・合规ミニレビュー #{round_n}"
        objective = (
            f"Goal「{goal}」を法務・合规の最小観点で点検せよ。\n"
            "個人情報・労働法制・監査ログ・同意・権限分離で穴はないか？\n"
            "必須の是正があれば proposed_tasks。無ければ CLEAR。"
        )
        acceptance = "合规是正タスクを出した、または CLEAR"
    elif phase_key == "test_audit":
        title = f"テスト監査 #{round_n}"
        objective = (
            f"Goal「{goal}」の自動テスト／検証が『正しい』とは限らない前提で監査せよ。\n"
            "緑でも信用するな。同値の偽、弱いアサーション、実装とテストの共謀、\n"
            "未行使の要件、偽陰性／偽陽性の余地を洗い出せ。\n"
            "是正（テスト強化・削除・分離）が必要なら proposed_tasks。妥当なら CLEAR。"
        )
        acceptance = "テスト是正タスクを出した、または CLEAR"
    elif phase_key == "quality":
        title = f"品質向上レビュー #{round_n}"
        objective = (
            f"Goal「{goal}」の完成品について品質向上部がレビューせよ。\n"
            "本当にこれでよかったか？ 改善できるところは無いか？\n"
            "信頼性・保守性・セキュリティ残留・テストの穴・観測性を見よ。\n"
            "改善があれば proposed_tasks。無ければ CLEAR。"
        )
        acceptance = "品質改善タスクを出した、または CLEAR と根拠を出した"
    elif phase_key == "sales":
        title = f"営業パッケージ点検 #{round_n}"
        objective = (
            f"Goal「{goal}」を売るための最小営業資料は揃っているか点検せよ（営業部・軽量）。\n"
            "提案の一文、対象顧客、デモ手順、価格／契約の置き場所、競合との差のメモ。\n"
            "足りなければ docs/ 等への資料タスクを proposed_tasks に出せ。揃っていれば CLEAR。"
        )
        acceptance = "営業資料タスクを出した、または CLEAR"
    elif phase_key == "maintenance":
        title = f"保守・永続化資料点検 #{round_n}"
        objective = (
            f"Goal「{goal}」を保守費で永続運用できる資料があるか点検せよ（保守部・軽量）。\n"
            "運用手順、障害時連絡、バックアップ／更新方針、サポート範囲、保守契約の前提メモ。\n"
            "足りなければ資料タスクを proposed_tasks に。揃っていれば CLEAR。"
        )
        acceptance = "保守資料タスクを出した、または CLEAR"
    else:
        title = f"完了前点検 #{round_n}"
        objective = f"Goal「{goal}」の追加点検。不足があれば proposed_tasks。無ければ CLEAR。"
        acceptance = "改善タスクを出した、または CLEAR"
    return title, objective, acceptance


def all_implementation_done(session: Session, project_id: str) -> bool:
    impl = list(
        session.scalars(
            select(Task).where(
                Task.project_id == project_id,
                Task.kind.in_((TaskKind.IMPLEMENTATION, "implementation")),
            )
        ).all()
    )
    if not impl:
        return False
    return all(t.status == TaskStatus.DONE for t in impl)


def closure_loops_finished(
    session: Session, project_id: str, config: AppConfig | None = None
) -> bool:
    cfg = config or load_config()
    if not cfg.closure_loops.enabled:
        return True
    if _open_closure_tasks(session, project_id):
        return False
    skip = set(cfg.closure_loops.skip_phases or [])
    active = [(k, kind, role) for k, kind, role in _PHASE_ORDER if k not in skip]
    if not active:
        return True
    last_key, last_kind, _ = active[-1]
    last_tasks = _tasks_of_kind(session, project_id, last_kind)
    phase = _phase_cfg(cfg, last_key)
    if not last_tasks:
        return False
    if not all(t.status == TaskStatus.DONE for t in last_tasks):
        return False
    if len(last_tasks) >= phase.max_rounds:
        return True
    return _consecutive_clears_at_end(last_tasks) >= phase.consecutive_clear_to_advance


def _phase_should_spawn(tasks: list[Task], phase: ClosureLoopPhase) -> bool:
    if any(t.status in _OPEN for t in tasks):
        return False
    done = [t for t in tasks if t.status == TaskStatus.DONE]
    if len(done) >= phase.max_rounds:
        return False
    if not done:
        return True
    clears = _consecutive_clears_at_end(done)
    if clears >= phase.consecutive_clear_to_advance:
        return False
    return True


def _phase_advanced(tasks: list[Task], phase: ClosureLoopPhase) -> bool:
    if any(t.status in _OPEN for t in tasks):
        return False
    done = [t for t in tasks if t.status == TaskStatus.DONE]
    if not done:
        return False
    if len(done) >= phase.max_rounds:
        return True
    return _consecutive_clears_at_end(done) >= phase.consecutive_clear_to_advance


def maybe_spawn_closure_loop(
    session: Session,
    *,
    project_id: str,
    config: AppConfig | None = None,
) -> Task | None:
    """Spawn next still-needed / UX / quality task when implementation queue is empty."""
    cfg = config or load_config()
    if not cfg.closure_loops.enabled:
        return None
    if not all_implementation_done(session, project_id):
        return None
    if _open_closure_tasks(session, project_id):
        return None

    project = session.get(Project, project_id)
    goal = project.goal if project else project_id

    for phase_key, kind, role in _PHASE_ORDER:
        if phase_key in set(cfg.closure_loops.skip_phases or []):
            continue
        phase = _phase_cfg(cfg, phase_key)
        tasks = _tasks_of_kind(session, project_id, kind)
        if not _phase_advanced(tasks, phase):
            if _phase_should_spawn(tasks, phase):
                return _spawn_round(
                    session,
                    project_id=project_id,
                    goal=goal or "",
                    phase_key=phase_key,
                    kind=kind,
                    role=role,
                    round_n=len([t for t in tasks if t.status == TaskStatus.DONE]) + 1,
                    config=cfg,
                )
            return None
    return None


def _spawn_round(
    session: Session,
    *,
    project_id: str,
    goal: str,
    phase_key: str,
    kind: str,
    role: str,
    round_n: int,
    config: AppConfig,
) -> Task:
    ensure_staffing_request(
        session,
        project_id=project_id,
        role=role,
        requested_count=1,
        reason=f"closure_loop:{phase_key}:{round_n}",
        auto_approve=True,
        config=config,
    )
    title, objective, acceptance = _phase_objectives(phase_key, goal, round_n)
    task = create_task(
        session,
        title=title,
        priority=95,
        project_id=project_id,
        objective=objective,
        role_required=role,
        kind=kind,
        risk_level="low",
        acceptance_criteria=acceptance,
        estimated_cost=0.0,
        config=config,
    )
    _set_constraints(
        task,
        {
            "closure_phase": phase_key,
            "closure_round": round_n,
            "closure_verdict": None,
        },
    )
    session.flush()
    progress(f"自問ループ  {title}  役割={role}  タスク={task.id}")
    record_audit(
        session,
        event_type="closure_loop_spawned",
        actor_type="system",
        actor_id="closure_loops",
        target_type="task",
        target_id=task.id,
        metadata={"phase": phase_key, "round": round_n, "role": role},
    )
    return task


def apply_closure_result(
    session: Session,
    *,
    task: Task,
    payload: dict[str, Any],
    config: AppConfig | None = None,
) -> list[str]:
    """Finalize closure task: record verdict and spawn gap/improvement implementation tasks."""
    cfg = config or load_config()
    verdict = str(
        payload.get("closure_verdict") or payload.get("verdict") or "CLEAR"
    ).upper()
    if verdict not in {"CLEAR", "GAPS"}:
        proposed = payload.get("proposed_tasks") or []
        verdict = "GAPS" if proposed else "CLEAR"

    constraints = _constraints_dict(task)
    constraints["closure_verdict"] = verdict
    constraints["closure_summary"] = str(payload.get("summary") or "")[:2000]
    _set_constraints(task, constraints)
    task.status = TaskStatus.DONE
    session.flush()

    created: list[str] = []
    if verdict == "GAPS":
        proposed = payload.get("proposed_tasks") or []
        if not isinstance(proposed, list):
            proposed = []
        for i, item in enumerate(proposed):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or f"Gap fix from {task.title} #{i + 1}").strip()
            if not title:
                continue
            role = (
                str(item.get("role_required") or "coding_worker").strip()
                or "coding_worker"
            )
            child = create_task(
                session,
                title=title[:120],
                priority=min(100, 80 + i),
                project_id=task.project_id,
                objective=str(item.get("objective") or title),
                role_required=role,
                kind=TaskKind.IMPLEMENTATION,
                risk_level=str(item.get("risk_level") or "medium"),
                acceptance_criteria=str(
                    item.get("acceptance_criteria") or "Gap closed and verifiable"
                ),
                parent_task_id=task.id,
                estimated_cost=0.0,
                config=cfg,
            )
            created.append(child.id)
        progress(f"自問ループ 残件あり  {task.title} → 新規実装 {len(created)} 件")
    else:
        progress(f"自問ループ CLEAR  {task.title}")

    record_audit(
        session,
        event_type="closure_loop_finished",
        actor_type="system",
        actor_id="closure_loops",
        target_type="task",
        target_id=task.id,
        metadata={
            "verdict": verdict,
            "created_tasks": created,
            "phase": constraints.get("closure_phase"),
            "round": constraints.get("closure_round"),
        },
    )
    return created
