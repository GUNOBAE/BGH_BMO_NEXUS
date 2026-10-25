# Phase 2A — NVMe 전체 용량 0B 읽기 전용 진단

> [Issue #3](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/3)의 **원인 범위를 좁히기 위한 절차**다. SSD 원인·기존 데이터·안정성은 미확정이다. 이 문서를 작성하면서 실제 Pi에서 새 관측을 실행하지 않았다. 아래 절차를 작성한 것과 현장 진단을 완료한 것을 구분한다.

## 이미 확인된 사실과 아직 필요한 증거

[기존 Pi 기록의 후속 점검 결과](09_PI_BASELINE_KR.md#후속-점검-결과-2026-10-25)와 Issue #3에 보고된 2026-10-25 관측만 반영한다.

| 항목 | 기존 소유자 보고 | 해석의 한계 |
|---|---|---|
| OS 루트 `/` | microSD `/dev/mmcblk0p2`, ext4 | SD 부팅 사실과 NVMe 용량 문제는 별개다. 부팅 순서를 바꿔 0B를 해결할 근거가 없다. |
| NVMe 전체 장치 `nvme0n1` | **0 byte** | 커널이 사용 가능한 전체 용량을 정상 보고하지 못한 상태다. 빈 SSD라는 뜻은 아니다. |
| `/sys/class/block/nvme0n1/size` | **0 sectors** | 이 값의 단위는 512바이트다. 물리 매체의 실제 용량이나 데이터 유무를 직접 확인한 결과가 아니다. |
| 하위 파티션 `nvme0n1p1` | **1,000,203,091,968 byte** | 전체 장치와 불일치한다. 파티션 크기 표시만으로 데이터가 읽히거나 복구됐다고 판단하지 않는다. |
| 파일시스템·마운트 | FSTYPE 미검출, 확인된 직접 마운트 레코드 없음 | 데이터 부재나 모든 별칭·매핑의 마운트 부재를 증명하지 않는다. |
| 기존 후속 도구와 테스트 | PR #2 병합, 소유자가 Pi 실행 및 **31개 모의 테스트 통과** 보고 | 도구 동작 확인이며 SSD 정상 판정이 아니다. |
| 컨트롤러 state·오류 유형·유효 PCIe 설정 | **아직 보고 없음 / 미확인** | 이 문서로 추가 관측할 대상이다. 정상이나 고장으로 미리 채우지 않는다. |

`lsblk`는 sysfs와 udev 정보를 이용한다. 따라서 `lsblk`의 0B와 sysfs의 0 sectors는 서로 독립적인 물리 용량 검사 두 번이 아니다. 파티션의 큰 값이 이전 정보로 남았거나 조회 시점이 달랐을 가능성도 **가설**이며, 현재 증거로 확인된 원인은 아니다. [Debian trixie의 lsblk 설명](https://manpages.debian.org/trixie/util-linux/lsblk.8.en.html)

## 원인 가설별 의사결정표

아래 행은 가능한 조사 방향이다. 현재 증거만으로 가능성의 순위를 매기거나 부품 고장을 확정하지 않는다. 표의 “다음 확인”은 아래 읽기 전용 절차에 한정한다.

| 가설 | 이 가설을 검토할 관측 | 그 관측으로도 확정할 수 없는 것 | 다음 확인 / 분기 |
|---|---|---|---|
| 전원 공급 또는 SSD·어댑터 연결 경로의 불안정 | 같은 부팅에서 저전압, PCIe 링크 오류, NVMe timeout/reset 관련 메시지가 함께 관찰됨 | 특정 전원장치·케이블·HAT·SSD가 원인이라는 결론. 경고가 없다고 SSD 전원이 정상인 것도 아님 | 컨트롤러 상태와 메시지의 대략적 발생 시점을 함께 요약. 현재 하드웨어 모델·전원 구성을 소유자가 비공개로 확인하되 분리·재장착하지 않음. |
| Gen 3 설정과 링크 안정성 문제 | 설정 파일에 Gen 3 관련 줄이 있고 PCIe 오류가 관찰됨 | 설정 줄이 실제 적용됐는지, 실제 협상 속도, Gen 3가 이번 0B의 원인인지 | 설정은 읽기만 하고 유효 설정·속도는 미확인으로 남김. 속도 변경이나 재부팅을 해결책으로 실행하지 않음. |
| 컨트롤러 초기화·복구 또는 namespace 식별/용량 갱신 문제 | state가 `resetting`, `connecting`, `dead` 등으로 관찰되거나 controller/timeout 관련 메시지가 있음 | SSD 펌웨어·호스트 드라이버·연결 경로 중 어느 쪽 문제인지 | state와 용량 관측을 같은 조사 묶음으로 기록. `live`여도 용량 0B이면 문제를 해결된 것으로 표시하지 않음. |
| 커널의 namespace 처리·지원 조건 문제 | 컨트롤러가 관찰되지만 용량이 0이고 식별·메타데이터 관련 메시지가 있음 | 현재 커널의 특정 코드 경로가 실행됐는지, 기존 데이터가 손상됐는지 | 오류 유형과 현재 기록의 커널 버전을 함께 검토. 드라이버·커널·펌웨어 변경은 하지 않음. |
| 파티션 용량 정보의 잔존 또는 관측 시점 차이 | 전체 0B와 큰 파티션 값이 동시에 표시되거나 관측 사이 값이 달라짐 | 파티션 표·매체 용량·데이터가 정상이라는 결론 | 기존 도구의 바이트/섹터 값과 컨트롤러 상태를 대조. 재스캔·파티션 다시 읽기·복구 도구로 상태를 바꾸지 않음. |
| 장치 열거 또는 설정 경로의 불일치 | `/dev` 노드, sysfs 컨트롤러, PCIe 목록이 서로 다르게 관찰됨 | 물리적 부재, 비활성 설정, 고장 중 하나라는 결론 | 누락과 조회 실패를 구분. HAT 자동 감지·조건부 설정·include를 고려해 설정 미확인으로 남김. |
| 파일시스템 미탐지만으로 0B를 설명하려는 해석 | FSTYPE만 비어 있음 | 새 SSD, 빈 SSD, 포맷 필요라는 결론 | 파일시스템 유무와 장치 용량 보고를 별개로 취급. 쓰기·마운트 없이 위의 컨트롤러/링크 관측으로 돌아감. |

공식 Pi 문서는 외부 PCIe를 Gen 2 단일 레인으로 설명하고, Pi 5의 Gen 3 동작은 인증되지 않아 불안정할 수 있다고 안내한다. 이는 조사 배경이며 **현재 Pi의 속도나 원인을 측정한 결과는 아니다**. [Pi 5 PCIe 연결](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-connector-for-pcie), [Gen 3 주의사항](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#pcie-gen-3-0)

공식 문서는 저전압 경고와 불안정한 전원의 영향을 설명한다. 공식 M.2 HAT+의 연결·전원 규격도 참고할 수 있으나, 실제 사용 중인 어댑터가 그 제품인지는 미확인이다. 특정 HAT의 규격을 현재 SSD의 공급 전원 측정값처럼 사용하지 않는다. [전원 경고](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#power-supply-warnings), [공식 M.2 HAT+](https://www.raspberrypi.com/documentation/accessories/m2-hat-plus.html)

Linux v6.18에는 namespace 처리 과정에서 용량을 0으로 설정하는 여러 경로가 있다. 이는 0B가 “비어 있음”과 동의어가 아니라는 소스상의 배경이다. 현재 Pi의 downstream 커널에서 어느 경로가 실행됐는지는 확인하지 않았다. [Linux namespace 처리 소스](https://github.com/torvalds/linux/blob/v6.18/drivers/nvme/host/core.c#L2189-L2285)

## 소유자가 실행할 읽기 전용 순서

아래 명령은 Bash 기준이며, 일반 사용자로 Pi의 NEXUS 저장소에서 필요한 단계만 실행한다. **2단계 이후의 수동 명령은 기존 도구의 비식별화 필터를 거치지 않으므로 로컬 검토 전용**이다. 오류 출력도 공개하지 않는다. 없는 명령·권한 부족·조회 실패는 미확인으로 기록하고, 설치·권한 상승·재부팅으로 보완하지 않는다. 파일 저장 리디렉션이나 `tee`를 사용하지 않는다.

### 1. 기존 도구로 용량 불일치 재관측

```bash
bash scripts/inspect_pi_followup.sh
```

전체 장치와 파티션의 바이트 수, sysfs 섹터 수, 루트 파일시스템을 비교한다. 이 도구는 파일 저장이나 서비스 제어를 하지 않는다. 큰 파티션 값과 0B가 다시 함께 표시되면 “불일치 지속”으로 적는다. 값이 양수로 바뀌어도 데이터 무결성·안정성을 확인한 것은 아니다. 종료 코드 0 역시 정상 판정이 아니다. 명령별 의미와 출력 제한은 [후속 도구 안내서](11_FOLLOWUP_INSPECTION_KR.md)를 따른다.

### 2. 장치 노드와 블록 장치 관측 범위 대조

```bash
ls -l /dev/nvme* 2>&1
lsblk -b -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
```

첫 명령은 컨트롤러·namespace·파티션의 장치 노드가 보이는지 읽는다. 파일 이름이 존재하는 것은 정상 I/O나 용량 확인의 증거가 아니다. 두 번째 명령은 바이트 단위 용량과 마운트 표기를 대조한다. 경로·사용자 이름·장치 관련 정보가 포함될 수 있으므로 원문을 공유하지 않는다. 와일드카드에 맞는 노드가 없거나 읽기에 실패한 경우도 원인 미확인으로 기록한다. [공식 장치 탐지 확인 방법](https://www.raspberrypi.com/documentation/accessories/m2-hat-plus.html#start-your-raspberry-pi)

### 3. 커널이 보고하는 컨트롤러 state 읽기

```bash
found=0
for state_file in /sys/class/nvme/nvme*/state; do
  [ -e "$state_file" ] || continue
  found=1
  controller=${state_file%/state}
  controller=${controller##*/}
  if [ -r "$state_file" ]; then
    printf '%s: ' "$controller"
    cat "$state_file" 2>/dev/null || printf '%s\n' '읽기 실패: 미확인'
  else
    printf '%s: 읽기 권한 없음: 미확인\n' "$controller"
  fi
done
if [ "$found" -eq 0 ]; then
  printf '%s\n' 'state 항목 미보고 또는 접근 불가: 미확인'
fi
```

`state`는 커널이 노출하는 가상 파일이다. 위 코드는 그 내용만 읽으며 다른 sysfs 제어 파일에 값을 쓰지 않는다. Linux v6.18의 값에는 `new`, `live`, `resetting`, `connecting`, `deleting`, `deleting (no IO)`, `dead`가 있다. 현재 커널이 다른 값을 보여 주면 원문을 자동 공개하거나 정상 값으로 치환하지 말고 “지원되지 않은 상태 표기, 미확인”으로 요약한다. [Linux 컨트롤러 state 소스](https://github.com/torvalds/linux/blob/v6.18/drivers/nvme/host/sysfs.c#L388-L409)

`live`는 컨트롤러 상태의 한 값이다. namespace 용량·파일시스템·데이터 무결성 검사를 수행했다는 뜻은 아니다. 반대로 초기화·연결·종료 관련 값은 조회 시점의 상태이므로 한 번의 결과만으로 고장 부품을 특정하지 않는다. 이 해석은 위 소스의 조회 범위에 따른 추론이다. 기존 도구는 이 `state`를 자동 수집하지 않으므로 별도 관측 여부를 기록한다.

### 4. PCIe 설정 문자열과 열거 상태 확인 — 필요한 경우만

```bash
if [ -r /boot/firmware/config.txt ]; then
  grep -nE '^[[:space:]]*(dtparam[[:space:]]*=[[:space:]]*(pciex1|nvme)|dtoverlay[[:space:]]*=[^#]*pcie)' /boot/firmware/config.txt ||
    printf '%s\n' '일치 줄 없음 또는 조회 실패: 유효 설정 미확인'
else
  printf '%s\n' 'config.txt 읽기 불가: 유효 설정 미확인'
fi

if command -v lspci >/dev/null 2>&1; then
  (
    set -o pipefail
    lspci -nn | grep -Ei 'non-volatile|nvme|pci bridge'
  ) || printf '%s\n' 'PCIe 목록 조회 실패 또는 일치 줄 없음: 미확인'
else
  printf '%s\n' 'lspci 없음: PCIe 목록 미확인 (설치하지 않음)'
fi
```

`grep -nE`는 PCIe 관련 설정 문자열과 줄 번호만 찾는다. `dtparam=nvme` 별칭도 포함하지만 설정을 해석하거나 수정하지 않는다. HAT+ 자동 감지, 조건부 구간, include된 파일, 다른 설정 위치 때문에 **한 파일에서 줄을 찾지 못했다고 PCIe 비활성으로 확정할 수 없다**. 줄이 있어도 실제 적용 상태와 협상 속도는 미확인이다. 관련 include나 조건을 더 읽어야 한다면 소유자가 비공개로 검토하고 설정은 변경하지 않는다. [HAT+ 감지와 PCIe 설정](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-connector-for-pcie), [설정 파일과 include](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/config_txt/what_is_config_txt.adoc), [조건부 설정](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/config_txt/conditional.adoc)

`lspci -nn`은 이미 설치돼 있을 때만 PCI 장치의 종류·식별 정보를 조회한다. NVMe/bridge 항목의 관찰은 PCIe 열거의 단서이며, 정상 namespace 용량이나 실제 협상 속도를 확인하지 않는다. 결과가 비거나 명령이 실패하면 미확인이다. 식별 정보가 포함된 출력은 공개하지 않는다.

### 5. 선택 사항: 커널 메시지는 로컬에서 읽고 사람이 분류

```bash
(
  set -o pipefail
  journalctl -k -b --no-pager |
    grep -Ei 'nvme|pcie|aer|under.?voltage' |
    tail -n 60
) || printf '%s\n' '로그 조회 실패 또는 일치 줄 없음: 원인 미확인'
```

이번 부팅의 커널 메시지 중 관련 문자열이 있는 **마지막 60줄**만 표시한다. `pipefail`은 앞쪽 명령의 실패가 마지막 명령의 성공으로 가려지는 일을 줄인다. 권한·보관 범위와 문자열 필터의 한계가 있어 빈 결과가 “오류 없음”을 뜻하지는 않는다. 필터에 맞지 않는 메시지와 앞선 메시지는 누락될 수 있다. [기존 로그 검토 원칙](11_FOLLOWUP_INSPECTION_KR.md#선택-사항-커널-메시지는-소유자가-로컬에서만-검토)

소유자는 원문을 읽고 아래 유형의 **관찰 여부**만 분류한다. 단순 문자열 출현 횟수를 고장 횟수처럼 해석하지 않는다.

| 로컬에서 본 유형 | 공개 가능한 요약 | 해석의 한계 |
|---|---|---|
| timeout / I/O 관련 | “NVMe 요청 지연·실패 관련 메시지 관찰, 발생 시점은 부팅 초기/운영 중” | 요청 경로 문제의 단서이며 단독으로 SSD 고장 확정 불가 |
| reset / controller 관련 | “커널의 컨트롤러 복구 관련 메시지 관찰” | 커널이 한 동작의 기록이다. 사용자가 reset 명령을 실행하라는 뜻이 아님 |
| PCIe / AER / link 관련 | “PCIe 링크 관련 오류 유형 관찰” | 전원·연결·장치·드라이버 중 원인 미확정 |
| 저전압 관련 | “접근 가능한 로그에서 저전압 관련 경고 관찰” | NVMe 전원 상태의 직접 측정이 아니며, 경고 미관찰도 정상 보증이 아님 |
| 읽기 실패 / 관련 줄 없음 | “접근 범위 또는 필터 범위에서 미확인” | 오류가 없었다는 결론 불가 |

개수를 적는다면 “필터된 마지막 60줄 중 해당 유형을 포함한 줄 수”처럼 범위를 함께 적는다. 이것은 전체 부팅의 오류 개수가 아니다. 시점은 상대적으로 일반화하고, 원본 로그·스크린샷·주소·시리얼·UUID·토큰·개인 경로는 Git 또는 공개 Issue/PR에 붙이지 않는다. 문자열 필터는 비식별화 도구가 아니다.

## 데이터 보존과 다음 결정의 경계

전체 0B가 지속되거나 원인·원본 데이터가 미확인인 동안 SSD에 Docker/클라우드 데이터나 백업을 쓰지 않는다. 양수 용량으로 돌아와도 곧바로 다음 설치 단계로 넘어가지 않고 원본 데이터·안정성·백업 상태의 별도 확인을 기다린다.

이 단계에서는 포맷·파티션 변경·모든 새 마운트(읽기 전용 포함)·파일시스템 검사/복구·패키지 설치·업데이트·PCIe 속도/설정 변경·부팅 순서 변경·드라이버 재로딩·reset/rescan·sysfs 쓰기·재부팅·하드웨어 분리/재장착을 하지 않는다. BMO 코드·프로세스, SSH·Tailscale·VNC·네트워크·냉각팬을 변경하지 않는다. 추가 조치가 필요하면 데이터 보존 근거, 정확한 명령, 위험과 복구 경로를 정리한 별도 제안을 검토한다.

문서는 기존 읽기 전용 도구를 재사용하므로 새 런타임 코드나 진단 도구를 추가하지 않는다. 호스트 설정을 바꾸지 않는 절차라 되돌릴 설정 변경도 없다. 문서 사용을 중단하거나 저장소의 문서 변경만 별도 리뷰로 되돌릴 수 있다.

## 현장 확인과 공개 보고

다음 항목은 이번 문서 작성 중 모두 미실행이다. 완료 표시는 소유자의 실제 증거가 있을 때만 갱신한다.

- [ ] 일반 사용자로 기존 도구를 실행해 용량 불일치의 지속/변화와 관측 범위를 기록.
- [ ] 컨트롤러 state를 읽고 조회 실패와 실제 상태 값을 구분.
- [ ] 필요한 경우 설정 문자열·PCIe 열거를 읽되 유효 설정과 속도의 미확인 범위를 유지.
- [ ] 필요한 경우 커널 메시지를 로컬 검토하고 오류 유형·시점·제한된 개수를 비식별화.
- [ ] BMO 동작·관리 접속·냉각팬이 유지되는지 소유자가 관찰.
- [ ] 원인 가설을 지지/반박하는 증거와 남은 불확실성을 검토한 뒤 다음 조사안을 결정.

아래 양식은 현재 알려진 사실과 미확인을 구분한 공개 보고용이다. 새 관측을 수행하기 전 값을 추정해 채우지 않는다.

```text
기존 기록: namespace 0 byte / sysfs 0 sectors / 파티션 1,000,203,091,968 byte
기존 root: microSD / ext4
이번 절차의 실제 Pi 실행: 미실행
새 용량 관측: 미확인
컨트롤러 state: 미확인
PCIe 관련 설정 문자열/열거: 미확인 (유효 설정·실제 속도는 별도 미확인)
접근 가능한 로그의 오류 유형·발생 시점·관측 범위: 미확인
SSD 기존 데이터·무결성·안정성: 미확인
원인: 미확정
호스트 변경·BMO 변경·SSD 쓰기/마운트: 없음
다음 단계: 추가 관측 결과 검토 후 별도 제안
```

Issue #3의 컨트롤러 상태·오류 분류 증거가 아직 없으므로 문서 PR만으로 현장 완료 조건을 충족하지 않는다. **Issue #3은 후속 관측과 판단이 남은 상태로 유지한다.** BMO 실행·종료 계약은 별도 [Issue #4](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/4)의 범위다.
