#!/usr/bin/env bash
# Local-dev installer: editable install + PATH shim in ~/.local/bin
# Future (GitHub one-liner) will use pipx / uvx; this script is for workspace clones.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
VENV="${ROOT}/.venv"
TARGET="${BIN_DIR}/ai-company"

mkdir -p "${BIN_DIR}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  python3 -m venv "${VENV}"
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"
python -m pip install -U pip -q
python -m pip install -e "${ROOT}" -q

ln -sfn "${VENV}/bin/ai-company" "${TARGET}"

if ! command -v ai-company >/dev/null 2>&1; then
  echo "Installed shim: ${TARGET}"
  echo "Add to PATH if needed:"
  echo "  export PATH=\"${BIN_DIR}:\$PATH\""
  echo "  # then: hash -r"
else
  echo "OK: $(command -v ai-company)"
fi

ai-company --help >/dev/null
echo "Usage from any project directory:"
echo "  mkdir -p ~/workspace/web勤怠 && cd ~/workspace/web勤怠"
echo "  ai-company start \"優秀なWeb勤怠を作って\" --autonomous --runner mock"
