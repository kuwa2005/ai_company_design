"""Redact secrets from logs and structured output before persistence."""

from __future__ import annotations

import re
from typing import Any

# Japanese secret keys (the product is used in Japanese, so these are the
# primary inputs: 「パスワード: xxx」「APIキーは xxx」など).
_JA_KEYS = (
    r"パスワード|パスフレーズ|暗証番号|ＡＰＩキー|API\s?キー|アクセスキー|"
    r"シークレット(?:キー)?|クライアントシークレット|アクセストークン|トークン|"
    r"秘密鍵|プライベートキー|認証情報"
)
_EN_KEYS = r"api[_-]?key|access[_-]?key|token|secret|password|passwd|passphrase"
# A value that looks like a secret rather than prose: 6+ chars incl. a digit or
# symbol. Used only where the separator is weak (whitespace / 「は」), so that
# 「パスワード再設定機能」 or "password reset flow" stay readable.
_SECRETISH = r"(?=[^\s、。，,]*[0-9!#$%&*+/@^_~\-])[^\s、。，,]{6,}"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "Authorization: Basic xxx" — the credential is the token after the scheme
    (
        re.compile(r"(?i)(authorization)\s*[=:]\s*(?:basic|bearer|digest|token)\s+\S+"),
        r"\1=***",
    ),
    # English key (+ optional _key suffix: secret_key, access_key) + = / :
    (
        re.compile(
            rf"(?i)((?:{_EN_KEYS}|authorization)(?:[_-]?key)?)\s*[=:]\s*\S+"
        ),
        r"\1=***",
    ),
    # 暗証番号 / PIN are usually 4 digits — too short for _SECRETISH
    (
        re.compile(r"(暗証番号|\bPIN)(?:\s*は\s*|\s*[=:：＝]\s*|\s+)\d{4,}"),
        r"\1=***",
    ),
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "sk-***"),
    # scheme://user:password@host (postgresql+psycopg://, mongodb+srv://, redis://:pw@,
    # https://…) → hide only the credentials; scheme, host, path and the text after
    # the URI stay (L-1). URIs without credentials are left alone.
    # Linear time (M-1): the scheme may only start where the previous char cannot
    # be part of a scheme, and its length is bounded — ``\b`` + ``*`` let every
    # ``.``/``-`` boundary restart a full scan (O(n²) on "a.a.a.…").
    (
        re.compile(
            r"(?i)(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]{0,31}://)"
            r"[^\s:/@\"'<>]*:[^\s/@\"'<>]+@"
        ),
        r"\1***:***@",
    ),
    # PEM private key blocks (秘密鍵ファイルの中身)
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "-----PRIVATE KEY ***-----",
    ),
    # Japanese key + explicit separator (= : ＝ ：) → always redact the value
    (
        re.compile(rf"(?i)({_JA_KEYS})\s*[=:：＝]\s*[^\s、。，,]+"),
        r"\1=***",
    ),
    # Japanese key + weak separator (space / 「は」) → only secret-looking values
    (
        re.compile(rf"(?i)({_JA_KEYS})(?:\s*は\s*|\s+){_SECRETISH}"),
        r"\1=***",
    ),
    # English key + whitespace only ("PASSWORD hunter3") → only secret-looking values
    (
        re.compile(rf"(?i)\b({_EN_KEYS})\s+{_SECRETISH}"),
        r"\1=***",
    ),
]


def redact_text(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


# C0 / DEL / C1, line/paragraph separators, and invisible format chars that can
# reorder or hide text on screen: soft hyphen, ALM, zero-width (U+200B-200F),
# bidi embeddings/overrides (U+202A-202E), word joiner + bidi isolates
# (U+2060-2069), BOM (S-4).
_INVISIBLE = r"\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff"
_CONTROL_CHARS = re.compile(rf"[\x00-\x1f\x7f-\x9f{_INVISIBLE}]")


def single_line(text: object) -> str:
    """Untrusted text → one line: control chars (incl. ESC, newlines) become spaces.

    Use for worker/LLM-supplied values that are embedded in prompts, minutes or
    terminal output, so they cannot open a new line or emit escape sequences.
    """
    return " ".join(_CONTROL_CHARS.sub(" ", str(text or "")).split())


_CONTROL_EXCEPT_NEWLINE = re.compile(rf"[\x00-\x09\x0b-\x1f\x7f-\x9f{_INVISIBLE}]")


def multi_line(text: object) -> str:
    """Untrusted text that may keep its line structure (instructions, lists).

    CRLF/CR become ``\\n``; other control chars (ESC, NUL, tabs, U+2028/9)
    become spaces. Use where the text is the task's *own* instructions; use
    ``single_line`` where it is embedded into other prompts or minutes.
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(_CONTROL_EXCEPT_NEWLINE.sub(" ", ln).split()) for ln in raw.split("\n")]
    return "\n".join(lines).strip("\n")
