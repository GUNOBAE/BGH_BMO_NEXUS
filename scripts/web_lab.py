"""SSD의 두 정적 사이트를 수동 실행한다. import 시 호스트 작업은 없다."""

import argparse
from contextlib import contextmanager
import fcntl
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys

try:
    from . import private_cloud as cloud
except ImportError:
    import private_cloud as cloud


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/web-lab/compose.yaml"
PROJECT = APP_DIR = "nexus-web-lab"
SITES = ("site-a", "site-b")
PORTS = (8090, 8091)
MARKER = ".nexus-web-lab-store"
IDENTITY = b"NEXUS independent static Web Lab store v1\n"
MAX_INDEX = 1024 * 1024


class WebLabError(RuntimeError):
    """경로·도구 출력·HTTP 본문 대신 고정 코드만 표시한다."""


@contextmanager
def operation_lock():
    # 클라우드의 장시간 세션 lock과 분리해 Lab을 언제든 독립 종료할 수 있다.
    fd = os.open("/run/lock/nexus-web-lab.lock",
                 os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_gid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise WebLabError("lock_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WebLabError("operation_busy") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def termination_signals(*, cleanup=False):
    previous = {}

    def interrupted(_number, _frame):
        raise KeyboardInterrupt

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, signal.SIG_IGN if cleanup else interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def compose_args():
    return cloud.docker_args() + ["compose", "--project-name", PROJECT,
                                 "--project-directory", str(COMPOSE.parent),
                                 "--env-file", "/dev/null", "--file", str(COMPOSE)]


def fixed_settings(settings):
    if settings.mount != Path("/mnt/ssd") or settings.label != "BMO_SSD":
        raise WebLabError("ssd_rejected")


def container_rows():
    """실행 중/정지된 컨테이너 모두 관측한다. 다른 프로젝트를 인수하지 않는다."""
    raw = cloud.run(cloud.docker_args() + [
        "ps", "--all", "--filter", f"label=com.docker.compose.project={PROJECT}",
        "--quiet", "--no-trunc",
    ])
    ids = raw.split()
    if not ids:
        return []
    if len(ids) > 2 or len(set(ids)) != len(ids) or any(
            not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
        raise WebLabError("containers_unknown")
    try:
        rows = json.loads(cloud.run(cloud.docker_args() + [
            "inspect", "--type", "container", *ids]))
        if (not isinstance(rows, list) or len(rows) != len(ids)
                or {row["Id"] for row in rows} != set(ids)
                or any(row["Config"]["Labels"].get("com.docker.compose.project")
                       != PROJECT for row in rows)):
            raise ValueError
        return rows
    except (ValueError, KeyError, TypeError, AttributeError):
        raise WebLabError("containers_unknown") from None


def check_ports():
    # bind와 TCP 거부를 모두 확인한다. Docker NAT만 있는 점유도 놓치지 않는다.
    for port in PORTS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))
        except OSError:
            raise WebLabError("port_busy") from None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(2)
                result = probe.connect_ex(("127.0.0.1", port))
            if result == 0:
                raise WebLabError("port_busy")
            if result != errno.ECONNREFUSED:
                raise WebLabError("port_unknown")
        except OSError:
            raise WebLabError("port_unknown") from None


def _verified(fd, signature, *, directory=False, mode=None):
    info = os.fstat(fd)
    device = (os.major(info.st_dev), os.minor(info.st_dev))
    if device != tuple(map(int, signature[1].split(":"))):
        raise WebLabError("ssd_changed")
    if directory:
        valid = stat.S_ISDIR(info.st_mode)
    else:
        valid = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if (not valid or info.st_uid != 0 or info.st_gid != 0
            or mode is not None and stat.S_IMODE(info.st_mode) != mode):
        raise WebLabError("path_rejected")
    return info


@contextmanager
def _child(parent, name, signature, mode):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _verified(fd, signature, directory=True, mode=mode)
        yield fd
    finally:
        os.close(fd)


def _read(parent, name, signature, mode, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = _verified(fd, signature, mode=mode)
        if info.st_size > limit:
            raise WebLabError("path_rejected")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            result = stream.read(limit + 1)
        if len(result) != info.st_size or len(result) > limit:
            raise WebLabError("path_rejected")
        return result
    finally:
        os.close(fd)


def _write_new(parent, name, content, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode, dir_fd=parent)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _new_directory(parent, name, mode):
    os.mkdir(name, mode=mode, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        # 새 경로만 정규화한다. restrictive umask와 mount setgid 상속을 처리한다.
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _audit_site(fd, signature, budget):
    # 정적 파일만 허용한다. 링크로 클라우드/미러/로봇 파일을 가져올 수 없다.
    for name in os.listdir(fd):
        budget[0] -= 1
        if budget[0] < 0:
            raise WebLabError("path_rejected")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with _child(fd, name, signature, 0o755) as child:
                _audit_site(child, signature, budget)
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=fd)
            try:
                _verified(file_fd, signature, mode=0o644)
            finally:
                os.close(file_fd)
        else:
            raise WebLabError("path_rejected")


def _sites(settings, signature, *, create):
    fixed_settings(settings)
    try:
        with cloud.directory_fd(settings.mount) as mount:
            # mount 자체의 mode는 기존 설정을 변경하거나 강제하지 않는다.
            info = os.fstat(mount)
            if (not stat.S_ISDIR(info.st_mode)
                    or (os.major(info.st_dev), os.minor(info.st_dev))
                    != tuple(map(int, signature[1].split(":")))):
                raise WebLabError("ssd_changed")
            created = False
            if create:
                try:
                    _new_directory(mount, APP_DIR, 0o700)
                    created = True
                except FileExistsError:
                    pass
            with _child(mount, APP_DIR, signature, 0o700) as base:
                if created:
                    _write_new(base, MARKER, IDENTITY, 0o600)
                    for site in SITES:
                        _new_directory(base, site, 0o755)
                        with _child(base, site, signature, 0o755) as child:
                            template = (COMPOSE.parent / site / "index.html").read_bytes()
                            _write_new(child, "index.html", template, 0o644)
                    os.fsync(base)
                if (set(os.listdir(base)) != {MARKER, *SITES}
                        or _read(base, MARKER, signature, 0o600, 128) != IDENTITY):
                    raise WebLabError("path_rejected")
                expected = {}
                for site in SITES:
                    with _child(base, site, signature, 0o755) as child:
                        _audit_site(child, signature, [10000])
                        content = _read(child, "index.html", signature, 0o644, MAX_INDEX)
                        if not content:
                            raise WebLabError("path_rejected")
                        expected[site] = hashlib.sha256(content).hexdigest()
                if len(set(expected.values())) != 2:
                    raise WebLabError("sites_not_distinct")
                return expected
    except (OSError, RecursionError):
        raise WebLabError("path_rejected") from None


def prepare_sites(settings, signature):
    return _sites(settings, signature, create=True)


def inspect_sites(settings, signature):
    return _sites(settings, signature, create=False)


def check_health(expected):
    if set(expected) != set(SITES) or len(set(expected.values())) != 2:
        raise WebLabError("sites_not_distinct")
    for site, port in zip(SITES, PORTS):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/", headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(MAX_INDEX + 1)
            if (response.status != 200 or len(body) > MAX_INDEX
                    or hashlib.sha256(body).hexdigest() != expected[site]):
                raise WebLabError("localhost_unhealthy")
        except (OSError, http.client.HTTPException):
            raise WebLabError("localhost_unhealthy") from None
        finally:
            connection.close()


def require_running(rows):
    try:
        found = set()
        for row in rows:
            service = row["Config"]["Labels"]["com.docker.compose.service"]
            if service not in SITES or service in found:
                raise ValueError
            found.add(service)
            port = str(PORTS[SITES.index(service)])
            if (row["State"]["Running"] is not True
                    or row["State"]["Health"]["Status"] != "healthy"
                    or row["HostConfig"]["PortBindings"] != {
                        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]}
                    or row["NetworkSettings"]["Ports"] != {
                        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]}):
                raise ValueError
            mounts = row["Mounts"]
            binds = [mount for mount in mounts if mount["Type"] != "tmpfs"]
            if (len(binds) != 1 or binds[0]["Type"] != "bind"
                    or binds[0]["Source"] != f"/mnt/ssd/{APP_DIR}/{service}"
                    or binds[0]["Destination"] != "/usr/share/nginx/html"
                    or binds[0]["RW"] is not False):
                raise ValueError
        if found != set(SITES):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise WebLabError("containers_unhealthy") from None


