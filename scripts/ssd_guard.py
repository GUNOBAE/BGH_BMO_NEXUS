#!/usr/bin/env python3
"""SSD guard for the SD-booted NEXUS host. Import has no side effects."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

import private_cloud as cloud
from ssd_guard_state import (Observation, advance, approve_five, block, initial,
                             enqueue)

CONFIG = Path("/etc/bmo-nexus/ssd-guard.json")
STATE_DIR = Path("/var/lib/bmo-nexus/ssd-guard")
STATE = STATE_DIR / "state.json"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C"}
PROJECTS = ("nexus-private-cloud", "nexus-web-lab")
HEALTH = {"healthy", "dead", "missing", "zero_size", "unsafe", "unknown"}


class GuardError(RuntimeError):
    pass


def secure_directory(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise GuardError("directory_permissions")


def read_json(path):
    secure_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > 65536):
            raise GuardError("file_permissions")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as source:
            return json.load(source)
    finally:
        os.close(fd)


def atomic_save(path, value):
    secure_directory(path.parent)
    data = (json.dumps(value, sort_keys=True) + "\n").encode()
    if len(data) > 65536:
        raise GuardError("state_too_large")
    fd, name = tempfile.mkstemp(prefix=".guard-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())  # SD only; never global sync() on a dead SSD.
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_lock():
    secure_directory(STATE_DIR)
    fd = os.open(STATE_DIR / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise GuardError("lock_permissions")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError("busy") from None
        yield
    finally:
        os.close(fd)


def validate_state(s):
    prototype = initial()
    if not isinstance(s, dict) or set(s) != set(prototype):
        raise GuardError("state_invalid")
    for key, default in prototype.items():
        value = s[key]
        if key == "good_since":
            if value is not None and (type(value) not in (int, float)
                                      or not math.isfinite(value) or value < 0):
                raise GuardError("state_invalid")
        elif type(default) is float:
            if type(value) not in (float, int) or not math.isfinite(value) or value < -1:
                raise GuardError("state_invalid")
        elif type(value) is not type(default):
            raise GuardError("state_invalid")
    if (s["version"] != 1 or s["phase"] not in {"DISARMED", "WATCHING", "RECOVERING", "BLOCKED"}
            or not 0 <= s["attempts"] <= 5 or s["total_attempts"] < s["attempts"]
            or s["batch"] < 1 or not 0 <= s["bad"] <= 3 or len(s["outbox"]) > 32
            or s["pending_boot"] and s["attempts"] == 0):
        raise GuardError("state_invalid")
    for event in s["outbox"]:
        if (not isinstance(event, dict) or set(event) != {"id", "message"}
                or not all(isinstance(v, str) and len(v) <= 2048 for v in event.values())):
            raise GuardError("state_invalid")
    return s


def validate_config(c):
    expected = {"version", "mount", "uuid", "controller", "namespace", "partition",
                "serial_sha256", "bmo_uid", "ntfy_url", "ntfy_topic", "ntfy_token"}
    if (not isinstance(c, dict) or set(c) != expected
            or type(c["version"]) is not int or c["version"] != 1):
        raise GuardError("config_invalid")
    if type(c["bmo_uid"]) is not int or c["bmo_uid"] <= 0:
        raise GuardError("config_invalid")
    for key in expected - {"version", "bmo_uid"}:
        if not isinstance(c[key], str):
            raise GuardError("config_invalid")
    mount = Path(c["mount"])
    u = urllib.parse.urlsplit(c["ntfy_url"])
    if (not mount.is_absolute() or str(mount) != c["mount"] or mount == Path("/")
            or ".." in mount.parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", str(mount))
            or not re.fullmatch(r"[a-fA-F0-9-]{36}", c["uuid"])
            or not re.fullmatch(r"nvme[0-9]+", c["controller"])
            or not re.fullmatch(re.escape(c["controller"]) + r"n[0-9]+", c["namespace"])
            or not re.fullmatch(re.escape(c["namespace"]) + r"p[0-9]+", c["partition"])
            or not re.fullmatch(r"[0-9a-f]{64}", c["serial_sha256"])
            or u.scheme != "https" or not u.hostname or u.username or u.password
            or u.query or u.fragment or u.path not in ("", "/")
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", c["ntfy_topic"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{0,256}", c["ntfy_token"])):
        raise GuardError("config_invalid")
    return c


def config_fingerprint(c):
    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()


def mount_rows(text):
    rows = []
    for line in text.splitlines():
        left, right = line.split(" - ", 1)
        l, r = left.split(), right.split()
        rows.append(dict(device=l[2], root=l[3], target=l[4], options=l[5].split(","),
                         fstype=r[0], source=r[1], super_options=r[2].split(",")))
    return rows


def check_sd_paths(rows, paths):
    # Compare kernel mount table, not stat/statvfs of the possibly dead mount.
    roots = [r for r in rows if r["target"] == "/"]
    if len(roots) != 1 or not re.fullmatch(r"/dev/mmcblk[0-9]+p[0-9]+", roots[0]["source"]):
        raise GuardError("sd_root_required")
    root = roots[0]
    for path in paths:
        matches = [r for r in rows if str(path) == r["target"]
                   or str(path).startswith(r["target"].rstrip("/") + "/")]
        match = max(matches, key=lambda r: len(r["target"]))
        if match["device"] != root["device"] or "rw" not in match["options"]:
            raise GuardError("guard_must_live_on_sd")


def observe_storage(c, *, read=lambda p: Path(p).read_text(),
                    readlink=lambda p: os.readlink(p)):
    """Never read user files, issue SMART commands or stat the SSD mount."""
    try:
        controller = f"/sys/class/nvme/{c['controller']}"
        try:
            state = read(controller + "/state").strip()
        except FileNotFoundError:
            return "missing"
        # Check identity whenever available, even while the controller is dead.
        try:
            serial = read(controller + "/serial").strip()
        except FileNotFoundError:
            if state != "dead":
                return "unknown"
        else:
            if hashlib.sha256(serial.encode()).hexdigest() != c["serial_sha256"]:
                return "unsafe"
        if state == "dead":
            return "dead"
        if state != "live":
            return "unknown"  # resetting/connecting is not a proved dead device.
        try:
            size = int(read(f"/sys/class/block/{c['namespace']}/size").strip())
        except FileNotFoundError:
            return "missing"
        if size == 0:
            return "zero_size"
        if size < 0:
            return "unknown"
        target = readlink(f"/dev/disk/by-uuid/{c['uuid']}")
        if Path(target).name != c["partition"]:
            return "unsafe"
        device = read(f"/sys/class/block/{c['partition']}/dev").strip()
        rows = [r for r in mount_rows(read("/proc/self/mountinfo")) if r["target"] == c["mount"]]
        if len(rows) != 1:
            return "unsafe"
        row = rows[0]
        if (row["device"] != device or row["fstype"] != "ext4" or row["root"] != "/"
                or "rw" not in row["options"] or "ro" in row["super_options"]):
            return "unsafe"
        return "healthy"
    except (OSError, ValueError, IndexError):
        return "unknown"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def notify(c, message):
    payload = {"topic": c["ntfy_topic"], "title": "BMO NEXUS SSD recovery",
               "message": message, "priority": 5}
    headers = {"Content-Type": "application/json"}
    if c["ntfy_token"]:
        headers["Authorization"] = "Bearer " + c["ntfy_token"]
    request = urllib.request.Request(c["ntfy_url"].rstrip("/") + "/",
                                     data=json.dumps(payload).encode(), headers=headers, method="POST")
    # No environment proxy, no redirects carrying credentials to another host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            result = json.loads(response.read(65536))
            return (response.status == 200 and isinstance(result, dict) and result.get("event") == "message"
                    and bool(result.get("id")) and result.get("topic") == c["ntfy_topic"])
    except (OSError, ValueError, urllib.error.URLError):
        return False


def command(argv, timeout=10):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, env=ENV,
                              timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise GuardError("command_failed") from None


def bmo_clear(c):
    try:
        cloud.check_bmo_stopped(cloud.Settings(Path(c["mount"]), c["uuid"], bmo_uid=c["bmo_uid"]))
        return True
    except (cloud.PrivateCloudError, OSError, ValueError, TypeError, AttributeError):
        return False


def stop_owned():
    """Stop only the two explicitly owned projects; leave all other services alone."""
    ok = True
    for project in PROJECTS:
        try:
            ids = command(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock",
                           "ps", "--filter", f"label=com.docker.compose.project={project}",
                           "--format", "{{.ID}}"], timeout=5).split()
            if any(not re.fullmatch(r"[0-9a-f]{12,64}", i) for i in ids):
                raise GuardError("container_ids_invalid")
            if ids:
                command(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock",
                         "stop", "--time", "15", *ids], timeout=20)
        except GuardError:
            ok = False
    return ok


def flush(c, s, boot, uptime, save):
    if not s["outbox"]:
        return
    if s["notify_boot"] == boot and s["notify_at"] >= 0 and uptime - s["notify_at"] < 300:
        return
    s.update(notify_boot=boot, notify_at=uptime)
    save(s)  # Persist retry throttle even if the HTTP request fails.
    if notify(c, s["outbox"][0]["message"]):
        s["outbox"].pop(0)
        save(s)


def tick(c, s, obs, *, save, stop=stop_owned, reboot=None, clear=None):
    """Inject side effects for offline tests; persist before stop/reboot/network."""
    reboot = reboot or (lambda: command(["/usr/bin/systemctl", "--no-block", "reboot"]))
    clear = clear or (lambda: bmo_clear(c))
    updated, action = advance(s, obs)
    if action == "reboot" and s["phase"] == "WATCHING":
        updated["incident"] = uuid.uuid4().hex
        updated["batch"] = 1
    if updated != s:
        save(updated)
    if action in ("reboot", "stop"):
        if not stop():
            enqueue(updated, "stop_unconfirmed", "NEXUS container shutdown could not be confirmed. Inspect the server after recovery; no storage repair was attempted.")
            save(updated)
    if action == "reboot":
        if not clear():
            block(updated, "bmo_not_clear", "SSD recovery paused: BMO activity appeared before reboot. Inspect the server locally.")
            save(updated)
        else:
            try:
                reboot()  # Normal orderly reboot only. No force, SysRq, or power cut.
            except GuardError:
                block(updated, "reboot_command_failed", "The reboot request failed. Automatic SSD recovery is paused. Inspect the server locally.")
                save(updated)
    return updated


def snapshot(c, s):
    # Deliberately exclude topic, token, UUID, serial hash, IPs, argv and private paths.
    return {"phase": s["phase"], "attempts": s["attempts"], "limit": 5,
            "total_attempts": s["total_attempts"], "batch": s["batch"],
            "incident": s["incident"], "reason": s["reason"],
            "pending_notifications": len(s["outbox"]), "health": observe_storage(c),
            "notification_test_passed": s["notification_test"] == config_fingerprint(c)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded SSD recovery. Requires SD-installed configuration.")
    parser.add_argument("action", choices=("status", "tick", "arm", "disarm", "retry-five", "resume", "test-notify"))
    parser.add_argument("--confirm-bmo-stopped", action="store_true")
    parser.add_argument("--incident", default="")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise GuardError("root_required")
        rows = mount_rows(Path("/proc/self/mountinfo").read_text())
        check_sd_paths(rows, (CONFIG, STATE, Path(__file__).resolve()))
        c = validate_config(read_json(CONFIG))
        with state_lock():
            s = validate_state(read_json(STATE))  # Missing/corrupt state NEVER resets the budget.
            save = lambda value: atomic_save(STATE, validate_state(value))
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            if not re.fullmatch(r"[0-9a-f-]{36}", boot) or not math.isfinite(uptime) or uptime < 0:
                raise GuardError("boot_observation_invalid")
            if args.action == "status":
                print(json.dumps(snapshot(c, s), indent=2))
                return 0
            if args.action == "disarm":
                # Preserve counter, pending reboot and outbox. No state deletion/reset.
                s["phase"] = "DISARMED"
                save(s)
                print("SSD GUARD DISARMED: counters preserved; server apps were not stopped.")
                return 0
            if args.action == "test-notify":
                if not notify(c, "BMO NEXUS test: SSD recovery alerts are connected. No reboot was requested."):
                    raise GuardError("notification_test_failed")
                s["notification_test"] = config_fingerprint(c)
                save(s)
                print("NTFY TEST ACCEPTED: verify this message on your phone before arming.")
                return 0
            if args.action in ("arm", "retry-five", "resume"):
                if not args.confirm_bmo_stopped or not bmo_clear(c):
                    raise GuardError("bmo_confirmation_required")
                if s["notification_test"] != config_fingerprint(c):
                    raise GuardError("run_test_notify_first")
                if args.action == "retry-five":
                    s = approve_five(s, args.incident)
                elif args.action == "resume":
                    if s["phase"] != "BLOCKED" or args.incident != s["incident"]:
                        raise GuardError("resume_not_applicable")
                    if s["pending_boot"] == boot or observe_storage(c) != "healthy":
                        raise GuardError("manual_recovery_required")
                    s.update(phase="RECOVERING", reason="", bad=0, good_since=None, sample_at=-1.0)
                    s["outbox"] = [e for e in s["outbox"] if not e["id"].endswith(":retry_limit")]
                else:
                    if s["phase"] != "DISARMED" or observe_storage(c) != "healthy":
                        raise GuardError("arm_requires_disarmed_healthy")
                    # Disarm/arm is not a way around the five-attempt budget.
                    if s["attempts"] or s["pending_boot"]:
                        s.update(phase="RECOVERING", good_since=None, bad=0)
                    else:
                        s.update(phase="WATCHING", incident=uuid.uuid4().hex, reason="")
                save(s)
                print("SSD GUARD READY: automatic reboots apply only while armed; disarm before ROBOT mode.")
                return 0
            obs = Observation(boot, uptime, observe_storage(c), bmo_clear(c))
            s = tick(c, s, obs, save=save)
            flush(c, s, boot, uptime, save)
            print(json.dumps({"phase": s["phase"], "attempts": s["attempts"],
                              "health": obs.health, "pending_notifications": len(s["outbox"])}))
    except (GuardError, OSError, ValueError, TypeError, KeyError, IndexError):
        # Avoid leaking credentials, raw kernel/process output or private filenames.
        print("SSD GUARD ERROR: configuration/state/operation rejected; no further reboot requested. Check permissions, status and the operator guide.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
