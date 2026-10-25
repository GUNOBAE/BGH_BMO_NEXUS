# BMO NEXUS — Codex / contributor working agreements

## Project scope
This repository implements the **SERVER-side infrastructure and mode lifecycle** for a Raspberry Pi 5-based BMO robot. Its sibling `GUNOBAE/BGH_BMO` and local BMO integration repositories are developed independently by another Codex session.

- **Work only in `BGH_BMO_NEXUS`.** Do not edit, move, reformat, restart, or overwrite BMO robot projects and their approved R10 face/voice/camera/RP2040 integration.
- Website UI/frontend, portfolio design, and app features belong to a separate chat/repository. Here: server infrastructure, data storage, backup, security, lifecycle, operational docs/tests.
- New features in small reviewable branches/PRs. No deployment or claimed real-Pi testing without explicit evidence.
- Begin by reading `README.md`, `docs/01_IMPLEMENTATION_PLAN_KR.md`, `docs/03_MODE_CONTROLLER_KR.md`, `docs/07_STATUS_AND_ASSUMPTIONS_KR.md`, and `docs/09_PI_BASELINE_KR.md`.

## Verified hardware snapshot (2026-10-25)
- Raspberry Pi 5, 8 GiB RAM; Debian 13 (trixie), aarch64; SD card `/dev/mmcblk0p2` is the root filesystem.
- **NVMe device `nvme0n1` reported size 0B while `nvme0n1p1` was listed as 931.5G**, with no detected filesystem or mount in the inventory. Treat SSD availability and data integrity as **unknown**, not empty or safe to initialize.
- `tailscaled`, `ssh`, `wayvnc`, `ollama`, and `bmo-case-fan.service` are running; Docker and cloudflared are not installed.
- Existing BMO GUI/conversation entrypoint and shutdown lifecycle are NOT yet verified. Do not invent service names or use broad kill commands.

## Safety gates
1. **No real-Pi configuration changes without explicit owner approval after showing exact commands and rollback path**. Initial tasks are read-only inspection, documentation, and offline/mock-tested utilities.
2. No `mkfs`, `fdisk` writes, `wipefs`, `dd`, repartitioning, mounting with write, `fstab` edits, OS migration, `apt upgrade`, or unattended installers until a separate reviewed phase.
3. Do not modify/stop/disable SSH, Tailscale, NetworkManager, VNC or `bmo-case-fan.service`; do not kill Ollama or robot processes until the robot owner has verified their use and lifecycle.
4. Never open WAN/router port forwards, Cloudflare public tunnels, SSH/VNC management endpoints, or Docker socket. Docker installation and exposed ports require a separate approval checkpoint. Binding `0.0.0.0` does not prove internet exposure.
5. Fail closed: ROBOT and SERVER workloads must not overlap; public tunnel must remain OFF outside an explicitly healthy SERVER mode; mount-required services must verify a real filesystem mount rather than only a directory.
6. Never commit tokens, `.env`, private files, personal backups, IPs, tailnet addresses, credentials, serial numbers, or raw diagnostic logs. Be alert to `ps` arguments and systemd `Environment=` exposing secrets.
7. Prefer minimal runtime dependencies and least privilege. Do not claim containers equal a separate OS/security boundary.

## Review and tests
- For shell code, run `bash -n` and, if available, `shellcheck`. For Python code, run appropriate offline tests; mock subprocess/system interactions rather than mutating the Pi.
- Every PR: intent, changed files, threat/rollback considerations, test evidence (distinguish local mock from real Pi), and Pi manual validation steps.
- Korean-first explanations, beginner-friendly comments: explain **what command does / why / what normal results mean**.
- If missing critical facts, report what is missing and add safe read-only observation steps. Do not guess and do not silently install/change host packages.

## First Codex task
See GitHub issues for **Read-only follow-up inspection: NVMe + BMO lifecycle**. Implement observation-only scripts/docs and tests on a dedicated branch, submit a PR; do not execute host-changing commands.