def _same_storage(settings, signature):
    if cloud.check_storage(settings) != signature:
        raise WebLabError("ssd_changed")


def up(settings, *, confirmed):
    if not confirmed:
        raise WebLabError("manual_confirmation_required")
    fixed_settings(settings)
    cloud.check_docker()
    signature = cloud.check_storage(settings)
    cloud.check_bmo_stopped(settings)
    if container_rows():
        raise WebLabError("containers_exist")
    check_ports()
    _same_storage(settings, signature)
    expected = prepare_sites(settings, signature)
    _same_storage(settings, signature)
    cloud.check_bmo_stopped(settings)
    # 실패 후에도 기존 사이트 파일을 덮어쓰거나 삭제하지 않는다.
    try:
        cloud.run(compose_args() + ["up", "--detach", "--wait", "--wait-timeout", "60",
                                    "--no-build", *SITES], timeout=180)
        _same_storage(settings, signature)
        cloud.check_bmo_stopped(settings)
        require_running(container_rows())
        check_health(expected)
        if inspect_sites(settings, signature) != expected:
            raise WebLabError("sites_changed")
        _same_storage(settings, signature)
    except (cloud.PrivateCloudError, WebLabError, OSError, KeyboardInterrupt):
        try:
            with termination_signals(cleanup=True):
                down(announce=False)
        except (cloud.PrivateCloudError, WebLabError, OSError, KeyboardInterrupt):
            raise WebLabError("start_cleanup_failed") from None
        raise
    print("WEB LAB ON: http://127.0.0.1:8090 / http://127.0.0.1:8091 (BMO를 실행하지 마세요)")


