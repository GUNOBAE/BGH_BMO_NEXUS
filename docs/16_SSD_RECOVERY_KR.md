# SSD dead 자동 복구 · ntfy 알림

기준: 2026-11-05. 코드 및 오프라인 검증 완료, **Pi 설치·실제 재부팅·휴대폰 알림 수신은 NOT_RUN**.
이 기능은 사용자가 요청한 SERVER 운용 중의 장애 복구다. 기존 BMO 런처/얼굴/음성/장치는 수정하지 않는다.

## 동작

1. 설치 직후는 `DISARMED`. ntfy 테스트를 전송하고 휴대폰에서 받은 뒤, BMO를 정상 종료하고 `arm` 한다.
2. 부팅 후 180초를 기다린다. 이후 약 30초 간격으로 커널의 NVMe 상태를 확인한다.
3. `dead`, 장치 소실, namespace 0 sector가 3번 연속 확인되면 복구를 시작한다. 최초 재부팅은 대략 부팅 후 4분 이후다.
4. **재부팅 요청 전** SD에 횟수와 boot ID를 원자적으로 저장하고 파일·부모 디렉터리를 fsync한다. 동일 부팅에서 중복 요청하지 않는다.
5. NEXUS의 두 Compose 프로젝트 컨테이너만 정지 요청 후 정상 `systemctl --no-block reboot`를 요청한다. 정지 확인 실패도 별도 알림에 남긴다. 강제 재부팅·SysRq·전원 차단은 사용하지 않는다.
6. 재부팅 이후에도 같은 장애면 반복한다. **초기 실행을 포함한 5번의 검사가 아니라, 최대 5회의 재부팅 요청**이다.
7. 다섯 번째 재부팅 후에도 실패가 확인되면 `BLOCKED`로 멈추고 다음 영어 알림을 보낸다.

> The SSD is still unavailable after 5 reboot attempts. Would you like me to try 5 more?

알림 뒤에는 자동 복구가 정지되었고 Tailscale SSH에서 승인하라는 안내도 붙는다.
추가 5회는 현재 incident ID와 명시적 `retry-five` 명령으로만 승인한다. 같은 승인 재실행은 거부한다.

| 상황 | 처리 |
|---|---|
| 정상 `live`, 예상 장치/UUID 매핑, ext4/rw mount | 관찰 계속 |
| `dead` / 소실 / 0 sector | 간격을 둔 3회 확인 후 5회 예산 내 재부팅 |
| `resetting`, 읽기 실패, UUID/시리얼 불일치, mount 없음/읽기 전용 | 재부팅하지 않고 `BLOCKED`, NEXUS 컨테이너 정지 요청·알림 |
| BMO가 실행 중이거나 프로세스 조사 불확실 | 재부팅 중지·알림. 로봇 프로세스 강제 종료 없음 |
| 재부팅 요청 뒤 180초간 boot ID 불변 | 추가 요청하지 않고 중지·알림 |
| 잠깐 정상 후 재발 | 기존 재부팅 예산 유지 |
| 30분 연속 정상 관찰 | 현재 배치 횟수 초기화, 누적 횟수 보존, 복구 완료 알림 |
| 관찰 공백 120초 초과/부팅 변경 | 연속 정상·실패 관찰 창 초기화. 재부팅 예산은 보존 |
| 인터넷 단절/ntfy 오류 | SD outbox 보존, 부팅 후 또는 5분 간격으로 전송 재시도 |
| 상태 파일 소실·손상·권한 이상 | 기본값으로 다시 만들지 않고 오류 종료, 자동 재부팅 없음 |

감시 코드는 SSD 파일·SMART·`df`·파일시스템 쓰기 시험을 수행하지 않는다. 살아 있는 것처럼 보이는 컨트롤러 뒤의 모든 I/O 장애를 탐지하는 기능은 아니다. 재부팅으로 파일시스템 손상이 복구된다고 주장하지 않으며 `fsck`/포맷/마운트/fstab/PCIe 설정을 자동 변경하지 않는다.

## 설치 전 조건

- OS `/`가 microSD의 `/dev/mmcblk…p…`에서 rw로 동작해야 한다.
- 현재 수동 클라우드용 `deploy/private-cloud/.env`가 root:root 0600이고 실제 SSD UUID·mount·UID를 포함해야 한다.
- 최초 설치는 SSD 정상·BMO 정지 상태에서 한다. 이미 설치한 경우 업데이트 전 `disarm`이 필요하다.
- `/usr/bin/python3`, `/usr/bin/docker`, `/usr/bin/systemctl`이 필요하다. 설치기는 패키지를 자동 설치하지 않는다.
- 기존 BMO의 부팅 자동 실행은 이 도구가 바꾸지 않는다. 자동 실행이 발견되면 재부팅 복구가 중지될 수 있으므로 Pi 담당이 먼저 확인한다.
- **SSD가 없어도 OS가 정상 부팅을 마칠 수 있어야 한다.** Pi 담당은 기존 fstab/mount unit이 SSD 실패 시 emergency mode로 빠지지 않는지 읽기 전용으로 확인한다. 이 조건이 충족되지 않으면 guard timer가 실행되기 전에 부팅이 멈출 수 있다. 설치기는 fstab를 수정하지 않는다.
- 기존 검사처럼 사용자 UID의 Python/Node/Chromium/오디오 앱도 보수적으로 BMO 가능성으로 취급한다. 클라우드 웹 화면은 다른 PC/휴대폰에서 확인한다.

