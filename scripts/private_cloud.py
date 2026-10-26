"""수동 SFTPGo ON/OFF. import는 아무 호스트 작업도 실행하지 않는다."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/private-cloud/compose.yaml"
PROJECT = "nexus-private-cloud"
APP_DIR = "nexus-private-cloud-test"


class PrivateCloudError(RuntimeError):
    """민감한 경로·argv·Docker 원문 대신 고정된 실패 이유만 출력한다."""


@dataclass(frozen=True)
class Settings:
    mount: Path
    uuid: str
    label: str = "BMO_SSD"
    min_free_bytes: int = 1073741824
    bmo_uid: int = 1000


def load_settings(path: Path) -> Settings:
    metadata = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise PrivateCloudError("config_permissions: .env는 root 소유의 일반 파일, 권한 600이어야 합니다")
    allowed = {"NEXUS_SSD_MOUNT", "NEXUS_SSD_UUID", "NEXUS_SSD_LABEL",
               "NEXUS_MIN_FREE_BYTES", "BMO_SCAN_UID"}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (not separator or key not in allowed or key in values
                or not value or not re.fullmatch(r"[A-Za-z0-9_./:-]+", value)):
            raise PrivateCloudError("config_invalid: 예시의 KEY=value 형식만 사용하세요")
        values[key] = value
    if set(values) != allowed:
        raise PrivateCloudError("config_missing: .env.example의 다섯 항목이 모두 필요합니다")
    mount = Path(values["NEXUS_SSD_MOUNT"])
    uuid = values["NEXUS_SSD_UUID"]
    try:
        free = int(values["NEXUS_MIN_FREE_BYTES"])
        uid = int(values["BMO_SCAN_UID"])
    except ValueError:
        raise PrivateCloudError("config_invalid: 용량과 UID는 양의 정수여야 합니다") from None
    if (not mount.is_absolute() or mount == Path("/") or ".." in mount.parts
            or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid)
            or free <= 0 or uid <= 0):
        raise PrivateCloudError("config_invalid: 실제 SSD UUID·절대 경로·양의 용량/UID를 설정하세요")
    return Settings(mount, uuid, values["NEXUS_SSD_LABEL"], free, uid)


def run(argv: list[str], *, mount: Path = Path("/mnt/ssd"), timeout: int = 30) -> str:
    # 상속한 DOCKER_HOST/CONTEXT/COMPOSE_FILE나 .env가 대상 daemon/프로젝트를 바꾸지 못한다.
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
                   "LC_ALL": "C", "NEXUS_SSD_MOUNT": str(mount)}
    try:
        result = subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise PrivateCloudError("command_failed: 필요한 도구·Docker 권한·상태를 로컬에서 확인하세요") from None
    return result.stdout


def docker_args() -> list[str]:
    return ["docker", "--host", "unix:///var/run/docker.sock"]


def compose_args(mount: Path = Path("/mnt/ssd")) -> list[str]:
    # mount는 run()의 제한된 환경으로 전달한다. down에는 .env나 SSD가 필요 없다.
    return docker_args() + ["compose", "--project-name", PROJECT,
                            "--project-directory", str(COMPOSE.parent),
                            "--env-file", "/dev/null", "--file", str(COMPOSE)]


def check_docker(*, for_start: bool = True):
    run(docker_args() + ["compose", "version"])
    if run(docker_args() + ["info", "--format", "{{.OSType}}"]).strip() != "linux":
        raise PrivateCloudError("docker_platform: 로컬 Linux Docker Engine이 필요합니다")
    if not for_start:
        return  # 구버전 여부는 새 기동만 막는다. 장애 중 종료를 방해하지 않는다.
    version = run(docker_args() + ["version", "--format", "{{.Server.Version}}"])
    # Docker 공식 문서: 28 미만은 loopback publish에도 LAN 접근 문제가 있었다.
    match = re.match(r"([0-9]+)\.", version.strip())
    if not match or int(match[1]) < 28:
        raise PrivateCloudError("docker_version: localhost 바인딩 보호를 위해 Docker Engine 28 이상이 필요합니다")


def check_storage(settings: Settings) -> tuple[str, str, str]:
    """읽기 전용 검사. 디렉터리만 있거나 SD의 부모 mount이면 거부한다."""
    output = run(["findmnt", "--kernel", "--list", "--json", "--mountpoint",
                  str(settings.mount), "--output", "TARGET,SOURCE,FSTYPE,UUID,LABEL,OPTIONS,MAJ:MIN"])
    try:
        rows = json.loads(output)["filesystems"]
        if len(rows) != 1:
            raise ValueError
        row = rows[0]
        options = row["options"].split(",")
        source = row["source"]
        match = re.fullmatch(r"/dev/(nvme[0-9]+n[0-9]+)(?:p[0-9]+)?", source)
        if (row["target"] != str(settings.mount) or row["fstype"] != "ext4"
                or row["uuid"] != settings.uuid or row["label"] != settings.label
                or "rw" not in options or "ro" in options or not match):
            raise ValueError
        disk = match[1]
        # nvme0n1 → nvme0: namespace의 controller 상태를 확인한다.
        controller = re.fullmatch(r"(nvme[0-9]+)n[0-9]+", disk)[1]
        device = os.stat(source)
        mounted = os.stat(settings.mount, follow_symlinks=False)
        major_minor = f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
        if (not stat.S_ISBLK(device.st_mode) or not stat.S_ISDIR(mounted.st_mode)
                or mounted.st_dev != device.st_rdev or major_minor != row["maj:min"]
                or settings.mount.resolve() != settings.mount):
            raise ValueError
        if (int(Path(f"/sys/class/block/{disk}/size").read_text().strip()) <= 0
                or Path(f"/sys/class/nvme/{controller}/state").read_text().strip() != "live"
                or not os.access(settings.mount, os.W_OK)
                or shutil.disk_usage(settings.mount).free < settings.min_free_bytes):
            raise ValueError
        return source, row["maj:min"], row["uuid"]
    except (KeyError, TypeError, ValueError, OSError):
        raise PrivateCloudError("ssd_rejected: SSD mount·ext4·UUID/label·rw·NVMe live/용량·여유 공간을 확인하세요") from None


def check_bmo_stopped(settings: Settings, proc_root: Path = Path("/proc")):
    """전체 목록을 조회한다. 원문 argv는 출력하지 않고 미확인은 안전하게 거부한다."""
    risky = re.compile(r"(?:python(?:[0-9.]+)?|node(?:js)?|chromium.*|chrome.*|aplay|arecord|piper|(?:libcamera|rpicam).*)$")
    launchers = {"launch-bmo", "launch-desktop", "launch-features"}
    try:
        processes = list(proc_root.iterdir())
        if not any(p.name.isdigit() for p in processes):
            raise PrivateCloudError("bmo_unknown: 프로세스 전체 목록을 확인할 수 없습니다")
        for process in processes:
            if not process.name.isdigit() or int(process.name) == os.getpid():
                continue
            try:
                status = (process / "status").read_text()
                uid = int(re.search(r"^Uid:\s+(\d+)", status, re.MULTILINE)[1])
                parent = int(re.search(r"^PPid:\s+(\d+)", status, re.MULTILINE)[1])
                state = re.search(r"^State:\s+(\S+)", status, re.MULTILINE)[1]
                comm = (process / "comm").read_text().strip()
                raw = (process / "cmdline").read_bytes()
            except FileNotFoundError:
                if not process.exists():
                    continue
                raise PrivateCloudError("bmo_unknown: 실행 관측 중 일부 정보가 누락됐습니다") from None
            if not raw:
                if parent in (0, 2) and state != "Z":
                    continue  # 커널 스레드만 빈 argv를 허용한다. zombie도 미확인이다.
                raise PrivateCloudError("bmo_unknown: 비어 있는 앱 실행 정보를 확인하세요")
            args = raw.decode("utf-8", errors="replace").split("\0")
            if (any("bmo_ai" in arg or Path(arg).name in launchers for arg in args)
                    or uid == settings.bmo_uid and risky.fullmatch(comm)):
                raise PrivateCloudError("bmo_busy_or_unknown: BMO 및 해당 사용자의 Python/Node/Chromium/음향 앱을 수동 확인하세요")
    except PrivateCloudError:
        raise
    except (OSError, ValueError, TypeError):
        raise PrivateCloudError("bmo_unknown: 프로세스 조사 권한/가시성이 부족합니다") from None


def check_port():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 8080))
    except OSError:
        raise PrivateCloudError("port_busy: localhost 8080을 사용하는 앱을 먼저 확인하세요") from None


@contextmanager
def directory_fd(path: Path):
    """각 구성요소를 symlink 없이 열어 SSD 검사 뒤 SD로 바뀌는 경로를 거부한다."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def prepare_directories(settings: Settings, signature: tuple[str, str, str]):
    """새 테스트 폴더만 생성한다. 기존 데이터의 재귀 chown/chmod는 하지 않는다."""
    device_number = tuple(map(int, signature[1].split(":")))

    def verified(fd):
        device = os.fstat(fd).st_dev
        if (os.major(device), os.minor(device)) != device_number:
            raise PrivateCloudError("ssd_changed: 경로가 검증한 SSD를 가리키지 않습니다")

    def child(parent, name, owner):
        created = False
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            verified(fd)
            if created:
                os.fchown(fd, owner, owner)
                os.fchmod(fd, 0o700)
            info = os.fstat(fd)
            if info.st_uid != owner or info.st_gid != owner or stat.S_IMODE(info.st_mode) != 0o700:
                raise PrivateCloudError("data_permissions: 기존 테스트 폴더의 소유자/권한을 검토하세요")
            return fd
        except BaseException:
            os.close(fd)
            raise

    with directory_fd(settings.mount) as mount_fd:
        verified(mount_fd)
        base = child(mount_fd, APP_DIR, 0)
        try:
            for name in ("data", "state"):
                fd = child(base, name, 1000)
                os.close(fd)
        finally:
            os.close(base)


