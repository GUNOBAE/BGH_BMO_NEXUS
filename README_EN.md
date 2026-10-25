# BMO NEXUS

[한국어](README.md) | **English** | [Original BMO robot](https://github.com/GUNOBAE/BGH_BMO)

**BMO NEXUS** reuses the hardware of a custom Raspberry Pi 5 BMO robot as a personal file cloud, GitHub backup host, and web development lab when the robot is not being demonstrated.

**Design principle:** one Raspberry Pi, one operating system, three mutually exclusive application modes.

| Mode | Robot software | Server apps | Public tunnel |
|---|---|---|---|
| ROBOT | Active | Stopped | Off |
| SERVER | Stopped | Active | Only approved demos |
| MAINTENANCE | Stopped | Stopped | Off |

Hardware: Raspberry Pi 5 with 8 GB RAM, 1 TB NVMe SSD, and 512 GB microSD. The actual system partition, mount points, existing autostart services, and current health are **not yet audited**. No repartitioning or reinstall is proposed before that audit.

Planned subsystems: lifecycle controller, private file access (SFTPGo candidate), Git mirror + restic backup, a tightly scoped public web lab, system monitoring, and separate Tailscale/private versus Cloudflare Tunnel/public access.

**Security note:** switching applications on a shared OS is not a strong isolation boundary. Public-facing services should be minimally privileged, strictly segmented, and not given access to BMO credentials or private data.

Current status: **design only; not deployed or field-verified**. Web frontend and portfolio design are a separate workstream. Read [the implementation plan](docs/01_IMPLEMENTATION_PLAN_KR.md) and [architecture/mode policy](docs/03_MODE_CONTROLLER_KR.md) before changing the Pi.
