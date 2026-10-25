#!/usr/bin/env bash
# BMO NEXUS - read-only system inventory. Does not use sudo or write any files.
set -u

print_header() { printf '\n\n=== %s ===\n' "$1"; }
try_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    "$@" 2>&1 || printf '(non-fatal: command returned an error)\n'
  else
    printf '%s: not installed / not available\n' "$1"
  fi
}

printf '%s\n' 'BMO NEXUS -- read-only Pi inventory'
printf '%s\n' 'No installations, restarts, service changes, file writes, or privileged operations.'
printf '%s\n' 'Review output for internal paths, usernames, and IPs before sharing. NEVER share API keys or tokens.'

print_header 'OS and CPU architecture'
try_cmd uname -a
if [ -r /etc/os-release ]; then
  grep -E '^(PRETTY_NAME|ID|VERSION_ID|VERSION_CODENAME)=' /etc/os-release || true
fi
try_cmd uname -m

print_header 'Hardware storage devices and mounts'
try_cmd lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS
try_cmd findmnt -no SOURCE,TARGET,FSTYPE /
try_cmd df -hT

print_header 'RAM and uptime'
try_cmd free -h
try_cmd uptime

print_header 'Enabled/running service inventory (first 100 entries)'
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-units --type=service --state=running --no-pager --plain 2>&1 | head -n 100 || true
  printf '\n--- BMO/web-related unit names (names only, not unit contents) ---\n'
  systemctl list-unit-files --type=service --no-pager --plain 2>/dev/null | grep -Ei 'bmo|fish|voice|docker|cloudflared|tail|nginx|apache|caddy|ssh|sftpgo' || true
else
  printf '%s\n' 'systemctl: not available'
fi

print_header 'Listening TCP sockets (could show local IPs; review before sharing)'
try_cmd ss -lnt

print_header 'Installed tool binaries and versions'
for tool in gh git docker cloudflared tailscale restic python3; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf '%s: %s\n' "$tool" "$(command -v "$tool")"
  else
    printf '%s: not found\n' "$tool"
  fi
done
if command -v docker >/dev/null 2>&1; then
  printf '\nDocker client version:\n'
  docker --version 2>&1 || true
  docker compose version 2>&1 || true
fi

print_header 'Current BMO process names (only command names, no arguments)'
if command -v ps >/dev/null 2>&1; then
  ps -eo pid=,comm= | grep -Ei 'bmo|chrom|python|fish|node|audio|docker|cloudflare' | head -n 50 || true
fi

printf '\n%s\n' 'Inspection complete. No settings were changed.'
