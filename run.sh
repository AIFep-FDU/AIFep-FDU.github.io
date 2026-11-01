#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${DASHSCOPE_API_KEY:?请先配置 DASHSCOPE_API_KEY}"
: "${DASHSCOPE_BASE_URL:?请先配置百炼 compatible-mode/v1 地址 DASHSCOPE_BASE_URL}"
exec "${TABLEQA_PYTHON:-python}" baseline.py run "$@"
