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
