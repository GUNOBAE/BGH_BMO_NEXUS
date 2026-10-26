"""수동 클라우드 세션. 실제 실행은 main에서만 하며 BMO를 제어하지 않는다."""

import argparse
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
import http.client
import io
import json
import os
import re
import signal
import subprocess
import sys
import time

try:
    from . import private_cloud as cloud
except ImportError:
    import private_cloud as cloud


class SessionError(RuntimeError):
    """주소·설정·원본 오류 대신 고정된 세션 실패 이유를 사용한다."""


ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C"}
SERVE = ["tailscale", "serve", "--https=443", "http://127.0.0.1:8080"]
CONFIG_KEYS = {"TCP", "Web", "Services", "AllowFunnel", "Foreground"}
DNS = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net", re.ASCII)
REASONS = {
    "manual_confirmation_required": "BMO를 정상 종료하고 확인 flag를 지정하세요",
    "root_required": "승인 후 sudo로 실행하세요",
    "operation_busy": "다른 NEXUS 작업 또는 세션이 진행 중입니다",
    "lock_invalid": "NEXUS lock의 소유권을 확인하세요",
    "config_permissions": "기존 .env의 root 소유권·권한 600을 확인하세요",
    "config_invalid": "기존 .env 형식과 값을 확인하세요",
    "config_missing": "기존 .env의 필수 항목을 확인하세요",
    "ssd_rejected": "SSD mount·identity·상태·여유 공간 검사가 거부됐습니다",
    "ssd_changed": "시작/실행 중 SSD identity가 바뀌었습니다",
    "bmo_busy_or_unknown": "BMO 또는 해당 사용자의 앱이 남아 있거나 미확인입니다",
    "bmo_unknown": "BMO 실행 상태를 확인할 수 없습니다",
    "port_busy": "localhost 8080을 다른 앱이 사용합니다",
    "docker_platform": "로컬 Linux Docker Engine이 필요합니다",
    "docker_version": "Docker Engine 28 이상이 필요합니다",
    "command_failed": "기존 Docker/조회 도구의 권한·상태를 로컬에서 확인하세요",
    "start_cleanup_failed": "기존 서버 시작 중 정리 실패가 보고됐습니다",
    "data_permissions": "기존 테스트 폴더의 소유권·권한을 확인하세요",
    "tailscale_observation_failed": "Tailscale 상태 조회가 실패했습니다",
    "tailscale_status_unknown": "Tailscale 상태 형식이 미확인입니다",
    "tailscale_https_not_ready": "기존 Tailscale 연결·HTTPS 승인을 확인하세요",
    "existing_serve_or_funnel": "기존 공유를 발견했거나 범위가 미확인입니다",
    "nexus_container_exists": "기존 NEXUS 컨테이너를 인수하지 않습니다",
    "localhost_unhealthy": "localhost health 확인이 실패했습니다",
    "serve_start_failed": "foreground Serve를 시작하지 못했습니다",
    "serve_ownership_unknown": "foreground 공유 소유 범위를 확인하지 못했습니다",
    "serve_ready_timeout": "foreground Serve 준비 시간이 초과됐습니다",
    "serve_ownership_changed": "실행 중 공유 소유 범위가 바뀌었습니다",
    "serve_exited": "foreground Serve가 오류로 종료됐습니다",
    "serve_stop_unconfirmed": "소유한 foreground 프로세스 종료가 미확인입니다",
    "serve_closure_unconfirmed": "실제 공유 중단이 미확인입니다",
    "container_shutdown_unconfirmed": "NEXUS 컨테이너 부재가 미확인입니다",
    "session_start_or_runtime_failed": "세션 시작/실행이 실패했습니다",
    "session_interrupted": "시작 전 세션이 중단됐습니다",
}


def safe_reason(error, fallback="session_start_or_runtime_failed"):
    # 알려진 코드만 표시한다. 예외의 경로·주소·도구 원문은 절대로 되찍지 않는다.
    code = str(error).partition(":")[0]
    return code if code in REASONS else fallback


def command(argv):
    try:
        return subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=10,
                              env=ENV).stdout
    except (OSError, subprocess.SubprocessError):
        raise SessionError("tailscale_observation_failed") from None


