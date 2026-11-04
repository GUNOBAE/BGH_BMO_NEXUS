#!/usr/bin/env python3
"""Read-only JSON status. No ports, credential output, SSD file reads or app starts."""

import json
from pathlib import Path
import sys

import ssd_guard as guard


def collect():
    result = {"schema": 1, "memory_available_mib": None, "memory_total_mib": None,
              "cpu_temperature_c": None, "load_1m": None, "uptime_seconds": None,
              "ssd_guard": {"phase": "unavailable"}, "server_projects": {}}
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])
        result["memory_total_mib"] = round(info["MemTotal"] / 1024)
        result["memory_available_mib"] = round(info["MemAvailable"] / 1024)
        result["load_1m"] = float(Path("/proc/loadavg").read_text().split()[0])
        result["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, KeyError):
        pass
    try:
        result["cpu_temperature_c"] = round(float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        c = guard.validate_config(guard.read_json(guard.CONFIG))
        s = guard.validate_state(guard.read_json(guard.STATE))
        result["ssd_guard"] = guard.snapshot(c, s)
    except (guard.GuardError, OSError, ValueError, TypeError):
        pass
    for project in guard.PROJECTS:
        try:
            output = guard.command(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock",
                      "ps", "--filter", f"label=com.docker.compose.project={project}",
                      "--format", "{{.State}}"], timeout=5)
            states = output.splitlines()
            result["server_projects"][project] = {"running": states.count("running"),
                                                   "other": len(states) - states.count("running")}
        except guard.GuardError:
            result["server_projects"][project] = {"status": "unknown"}
    return result


if __name__ == "__main__":
    if len(sys.argv) != 1:
        sys.exit("Usage: python3 scripts/nexus_status.py")
    print(json.dumps(collect(), indent=2))
