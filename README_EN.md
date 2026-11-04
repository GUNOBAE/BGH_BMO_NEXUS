# BMO NEXUS

[한국어](README.md) | **English** | [Original BMO robot](https://github.com/GUNOBAE/BGH_BMO)

**BMO NEXUS** reuses the hardware of a custom Raspberry Pi 5 BMO robot as a personal file cloud, GitHub backup host, and web development lab when the robot is not being demonstrated.

**Design principle:** one Raspberry Pi, one operating system, three mutually exclusive application modes.

| Mode | Robot software | Server apps | Public tunnel |
|---|---|---|---|
| ROBOT | Active | Stopped | Off |
| SERVER | Stopped | Active | Only approved demos |
| MAINTENANCE | Stopped | Stopped | Off |

Hardware: Raspberry Pi 5 with 8 GB RAM, 1 TB NVMe SSD, and 512 GB microSD. The baseline confirmed SD root; later owner reports confirmed the SSD mount and private-server use. A week without observed SSD dead events after adding 5V was reported on 2026-11-05; this is not proof of a resolved hardware cause.

Planned subsystems: lifecycle controller, private file access (SFTPGo candidate), Git mirror + restic backup, a tightly scoped public web lab, system monitoring, and separate Tailscale/private versus Cloudflare Tunnel/public access.

**Security note:** switching applications on a shared OS is not a strong isolation boundary. Public-facing services should be minimally privileged, strictly segmented, and not given access to BMO credentials or private data.

Current status: private-cloud/Tailscale and Git mirrors have owner-reported Pi success; the live mode controller remains pending. New SSD recovery code provides a durable five-request reboot budget, ntfy alerts, and explicit approval for another batch. New installs are disarmed. **Recovery deployment, real reboot testing, and phone delivery are NOT_RUN.** See the [operator guide](docs/16_SSD_RECOVERY_KR.md) and [current roadmap](docs/17_NEXT_STEPS_2026_11_KR.md). Web frontend and portfolio design remain a separate workstream.