def down(*, announce=True):
    # SSD 장애나 설정 파일 부재도 자기 프로젝트 종료를 막지 않는다.
    cloud.check_docker(for_start=False)
    cloud.run(compose_args() + ["down", "--timeout", "30"], timeout=90)
    if container_rows():
        raise WebLabError("down_incomplete")
    check_ports()
    if announce:
        print("WEB LAB OFF: 컨테이너 0개·두 포트 닫힘 확인. 사이트 데이터는 보존합니다")


def status(settings=None):
    cloud.check_docker(for_start=False)
    rows = container_rows()
    if not rows:
        check_ports()
        print("WEB LAB OFF: 컨테이너 0개·두 포트 닫힘 확인")
        return
    require_running(rows)
    if settings is None:
        settings = cloud.load_settings(cloud.COMPOSE.parent / ".env")
    fixed_settings(settings)
    signature = cloud.check_storage(settings)
    cloud.check_bmo_stopped(settings)
    expected = inspect_sites(settings, signature)
    check_health(expected)
    _same_storage(settings, signature)
    print("WEB LAB ON: http://127.0.0.1:8090 / http://127.0.0.1:8091 (BMO를 실행하지 마세요)")


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "WEB LAB ERROR: arguments_invalid (up/down/status와 확인 flag만 사용하세요)\n")


def main(argv=None):
    parser = Parser(description="SSD localhost Web Lab 수동 실행; Pi 검증은 별도")
    parser.add_argument("action", choices=("up", "down", "status"))
    parser.add_argument("--confirm-bmo-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise WebLabError("root_required")
        if args.action == "up" and not args.confirm_bmo_stopped:
            raise WebLabError("manual_confirmation_required")
        with operation_lock(), termination_signals():
            if args.action == "up":
                settings = cloud.load_settings(cloud.COMPOSE.parent / ".env")
                up(settings, confirmed=True)
            elif args.action == "down":
                down()
            else:
                status()
    except (WebLabError, cloud.PrivateCloudError, OSError, KeyboardInterrupt) as error:
        codes = {"manual_confirmation_required", "root_required", "ssd_rejected",
                 "ssd_changed", "port_busy", "port_unknown", "path_rejected", "sites_not_distinct",
                 "containers_unknown", "containers_unhealthy", "containers_exist",
                 "localhost_unhealthy", "sites_changed", "down_incomplete",
                 "start_cleanup_failed", "operation_busy", "lock_invalid",
                 "bmo_busy_or_unknown", "bmo_unknown", "docker_platform",
                 "docker_version", "command_failed", "config_permissions",
                 "config_invalid", "config_missing"}
        code = str(error).partition(":")[0]
        if code not in codes:
            code = "observation_failed"
        print(f"WEB LAB ERROR: {code}. 성공 미확인: BMO를 실행하지 마세요", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
