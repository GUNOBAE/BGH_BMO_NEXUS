#!/usr/bin/env bash
# 승인 후 운영자가 직접 실행한다. 기존 GUI에서 BMO를 먼저 정상 종료한다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/web_lab.py" up "$@"
