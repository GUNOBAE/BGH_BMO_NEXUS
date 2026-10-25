# Phase 3 — 오프라인 Mock Mode Controller

이 문서는 [Issue #7](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/7)의
**메모리 안에서만 동작하는 모드 전환 모의 구현**을 설명한다.
[PR #6](https://github.com/GUNOBAE/BGH_BMO_NEXUS/pull/6)의
[수명주기 계약](13_BMO_LIFECYCLE_CONTRACT_KR.md)과
[계약 검토 댓글](https://github.com/GUNOBAE/BGH_BMO_NEXUS/pull/6#issuecomment-6400800277)을
이어받는다. 실제 BMO adapter·종료 완료 ACK·외부 실행 방지 정책을 구현하거나 검증한 것이 아니다.

## 1. 완료 범위와 실행 경계

`nexus_mock`의 입력은 테스트가 직접 제공하는 가상 관측과 요청이다. 출력은 메모리 안의
상태·고정 오류 사유·가짜 실행/정리 요청 기록이다. 시작 또는 종료 기록이 추가되어도
실제 Python/BMO/Chromium/서버 프로세스는 실행하거나 종료하지 않는다.

| 대상 | 이번 구현이 확인하는 것 | 실제 환경 상태 |
|---|---|---|
| 시계·deadline | 수동으로 진행시키는 `FakeClock`으로 응답 지연과 증거 만료 판정 | 실제 종료 시간 상한은 미확인 |
| supervisor·소유권 | 가짜 run identity, root 종료·회수, 외부 실행 충돌 | 실제 프로세스 조회·signal·자동 입양 없음 |
| 정리 완료 | run에 묶인 자원 보고와 root 종료가 모두 있어야 다음 시작 | 기존 BMO에 새 ACK가 있다는 뜻이 아님 |
| 저장소 | 가짜 mount·identity·쓰기 가능·건강 증거를 gate로 사용 | SSD 읽기/쓰기·mount·검사 모두 NOT_RUN |
| 건강·공개 경로 | 가짜 health와 CLOSED/OPEN/UNKNOWN 관측, 별도 공개 허가 | HTTP 조회·포트 bind·터널 API 모두 없음 |
| 배포·하드웨어 | 해당 동작 경로 없음 | 실제 Pi 전환·장치 반환 모두 NOT_RUN |

호스트 설정, BMO 원본, 승인 R10/Fish/Piper, 펌웨어, 서비스, API 키와 개인 데이터는 이
구현의 입력 대상이 아니다. 실제 시작 argv·경로·서비스 이름을 추측해 실행하는 fallback도 없다.
보호 서비스나 외부 BMO는 가짜 정리 요청의 대상으로도 선택하지 않는다.

## 2. 상태, 목표와 관측

목표 모드와 실제 관측 상태는 별도로 유지한다. SERVER를 요청했다고 곧바로 SERVER_READY가
되지는 않는다. 전환 ID는 새 전환/복구 때 증가하며 과거 응답을 현재 작업의 성공으로 재사용하지 않는다.

| 상태 | 의미 |
|---|---|
| `UNKNOWN` | 부팅/제어기 재생성 후 상태. 양쪽이 정지했다는 증거 없음 |
| `MAINTENANCE` | 양쪽 workload와 필수 자원이 정리됐고 공개 CLOSED가 확인됨 |
| `ROBOT_STARTING` | ROBOT 준비/시작 전환 중. 공개·사전 gate 확인 및 동일 run의 ready 확인 전 |
| `ROBOT_READY` | 같은 run의 준비·소유권·비공개 조건이 유효함 |
| `ROBOT_STOPPING` | 종료 요청 중. 수락만으로 완료가 되지 않음 |
| `SERVER_STARTING` | SERVER 준비/시작 전환 중. SSD 등 gate 확인 및 동일 run의 ready 확인 전 |
| `SERVER_READY` | 같은 run의 건강·저장소·상호 배제 조건이 유효함. 공개는 별도 허가 |
| `SERVER_STOPPING` | SERVER의 모든 등록 자원과 root 정리를 기다림 |
| `ERROR` | 실패 또는 증거 불명. workload가 남아 있을 수 있으므로 정비 상태가 아님 |

새 제어기는 저장했던 READY나 공개 허가를 복원하지 않는다. 초기 UNKNOWN에서 양쪽 부재,
자원 해제, 소유권, 공개 CLOSED를 **명시적인 가짜 증거로** 확인해야 MAINTENANCE로 갈 수 있다.
"목록이 없으니 정지했을 것"이라는 추정은 통과 사유가 아니다.

같은 `FakeWorld`에서 제어기를 새로 만들면 `new_epoch()`가 더 큰 세대를 할당한다.
같거나 작은 세대를 명시해 재사용하면 거부한다. 전환 ID도 같은 세계의 공유 카운터에서
계속 증가하며, 새 세대가 생기면 이전 제어기는 실행/복구를 진행할 수 없다.
이전 세대의 token은 성공 응답으로 사용할 수 없다. 이 값은 메모리에만 있다. `FakeWorld`까지
새로 만들면 과거 세대를 기억하지 못하므로, 실제 crash/reboot용 영속 세대·인증·복구 저장소를
구현했다는 뜻이 아니다. 현장 연동 전에 별도 설계·검증해야 한다.

## 3. 이벤트 처리 순서와 gate

비동기 작업을 실제로 실행하는 대신, 호출자가 가짜 이벤트를 한 개씩 전달한다. 가상 시간을
진행시킨 뒤에는 `tick()`을 호출한다. 실제 sleep·백그라운드 감시·시스템 event loop는 없다.
관측 변경도 직접 주입한 뒤 `tick()` 또는 다음 이벤트 처리로 판정하므로, 이 Mock은 실제
장애를 스스로 찾아내는 상시 감시기가 아니다.

| 코드 | 역할 |
|---|---|
| `models.py` | 상태/모드/단계, `Token`, 가짜 identity·정리·SSD·공개·ACK 자료형 |
| `fakes.py` | `FakeClock`, 직접 갱신하는 `FakeWorld` 관측과 메모리 명령 기록 |
| `controller.py` | `ModeController` gate·이벤트 처리·deadline·오류 판정 |
| `reconcile()` | UNKNOWN/ERROR에서 새 증거로 양쪽 정리를 확인. 자동 재시작 없음 |
| `request(target, command_id)` | 사용자 요청을 가상 전환으로 등록. 실제 인증/권한 검사 구현이 아님 |
| `handle(Ack(...))` | 현재 `token`의 세대·전환·숫자 단계·run, 단계 내 연속 순번과 유효 시점을 확인 |
| `operator_public_approval()` | private SERVER_READY에서 별도의 가짜 공개 허가·요청 기록 |

`Policy(timeout=10, evidence_ttl=20)`의 기본 단위는 **가상 tick**이다. 실제 Pi의 초 단위
시간 보장이 아니다. `FakeWorld`의 소유권/profile/기본 자원 검증 flag도 테스트가 제공하는
가정이며, 실제 인증·GUI·interpreter·장치 검사 구현으로 해석하지 않는다.

정상 전환은 다음 순서로만 진행한다.

1. 목표·실행 승인·단일 전환 소유권을 확인하고 새 전환 ID를 발급한다.
2. 공개 허가를 철회하고 공개 경로가 실제로 `CLOSED`인지 가짜 관측으로 확인한다.
3. 현재 **소유한** workload만 종료 대상으로 잡는다.
4. 종료 요청 수락과 별도로, 필수 자원 정리 보고 및 같은 root의 종료·회수를 기다린다.
5. 대상의 사전 조건을 다시 검사한다. SERVER에는 실제 mount를 뜻하는 가짜 SSD 증거도 필요하다.
6. 대상 시작 기록을 한 번만 추가하고 동일 run의 ready/health를 확인한다.
7. SERVER_READY여도 기본 공개 허가는 없다. 별도 명시적 승인과 유효한 모든 증거가 있어야
   가짜 공개 요청이 가능하다.

처리 단계는 `CLOSED → STOP_ACCEPTED → CLEANUP → FINAL_CLOSED → START_ACCEPTED → READY`다.
현재 workload가 없으면 종료 단계는 생략한다. `CLEANUP`에서 `ROOT_EXITED`와
`CLEANUP_COMPLETE`는 어느 순서로 와도 둘 다 필요하다. 정리가 끝난 뒤 `FINAL_CLOSED`에서
공개 CLOSED를 다시 확인한 다음에만 상대 workload를 시작한다. 각 단계에는 새 token과
deadline이 생긴다. 시작 수락 뒤 별도 ready가 있어야 관측 상태가 READY로 바뀐다.
ACK 순번은 단계마다 1부터 시작하여 1씩 증가해야 한다. 단계 발급 이전이나 미래 시각의
ACK, 순번 건너뛰기·재전송은 모두 ERROR다. CLOSED 응답에는 close 요청 이후의 관측이 필요하다.

가짜 supervisor의 workload 기록에도 상호 배제를 적용한다. 상태 이름만 바꾸어 양쪽 실행을
숨기지 않는다. NEXUS 내부의 단일 전환 소유권은 외부 수동 BMO 실행을 예방하는 실제 lock이 아니다.
Mock에서 주입한 승인된 소유권 정책은 **시험 가정**이며 현장 정책이 구현되었다는 증거가 아니다.

### 공개 경로가 설치되지 않은 상황

공개 요청 OFF와 관측 CLOSED를 구분한다. close 요청을 기록한 것만으로 CLOSED가 되지 않는다.
터널 프로그램 미설치·미설정 또는 관측 누락만으로도 CLOSED를 만들지 않는다.

테스트에서 "공개 route 없음"을 증명할 때는 **확인한 경계 전체를 포함하는 검증된 빈 route
목록**을 명시한다. 그 범위가 불명확하거나 조회가 실패하면 UNKNOWN이며 전환은 차단된다.
이 증거는 승인된 NEXUS 공개 경로만 다룬다. 시스템 전체의 인터넷 무노출을 보증하거나
SSH/VNC/Tailscale·방화벽·공유기 설정을 수정할 권한이 아니다.

### SSD와 ready

디렉터리 존재, 과거 mount 성공 또는 capacity 복귀 보고만으로 SERVER를 허용하지 않는다.
가짜 SSD 증거는 기대 저장소 identity와 실제 mount 의미, 쓰기 가능/건강·승인 상태 및 유효성을
함께 판정한다. 시작 직전의 교체·분리와 READY 이후 연결 상실도 실패로 처리한다.
SD fallback, 자동 mount·포맷·복구 동작은 없다.

ready/health도 PID 존재·HTTP200·로딩 100%와 구분한다. 이 Mock의 `ready`는 동일 run/profile의
로컬 준비 증언이라는 **가상 계약 메시지**다. 마이크·카메라·서보·GPT/Fish를 실제로 호출해
건강을 시험하지 않으며, 유료 API 성공이나 실물 합격을 뜻하지 않는다.

## 4. 종료 수락과 정리 완료

`stop_accepted`는 종료를 시작하겠다는 가상 응답이다. 다음 모드를 시작할 근거가 아니다.
`cleanup_complete`도 실제 BMO가 제공하는 endpoint가 아니라, 향후 계약을 검증하기 위해
테스트가 만들어 전달하는 가상 응답이다. 발행 주체·인증·전송 채널·실제 시간 상한은 미확인이다.

다음 두 조건이 **동시에** 있어야 소유 workload의 정리를 인정한다.

- 같은 run identity의 root가 종료됐고 supervisor에서 회수됐다는 증거.
- 모든 필수 자원에 대한 동일 run의 `released` 또는 검증된 `not_acquired` 증거.

`released`는 취득했던 자원의 반환이 확인됐다는 뜻이다. `not_acquired`는 검증된 profile과
해당 run의 기록에 의해 처음부터 취득하지 않았다는 뜻이며, 단순 설정 flag나 미발견과 다르다.
보고 누락·close 실패·살아 있는 취소 worker·미확인 child는 `unknown`이다.
ACK만 있고 root가 살아 있거나 root가 없어졌으나 child/장치가 unknown인 경우 모두 실패한다.

host USB를 사용하지 않았다는 증거는 RP2040·서보가 멈췄다는 증거가 아니다. MCU 안전 상태는
별도 합의한 정책/증거로 남겨야 하며 자동 reset이나 servo 이동으로 gate를 해결하지 않는다.

## 5. 실패, 오래된 응답과 복구

Issue #7은 PR #6의 문서 단계보다 엄격하게 **오래되거나 재전송된 ACK도 ERROR**로 정한다.
과거 계약 문서의 "현재 전환을 진행시키지 않는다"는 설명을 성공/무시 허용으로 해석하지 않는다.
run·전환·단계·세대·identity와 유효 시점이 현재 기대값과 일치해야 한다.
동일 사용자 요청을 다시 보낸 경우의 멱등 처리와 ACK 재전송은 별개다.

| 가짜 실패 입력 | 요구되는 결과 |
|---|---|
| 시작/종료/공개 확인 deadline 초과 | ERROR, 공개 허가 철회, 대상 추가 시작 없음 |
| stale/replayed/wrong-run/wrong-step ACK 또는 PID 재사용 | ERROR, 다른 run 입양/종료 없음 |
| 필수 자원 unknown·누락, worker 잔존 | ERROR, 상대 모드 시작 없음 |
| 공개 UNKNOWN·CLOSED 증거 만료·예상 밖 OPEN | ERROR, close 요청 기록과 관측값을 구분 |
| SSD 미연결·잘못된 mount·read-only·교체/분리 | SERVER 시작 차단 또는 ERROR, SD 쓰기 대체 없음 |
| 외부 BMO 실행·미확인 소유권/재시작 정책 | ERROR, 공개 허가 철회, 외부 BMO 정리 요청 없음 |
| READY 이후 health/증거 상실 | ERROR, 상대 모드 자동 시작 없음 |
| 반대 목표의 동시 전환 요청 | conflict를 ERROR로 기록, 두 전환 병행 없음 |

"target start zero"는 시작 gate 앞에서 실패하면 대상 시작 기록이 **0건**이라는 의미다.
이미 READY였던 실행 중 장애는 과거 정상 시작 기록을 지우지 않고, 장애 이후 **추가 시작 0건**을
검증한다. 공개 허가 철회 역시 실제 CLOSED 확인으로 둔갑시키지 않는다.

ERROR에서 이전 모드로 자동 복귀하거나 상대 모드를 실행하지 않는다. 명시적 `reconcile()`은
세대와 전환 ID를 증가시키고 UNKNOWN부터 남은 workload/자원과 공개 상태를 재판정한다.
해당 호출 안에서 모든 증거가 유효하면 MAINTENANCE가 될 수 있지만, workload는 시작하지 않는다.
Mock 오류 처리가 실물 정지나 외부 재시작 예방을 보장하지 않는다.

가짜 시작이 기록되면 이전의 baseline 자원 정리 증거를 무효화한다. 처음 ERROR가 발생할 때도
`baseline_resources_verified`와 `inventory_verified`를 무효화하므로 이전에 비어 있었다는
증거를 재활용해 복구할 수 없다. 테스트가 `observe_inventory()`와
`observe_baseline_resources()`로 **새 전수 관측/자원 확인을 명시**하고, 남은 root가 없고
소유권·공개 CLOSED 증거까지 유효해야 `reconcile()`이 통과한다. 이 메서드는 자원을 스스로
정리하는 기능이 아니라 테스트 작성자가 새 가짜 사실을 제공하는 인터페이스다.

오류 처리는 공개 허가를 철회하고 메모리에 `close_public` 요청만 기록한다. 현재 소유한 root도
자동 종료/정리하지 않으며, ERROR에 남은 자원은 미확정으로 유지한다. 계약 문서의 현장 오류
대응에 나오는 **확인된 자기 소유 SERVER quiesce/escalation은 이번 최소 Mock에 미구현**이다.
오류 정리 정책을 완성했거나 실제 공개 경로/서버를 멈췄다고 보고하지 않는다.

## 6. 모의 테스트 실행과 결과 기록

저장소 루트에서 Python 표준 라이브러리만으로 실행한다. 패키지 설치, 서비스 접근, API 키가 필요 없다.

```bash
python3 -B -m unittest discover -s tests -v
git diff --check
```

첫 명령은 기존 점검 도구의 모의 회귀 검사와 새 상태기계 검사를 실행한다. `-B`는 bytecode
캐시 생성을 막는다. 기존 wrapper 검사는 임시 디렉터리의 비어 있거나 가짜인 PATH에서만
shell/Python을 실행한다. 실제 점검 바이너리를 호출하거나 Pi 현황을 조사하는 실행이 아니다.
두 번째 명령은 변경 파일의 공백 오류를 확인한다.

정상 왕복/정비 진입, 종료 수락만 있는 지연, cleanup/root 증거 분리, stale 응답, PID 재사용,
터널 UNKNOWN/미설정, SSD 실패, 외부 BMO 충돌, 재시작/재요청을 가상 입력으로 재현한다.
정확한 테스트 수·실행 결과·변경 SHA·CI 링크는 이 변경의 PR에 기록한다. 기존 NEXUS31개나
BMO 저장소의 테스트 수를 새 상태기계의 통과 수로 합산해 주장하지 않는다.

테스트가 PASS여도 실제 ROBOT↔SERVER, root/child 정리, 장치 반환, SSD 안정성, 공개 차단,
재로그인/재부팅 동작은 모두 **NOT_RUN**이다.

## 7. 변경을 되돌리는 방법과 다음 승인 경계

이번 변경에는 호스트에 되돌릴 설정이 없다. Mock 사용을 중단하거나, 별도 리뷰를 통해 이
NEXUS 소스·테스트·문서·CI 변경만 Git에서 되돌린다. BMO checkout 변경, 프로세스 종료,
백업 삭제, 서비스 restart는 rollback 절차에 포함되지 않는다. PR #6의 계약 문서는 보존한다.

실제 연동을 시작하려면 다음 증거와 별도 승인이 먼저 필요하다.

1. BMO 담당이 확정한 artifact/interpreter/GUI 사용자·환경 및 조용한 로컬 readiness profile.
2. 인증된 실제 ready/cleanup 계약, run/전환/단계 identity, root 회수 및 자원별 반환·시간 상한.
   실제 재시작을 가로지르는 세대의 영속성·재전송 방지와 오류 시 소유 workload 정리 정책도 필요하다.
3. 수동 시작·autostart까지 포함한 양쪽 공통 소유권/재시작 방지 정책. 사후 감지만으로 대체 불가.
4. SSD 데이터 보존·무결성·안정성·독립 백업/복원 근거와 현재 mount 검증 방식.
5. 서버 workload 목록·최소 권한·공개 경로 검증 범위·안전한 변경 명령과 rollback 검토.
6. 사용자 입회 하의 종료/재진입·장치 반환 시험. 보호 서비스와 MCU는 승인 범위 밖에서 조작 금지.

Mock PR 검토·병합과 실제 Pi 배포 승인은 서로 다른 단계다. 이 단계에서 main 자동 병합이나
실제 실행 명령/서비스 설치 파일을 제공하지 않는다.
