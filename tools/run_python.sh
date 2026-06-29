#!/usr/bin/env bash
#
# 以 tools/.venv（若存在）執行 Python 腳本，否則 fallback 至 uv run 或系統 python3。
#
# 用法:
#   tools/run_python.sh tools/sync_external_storage.py --help
#   tools/run_python.sh tools/gen_mounts_local_test.py --users chad --style account -o /tmp/m.json
#

set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${TOOLS_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

if [[ $# -lt 1 ]]; then
    echo "用法: $0 <script.py> [args...]" >&2
    exit 1
fi

VENV_PYTHON="${TOOLS_DIR}/.venv/bin/python"
if [[ -x "${VENV_PYTHON}" ]]; then
    exec "${VENV_PYTHON}" "$@"
fi

if command -v uv &>/dev/null && [[ -f "${TOOLS_DIR}/pyproject.toml" ]]; then
    exec uv run --directory "${TOOLS_DIR}" python "$@"
fi

if command -v python3 &>/dev/null; then
    exec python3 "$@"
fi

echo "❌ 找不到 Python：請在 tools/ 執行 uv sync 建立 .venv" >&2
exit 1
