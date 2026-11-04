# NEXUS 재개 계획과 이번 결과

기준: 2026-11-05. 기존 기획서, 현재 main `ca7686f`, 기존 실기 성공 대화 기록과 이번 소유자 보고를 구분해 정리했다. 이번 개발 환경은 Pi가 아니다.

## 만들기로 했던 것

**BMO를 쓰지 않을 때 같은 Raspberry Pi를 개인 클라우드·GitHub 백업·웹 실험 서버로 사용한다.**
로봇과 서버는 교대로 사용하고, 관리는 Tailscale, 공개할 실험 사이트만 추후 Cloudflare 경로를 사용한다. 도메인 구입은 보류한다. 포트폴리오는 로봇이 꺼져도 볼 수 있게 별도 호스팅을 유지하고, 웹 UI 제작은 별도 작업이다.

| 기능 | 근거 있는 현재 상태 | 이번 결과/다음 행동 |
|---|---|---|
| 개인 파일 클라우드 SFTPGo | 소유자 보고: 파일 서비스·Tailscale 원격접속 성공. 최신 session wrapper 전체 검증과 구분 | 기존 수동 세션 유지, 복구 설치 후 업/다운로드 회귀 확인 |
| NEXUS/BMO GitHub 미러 | 소유자 보고: 두 저장소 백업 성공, BMO 객체·복원 검증 성공 | 같은 SSD 외의 독립 백업 추가가 다음 핵심 |
| SSD 안정화 | 소유자 보고: 5V 추가 후 약 일주일간 dead 미관찰 | 원인 확정 아님. 이번 5회 복구·ntfy 기능 적용 후 관찰 |
| SSD 자동 복구 | 이번 코드 구현, 오프라인 테스트. Pi 설치 NOT_RUN | [설치·알림·롤백 안내](16_SSD_RECOVERY_KR.md)로 적용 |
| 상태 확인 | 기존 개별 status에 더해 이번 통합 읽기 전용 JSON 도구 추가 | `sudo python3 -B scripts/nexus_status.py` |
| Web Lab | PR #18/main에 두 localhost 사이트 구현, 실제 Pi 검증은 확인 못 함 | A/B 페이지와 종료 후 포트 닫힘 확인 |
| ROBOT/SERVER 자동 전환 | Mock 상태기계·계약 문서 구현, live ownership/ready/cleanup 검증 부족 | BMO 담당에게 정확한 부팅/종료 계약 인수 후 연결 |
| 재부팅 후 서버 자동 재개 | 이번 SSD guard에는 미포함 | BMO autostart와 서버 소유권 계약 확인 후 별도 구현 |
| 공개 사이트/도메인 | 도메인 보류, 공개 검증 미완료 | 지금은 사설망/localhost 운영. 공개 사이트 필요 시 재개 |
| Gallery·Notes·Drop | 확장 아이디어 | 운영 안정화 뒤 별도 앱 제작 |

## 이번 구현물

- `scripts/ssd_guard_state.py`: 3회 장애 확인, 5회 재부팅 예산, boot ID 중복 방지, 30분 안정화, 추가 배치 승인.
- `scripts/ssd_guard.py`: SD의 원자적 상태 저장/잠금, sysfs·mountinfo 점검, 기존 BMO 실행 차단 검사, 정상 재부팅, ntfy outbox.
- `scripts/install_ssd_guard.py`, `deploy/ssd-guard/`: SD 설치, 보존형 업데이트, DISARMED 기본값, systemd timer.
- `scripts/nexus_status.py`: CPU 온도·메모리·load·uptime·SSD guard·두 서버 프로젝트 실행 수를 JSON으로 조회. 설정/UUID/topic/토큰 출력 없음.
- `tests/test_ssd_guard.py`: 실제 재부팅·Docker·알림을 실행하지 않는 회귀 테스트.

Status 도구의 `unknown`은 실패를 성공으로 숨기지 않는 값이다. 실행 수만 보여주며 HTTP 건강/백업 최신성을 증명하지 않는다. 파일 접근이 멈춘 SSD를 건드리지 않도록 용량·미러 디렉터리 재귀 조회는 넣지 않았다.

## 우선순위와 수용 기준

