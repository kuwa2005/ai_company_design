"""招集キーワードの誤検知: 社内ツールの依頼で営業部・マーケティング部を呼ばない."""

from __future__ import annotations

import pytest

from ai_company.org import DEPARTMENTS, is_internal_goal, summon_hits
from ai_company.services.executive_meeting import select_attendees

_BIZ = {"sales", "marketing"}


def _biz(goal: str) -> set[str]:
    attendees, _ = select_attendees(goal)
    return set(attendees) & _BIZ


@pytest.mark.parametrize(
    "goal",
    [
        "社内用のCLIツールを作る",
        "社内向けの売上集計ツールを作る",  # 「売」単体の誤反応
        "売上集計ツールを作る",
        "社内のユーザー管理サービスを作る",  # ユーザー / サービス
        "社員向けの顧客管理ツール（提案履歴つき）",  # 顧客 / 提案
        "社内の企画書テンプレート管理ツール",  # 企画
        "自社用のSaaS利用状況ダッシュボード",  # saas
        "社内用の販売管理システムを作る",  # 販売（意図なし）
        "internal tool for customer data cleanup",
    ],
)
def test_internal_tools_do_not_summon_sales_or_marketing(goal: str) -> None:
    assert _biz(goal) == set(), goal


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("顧客向けの勤怠管理Webサービスを作って販売したい", {"sales", "marketing"}),
        ("顧客向けSaaSを販売する", {"sales", "marketing"}),
        # 外部への販売: 営業部に加えてマーケティング部も招集（社外販売の同時招集）
        ("社内ツールを作って外部にも販売したい", {"sales", "marketing"}),
        ("新サービスのランディングページを作る", {"sales", "marketing"}),  # 社外向け
        ("ユーザー向けの家計簿アプリ", {"marketing"}),
        # 「有料」は販売の意図: 価格を決めるマーケティング部も招集
        ("有料プランの見積機能をつけたWebアプリ", {"sales", "marketing"}),
    ],
)
def test_external_or_selling_goals_still_summon(goal: str, expected: set[str]) -> None:
    assert _biz(goal) == expected, goal


def test_internal_absentee_reason_is_explicit() -> None:
    _, absentees = select_attendees("社内のユーザー管理サービスを作る")
    assert "社内向け" in absentees["sales"]
    assert "社内向け" in absentees["marketing"]


def test_strong_and_weak_keywords_do_not_overlap() -> None:
    assert is_internal_goal("社内用ツール") and not is_internal_goal("顧客向けアプリ")
    for dept in DEPARTMENTS:
        assert not set(dept.keywords) & set(dept.weak_keywords), dept.id
        # 「売」単体は使わない（「売上」に誤反応する）
        assert "売" not in dept.keywords
    sales = next(d for d in DEPARTMENTS if d.id == "sales")
    assert summon_hits(sales, "社内の売上レポート") == []


# --- G-1: 社外向けの目印が社内向けの目印に勝つ / 「内製」は目印にしない ---------


@pytest.mark.parametrize(
    ("goal", "must_include"),
    [
        ("社内勤怠システムをSaaS化して他社に提供", {"sales", "marketing"}),
        ("内製化のための顧客向けポータル", {"sales"}),
        ("社内報アプリを社外の取引先にも提供", {"sales"}),
        ("社内ツールをお客様向けにも公開したい", {"sales"}),
    ],
)
def test_external_marker_wins_over_internal(goal: str, must_include: set[str]) -> None:
    assert must_include <= _biz(goal), goal


def test_internal_external_marker_predicates() -> None:
    assert not is_internal_goal("社内勤怠を他社に提供")
    assert not is_internal_goal("内製化のための顧客向けポータル")
    assert not is_internal_goal("内製化したポータル")  # 内製 alone is not internal
    assert is_internal_goal("外部のAPIと連携する社内ツール")  # 外部の ≠ 社外向け


# --- G-2: 強いキーワードの誤検知 -------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "社内の売れ筋分析ツールを作る",
        "社内向け見積作成ツールを作る",
        "社内の課金データ集計バッチ",
        "Salesforce連携の社内ツール",
        "社内の市場データ分析",
        "社内のブランドガイドライン管理",
    ],
)
def test_internal_tools_with_business_words_do_not_summon(goal: str) -> None:
    assert _biz(goal) == set(), goal


def test_ascii_keywords_match_whole_words() -> None:
    sales = next(d for d in DEPARTMENTS if d.id == "sales")
    assert summon_hits(sales, "Salesforce連携") == []
    assert "sales" in summon_hits(sales, "sales向けダッシュボード")
    assert "売れる" in summon_hits(sales, "売れる商品を作る")


# --- selling goals co-summon 法務部 / マーケティング部 -----------------------------


def test_selling_goal_summons_legal_and_marketing() -> None:
    from ai_company.services.executive_meeting import select_attendees

    for goal in (
        "小さな備品貸出管理Webアプリを作って、社外の中小企業にも販売したい",
        "備品管理アプリを有料で売りたい",
        "社内勤怠を他社に提供したい",
    ):
        attendees, absentees = select_attendees(goal)
        assert {"sales", "legal", "marketing"} <= set(attendees), goal
        assert "legal" not in absentees and "marketing" not in absentees


def test_internal_goals_do_not_co_summon() -> None:
    from ai_company.services.executive_meeting import select_attendees

    for goal in ("社内用のCLIツールを作る", "社内の売上集計ツールを作る", "ブログを作りたい"):
        attendees, _ = select_attendees(goal)
        assert "legal" not in attendees and "marketing" not in attendees, goal


def test_selling_reason_is_reported_as_hit() -> None:
    from ai_company.org import department_by_id, summon_hits

    hits = summon_hits(department_by_id("legal"), "備品管理アプリを有料で売りたい")
    assert hits == ["社外販売（契約・利用規約・個人情報）"]
