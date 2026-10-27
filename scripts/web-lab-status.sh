#!/usr/bin/env bash
# Web Lab 컨테이너와 localhost 두 응답을 조회한다. 시작·종료는 하지 않는다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/web_lab.py" status "$@"
