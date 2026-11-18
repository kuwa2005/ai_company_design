"""Redact secrets from logs and structured output before persistence.

Two layers:

1. **伏せ字語リスト**（利用者が登録する固有名詞）: 顧客名・個人名・社内の案件名など、
   パターンでは見つけられない語を ``<data_dir>/redact_terms.txt`` に1行1語で書く。
   ``語 => 置き換え名`` と書くと、その名前に置き換える（例: ``株式会社〇〇 => 顧客A社``）。
2. **パターン**: パスワード・APIキー・接続文字列・秘密鍵・既知のトークン形式・
   メールアドレス・電話番号。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
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
    # Well-known token formats (no key word needed): Anthropic / AWS / GitHub /
    # Slack / Google API keys and JWTs.
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    (re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"), "AKIA***"),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}"), "gh*_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9\-]{10,}"), "xox*-***"),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}"), "AIza***"),
    (
        re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        "eyJ***(JWT)",
    ),
    # E-mail addresses (personal data). Documentation domains and git@host stay.
    (
        re.compile(
            r"(?<![A-Za-z0-9._%+\-])(?!git@)[A-Za-z0-9._%+\-]{1,64}@"
            r"(?!(?:[A-Za-z0-9\-]+\.)*(?:example\.(?:com|org|net)|localhost|invalid|test)\b)"
            r"[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,8}\.?(?![A-Za-z0-9\-])"
        ),
        "***@***",
    ),
    # Japanese phone numbers: 03-1234-5678 / 090-1234-5678 / 09012345678 / +81-90-…
    (
        re.compile(
            r"(?<![\d\-])(?:\+81[\-\s]?\d{1,4}|0\d{1,4})-\d{1,4}-\d{3,4}(?![\d\-])"
        ),
        "***-****-****",
    ),
    (re.compile(r"(?<!\d)0[789]0\d{8}(?!\d)"), "***-****-****"),
]


# --- 伏せ字語リスト（利用者が登録する固有名詞） ---------------------------------

_TERMS_FILE_NAME = "redact_terms.txt"
_ALIAS_SEP = "=>"
_DEFAULT_LABEL = "[伏せ字]"
_MIN_TERM_LEN = 2
_MAX_TERMS = 2000
_terms_cache: dict[str, Any] = {"key": None, "pattern": None, "repl": {}}


def redact_terms_path() -> Path:
    """``AI_COMPANY_REDACT_TERMS`` if set, else ``<data_dir>/redact_terms.txt``."""
    override = os.environ.get("AI_COMPANY_REDACT_TERMS")
    if override:
        return Path(override).expanduser().resolve()
    from ai_company.paths import data_dir

    return data_dir() / _TERMS_FILE_NAME


def parse_redact_terms(text: str) -> dict[str, str]:
    """``語`` or ``語 => 置き換え名`` per line; ``#`` comments and blank lines skipped."""
    terms: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        term, sep, alias = line.partition(_ALIAS_SEP)
        term = term.strip()
        alias = alias.strip() if sep else ""
        if len(term) < _MIN_TERM_LEN:
            continue
        # The replacement must not itself leak or contain a registered term.
        terms[term] = alias or _DEFAULT_LABEL
        if len(terms) >= _MAX_TERMS:
            break
    for term, alias in list(terms.items()):
        if alias != _DEFAULT_LABEL and any(t.lower() in alias.lower() for t in terms):
            terms[term] = _DEFAULT_LABEL
    return terms


def _load_terms() -> tuple[re.Pattern[str] | None, dict[str, str]]:
    path = redact_terms_path()
    try:
        st = path.stat()
    except OSError:
        return None, {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _terms_cache["key"] == key:
        return _terms_cache["pattern"], _terms_cache["repl"]
    try:
        terms = parse_redact_terms(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        terms = {}
    pattern: re.Pattern[str] | None = None
    repl = {t.lower(): a for t, a in terms.items()}
    if terms:
        # Longest first so 「株式会社〇〇商事」 wins over 「〇〇」.
        alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        pattern = re.compile(alts, re.IGNORECASE)
    _terms_cache.update({"key": key, "pattern": pattern, "repl": repl})
    return pattern, repl


def clear_redact_terms_cache() -> None:
    _terms_cache.update({"key": None, "pattern": None, "repl": {}})


def redact_terms(text: str) -> str:
    """Replace the owner's registered terms (customer / person / project names)."""
    pattern, repl = _load_terms()
    if pattern is None or not text:
        return text
    return pattern.sub(lambda m: repl.get(m.group(0).lower(), _DEFAULT_LABEL), text)


def redact_text(text: str | None) -> str:
    if not text:
        return ""
    out = redact_terms(text)
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
