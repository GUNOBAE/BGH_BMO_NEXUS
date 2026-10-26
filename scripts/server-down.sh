#!/usr/bin/env bash
# NEXUS 컨테이너만 내린다. 데이터 삭제·SSD 언마운트·BMO 시작은 하지 않는다.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$SCRIPT_DIR/private_cloud.py" down "$@"