def parse_status(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise SessionError("tailscale_status_unknown") from None
    if value is None:
        return {}
    if (not isinstance(value, dict) or set(value) - CONFIG_KEYS
            or any(not isinstance(item, dict) for item in value.values())):
        raise SessionError("tailscale_status_unknown")
    return value


def require_empty(snapshot):
    if len(snapshot) != 2 or any(any(config.values()) for config in snapshot):
        raise SessionError("existing_serve_or_funnel: 기존 공유를 변경하지 않습니다")


@dataclass(frozen=True)
class Lease:
    session_id: str
    host: str


def inspect_lease(snapshot):
    """빈 시작 상태 이후 등장한 단 하나의 foreground/443/root 경로만 허용한다."""
    try:
        left, right = snapshot
        if left != right or any(left.get(k) for k in CONFIG_KEYS - {"Foreground"}):
            raise ValueError
        foreground = left["Foreground"]
        if len(foreground) != 1:
            raise ValueError
        session_id, config = next(iter(foreground.items()))
        if (not isinstance(session_id, str) or not session_id
                or not isinstance(config, dict) or set(config) - CONFIG_KEYS
                or any(config.get(k) for k in ("Services", "AllowFunnel", "Foreground"))
                or config.get("TCP") != {"443": {"HTTPS": True}}):
            raise ValueError
        web = config["Web"]
        if not isinstance(web, dict) or len(web) != 1:
            raise ValueError
        host_port, routes = next(iter(web.items()))
        if (not isinstance(host_port, str) or not host_port.endswith(":443")
                or not DNS.fullmatch(host_port[:-4])
                or routes != {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}):
            raise ValueError
        return Lease(session_id, host_port[:-4])
    except (KeyError, TypeError, ValueError):
        raise SessionError("serve_ownership_unknown") from None


@contextmanager
def signals():
    """종료 요청은 Python 정리 경로로 전달하고, 정리 중 반복 요청은 보류한다."""
    previous = {}

    def interrupted(_number, _frame):
        raise KeyboardInterrupt

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def cleanup_signals():
    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, signal.SIG_IGN)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def deferred_start():
    # Popen이 성공한 직후 핸들을 저장하기 전에 종료 신호가 끼어드는 틈을 막는다.
    # 커널 signal mask를 막으면 자식까지 상속하므로 Python 처리기만 잠시 보류한다.
    previous = {}
    pending = False

    def deferred(_number, _frame):
        nonlocal pending
        pending = True

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, deferred)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if pending:
            raise KeyboardInterrupt


class Runtime:
    """Tailscale는 조회와 소유한 foreground 자식만 다룬다. 전역 off/reset은 없다."""

    def __init__(self):
        self.host = None

    def check_tailscale(self):
        try:
            status = json.loads(command(["tailscale", "status", "--json", "--peers=false"]))
            own = status["Self"]
            host = own["DNSName"].rstrip(".")
            capmap = own.get("CapMap", {})
            caps = own.get("Capabilities", [])
            if (status["BackendState"] != "Running" or not DNS.fullmatch(host)
                    or not isinstance(capmap, dict) or not isinstance(caps, list)
                    or "https" not in capmap and "https" not in caps):
                raise ValueError
            self.host = host
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SessionError("tailscale_https_not_ready: 기존 HTTPS 승인을 로컬에서 확인하세요") from None

    def snapshot(self):
        return tuple(parse_status(command(["tailscale", kind, "status", "--json"]))
                     for kind in ("serve", "funnel"))

    def containers_absent(self):
        remaining = cloud.run(cloud.docker_args() + [
            "ps", "--all", "--filter", f"label=com.docker.compose.project={cloud.PROJECT}",
            "--format", "{{.ID}}",
        ])
        if remaining.strip():
            raise SessionError("nexus_container_exists: 기존 NEXUS 작업을 인수하지 않습니다")

    def cloud_up(self, settings):
        # 개별 SERVER ON/OFF가 세션 전체 상태로 오해되지 않도록 최종 상태만 표시한다.
        with redirect_stdout(io.StringIO()):
            cloud.up(settings, confirmed=True)

    def cloud_down(self):
        with redirect_stdout(io.StringIO()):
            cloud.down()
        self.containers_absent()

    def health(self):
        # 프록시 환경변수·리디렉션 없이 고정 localhost /healthz만 확인한다.
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=3)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            if response.status != 200:
                raise SessionError("localhost_unhealthy")
            response.read(1024)
        except (OSError, http.client.HTTPException):
            raise SessionError("localhost_unhealthy") from None
        finally:
            connection.close()

    def start_proxy(self):
        try:
            # 터미널 Ctrl+C는 부모가 받고 소유한 자식에 SIGINT를 한 번 전달한다.
            return subprocess.Popen(SERVE, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, env=ENV)
        except OSError:
            raise SessionError("serve_start_failed") from None

    def proxy_ready(self, child):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise SessionError("serve_start_failed")
            snapshot = self.snapshot()
            if snapshot[0] != snapshot[1]:
                time.sleep(0.25)  # 두 조회 사이의 준비 상태 전이는 제한 시간 안에서 다시 확인한다.
                continue
            if any(any(config.values()) for config in snapshot):
                lease = inspect_lease(snapshot)
                if lease.host != self.host or child.poll() is not None:
                    raise SessionError("serve_ownership_unknown")
                return lease
            time.sleep(0.25)
        raise SessionError("serve_ready_timeout")

    def watch(self, child, settings, lease):
        signature = cloud.check_storage(settings)
        while child.poll() is None:
            if inspect_lease(self.snapshot()) != lease:
                raise SessionError("serve_ownership_changed")
            self.health()
            if cloud.check_storage(settings) != signature:
                raise SessionError("ssd_changed")
            cloud.check_bmo_stopped(settings)
            time.sleep(2)
        if child.returncode != 0:
            raise SessionError("serve_exited")

    def stop_proxy(self, child):
        try:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
            child.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise SessionError("serve_stop_unconfirmed") from None

    def verify_closed(self, lease):
        """알 수 없는 잔류 공유가 있어도 전역 설정을 지우지 않고 실패로 남긴다."""
        deadline = time.monotonic() + 10
        while True:
            snapshot = self.snapshot()
            # 다른 주체가 같은 백엔드를 다시 공유했는지도 보수적으로 거부한다.
            if all(not any(config.values()) for config in snapshot):
                return
            if time.monotonic() >= deadline:
                raise SessionError("serve_residue_or_foreign_config")
            time.sleep(0.25)


