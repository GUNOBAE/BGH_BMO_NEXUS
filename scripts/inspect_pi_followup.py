#!/usr/bin/env python3
"""Pi 후속 관측: 읽기 전용 명령의 필요한 필드만 stdout으로 표시한다."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from dataclasses import dataclass
from collections import Counter


COMMANDS = {
    "block": ("lsblk", "--bytes", "--json", "--output", "NAME,TYPE,SIZE,FSTYPE"),
    "root": ("findmnt", "--json", "--mountpoint", "/", "--output", "SOURCE,FSTYPE"),
    "mounts": ("findmnt", "--json", "--list", "--output", "SOURCE,FSTYPE"),
    "process": ("ps", "-eo", "comm="),
}
for _scope in ("system", "user"):
    _prefix = ("systemctl", "--user") if _scope == "user" else ("systemctl",)
    COMMANDS[f"{_scope}_running"] = _prefix + (
        "list-units", "--type=service", "--state=running",
        "--no-pager", "--plain", "--no-legend",
    )
    COMMANDS[f"{_scope}_files"] = _prefix + (
        "list-unit-files", "--type=service", "--no-pager", "--no-legend",
    )

# 임의 이름에 토큰/주소가 포함될 수 있어 알려진 이름만 표시한다.
NVME = re.compile(r"nvme[0-9]+n[0-9]+(?:p[0-9]+)?", re.ASCII)
NAMESPACE = re.compile(r"nvme[0-9]+n[0-9]+", re.ASCII)
BLOCK_SOURCE = re.compile(
    r"/dev/(?:nvme[0-9]+n[0-9]+(?:p[0-9]+)?|mmcblk[0-9]+(?:p[0-9]+)?|sd[a-z]+[0-9]*)",
    re.ASCII,
)
FSTYPES = frozenset((
    "ext2", "ext3", "ext4", "btrfs", "xfs", "vfat", "exfat", "ntfs", "ntfs3",
    "f2fs", "swap", "squashfs", "tmpfs", "overlay", "zfs", "fuseblk", "iso9660",
    "crypto_LUKS", "LVM2_member", "linux_raid_member",
))
PROCESSES = frozenset((
    "bmo", "python", "python3", "node", "nodejs", "chromium", "chromium-browse",
    "chromium-brows", "chrome", "ollama", "Xorg", "Xwayland", "wayfire", "labwc",
    "weston", "wayvnc", "pulseaudio", "pipewire", "wireplumber", "fish",
))
SERVICES = frozenset((
    "bmo-case-fan.service", "ssh.service", "sshd.service", "tailscaled.service",
    "wayvnc.service", "ollama.service", "NetworkManager.service", "docker.service",
    "cloudflared.service", "restic.service", "nginx.service", "apache2.service",
    "caddy.service", "pipewire.service", "pulseaudio.service", "wireplumber.service",
))
UNIT_STATES = frozenset((
    "enabled", "enabled-runtime", "disabled", "static", "masked", "masked-runtime",
    "alias", "indirect", "generated", "transient", "linked", "linked-runtime", "bad",
))
STATUS = {
    "missing": "명령 없음: 미확인 (자동 설치하지 않음)",
    "error": "조회 실패: 권한/세션/지원 여부 미확인",
    "timeout": "조회 시간 초과: 미확인",
    "invalid": "응답 형식 확인 불가: 미확인",
}


@dataclass(frozen=True)
class Probe:
    status: str
    stdout: str = ""


def run_command(argv):
    """명령 목록을 고정하고 shell/권한 상승/원본 오류 출력을 사용하지 않는다."""
    if argv not in COMMANDS.values():
        return Probe("error")
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", check=False, timeout=8,
            env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
        )
    except FileNotFoundError:
        return Probe("missing")
    except subprocess.TimeoutExpired:
        return Probe("timeout")
    except OSError:
        return Probe("error")
    # 실패한 명령의 stdout도 부분 결과/비밀값이 있을 수 있어 전부 버린다.
    return Probe("ok", result.stdout) if result.returncode == 0 else Probe("error")


def nonnegative_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,30}", value):
        return int(value)
    return None


def read_sysfs_sectors(name):
    """커널 용량의 단위는 512바이트 섹터다. 고정 sysfs 경로만 읽는다."""
    if not NAMESPACE.fullmatch(name):
        return None
    try:
        with (Path("/sys/class/block") / name / "size").open(encoding="ascii") as handle:
            return nonnegative_int(handle.read(64).strip())
    except (OSError, UnicodeError, ValueError):
        return None


def json_rows(probe, field):
    if probe.status != "ok":
        return probe.status, []
    try:
        payload = json.loads(probe.stdout)
        rows = payload[field]
        if not isinstance(rows, list):
            raise ValueError
        flattened = []

        def walk(items):
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError
                flattened.append(item)
                children = item.get("children", [])
                if not isinstance(children, list):
                    raise ValueError
                walk(children)

        walk(rows)
        return "ok", flattened
    except (ValueError, KeyError, TypeError, RecursionError):
        return "invalid", []


def fstype(value):
    if value is None or value == "":
        return "미검출 (빈 디스크라는 뜻 아님)"
    return value if isinstance(value, str) and value in FSTYPES else "기타/비공개"


def status_text(status):
    return STATUS.get(status, STATUS["invalid"])


def service_lines(probe, running):
    if probe.status != "ok":
        return [status_text(probe.status)]
    visible = set()
    hidden = set()
    for line in probe.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[0].endswith(".service"):
            continue
        unit = fields[0]
        if unit not in SERVICES:
            if "bmo" in unit.lower():
                hidden.add(unit)
            continue
        if running:
            if fields[1:4] == ["loaded", "active", "running"]:
                visible.add(f"{unit}: running")
        elif fields[1] in UNIT_STATES:
            visible.add(f"{unit}: {fields[1]}")
    return sorted(visible) + [
        f"BMO 이름을 포함한 기타 유닛: {len(hidden)}개 (이름 비공개)",
        "허용 목록 밖의 이름은 생략됨. 이 목록만으로 런처/의존성/안전 종료를 확정할 수 없음.",
    ]


def build_report(run=run_command, read_size=read_sysfs_sectors):
    lines = [
        "BMO NEXUS — 읽기 전용 후속 관측",
        "설치/서비스 제어/마운트/파일 저장 없음. 원본 오류/인자/환경/로그를 출력하지 않음.",
        "이 결과는 한 시점의 제한된 관측이며 정상 판정이나 모드 전환 승인이 아닙니다.",
        "\n[루트 파일시스템]",
    ]
    root_status, roots = json_rows(run(COMMANDS["root"]), "filesystems")
    if root_status == "ok" and len(roots) == 1:
        source = roots[0].get("source")
        safe_source = source if isinstance(source, str) and BLOCK_SOURCE.fullmatch(source) else "기타/비공개"
        lines.append(f"SOURCE={safe_source}, FSTYPE={fstype(roots[0].get('fstype'))}")
    else:
        lines.append(status_text(root_status if root_status != "ok" else "invalid"))

    lines.append("\n[NVMe 용량·파일시스템·마운트 관측]")
    block_status, blocks = json_rows(run(COMMANDS["block"]), "blockdevices")
    mount_status, mounts = json_rows(run(COMMANDS["mounts"]), "filesystems")
    if mount_status != "ok":
        lines.append(f"마운트 조회: {status_text(mount_status)}")
    if block_status != "ok":
        lines.append(status_text(block_status))
    else:
        selected = [b for b in blocks if isinstance(b.get("name"), str) and NVME.fullmatch(b["name"])]
        if not selected:
            lines.append("관측 목록에 NVMe 이름이 미보고됨. 물리적 부재/빈 디스크로 단정하지 않음.")
        for block in selected:
            name = block["name"]
            size = nonnegative_int(block.get("size"))
            kind = block.get("type") if block.get("type") in ("disk", "part") else "미확인"
            size_text = str(size) if size is not None else "미확인"
            lines.append(f"{name}: TYPE={kind}, SIZE_BYTES={size_text}, FSTYPE={fstype(block.get('fstype'))}")
            if mount_status == "ok":
                count = sum(m.get("source") == f"/dev/{name}" for m in mounts)
                lines.append(f"  동일 SOURCE의 마운트 레코드={count} (경로 비공개; 별칭/mapper 등은 미확인)")
            if NAMESPACE.fullmatch(name):
                sectors = nonnegative_int(read_size(name))
                lines.append("  sysfs sectors=" + (str(sectors) if sectors is not None else "미확인"))
                if sectors is not None:
                    lines.append(f"  sysfs bytes (sectors × 512)={sectors * 512}")
                    if size is not None and size != sectors * 512:
                        lines.append("  용량 관측 불일치: 시점/인식 상태와 원인 미확인.")
                children = [nonnegative_int(b.get("size")) for b in selected
                            if re.fullmatch(re.escape(name) + r"p[0-9]+", b["name"])]
                if size == 0:
                    lines.append("  namespace 0B: SSD 가용성/기존 데이터 미확인. 초기화/마운트/설치 진행 금지.")
                if size is not None and any(s is not None and s > size for s in children):
                    lines.append("  namespace보다 큰 파티션 보고: 용량 불일치, 원인 미확인.")
    lines.append("양수 용량/파일시스템 표시도 데이터 무결성·안정성·서버 사용 가능의 증거가 아닙니다.")
    lines.append("마운트 레코드 수는 서비스에 필요한 실제 파일시스템 마운트 검사를 대체하지 않습니다.")

    lines.append("\n[프로세스 이름: 알려진 이름과 개수만]")
    process = run(COMMANDS["process"])
    if process.status != "ok":
        lines.append(status_text(process.status))
    else:
        counts = Counter()
        for row in process.stdout.splitlines():
            fields = row.split()
            if len(fields) != 1:
                continue
            name = fields[0]
            if name in PROCESSES or re.fullmatch(r"python3\.[0-9]{1,2}", name):
                counts[name] += 1
        lines.extend(f"NAME={name} COUNT={counts[name]}" for name in sorted(counts))
        lines.append(f"표시 {sum(counts.values())}개. 임의 이름은 생략됨. Python/Node/브라우저가 BMO라는 뜻은 아닙니다.")
    for scope in ("system", "user"):
        label = "시스템" if scope == "system" else "현재 사용자 세션 (다른 사용자는 미확인)"
        for suffix, title in (("running", "실행 중"), ("files", "유닛 파일 상태")):
            lines.append(f"\n[{label}: {title}]")
            lines.extend(service_lines(run(COMMANDS[f"{scope}_{suffix}"]), suffix == "running"))
    lines.append("\n관측 시도 종료. 미확인 항목은 docs/11_FOLLOWUP_INSPECTION_KR.md에 따라 소유자가 검토합니다.")
    return "\n".join(lines)


def main():
    if len(sys.argv) != 1:
        print("이 점검기는 인자를 받지 않습니다. bash scripts/inspect_pi_followup.sh 로 실행하세요.")
        return 2
    try:
        print(build_report())
        return 0
    except Exception:
        # 예상하지 못한 형식에서도 경로/명령 출력이 들어간 traceback을 공개하지 않는다.
        print("점검 처리 실패: 미확인. 원본 오류는 개인정보 보호를 위해 출력하지 않습니다.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
