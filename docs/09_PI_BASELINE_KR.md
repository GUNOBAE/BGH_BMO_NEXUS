# Raspberry Pi 5 — 최초 현장 점검 결과 (2026-10-25)

> **확인된 사실만 기록한 공개용 요약**. 원본 로그, Tailscale 주소, 개인 경로, 자격증명은 넣지 않는다. 이 문서는 설치 성공 보고서가 아니다.

## 실측 요약

| 항목 | 결과 | 해석 |
|---|---|---|
| 하드웨어 | Raspberry Pi 5, RAM 8 GB | 재활용 서버 본체 |
| OS | Debian GNU/Linux 13 (trixie), aarch64, 6.18.50+rpt-rpi-2712 | arm64 패키지 경로 사용 |
| 부팅 루트 | `/dev/mmcblk0p2` → `/`, ext4 | **512 GB microSD가 현재 OS 루트** |
| SD 사용량 | 루트 약 470G, 사용 31G, 여유 420G (`df` 기준) | 기존 BMO와 OS 보존 |
| NVMe | `nvme0n1` 크기 **0B**, 하위 `nvme0n1p1` 크기 931.5G | **불일치/인식 이상 가능성. 원인 미확인** |
| NVMe 파일시스템 | 해당 파티션의 FSTYPE 미표시, 마운트 지점 없음 | *새 디스크/빈 디스크로 판단 금지* |
| RAM | 약 7.9 GiB 전체, 6.1 GiB available (한 시점) | 경량 웹 서비스 계획 가능 |
| Tailscale, SSH | 작동 중 | 관리 연결 유지 |
| VNC | wayvnc 실행 중 | 접근 범위 점검 필요 |
| RPC/NFS | rpcbind가 TCP 111번에서 LISTEN | 사용 필요성 및 LAN 접근 범위 확인 |
| Ollama | 서비스 실행 중 (localhost 11434) | BMO가 필요로 하는지 확인 전 변경 금지 |
| BMO 냉각 | `bmo-case-fan.service` 실행 중 | **상시 유지 대상** |
| Docker, cloudflared, restic | 미설치 | 도입은 다음 승인 단계 |
| BMO 앱 | python/node/chromium 프로세스 관측 | 실행 엔트리포인트/종료 정책은 미확정 |

**보안 관찰:** SSH 22, VNC 5900, rpcbind 111이 와일드카드 인터페이스에서 LISTEN 상태였다. 이는 Pi의 네트워크 인터페이스에서 수신 대기한다는 의미일 뿐, 실제 공인 인터넷 노출 여부는 공유기/방화벽/Tailscale ACL 확인 전에는 알 수 없다.

## NVMe 0B 불일치 — 가장 먼저 해결할 판정

표시된 `931.5G` 파티션만 보고 용량이 정상이라고 결론 내리면 안 된다. 반대로 `0B`라고 해서 비어 있는 SSD라고 단정해도 안 된다. 전원/연결/PCIe 링크/컨트롤러/커널 상태 등 원인은 아직 확인하지 않았다.

다음은 **읽기만 하는** 후속 점검 예시. 개인 경로와 장치 식별번호가 포함될 수 있으므로 전체 원문을 공개 이슈에 게시하지 않는다.

```bash
lsblk -b -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
lsblk -f
findmnt -no SOURCE,TARGET,FSTYPE /
cat /sys/class/block/nvme0n1/size 2>&1
journalctl -k -b --no-pager | grep -Ei 'nvme|pcie' | tail -n 50
```

- `lsblk -b`: 반올림 없는 실제 보고 바이트 수와 파일시스템 탐지 정보 조회
- `findmnt`: 루트 파일시스템 장치 확인
- `/sys/class/block/.../size`: 커널이 보고하는 장치 섹터 수 (정상 인식 판단의 단서)
- `journalctl -k -b`: 이번 부팅의 NVMe/PCIe 오류 메시지 확인

**금지:** `mkfs`, `wipefs`, `parted` 변경, 재파티셔닝, 쓰기 마운트, USB/PCIe 분리, 재부팅을 통한 탐색 등은 현재 단계에서 하지 않는다.

## BMO 실행 방식 추가 조사 필요

`systemctl`에 확인된 BMO 관련 시스템 서비스는 냉각팬뿐이다. python/node/chromium 프로세스가 발견됐지만 실제 BMO 런처, 자식 프로세스, GUI 자동실행 여부는 아직 모른다.

기존 로봇 Codex가 별도 저장소에서 개발 중이므로, NEXUS 작업은 **현재 BMO 프로세스를 종료하거나 실행 방식을 변경하지 않는다**. 기존 개발 담당자로부터 BMO 시작/정지 인터페이스(계약)를 확보한 후 모드 제어기를 구현한다.

## 다음 결정을 내리기 위한 완료 조건

1. NVMe 실제 용량이 정상적으로 보고되는지, 기존 파일시스템/데이터가 무엇인지 **읽기 전용으로** 확인.
2. BMO 런처 및 정지 방식 문서화. 불명확한 상태에서 자동 프로세스 중지 금지.
3. BMO 원본/비추적 작업파일 포함 별도 백업 위치와 복원 절차 확인.
4. 그 후에만 서버 저장 디렉터리/도커/모드 전환 설계 단계로 이동.

상태: **사전 진단 완료, 저장장치/BMO lifecycle 정밀 확인 대기.** 실서비스 설치, 인터넷 공개 및 모드 제어기는 아직 없다.

## 후속 점검 결과 (2026-10-25)

- **PR #2 병합 완료:** 현장 사용자가 NEXUS 진단 스크립트를 Pi에서 실행하고 `bash -n` 및 Python 모의 테스트 **31개 모두 통과**를 보고. 이는 테스트 기능/출력의 확인이며 저장장치/서버 정상 판정이 아님.
- `nvme0n1` 전체 보고 크기 **0 byte**, sysfs 섹터 수 **0**, 하위 `nvme0n1p1` 보고 크기 **1,000,203,091,968 byte**로 불일치 재현. 파일시스템 미검출. 원본 데이터 유무, 원인, 안정성은 미확정.
- 현재 `/`는 여전히 `/dev/mmcblk0p2` (ext4). 
- 서비스와 프로세스의 허용된 이름만 확인했으며, **정확한 BMO 시작/종료 계약은 확보하지 못함**.
- 본 단계에서 디스크 포맷/마운트/서버 설치/프로세스 중지는 시행하지 않음.

다음 작업은 [NVMe 0B 진단 Issue #3](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/3)과 [BMO 실행 계약 Issue #4](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/4)로 나누어 관리한다. 원본 로그는 공개 저장소에 등록하지 않는다.