def up(settings: Settings, *, confirmed: bool):
    if not confirmed:
        raise PrivateCloudError("manual_confirmation_required: BMO를 수동 종료한 뒤 --confirm-bmo-stopped를 지정하세요")
    check_docker()
    signature = check_storage(settings)
    check_bmo_stopped(settings)
    check_port()
    if check_storage(settings) != signature:
        raise PrivateCloudError("ssd_changed: 검사 중 mount identity가 바뀌었습니다")
    prepare_directories(settings, signature)
    if check_storage(settings) != signature:
        raise PrivateCloudError("ssd_changed: 시작 직전 mount identity가 바뀌었습니다")
    check_bmo_stopped(settings)
    try:
        run(compose_args(settings.mount) + ["up", "--detach", "--wait", "--wait-timeout", "60",
                                          "--no-build", "sftpgo"], mount=settings.mount, timeout=180)
        # 이미지 다운로드/시작 사이 변화도 확인하고 실패하면 자기 프로젝트만 내린다.
        if check_storage(settings) != signature:
            raise PrivateCloudError("ssd_changed: 시작 도중 SSD가 바뀌었습니다")
        check_bmo_stopped(settings)
    except (PrivateCloudError, KeyboardInterrupt):
        try:
            down()
        except PrivateCloudError:
            raise PrivateCloudError("start_cleanup_failed: sudo bash scripts/server-down.sh로 종료를 확인하세요") from None
        raise
    print("SERVER ON: http://127.0.0.1:8080/web/admin (BMO를 실행하지 마세요)")


