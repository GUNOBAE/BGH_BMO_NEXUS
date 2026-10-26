# PRIVATE CLOUD MVP — 수동 ON/OFF

**NOT_DEPLOYED · 실제 Pi 설치/기동/파일 전송은 NOT_RUN.** [Issue #9](https://github.com/GUNOBAE/BGH_BMO_NEXUS/issues/9)의 목표는 BMO를 수동 종료한 동안 SFTPGo에 테스트 파일을 올리고 받는 것이다. 자동 모드 전환이나 BMO 실행·종료는 하지 않는다.

## 1. Docker 설치는 별도 승인 후

아래는 **검토용 현장 명령**이며 이번 개발에서는 실행하지 않았다. 정확한 설치 범위와 복구 경로를 소유자가 승인한 뒤 진행한다. 기존 Docker/containerd가 있거나 패키지 충돌이 있으면 임의 제거하지 않는다.

[Docker 공식 설치 안내](https://docs.docker.com/engine/install/debian/)는 Debian 13 / arm64를 지원한다. 다음 명령은 apt 저장소·패키지·Docker 서비스를 추가하며, 이미지·컨테이너 계층과 Docker 로그는 기본적으로 SD의 `/var/lib/docker`에 저장된다. 앱 파일과 계정 DB는 아래 SSD 경로에 별도 보관한다.

```bash
# Debian 13 arm64를 확인한 뒤: 서명 키와 공식 저장소 등록
sudo apt update
sudo apt install ca-certificates curl
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod 0644 /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: arm64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
# Docker Engine, CLI, Compose 설치; 설치 중 서비스가 시작될 수 있음
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker version
sudo docker compose version
```

Docker Engine은 **28 이상**을 사용한다. 이전 버전의 localhost publish에는 같은 LAN에서 접근 가능한 문제가 있었다. [공식 포트 안내](https://docs.docker.com/engine/network/port-publishing/)
Debian의 Docker daemon은 기본적으로 재부팅 시 시작하지만, 이 컨테이너는 `restart: "no"`여서 자동 복귀하지 않는다. [공식 부팅 정책](https://docs.docker.com/engine/install/linux-postinstall/#configure-docker-to-start-on-boot-with-systemd)
이 절차에는 재부팅, docker 그룹 추가, SSH/Tailscale/VNC/NetworkManager/팬/Ollama 변경이 없다. 재부팅이 필요하면 별도로 승인받고 기존 BMO 부팅 동작을 확인한다.

## 2. 최초 설정과 SSD 확인

2026-10-26 소유자는 SSD가 `BMO_SSD` / ext4 / `/mnt/ssd`, controller live로 복귀했다고 보고했다. 과거 0B/dead 장애의 원인·장기 안정성·데이터 무결성은 미확인이다. 기존 데이터와 독립 백업 위치를 먼저 확인하고 **버려도 되는 테스트 파일만** 사용한다. 이 도구는 디스크를 포맷·마운트·언마운트하지 않는다.

NEXUS 저장소 루트에서 다음을 진행한다. `findmnt`는 정확히 `/mnt/ssd`에 있는 mount만 읽으며, 디렉터리 존재만으로 SSD라고 판단하지 않는다. 정상은 `/dev/nvme0n1p1`, ext4, `BMO_SSD`, 실제 UUID, `rw`가 확인되는 것이다. 결과 원문·UUID·개인 경로를 GitHub에 올리지 않는다.

```bash
sudo findmnt --mountpoint /mnt/ssd -o TARGET,SOURCE,FSTYPE,LABEL,UUID,OPTIONS
# 최초 한 번만 복사: 기존 .env가 있으면 덮어쓰지 않음
cp -n deploy/private-cloud/.env.example deploy/private-cloud/.env
chmod 600 deploy/private-cloud/.env
```

`.env`의 `NEXUS_SSD_UUID=CHANGE_ME`를 방금 확인한 UUID로 바꾼다. 기본 mount `/mnt/ssd`, label `BMO_SSD`, 최소 여유 공간 `1073741824` bytes(1 GiB)는 현장과 대조한다. `BMO_SCAN_UID=1000`은 BMO 실행 사용자의 실제 UID와 일치시킨다. 따옴표·변수 치환 없이 `KEY=value`로 쓰며 암호나 토큰은 넣지 않는다. 편집 후 관리자가 소유하도록 보호한다.

```bash
sudo chown root:root deploy/private-cloud/.env
sudo chmod 600 deploy/private-cloud/.env
```

`server-up`은 검증된 SSD 안의 `/mnt/ssd/nexus-private-cloud-test/{data,state}`만 준비한다. 새 상위 폴더는 root, `data`와 `state`는 UID/GID `1000:1000` 소유이며 모두 mode `0700`이다. 기존 경로가 symlink이거나 소유권·권한이 맞지 않으면 거부한다. 기존 데이터 전체를 재귀 chown하거나 삭제하지 않는다. `.env`는 Git에서 제외되며 이후 수정에는 `sudoedit deploy/private-cloud/.env`를 사용한다.

## 3. BMO 종료 → 서버 시작 → 파일 전송

기존 BMO의 승인된 GUI/런처에서 수동으로 종료하고 정리가 끝났는지 확인한다. 아래 확인 flag는 운영자가 이를 확인했다는 뜻이다. 스크립트는 `/proc`에서 BMO 관련 프로세스와 해당 UID의 Python/Node/Chromium/오디오 후보를 보수적으로 확인하며, 동작 중이거나 관측 권한이 부족하면 거부한다. 로봇 외 앱도 오탐할 수 있으므로 해당 앱을 확인한 뒤 안전하게 수동 종료한다. 팬·Ollama·SSH/Tailscale/VNC 등 보호 서비스 때문에 거부되면 서비스를 끄지 말고 판별 보완을 요청한다. 자동 kill은 없다. 서버가 켜져 있는 동안 BMO를 다시 시작하지 않는다.

```bash
sudo bash scripts/server-up.sh --confirm-bmo-stopped
```

실제 filesystem mount·UUID/label·ext4/rw·NVMe live/용량·여유 공간·권한, BMO 상태, localhost 8080 충돌, Docker/Compose를 먼저 검사한다. 통과하면 고정한 `ghcr.io/drakkan/sftpgo:v2.7.6`를 받아 NEXUS 프로젝트만 시작한다. 이미지를 받으려면 인터넷이 필요하며 첫 실행은 시간이 걸릴 수 있다. `/run/lock/nexus-private-cloud.lock`으로 ON/OFF 동시 실행을 막으며 `operation_busy`이면 먼저 진행 중인 작업을 확인한다.
정상 완료는 `SERVER ON: http://127.0.0.1:8080/web/admin`으로 시작하는 출력이다. `SERVER ERROR: ssd_rejected`, `bmo_busy_or_unknown`, `bmo_unknown`, `port_busy` 등은 시작 거부이므로 원인을 확인한다. 일부 기동 뒤 오류가 나면 자기 프로젝트를 내리며, `start_cleanup_failed`이면 아래 종료 명령의 성공을 먼저 확인한다.

Pi 자체 브라우저에서 다음 순서로 확인한다. 다른 PC의 `127.0.0.1`은 그 PC 자신이므로 Pi에 접속되지 않는다.

1. `http://127.0.0.1:8080/web/admin`에서 **첫 관리자 계정을 직접 만든다**. 강력하고 재사용하지 않은 암호를 쓰고 2FA를 설정한다. 최초 등록 화면을 원격으로 노출하지 않는다.
2. 관리자 화면에서 일반 파일 사용자를 만들고 local filesystem home을 `/srv/sftpgo/data/<사용자명>` 아래로 지정한다. 필요한 목록·업로드·다운로드 권한만 준다. 이 경로는 SSD의 `data`에 대응한다.
3. `http://127.0.0.1:8080/web/client`에서 일반 사용자로 로그인해 작은 테스트 파일을 업로드·다운로드하고 내용이 같은지 확인한다. 암호는 웹 UI에서만 입력한다. [SFTPGo 공식 시작 안내](https://github.com/sftpgo/docs/blob/main/docs/docker.md#how-to-use-the-sftpgo-image)

공개 포트는 `127.0.0.1:8080:8080` 하나이며 SFTP/FTP/WebDAV 포트는 열지 않는다. 컨테이너는 non-root·읽기 전용 root filesystem과 제한된 SSD bind를 사용한다. 컨테이너는 별도 OS 보안 경계가 아니며, bridge network는 외부 송신 차단을 보장하지 않는다. Cloudflare·포트포워딩·Tailscale Serve·Docker socket 공개는 이번 범위에 없다.

## 4. 서버 종료 → BMO 수동 실행

```bash
sudo bash scripts/server-down.sh
```

고정된 `nexus-private-cloud` 프로젝트만 종료하며 SSD 검사나 `.env`가 없어도 종료를 시도할 수 있다. 정상 출력은 `SERVER OFF:`로 시작한다. 데이터·계정 DB를 보존하고 volume 삭제, SSD 언마운트, BMO 시작은 하지 않는다. 종료 성공을 확인한 뒤 BMO를 기존 방법으로 수동 실행한다. 종료 실패나 남은 컨테이너가 있으면 BMO 시작을 보류한다.

실패 시 시작을 강행하지 않는다. mount/UUID/NVMe 오류는 SSD를 복구·재확인하고, BMO 미확인은 해당 앱의 정상 종료를 확인한다. 8080 사용 중이면 사용 앱을 확인하며 임의 kill하지 않는다. 시작 후 SSD 이상이나 웹 접속 실패가 생겨도 `server-down`으로 먼저 내린다. 지속 SSD 감시나 자동 BMO 상호 배제는 구현하지 않는다.

MVP 롤백은 `server-down` 후 사용을 중단하는 것이다. SSD 테스트 디렉터리와 계정 DB는 그대로 남는다. Docker 설치 자체를 되돌려야 한다면 신규 설치 범위를 확인한 별도 승인 후 `sudo apt remove docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`을 검토한다. `/var/lib/docker`나 SSD 데이터를 자동 삭제하지 않는다.

## 5. 검수 기록

오프라인 검사는 `python3 -B -m unittest discover -s tests -v`와 `bash -n scripts/server-up.sh scripts/server-down.sh`로 실행한다. 가짜 mount·프로세스·Docker 입력만 검증하며 실제 설치나 전송 성공의 근거가 아니다.
소유자 승인 후 현장 기록에는 Docker/Compose 버전, 사전 검사 통과, localhost health·수동 계정 생성·파일 왕복, 종료 후 컨테이너 부재, BMO 수동 재실행 결과를 민감정보 없이 남긴다. 현재 전부 **NOT_RUN**이며 실제 Pi/BMO·보호 서비스·저장장치에는 변경하지 않았다.