| 순서 | 작업 | 완료 판단 | 남은 입력/실기 작업 |
|---|---|---|---|
| 1 | guard + ntfy 실제 설치 | 정상 SSD 감지, 휴대폰 알림, arm/disarm, 재부팅 후 상태 유지 | Pi에서 설치 및 ntfy 구독. 실제 적용 증거 필요 |
| 2 | Web Lab 실제 검증 | 서로 다른 A/B 응답, down 후 두 포트 닫힘, 기존 데이터 보존 | 기존 up/status/down 명령 실행 |
| 3 | 독립 백업 | SSD와 다른 장치/기기에 암호화 사본, 임의 파일 복원 대조 | 외장 디스크 또는 다른 기기 경로·용량·접속 정보 결정 |
| 4 | live mode 계약 | ROBOT/SERVER 상호 배제, 시작 준비/종료 정리 ACK, 부팅 선택 검증 | BMO 담당의 실제 런처·정리·자동 시작 정보 |
| 5 | 서버 자동 재개/정기 백업 | SSD 안정 확인 후 서버 전용 앱만 재개, ROBOT 모드 백업 금지 | 4번 완료 뒤 구현·Pi 왕복 테스트 |
| 6 | 외부 heartbeat/정교한 경보 | Pi 전체 먹통과 인터넷 장애를 독립 기기가 감지 | 별도 상시 기기/외부 서비스 선택 |

외부 백업 저장소를 임의로 같은 SSD에 만들어 완료라고 하지 않는다. 외부 목적지가 정해지면 기존 세대 미러와 개인 파일을 restic 스냅샷으로 보관하고 별도 경로에 복원해 비교하는 작업으로 이어간다. 자격증명·미커밋 BMO 데이터는 공개 Git mirror에 포함되지 않으므로 별도 암호화 백업 범위에 넣는다. SFTPGo SQLite 상태는 앱이 정지된 일관된 시점 또는 검증된 애플리케이션 백업 절차로 복사한다.

## Pi 담당 Codex에게 넘길 작업

이 브랜치와 `docs/16_SSD_RECOVERY_KR.md`를 읽고 현재 Pi 상태를 읽기 전용으로 확인한다. BMO 앱의 자동 실행 여부, SD root, SSD live/UUID/mount, SSD 실패 시에도 OS 부팅이 가능한 fstab/mount 정책, 기존 NEXUS .env 권한을 확인한다. 변경된 코드/오프라인 테스트와 실기 성공을 구분한다.

사용자가 요청한 최대 5회 재부팅과 ntfy 알림을 적용한다. guard는 DISARMED로 설치하고, 사용자 휴대폰 구독과 test-notify 수신을 확인한 뒤 SERVER 세션에서 arm한다. 로봇 원본·부팅 설정·SSH/Tailscale/fan은 임의 수정하지 않는다. 실제 SSD 전원을 뽑는 장애 시험은 하지 않는다.

적용 후 guard status, timer 상태, 휴대폰 수신 여부, 기존 cloud/Web Lab 회귀 결과를 비식별 요약한다. 실패하면 문서의 disarm/disable 절차를 사용하고 재시도 상태를 지우지 않는다. 네트워크/상태 파일/장치 관찰 중 어떤 단계가 실패했는지 알려준다.

## 조사 결과가 구현에 반영된 부분

| 공식 자료 | 적용 판단 |
|---|---|
| [systemd timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html) | OnBootSec 180초 + OnUnitInactiveSec 30초. 예산은 별도 영속 상태로 관리 |
| [ntfy publish/auth](https://docs.ntfy.sh/publish/) | 외부 HTTPS JSON 알림, 재전송 outbox, 토큰 헤더, 공개 제어 API 없음 |
| [Docker restart](https://docs.docker.com/engine/containers/start-containers-automatically/) | 현재 `restart: no` 보존. 자동 복구가 BMO와 서버를 동시에 띄우지 않음 |
| [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) | 기존 사설 관리 경로 유지. 추가 5회 승인은 인증된 SSH로 수행 |
| [restic snapshot](https://restic.readthedocs.io/en/stable/040_backup.html) · [restore](https://restic.readthedocs.io/en/stable/050_restore.html) | 외부 사본과 실제 복원 검증을 다음 단계로 분리 |

알림/카운터 구현 성공은 전원 원인 해결, 장시간 실기 안정성, 데이터 무결성, 전체 mode controller 완성을 뜻하지 않는다.