def run_session(settings, *, confirmed, runtime=None):
    if not confirmed:
        raise SessionError("manual_confirmation_required: BMO 정상 종료 후 확인 flag가 필요합니다")
    runtime = runtime or Runtime()
    runtime.check_tailscale()
    require_empty(runtime.snapshot())
    runtime.containers_absent()
    attempted = False
    child = None
    lease = None
    failure = None
    try:
        attempted = True  # 빈 NEXUS 상태를 확인한 뒤에만 이 프로젝트의 정리를 허용한다.
        runtime.cloud_up(settings)
        runtime.health()
        require_empty(runtime.snapshot())
        with deferred_start():
            child = runtime.start_proxy()
        lease = runtime.proxy_ready(child)
        print(f"CLOUD SESSION ON: https://{lease.host}/web/client (Ctrl+C로 종료; BMO 실행 금지)")
        runtime.watch(child, settings, lease)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        failure = safe_reason(error)
    finally:
        with cleanup_signals():
            if attempted:
                print("CLOUD SESSION STOPPING: 공유 중단과 NEXUS 컨테이너 종료를 확인 중입니다")
            if child is not None:
                try:
                    runtime.stop_proxy(child)
                except Exception:
                    failure = "serve_stop_unconfirmed"
            if attempted:
                try:
                    runtime.verify_closed(lease)
                except Exception:
                    failure = "serve_closure_unconfirmed"
                try:
                    runtime.cloud_down()
                except Exception:
                    failure = "container_shutdown_unconfirmed"
    if failure:
        raise SessionError(failure + ": BMO를 재시작하지 마세요. 로컬 공유/컨테이너 상태 확인이 필요합니다")
    print("CLOUD SESSION OFF: 공유 중단·NEXUS 컨테이너 부재 확인. 데이터와 SSD mount는 유지합니다")


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, _message):
            self.exit(2, "CLOUD SESSION ERROR: arguments_invalid: 사용법과 확인 flag를 확인하세요\n")

    parser = Parser(description="승인된 수동 클라우드·Tailscale foreground 세션")
    parser.add_argument("--confirm-bmo-stopped", action="store_true")
    parser.add_argument("--config", type=cloud.Path, default=cloud.COMPOSE.parent / ".env")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise SessionError("root_required: 승인 후 sudo bash scripts/cloud-session.sh를 사용하세요")
        if not args.confirm_bmo_stopped:
            raise SessionError("manual_confirmation_required: BMO 정상 종료 후 --confirm-bmo-stopped를 지정하세요")
        with signals(), cloud.operation_lock():
            run_session(cloud.load_settings(args.config), confirmed=True)
    except (Exception, KeyboardInterrupt) as error:
        code = safe_reason(error, "session_interrupted" if isinstance(error, KeyboardInterrupt)
                           else "session_start_or_runtime_failed")
        print(f"CLOUD SESSION ERROR: {code}: {REASONS[code]}. "
              "종료/공유 상태 확인 전 BMO를 재시작하지 마세요", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
