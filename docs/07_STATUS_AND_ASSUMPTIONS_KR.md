# BMO NEXUS 사실 / 가정 / 미해결 사항

기준: 2026-10-25. 본 문서는 사용자가 Raspberry Pi에서 직접 실행한 `inspect_pi.sh`의 **민감 정보를 생략한 결과**를 반영한다. 자세한 실측 표는 [09_PI_BASELINE_KR.md](09_PI_BASELINE_KR.md).

## 실측 확인 (2026-10-25)

- Raspberry Pi 5, RAM 8 GB, OS Debian GNU/Linux 13 (trixie), aarch64.
- microSD 512GB: `/dev/mmcblk0p2` (ext4)가 루트 `/`로 마운트됨. OS가 **SD에서 부팅 중**.
- NVMe 1TB: `nvme0n1` 크기 **0B**로 표시되는 반면 `nvme0n1p1`은 931.5G로 표시됨. 파일시스템과 마운트 확인 불가. **정상/공백/고장 어느 것도 아직 단정 금지**.
- Docker, cloudflared, restic은 발견되지 않음. Tailscale, SSH, WayVNC, Ollama, BMO case fan 서비스는 실행 중.
- BMO 관련 GUI/대화 프로세스가 있으나 launcher/autostart/자식 정리 방식은 미확인.
- SSH/VNC/rpcbind가 로컬 모든 인터페이스에 바인딩됨. **인터넷 노출을 의미하지는 않음**.
- GitHub 저장소 `GUNOBAE/BGH_BMO_NEXUS`는 생성/초기 업로드 완료.

## 운영/프로젝트 확정 결정

- BMO 로봇과 NEXUS 서버 앱은 동시에 돌리지 않는 Dual-Mode 전략.
- Codex는 **BMO 개발 담당**과 **NEXUS 저장소 개발 담당**으로 분리. NEXUS는 기존 BMO 리포지터리에 변경을 가하지 않는다.
- 원래 BMO 화면/음성/기구 프로젝트는 기존 리포에 보존. 웹사이트/포트폴리오 UI는 별도 대화에서 구현.
- SERVER ↔ ROBOT 앱 전환은 실제 launcher/stop 계약이 나온 뒤 구현. SSH/Tailscale 및 냉각팬은 기본 유지.

## 해결 전에는 진행하지 않을 사항

1. NVMe의 0B 보고 원인과 원본 데이터 확인 전 포맷/파티션/서버 데이터 설치 금지.
2. BMO 시작/정지 방식과 백업 검증 전 `systemctl disable`, `killall` 등 변경 금지.
3. 서버 테스트/공개 전 Docker와 포트·방화벽 정책 설계 및 사용자 승인 필요.
4. 아직 BMO/Server 전환 제어기가 완성되거나 현장 검증된 것이 아니다.

## 다음 작업 (Codex Task 01)

- [Issue #1](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/1) — read-only NVMe/프로세스 후속 점검 도구와 문서/테스트.
- 저장소 루트의 `AGENTS.md`와 [10_CODEX_WORKFLOW_KR.md](10_CODEX_WORKFLOW_KR.md)에서 협업/금지 규칙 확인.
- 현장 명령은 사용자 승인으로 실행. 민감 데이터가 들어간 원문을 GitHub에 올리지 않는다.

## Phase 2B 인계 반영 (2026-10-26, 새 현장 조사 아님)

[Issue #4 상세 인계](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/4#issuecomment-6400459832)와
[NEXUS 리뷰](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/4#issuecomment-6400500959)를 바탕으로
[BMO lifecycle 계약](13_BMO_LIFECYCLE_CONTRACT_KR.md)을 작성했다. 위의 2026-10-25 기록을
현재 상태로 덮어쓰지 않고, 관측/코드/미검증/새 설계를 구분한다.

- 최신 BMO `0287bb23c1fafc357c9a18419f62b2ebdf0de767`의 UID1000 수동 Desktop/PTT 옵션 실행과
  Chromium 자식이 관측됐다. 이후 PID 소멸은 정상 종료 시험 합격이 아니다.
- 조사 범위에서 BMO 앱 autostart를 발견하지 못했다. 최신 checkout의 기본 venv는 없었고
  실제 interpreter 선택은 미확인이다. runtime 권한 변화도 자동 수리의 근거가 아니다.
- Desktop 종료 요청은 구현돼 있으나 stable ready/clean-exit ACK·전체 종료 상한·실물 장치 반환은
  미확인이다. 따라서 NEXUS 제어기/adapter는 미구현·Pi 미적용이며 실제 전환을 허용하지 않는다.
- Ollama는 확인한 BMO 코드 의존성이 없지만 별도 서비스가 실행 중이었고 다른 사용처는 미확인이다.
  SSH/Tailscale/VNC/NetworkManager/팬과 함께 그대로 유지한다.
- Issue #4 후속 인계에는 SSD 용량·ext4·rw mount·controller live 복귀라는 소유자 보고가 있다.
  이번 문서에서 재측정하지 않았고 원인·데이터 무결성·재발/장기 안정성·백업 승인·서버 설치는 별개다.
- 다음 단계는 이 문서 PR 검토와 별도 Mock 상태기계 작업의 합의다. 실제 BMO 종료나 서버 배포가 아니다.