## Pi에 적용할 정확한 명령

먼저 이 변경이 포함된 NEXUS checkout으로 이동한다. 기존 작업 파일이 있다면 강제 checkout/reset하지 않는다.
현재 저장소가 다른 작업에 사용 중이면 별도 worktree를 만들 수 있다.

```bash
cd ~/Downloads/BGH_BMO_NEXUS
git fetch origin feature/ssd-recovery-ntfy
git worktree add ../BGH_BMO_NEXUS_RECOVERY origin/feature/ssd-recovery-ntfy
sudo install -o root -g root -m 600 deploy/private-cloud/.env ../BGH_BMO_NEXUS_RECOVERY/deploy/private-cloud/.env
cd ../BGH_BMO_NEXUS_RECOVERY
```

위 디렉터리가 이미 있으면 다른 이름을 사용하거나 기존 worktree의 상태를 확인한다. 비공개 `.env`는 Git에 추가하지 않는다.

설치 내용을 보기만 하는 명령:

```bash
python3 -E -s -B scripts/install_ssd_guard.py
```

BMO를 기존 방법으로 정상 종료한 다음 설치:

```bash
sudo python3 -E -s -B scripts/install_ssd_guard.py --apply
sudo ssd-guard status
systemctl status nexus-ssd-guard.timer --no-pager
```

정상 결과는 `INSTALLED, DISARMED`와 `phase: DISARMED`, `health: healthy`다. 설치만으로 재부팅하거나 알림을 전송하지 않는다.

| 설치 위치 | 내용 |
|---|---|
| `/opt/bmo-nexus-ssd-guard/` | root 소유 감시 코드와 기존 BMO 정지 검사 |
| `/etc/bmo-nexus/ssd-guard.json` | 0600 비공개 SSD 식별 정보, ntfy 설정 |
| `/var/lib/bmo-nexus/ssd-guard/state.json` | 0600 SD 재시도 상태·outbox |
| `/usr/local/sbin/ssd-guard` | 관리 명령 |
| `/etc/systemd/system/nexus-ssd-guard.{service,timer}` | 재부팅 후에도 실행되는 감시 타이머 |

이 위치들이 OS SD와 같은 장치에 있는지 검사한다. 서버 데이터의 SD 대체 저장은 하지 않는다.

## ntfy 휴대폰 연결

1. 휴대폰에 ntfy 앱을 설치한다. 기본 서버는 `https://ntfy.sh`다.
2. Pi에서 아래 명령으로 임의 생성된 구독 topic을 **로컬에서만** 확인한다.

```bash
sudo python3 -c 'import json; print(json.load(open("/etc/bmo-nexus/ssd-guard.json"))["ntfy_topic"])'
```

3. 앱에서 같은 서버와 topic을 구독하고 알림 권한을 켠다.
4. 아래 테스트 알림이 실제 휴대폰에 도착하는지 확인한다.

```bash
sudo ssd-guard test-notify
```

`NTFY TEST ACCEPTED`는 ntfy API가 수락했다는 뜻이다. 휴대폰 수신은 직접 확인한다.
기본 topic은 128비트 난수로 만든다. 공개 ntfy의 topic 이름을 아는 사람은 접근할 수 있으므로 비밀로 관리한다. 알림에는 UUID·시리얼·IP·토큰·원본 로그를 넣지 않는다.
접근 제어가 필요한 경우 별도 보호 topic의 `ntfy_url`, `ntfy_topic`, `ntfy_token`을 `sudoedit /etc/bmo-nexus/ssd-guard.json`으로 설정한다. HTTPS 기본 URL만 지원하고 토큰은 헤더로 전송한다. 설정 변경 후 테스트를 다시 해야 arm할 수 있다.
SSD와 함께 중단될 같은 Pi 위의 ntfy 서버를 수신 대상으로 삼지 않는다. 외부 ntfy 또는 독립 기기를 사용한다.

## 서버 운용 시작과 종료

서버 모드를 선택할 때 BMO를 종료하고:

```bash
sudo ssd-guard arm --confirm-bmo-stopped
sudo bash scripts/cloud-session.sh --confirm-bmo-stopped
```

Web Lab 또는 미러 작업을 사용할 때도 서버 세션 시작 전에 `arm`을 한 번 실행한다. guard는 기존 모드 제어기를 대신하지 않는다. 자동 모드 연동은 아직 없으며, `arm`은 서버 모드에 있겠다는 명시적 운영 선택이다.

서버를 끝낼 때는 클라우드 세션 Ctrl+C로 `CLOUD SESSION OFF` 확인, Web Lab/백업 작업도 종료 확인 후:

```bash
sudo ssd-guard disarm
```

