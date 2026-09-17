"""AI company role catalog and reporting lines (who owns what)."""

from __future__ import annotations

from dataclasses import dataclass

from ai_company.ui_ja import role_ja


@dataclass(frozen=True)
class RoleDef:
    id: str
    title_ja: str
    layer: str  # president | manager | specialist | worker
    reports_to: str | None
    handles: str


# Flat catalog with explicit supervisor edges (nearest boss).
ROLES: dict[str, RoleDef] = {
    "human_owner": RoleDef(
        id="human_owner",
        title_ja="社長（人間）",
        layer="president",
        reports_to=None,
        handles="方針・予算・マネージャー尽きた後の最終判断のみ",
    ),
    "secretary": RoleDef(
        id="secretary",
        title_ja="秘書",
        layer="manager",
        reports_to="human_owner",
        handles="計画・人員依頼・状況要約（Worker起動はしない）",
    ),
    "engineering_manager": RoleDef(
        id="engineering_manager",
        title_ja="実装マネージャー",
        layer="manager",
        reports_to="technical_director",
        handles="実装失敗・修正上限後の再計画。末端の直近上司",
    ),
    "technical_director": RoleDef(
        id="technical_director",
        title_ja="技術責任者",
        layer="manager",
        reports_to="human_owner",
        handles="実装マネージャーで解決できない技術判断・部門横断の再計画",
    ),
    "judge": RoleDef(
        id="judge",
        title_ja="判定",
        layer="specialist",
        reports_to="engineering_manager",
        handles="独立検証結果の PASS/REJECT 判定",
    ),
    "coding_worker": RoleDef(
        id="coding_worker",
        title_ja="実装",
        layer="worker",
        reports_to="engineering_manager",
        handles="実装・修正の実行",
    ),
    "backend_engineer": RoleDef(
        id="backend_engineer",
        title_ja="バックエンド",
        layer="worker",
        reports_to="engineering_manager",
        handles="API・データモデル等の実装",
    ),
    "frontend_engineer": RoleDef(
        id="frontend_engineer",
        title_ja="フロントエンド",
        layer="worker",
        reports_to="engineering_manager",
        handles="Web UI 実装",
    ),
    "test_reviewer": RoleDef(
        id="test_reviewer",
        title_ja="テスト検証",
        layer="specialist",
        reports_to="judge",
        handles="独立テスト検証",
    ),
    "spec_reviewer": RoleDef(
        id="spec_reviewer",
        title_ja="仕様検証",
        layer="specialist",
        reports_to="judge",
        handles="仕様適合検証",
    ),
    "security_reviewer": RoleDef(
        id="security_reviewer",
        title_ja="セキュリティ検証",
        layer="specialist",
        reports_to="judge",
        handles="セキュリティ検証",
    ),
    "edge_case_reviewer": RoleDef(
        id="edge_case_reviewer",
        title_ja="例外検証",
        layer="specialist",
        reports_to="judge",
        handles="例外・境界条件検証",
    ),
    "architecture_reviewer": RoleDef(
        id="architecture_reviewer",
        title_ja="設計検証",
        layer="specialist",
        reports_to="judge",
        handles="アーキテクチャ検証",
    ),
    "reviewer": RoleDef(
        id="reviewer",
        title_ja="レビュー",
        layer="specialist",
        reports_to="judge",
        handles="汎用レビュー",
    ),
}


LAYER_JA = {
    "president": "社長層",
    "manager": "管理職",
    "specialist": "専門職",
    "worker": "実行職",
}


def role_def(role_id: str) -> RoleDef:
    if role_id in ROLES:
        return ROLES[role_id]
    return RoleDef(
        id=role_id,
        title_ja=role_ja(role_id),
        layer="worker",
        reports_to="engineering_manager",
        handles="（未登録ロール）",
    )


def supervisor_of(role_id: str) -> str | None:
    return role_def(role_id).reports_to


def escalation_chain(role_id: str, *, max_depth: int = 16) -> list[str]:
    """Return the reporting chain and fail closed if the org chart has a cycle."""
    chain: list[str] = []
    seen = {role_id}
    current = role_id
    for _ in range(max_depth):
        supervisor = supervisor_of(current)
        if supervisor is None:
            return chain
        if supervisor in seen:
            raise ValueError(f"reporting cycle detected at role: {supervisor}")
        chain.append(supervisor)
        seen.add(supervisor)
        current = supervisor
    raise ValueError(f"reporting chain exceeds max depth: {role_id}")


def format_role_label(role_id: str) -> str:
    r = role_def(role_id)
    return f"{r.title_ja}（{r.id}）"


def org_chart_text() -> str:
    lines = ["【AI社員ロール一覧】", ""]
    order = ["president", "manager", "specialist", "worker"]
    for layer in order:
        lines.append(f"■ {LAYER_JA[layer]}")
        for role in ROLES.values():
            if role.layer != layer:
                continue
            boss = (
                format_role_label(role.reports_to)
                if role.reports_to
                else "（最上位）"
            )
            lines.append(f"  - {format_role_label(role.id)}")
            lines.append(f"      直属上司: {boss}")
            lines.append(f"      担当: {role.handles}")
        lines.append("")
    lines.append("失敗時の基本ルート:")
    lines.append("  実行職 → 実装マネージャー（再計画）→ それでも駄目なら社長（人間）")
    lines.append("  検証専門職 → 判定 →（REJECT）実装側の修正／マネージャー再計画")
    return "\n".join(lines)


def escalation_route_text(*, worker_role: str, stage: str) -> str:
    """Human-readable one-liner for who is acting now."""
    worker = format_role_label(worker_role)
    manager = format_role_label("engineering_manager")
    president = format_role_label("human_owner")
    if stage == "worker_retry":
        return f"対応: {worker} が再試行（上司未介入）"
    if stage == "repair":
        return f"対応: {worker} が修正タスクを実行（判定の指示）"
    if stage == "manager_replan":
        return f"対応: {manager} が直近上司として再計画（社長は不要）"
    if stage == "human":
        return f"対応: {president} の最終判断が必要（マネージャー経路を使い切り）"
    if stage == "validation":
        return f"対応: 検証専門職 → {format_role_label('judge')}"
    return f"対応ロール: {worker}"
