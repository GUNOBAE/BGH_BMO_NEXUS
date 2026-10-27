#!/usr/bin/env bash
# 기존 BMO는 수동 종료한다. SSD 검사 후 새 미러 세대만 만들고 이전 사본은 유지한다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/github_mirror.py" "$@"
