#!/usr/bin/env bash
# 설치·파일 저장 없이 후속 점검기를 실행한다. 출력은 표준 출력만 사용한다.
set -eu

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'python3 없음: 점검하지 못했습니다. 패키지를 자동 설치하지 않습니다.'
  exit 0
fi

# -B: Python 바이트코드 캐시(__pycache__)를 만들지 않는다.
script_dir=.
if [[ ${BASH_SOURCE[0]} == */* ]]; then
  script_dir=${BASH_SOURCE[0]%/*}
fi
exec python3 -B "$script_dir/inspect_pi_followup.py" "$@"
