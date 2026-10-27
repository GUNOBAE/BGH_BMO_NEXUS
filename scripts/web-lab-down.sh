#!/usr/bin/env bash
# Web Lab만 내리고 종료를 검증한다. 사이트 데이터·SSD mount는 보존한다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/web_lab.py" down "$@"
