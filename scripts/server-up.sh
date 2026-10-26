#!/usr/bin/env bash
# 승인 후 운영자가 직접 실행한다. BMO를 대신 종료하지 않는다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/private_cloud.py" up "$@"