def down():
    check_docker(for_start=False)
    run(compose_args() + ["down", "--timeout", "30"], timeout=90)
    remaining = run(docker_args() + ["ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
                                     "--format", "{{.ID}}"])
    if remaining.strip():
        raise PrivateCloudError("down_incomplete: NEXUS 컨테이너가 남았습니다. BMO를 실행하지 마세요")
    print("SERVER OFF: NEXUS 컨테이너 종료. 테스트 데이터와 SSD mount는 유지합니다")


@contextmanager
def operation_lock():
    fd = os.open("/run/lock/nexus-private-cloud.lock",
                 os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise PrivateCloudError("lock_invalid: 로컬 lock의 소유권을 확인하세요")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrivateCloudError("operation_busy: 다른 ON/OFF 작업이 진행 중입니다") from None
        yield
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SFTPGo 수동 ON/OFF; 실제 실행은 소유자 승인 후")
    parser.add_argument("action", choices=("up", "down"))
    parser.add_argument("--config", type=Path, default=COMPOSE.parent / ".env")
    parser.add_argument("--confirm-bmo-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise PrivateCloudError("root_required: 승인 후 sudo bash scripts/server-up.sh 또는 server-down.sh를 사용하세요")
        with operation_lock():
            if args.action == "up":
                up(load_settings(args.config), confirmed=args.confirm_bmo_stopped)
            else:
                down()
    except (PrivateCloudError, OSError) as error:
        message = str(error) if isinstance(error, PrivateCloudError) else "observation_failed: 설정/권한/관측 도구를 확인하세요"
        print("SERVER ERROR: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
