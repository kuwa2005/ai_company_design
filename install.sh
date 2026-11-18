#!/usr/bin/env bash
# AI Company one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/kuwa2005/ai_company_design/main/install.sh | bash
#
# What it does (no sudo, nothing outside the two directories below):
#   1. clone (or update) the repository into  $AI_COMPANY_HOME   (default ~/.local/share/ai-company)
#   2. create a Python venv there and `pip install -e .`
#   3. link the command to                      $AI_COMPANY_BIN_DIR/ai-company (default ~/.local/bin)
# It does not edit your shell profile; it prints the PATH line to add if needed.
#
# Options (arguments after `bash -s --`, or environment variables):
#   --version <tag|branch>   AI_COMPANY_VERSION   default: latest release tag (falls back to main)
#   --home <dir>             AI_COMPANY_HOME      install location of the source + venv
#   --bin-dir <dir>          AI_COMPANY_BIN_DIR   where the `ai-company` link goes
#   --repo <url>             AI_COMPANY_REPO      git URL (default: the GitHub repository)
#   --uninstall              remove the link and $AI_COMPANY_HOME
#
# Example (pin a version):
#   curl -fsSL https://raw.githubusercontent.com/kuwa2005/ai_company_design/main/install.sh | bash -s -- --version v0.1.0
set -euo pipefail

REPO="${AI_COMPANY_REPO:-https://github.com/kuwa2005/ai_company_design.git}"
VERSION="${AI_COMPANY_VERSION:-}"
HOME_DIR="${AI_COMPANY_HOME:-${HOME}/.local/share/ai-company}"
BIN_DIR="${AI_COMPANY_BIN_DIR:-${HOME}/.local/bin}"
UNINSTALL=0

say() { printf '[ai-company] %s\n' "$*"; }
die() { printf '[ai-company] エラー: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a value}"; shift 2 ;;
    --home) HOME_DIR="${2:?--home needs a value}"; shift 2 ;;
    --bin-dir) BIN_DIR="${2:?--bin-dir needs a value}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,22p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "不明なオプション: $1" ;;
  esac
done

SRC="${HOME_DIR}/src"
VENV="${HOME_DIR}/venv"
TARGET="${BIN_DIR}/ai-company"

if [[ "${UNINSTALL}" == 1 ]]; then
  if [[ -L "${TARGET}" ]]; then rm -f "${TARGET}"; say "削除: ${TARGET}"; fi
  if [[ -d "${HOME_DIR}" ]]; then
    case "${HOME_DIR}" in
      ""|"/"|"${HOME}"|"${HOME}/") die "安全のため ${HOME_DIR} は削除しません" ;;
    esac
    rm -rf "${HOME_DIR}"; say "削除: ${HOME_DIR}"
  fi
  say "アンインストールしました（各作業フォルダの .ai_company/ は残っています）"
  exit 0
fi

# --- prerequisites ---------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git が見つかりません。先に git をインストールしてください"

PY=""
for cand in python3.13 python3.12 python3; do
  if command -v "${cand}" >/dev/null 2>&1 \
    && "${cand}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY="$(command -v "${cand}")"; break
  fi
done
[[ -n "${PY}" ]] || die "Python 3.12 以上が見つかりません（python3 --version で確認してください）"
"${PY}" -c 'import venv, ensurepip' 2>/dev/null \
  || die "Python の venv が使えません（Debian/Ubuntu なら: sudo apt install python3-venv）"

# --- version ---------------------------------------------------------------
if [[ -z "${VERSION}" ]]; then
  VERSION="$(git ls-remote --tags --refs "${REPO}" 'v*' 2>/dev/null \
    | sed 's#.*refs/tags/##' | sort -V | tail -n 1 || true)"
  [[ -n "${VERSION}" ]] || VERSION="main"
fi
say "インストールするバージョン: ${VERSION}"

# --- source ----------------------------------------------------------------
mkdir -p "${HOME_DIR}" "${BIN_DIR}"
if [[ -d "${SRC}/.git" ]]; then
  say "更新: ${SRC}"
  git -C "${SRC}" fetch --quiet --tags --force origin
  if git -C "${SRC}" rev-parse --verify --quiet "refs/tags/${VERSION}" >/dev/null; then
    git -C "${SRC}" checkout --quiet --force "refs/tags/${VERSION}"
  else
    git -C "${SRC}" fetch --quiet origin "${VERSION}"
    git -C "${SRC}" checkout --quiet --force FETCH_HEAD
  fi
else
  [[ ! -e "${SRC}" ]] || die "${SRC} が既にあり、git リポジトリではありません。削除してから再実行してください"
  say "取得: ${REPO}"
  git clone --quiet --depth 1 --branch "${VERSION}" "${REPO}" "${SRC}"
fi

# --- venv + package --------------------------------------------------------
if [[ ! -x "${VENV}/bin/python" ]]; then
  say "Python 仮想環境を作成: ${VENV}"
  "${PY}" -m venv "${VENV}"
fi
"${VENV}/bin/python" -m pip install --quiet --upgrade pip
# editable: config/ と prompts/ はソースの場所から読む
"${VENV}/bin/python" -m pip install --quiet -e "${SRC}"

ln -sfn "${VENV}/bin/ai-company" "${TARGET}"
"${TARGET}" --help >/dev/null || die "インストール後の起動確認に失敗しました"

say "インストール完了: ${TARGET} （$(git -C "${SRC}" describe --tags --always 2>/dev/null || echo "${VERSION}")）"
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *)
    say "${BIN_DIR} が PATH にありません。次の行をシェルの設定（~/.bashrc など）に追加してください:"
    printf '  export PATH="%s:$PATH"\n' "${BIN_DIR}"
    ;;
esac
cat <<'EOS'

次の一歩（作業フォルダごとに）:
  mkdir -p ~/workspace/my-app && cd ~/workspace/my-app && git init
  ai-company init && ai-company doctor
  ai-company start "小さなCLIツールを作る" --autonomous --runner mock

使い方: ユーザーズマニュアル.md（https://github.com/kuwa2005/ai_company_design）
実際の開発には Cursor Agent CLI（agent）のインストールとログインが必要です。
EOS
