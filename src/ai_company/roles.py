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
        handles="計画・人員依頼・状況要約（Worker起動はしない）。事業企画の薄い帽子も兼ねる",
    ),
    "project_coordinator": RoleDef(
        id="project_coordinator",
        title_ja="プロジェクト推進",
        layer="manager",
        reports_to="secretary",
        handles="スコープ・依存・優先度・遅延リスクの点検と是正タスク起票",
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
        title_ja="テスト検証（ゲート）",
        layer="specialist",
        reports_to="judge",
        handles="実装ゲート検証（緑≠正しい。妥当性は test_auditor）",
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
    "completeness_auditor": RoleDef(
        id="completeness_auditor",
        title_ja="残件監査（本当にもう無いか）",
        layer="specialist",
        reports_to="secretary",
        handles="実装キュー空後にゴール残件を執拗に再発見し、不足タスクを起票",
    ),
    "product_planner": RoleDef(
        id="product_planner",
        title_ja="商品企画",
        layer="specialist",
        reports_to="secretary",
        handles="価値適合・優先機能・非目標の点検と企画改善タスク起票",
    ),
    "usability_auditor": RoleDef(
        id="usability_auditor",
        title_ja="使い勝手監査",
        layer="specialist",
        reports_to="secretary",
        handles="動く完成品の使い勝手改善余地を自問し、改善タスクを起票",
    ),
    "customer_success": RoleDef(
        id="customer_success",
        title_ja="カスタマーサクセス",
        layer="specialist",
        reports_to="secretary",
        handles="導入・定着・運用しやすさの点検とCS改善タスク起票",
    ),
    "compliance_auditor": RoleDef(
        id="compliance_auditor",
        title_ja="法務・合规（ミニ）",
        layer="specialist",
        reports_to="secretary",
        handles="個人情報・労働法制・監査ログ等の最小合规点検",
    ),
    "test_auditor": RoleDef(
        id="test_auditor",
        title_ja="テスト監査",
        layer="specialist",
        reports_to="secretary",
        handles="テストが緑でも正しいとは限らない前提で、テスト妥当性を監査し是正を起票",
    ),
    "quality_auditor": RoleDef(
        id="quality_auditor",
        title_ja="品質向上部",
        layer="specialist",
        reports_to="secretary",
        handles="完成品の品質レビュー（本当にこれでよかったか／改善余地）",
    ),
    "sales": RoleDef(
        id="sales",
        title_ja="営業（軽量）",
        layer="specialist",
        reports_to="secretary",
        handles="提案・デモ・価格メモなど売るための最小資料の起票",
    ),
    "maintenance": RoleDef(
        id="maintenance",
        title_ja="保守部（軽量）",
        layer="specialist",
        reports_to="secretary",
        handles="保守費・永続運用のための手順／契約前提資料の起票",
    ),
    "reviewer": RoleDef(
        id="reviewer",
        title_ja="レビュー",
        layer="specialist",
        reports_to="judge",
        handles="汎用レビュー",
    ),
    "it_operations": RoleDef(
        id="it_operations",
        title_ja="情報システム",
        layer="worker",
        reports_to="technical_director",
        handles="権限・シード・運用手順・『動くが運用不能』の防止",
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
    lines.append("完了前の自問ルート:")
    lines.append(
        "  実装キュー空 → 残件監査 → プロジェクト推進 → 商品企画 → "
        "使い勝手 → CS → 情報システム → 法務合规 → テスト監査 → "
        "品質向上部 → 営業 → 保守部 → リリース"
    )
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
