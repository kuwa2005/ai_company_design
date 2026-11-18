"""日本語向け表示ラベル（内部IDはそのまま、人が見る文言だけ翻訳）。"""

from __future__ import annotations

STATUS_JA = {
    "DONE": "完了",
    "RUNNING": "実行中",
    "READY": "実行待ち",
    "WAITING": "依存待ち",
    "REVIEW_REQUIRED": "検証待ち",
    "BLOCKED": "停止（要判断）",
    "FAILED": "失敗",
    "DEFERRED_BUDGET": "予算待ち",
    "CANCELLED": "取消",
    "BACKLOG": "バックログ",
    "ACTIVE": "進行中",
    "PAUSED": "一時停止",
}

KIND_JA = {
    "implementation": "実装",
    "validation": "検証",
    "judge": "判定",
    "repair": "修正",
    "replan": "上司再計画",
    "still_needed": "残件自問",
    "project_health": "プロジェクト推進",
    "product_fit": "商品企画",
    "usability_audit": "使い勝手自問",
    "customer_success": "CS点検",
    "it_ops_audit": "情報システム点検",
    "compliance_audit": "法務・コンプライアンス",
    "test_audit": "テスト監査",
    "quality_audit": "品質向上",
    "sales_audit": "営業点検",
    "maintenance_audit": "保守資料点検",
    "head_brief": "部長所見",
    "attendance_review": "招集見直し",
}

ROLE_JA = {
    "coding_worker": "実装",
    "backend_engineer": "バックエンド",
    "design_engineer": "設計担当",
    "acceptance_engineer": "受け入れテスト担当",
    "frontend_engineer": "フロントエンド",
    "engineering_manager": "実装マネージャー",
    "technical_director": "技術責任者",
    "project_coordinator": "プロジェクト推進",
    "product_planner": "商品企画",
    "customer_success": "カスタマーサクセス",
    "compliance_auditor": "法務・コンプライアンス",
    "test_auditor": "テスト監査",
    "it_operations": "情報システム",
    "sales": "営業",
    "maintenance": "保守担当",
    "reviewer": "レビュー",
    "test_reviewer": "テスト検証",
    "spec_reviewer": "仕様検証",
    "security_reviewer": "セキュリティ検証",
    "edge_case_reviewer": "例外検証",
    "architecture_reviewer": "設計検証",
    "completeness_auditor": "残件監査",
    "usability_auditor": "使い勝手監査",
    "quality_auditor": "品質監査",
    "judge": "判定",
    "secretary": "秘書",
    "qa_director": "品質保証部長",
    "sales_director": "営業部長",
    "marketing_director": "マーケティング部長",
    "legal_director": "法務部長",
    "operations_director": "運用保守部長",
}

RUNNER_JA = {
    "mock": "模擬",
    "cursor": "Cursor",
}


def status_ja(value: str | None) -> str:
    if not value:
        return "-"
    return STATUS_JA.get(str(value), str(value))


def kind_ja(value: str | None) -> str:
    if not value:
        return "-"
    return KIND_JA.get(str(value), str(value))


def role_ja(value: str | None) -> str:
    if not value:
        return "-"
    return ROLE_JA.get(str(value), str(value))


def runner_ja(value: str | None) -> str:
    if not value:
        return "-"
    return RUNNER_JA.get(str(value), str(value))