그다음 BMO를 기존 방법으로 실행한다. `disarm` 자체는 서버를 정지시키지 않고, 이미 OS에 전달된 재부팅을 취소하지도 않는다.

재부팅 후 감시와 진행 중 복구는 자동으로 이어진다. **클라우드·Web Lab·공개 터널·BMO는 이 도구가 자동 시작하지 않는다.** 기존 `restart: "no"`를 유지한다. SSD 복구 후 앱을 다시 올리려면 상태를 확인하고 기존 수동 시작 명령을 실행한다. 기존 미러 작업은 새 명령으로 재실행한다.

## 5회 실패 알림을 받은 뒤

Tailscale SSH로 Pi에 접속해:

```bash
sudo ssd-guard status
sudo ssd-guard retry-five --incident STATUS에서_확인한_ID --confirm-bmo-stopped
```

상태의 실제 `incident` 값을 넣는다. 승인 직후 재부팅하는 것이 아니라 다음 관찰들에서 다시 장애를 확인한 뒤 최대 5회를 시도한다. `total_attempts`는 5 → 10처럼 누적된다. topic 알림에 답장하거나 공개 HTTP 버튼을 누르는 방식은 아니다. 인증된 SSH가 승인 경로다.

다른 이유로 `BLOCKED`라면 원인을 해결한 뒤 `resume`을 사용한다. 정상 SSD와 BMO 정지를 요구하며 횟수를 지우지 않는다. 실패한 재부팅 요청과 같은 boot ID이면 먼저 수동 점검/재부팅이 필요하다.

```bash
sudo ssd-guard resume --incident STATUS에서_확인한_ID --confirm-bmo-stopped
```

## 한계와 검증

- 이 기능은 OS와 SD, 네트워크가 작동할 때 SSD 장애를 복구한다. **OS 전체 멈춤·SD 고장·전원 단절에는 자체 알림/재부팅을 보장할 수 없다.** 별도 하드웨어 watchdog/외부 heartbeat는 후속 작업이다.
- 정상 종료가 SSD I/O 대기로 멈출 수 있다. 강제 재부팅을 몰래 추가하지 않는다. 이 경우 원격 접근 또는 전원 점검이 필요하다.
- 카운터는 재부팅 *요청 전에 예약*하므로 저장 직후 프로세스가 죽거나 요청이 실패하면 실제 완료한 재부팅보다 보수적으로 많이 셀 수 있다. 예산 초과를 피하는 설계다.
- outbox는 전달 성공 응답 후 제거한다. 전송 성공 직후 전원 단절/응답 유실 시 같은 알림이 재전송될 수 있다.
- 상태 파일 자체가 손상되면 자동 재부팅 없이 오류 로그를 남긴다. 이 경우 durable outbox를 신뢰할 수 없어 ntfy 알림을 보장하지 않는다.
- 공유 토큰이나 topic 수신 자체로 원격 명령을 실행하지 않는다. 외부 HTTP 수신 포트가 없다.
- 이 도구의 BMO 프로세스 검사는 기존 보수적 검사이며, 별도 프로세스의 동시 시작을 원천 봉쇄하는 live mode ownership 계약은 아직 없다. 마지막 재검사와 reboot 사이의 경쟁 가능성도 그 계약으로 해결해야 한다.

오프라인 검증:

```bash
python3 -B -m unittest discover -s tests -v
```

실제 장애를 만들려고 SSD 전원을 뽑거나 sysfs 값을 조작하지 않는다. 최초 실기 수용은 DISARMED 설치 → 정상 status → ntfy 휴대폰 수신 → arm/disarm → 기존 서버 기능 회귀 확인 순서다. 실제 5회 재부팅 장애 시험은 중요 데이터의 독립 백업과 BMO 부팅 정책 확인 후 별도 유지보수 시간에 수행한다.

## 롤백

```bash
sudo ssd-guard disarm
sudo systemctl disable --now nexus-ssd-guard.timer
sudo systemctl stop nexus-ssd-guard.service
```

SD 상태·비공개 설정·코드는 진단을 위해 남긴다. 자동 재부팅 요청이 이미 전달됐다면 이 명령으로 취소되는 것은 아니다. SSD 데이터·기존 서버·BMO·Tailscale/SSH/fan 설정은 삭제하지 않는다. 상태 파일을 지워 재시도 횟수를 초기화하지 않는다.

## 공식 근거 (2026-11-05 확인)

- [systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html): 부팅 유예와 반복 검사. 재부팅 예산은 timer의 Persistent가 아니라 별도 상태 파일이 담당한다.
- [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html): 정상 reboot 요청. 강제 재부팅 옵션은 사용하지 않는다.
- [ntfy publish](https://docs.ntfy.sh/publish/): HTTPS JSON 전송, 우선순위, 인증, topic 관리.
- [Docker restart policy](https://docs.docker.com/engine/containers/start-containers-automatically/): 기존 `restart: "no"` 유지.
- [Raspberry Pi hardware](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html): NVMe/전원/하드웨어 점검의 별도 근거. 추가 5V 후 일주일 무재발은 소유자 관찰이며 원인 확정 자료가 아니다.
