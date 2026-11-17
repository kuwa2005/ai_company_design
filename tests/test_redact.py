"""Secret redaction: English key=value plus Japanese keys / whitespace forms (R-3)."""

from __future__ import annotations

import pytest

from ai_company.services.executive_meeting import quote_goal_block
from ai_company.services.redact import redact_obj, redact_text


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("password=abc123", "abc123"),
        ("パスワード: hunter2", "hunter2"),
        ("パスワード：hunter2", "hunter2"),
        ("パスワード＝hunter2", "hunter2"),
        ("暗証番号=4321", "4321"),
        ("APIキー: AKIA1234567890", "AKIA1234567890"),
        ("API キー=xyz-789", "xyz-789"),
        ("ＡＰＩキー：abc_123", "abc_123"),
        ("トークンは ghp_abcdef123456 です", "ghp_abcdef123456"),
        ("アクセストークン abcd-efgh-1234", "abcd-efgh-1234"),
        ("秘密鍵: keydata-01", "keydata-01"),
        ("認証情報 admin:Passw0rd!", "admin:Passw0rd!"),
        ("PASSWORD hunter3", "hunter3"),
        ("api_key   s3cr3t-value", "s3cr3t-value"),
        ("token\tabc.def.123456", "abc.def.123456"),
        # L-1: = / : forms of access_key / secret_key / passphrase
        ("access_key=abc123", "abc123"),
        ("secret_key=abc123", "abc123"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtn", "wJalrXUtn"),
        ("passphrase: open-sesame", "open-sesame"),
        ("client_secret=abc123", "abc123"),
        # L-2: short PINs with a weak separator
        ("暗証番号 1234", "1234"),
        ("暗証番号は1234", "1234"),
        ("PIN 0000", "0000"),
        # L-5: the credential after the auth scheme
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("authorization=Bearer abc.def.ghi", "abc.def.ghi"),
    ],
)
def test_secret_values_are_redacted(raw: str, secret: str) -> None:
    out = redact_text(raw)
    assert secret not in out, out
    assert "***" in out


@pytest.mark.parametrize(
    "prose",
    [
        "パスワード再設定機能を作る",
        "パスワードは定期的に変更してもらう",
        "トークン単位で課金する",
        "password reset flow",
        "token budget per user",
        "秘密鍵の保管方法を決める",
        "暗証番号の再設定画面",
        "PIN番号を入力する画面",
        "secret key rotation policy",
    ],
)
def test_ordinary_prose_is_kept(prose: str) -> None:
    assert redact_text(prose) == prose


def test_pem_private_key_block_is_redacted() -> None:
    raw = (
        "設定:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow123\nabcDEF\n"
        "-----END RSA PRIVATE KEY-----\n以上"
    )
    out = redact_text(raw)
    assert "MIIEow123" not in out and "abcDEF" not in out
    assert out.startswith("設定:") and out.endswith("以上")


def test_redact_obj_applies_japanese_patterns_recursively() -> None:
    obj = {"summary": "パスワード: hunter2", "items": ["APIキー=k-123456"], "n": 3}
    out = redact_obj(obj)
    assert "hunter2" not in out["summary"]
    assert "k-123456" not in out["items"][0]
    assert out["n"] == 3


def test_meeting_goal_block_hides_japanese_secrets() -> None:
    block = "\n".join(
        quote_goal_block("勤怠Webを作る。管理者パスワード: hunter2、PASSWORD hunter3")
    )
    assert "hunter2" not in block
    assert "hunter3" not in block
    assert "勤怠Webを作る" in block
