#!/usr/bin/env bash
# BMO는 수동 종료한다. Ctrl+C는 이 세션의 공유와 NEXUS 컨테이너를 순서대로 정리한다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/cloud_session.py" "$@"
