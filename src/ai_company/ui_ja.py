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
}

ROLE_JA = {
    "coding_worker": "実装",
    "backend_engineer": "バックエンド",
    "frontend_engineer": "フロントエンド",
    "engineering_manager": "実装マネージャー",
    "reviewer": "レビュー",
    "test_reviewer": "テスト検証",
    "spec_reviewer": "仕様検証",
    "security_reviewer": "セキュリティ検証",
    "edge_case_reviewer": "例外検証",
    "architecture_reviewer": "設計検証",
    "judge": "判定",
    "secretary": "秘書",
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
