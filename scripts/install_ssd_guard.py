#!/usr/bin/env python3
"""Explicit installer. Does not mount disks, install packages, or start apps."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import secrets
import sys

import private_cloud as cloud
import ssd_guard as guard
from ssd_guard_state import initial

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = Path("/opt/bmo-nexus-ssd-guard")
UNITS = Path("/etc/systemd/system")
WRAPPER = Path("/usr/local/sbin/ssd-guard")


def directory(path, mode):
    if not path.exists():
        path.mkdir(mode=mode)
    guard.secure_directory(path)


def install_file(source, target, mode):
    guard.secure_directory(target.parent)
    # Atomic replacement never follows an existing destination symlink.
    temp = target.with_name(target.name + ".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(source.read_bytes())
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the disarmed SSD guard on SD. See docs/16_SSD_RECOVERY_KR.md first.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print("Plan: copy guard to /opt, private config to /etc/bmo-nexus, persistent counter to /var/lib/bmo-nexus/ssd-guard; install a 30-second systemd timer. New installs remain DISARMED. Existing config/state are preserved. No reboot, SSD writes, fstab changes, packages, app startup or notification sending. Run with --apply to install.")
        return 0
    try:
        if os.geteuid() != 0:
            raise guard.GuardError("root_required")
        rows = guard.mount_rows(Path("/proc/self/mountinfo").read_text())
        guard.check_sd_paths(rows, (PROGRAM, guard.CONFIG, guard.STATE, UNITS, WRAPPER))
        for binary in ("/usr/bin/systemctl", "/usr/bin/docker", "/usr/bin/python3"):
            if not os.access(binary, os.X_OK):
                raise guard.GuardError("required_program_missing")
        config_exists, state_exists = guard.CONFIG.exists(), guard.STATE.exists()
        if config_exists != state_exists:
            raise guard.GuardError("partial_install_preserve_state")
        if config_exists:
            c = guard.validate_config(guard.read_json(guard.CONFIG))
            s = guard.validate_state(guard.read_json(guard.STATE))
            if s["phase"] != "DISARMED":
                raise guard.GuardError("disarm_before_update")
            if not guard.bmo_clear(c):
                raise guard.GuardError("bmo_not_clear")
        else:
            settings = cloud.load_settings(ROOT / "deploy/private-cloud/.env")
            source, _, _ = cloud.check_storage(settings)
            cloud.check_bmo_stopped(settings)
            partition = Path(source).name
            namespace = re.fullmatch(r"(nvme[0-9]+n[0-9]+)p[0-9]+", partition)[1]
            controller = re.fullmatch(r"(nvme[0-9]+)n[0-9]+", namespace)[1]
            serial = Path(f"/sys/class/nvme/{controller}/serial").read_text().strip()
            if not serial:
                raise guard.GuardError("serial_missing")
            c = guard.validate_config(dict(version=1, mount=str(settings.mount), uuid=settings.uuid,
                     controller=controller, namespace=namespace, partition=partition,
                     serial_sha256=hashlib.sha256(serial.encode()).hexdigest(), bmo_uid=settings.bmo_uid,
                     ntfy_url="https://ntfy.sh", ntfy_topic="bmo-" + secrets.token_hex(16), ntfy_token=""))
            s = initial()
        directory(Path("/etc/bmo-nexus"), 0o700)
        directory(Path("/var/lib/bmo-nexus"), 0o700)
        directory(guard.STATE_DIR, 0o700)
        directory(PROGRAM, 0o755)
        # Timer is stopped during upgrades; lock also excludes an in-flight manual tick.
        guard.command(["/usr/bin/systemctl", "stop", "nexus-ssd-guard.timer"], timeout=10) if config_exists else None
        with guard.state_lock():
            if config_exists:
                if guard.validate_state(guard.read_json(guard.STATE))["phase"] != "DISARMED":
                    raise guard.GuardError("state_changed")
            for name in ("ssd_guard.py", "ssd_guard_state.py", "private_cloud.py"):
                install_file(ROOT / "scripts" / name, PROGRAM / name, 0o644)
            for name in ("nexus-ssd-guard.service", "nexus-ssd-guard.timer"):
                install_file(ROOT / "deploy/ssd-guard" / name, UNITS / name, 0o644)
            install_file(ROOT / "deploy/ssd-guard/ssd-guard", WRAPPER, 0o755)
            if not config_exists:
                guard.atomic_save(guard.CONFIG, c)
                guard.atomic_save(guard.STATE, s)
        guard.command(["/usr/bin/systemctl", "daemon-reload"])
        guard.command(["/usr/bin/systemctl", "enable", "--now", "nexus-ssd-guard.timer"])
        print("INSTALLED, DISARMED. Existing counters/config were preserved. Set up ntfy, run test-notify, then explicitly arm for SERVER mode.")
        print("Show the private topic locally: sudo python3 -c 'import json; print(json.load(open(\"/etc/bmo-nexus/ssd-guard.json\"))[\"ntfy_topic\"])'")
    except (guard.GuardError, cloud.PrivateCloudError, OSError, ValueError, TypeError, IndexError, KeyError):
        print("INSTALL REJECTED: check SD root, healthy SSD, stopped BMO, existing state and permissions. No state was intentionally reset; retain existing files for diagnosis.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
