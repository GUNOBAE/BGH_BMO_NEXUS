# Mode Controller — ROBOT / SERVER / MAINTENANCE

> 현재는 **구현 명세**다. `nexus mode ...` 명령이나 systemd 유닛이 Pi에 설치되어 있다는 뜻이 아니다.

## 한 OS의 선택적 실행

```text
                          Raspberry Pi OS
                                |
                      Mode Controller (planned)
                                |
         +----------------------+-----------------------+
         |                      |                       |
    ROBOT MODE             SERVER MODE             MAINTENANCE
    BMO software ON       BMO software OFF        Both OFF
    server apps OFF       private apps ON         public tunnel OFF
    public tunnel OFF     approved tunnel ON      SSH/Tailscale retained
```

- 기존 BMO의 화면·음성·카메라·RP2040 구동 방식은 보존한다.
- 서버 앱을 Docker Compose 등으로 새로 구성하되 **기존 BMO 자체를 Docker로 강제 이전하지 않는다.**
- `systemd`는 실행/중지/로그 관리 계층으로 쓰고, 시작 순서를 관리하는 얇은 모드 제어기를 별도로 둔다.
- 인증 가능한 로컬 CLI부터 시작하고, 웹 제어 패널은 안정성 확보 이후 사설망 전용으로 검토한다.

## 필수 불변식 (어겨지면 전환 실패)

1. 두 워크로드가 동시에 살아 있지 않을 것. 종료됐는지 프로세스/서비스/PID/컨테이너를 **실제로 검사**한다.
2. ROBOT/MAINTENANCE 모드에선 공개 Cloudflare Tunnel이 실행 중이지 않을 것.
3. 공개 터널은 SERVER 기능과 마운트/권한/건강 검사를 통과한 뒤 **마지막에** 시작할 것.
4. 공개 사이트 컨테이너는 개인 BMO 데이터, SSH 키, 토큰, Docker socket에 접근하지 않을 것.
5. SSD가 필요한 서비스는 실제 SSD 마운트가 없으면 시작하지 않을 것.
6. 한 번에 한 전환만 수행(lock). 실패하면 대상 서비스를 억지로 실행하지 않고 비공개 상태 유지.

## 전환 알고리즘 (개념용 의사코드)

```text
function switch_mode(target):
  validate target and operator authorization
  acquire exclusive lock
  stop public tunnel; verify stopped
  stop currently active workloads; verify stopped
  if cannot prove workloads stopped:
      log failure; never start target; ask operator to repair
  check target prerequisites (SSD present, required devices/ports/units)
  start target workload only
  perform health checks
  if target == SERVER and explicitly approved public demo exists:
      start public tunnel LAST
  record current mode, version, check results and timestamp
  on error: keep public access OFF; stop what can be stopped safely; report inconsistent state
  release lock
```

**종료 확인**을 시도하는 동안 SSH/Tailscale, 네트워크 매니저, 운영체제 자체를 중지 대상으로 잡지 않는다. `systemctl isolate`처럼 불필요한 유닛까지 멈출 수 있는 명령은 초기 버전에서 피한다.

## 전환 순서

- **SERVER → ROBOT:** 터널 중단 → 모든 서버 컨테이너/백업 작업 종료 및 확인 → BMO에 필요한 디스플레이·오디오·USB 점유 점검 → 기존 BMO 정상 실행 → 얼굴/음성/버튼 검증.
- **ROBOT → SERVER:** BMO 종료 → 프로세스/하드웨어 리소스 해제 확인 → SSD 존재 및 데이터 경로 확인 → 백업·클라우드·웹앱 시작 → 사설망 시험 → 승인된 공개 라우팅만 시작.
- **ANY → MAINTENANCE:** 공개 터널 OFF → 로봇·서버 앱 정지 → 로그 기록. 관리용 SSH/Tailscale은 유지.

## 실제 구현 전 필요한 정보

- BMO 시작 방법: systemd system unit, user unit, LXDE Wayfire autostart, shell, 다른 supervisor?
- BMO 앱 종료 시 어떤 자식 프로세스와 장치를 함께 정리하는가?
- 현재 부팅 때 BMO/Chromium kiosk 자동 실행 여부, 서비스 이름과 사용자.
- Docker/Cloudflare Tunnel 기존 설치 여부, 자동 재시작 및 포트 정책.
- 실제 root/SSD 마운트 정책, UPS 장시간 동작 여부.

## 재부팅 정책

현재 Pi의 기존 부팅 동작을 **바꾸지 않는다.** 최종 시스템을 구현하면서 안전한 기본 모드(필요시 MAINTENANCE) 또는 사용자가 마지막으로 선택한 모드 복구 중 무엇이 적절한지 검증 후 확정한다. 기본 원칙은 재부팅 중 공개 터널 자동으로 켜지지 않게 하는 것.
