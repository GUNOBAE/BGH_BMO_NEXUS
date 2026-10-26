"""모든 식별자/증거는 테스트가 공급하는 가상 값이다. BMO 프로토콜이 아니다."""

from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    MAINTENANCE = "MAINTENANCE"
    ROBOT = "ROBOT"
    SERVER = "SERVER"


class State(str, Enum):
    UNKNOWN = "UNKNOWN"
    MAINTENANCE = "MAINTENANCE"
    ROBOT_STARTING = "ROBOT_STARTING"
    ROBOT_READY = "ROBOT_READY"
    ROBOT_STOPPING = "ROBOT_STOPPING"
    SERVER_STARTING = "SERVER_STARTING"
    SERVER_READY = "SERVER_READY"
    SERVER_STOPPING = "SERVER_STOPPING"
    ERROR = "ERROR"


class Step(str, Enum):
    IDLE = "IDLE"
    CLOSED = "CLOSED"
    STOP_ACCEPTED = "STOP_ACCEPTED"
    CLEANUP = "CLEANUP"
    FINAL_CLOSED = "FINAL_CLOSED"
    START_ACCEPTED = "START_ACCEPTED"
    READY = "READY"
    PUBLIC_OPEN = "PUBLIC_OPEN"


class AckKind(str, Enum):
    CLOSED = "CLOSED"
    STOP_ACCEPTED = "STOP_ACCEPTED"
    ROOT_EXITED = "ROOT_EXITED"
    CLEANUP_COMPLETE = "CLEANUP_COMPLETE"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    START_ACCEPTED = "START_ACCEPTED"
    READY = "READY"
    PUBLIC_OPENED = "PUBLIC_OPENED"


class Tunnel(str, Enum):
    UNKNOWN = "UNKNOWN"
    CLOSED = "CLOSED"
    OPEN = "OPEN"


class Release(str, Enum):
    RELEASED = "released"
    NOT_ACQUIRED = "not_acquired"
    UNKNOWN = "unknown"


# MCU 정책 증거는 승인된 정리 정책 확인을 뜻한다. 서보 전원 차단의 증명이 아니다.
REQUIRED = {
    Mode.ROBOT: frozenset({
        "chromium", "loopback_server", "audio", "microphone", "camera",
        "rp2040_host", "workers", "runtime_lock", "mcu_policy",
    }),
    Mode.SERVER: frozenset({
        "services", "containers", "backup_workers", "ports", "storage_writers",
        "restart_policy",
    }),
}


@dataclass(frozen=True)
class Policy:
    # 가상 tick 단위이며 Pi의 실제 종료 시간 약속이 아니다.
    timeout: int = 10
    evidence_ttl: int = 20

    def __post_init__(self):
        if self.timeout <= 0 or self.evidence_ttl <= 0:
            raise ValueError("positive fake durations required")


@dataclass(frozen=True)
class Token:
    epoch: int
    transition: int
    step: int
    run: str


@dataclass(frozen=True)
class Identity:
    mode: Mode
    run: str
    slot: int  # 실제 PID가 아닌 가상 슬롯 번호
    birth: int
    epoch: int
    artifact: str = "fixture-v1"
    owner: str = "nexus"


@dataclass(frozen=True)
class TunnelEvidence:
    status: Tunnel = Tunnel.UNKNOWN
    at: int = -1
    scope_verified: bool = False
    configured: bool = False
    empty_inventory_verified: bool = False
    # 테스트가 승인한 route 묶음의 식별자. 빈 목록은 공개 승인 범위가 아니다.
    approved_route_scope: str | None = None


@dataclass(frozen=True)
class SSDEvidence:
    at: int = -1
    revision: int = 0
    mounted: bool = False
    identity_verified: bool = False
    filesystem_verified: bool = False
    writable: bool = False
    healthy: bool = False
    capacity_ok: bool = False
    permissions_ok: bool = False
    backup_approved: bool = False


@dataclass(frozen=True)
class Health:
    at: int
    ready: bool


@dataclass(frozen=True)
class CleanupReport:
    identity: Identity
    resources: tuple[tuple[str, Release], ...]
    not_acquired_proven: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Ack:
    token: Token
    kind: AckKind
    seq: int
    at: int
    identity: Identity | None = None
    cleanup: CleanupReport | None = None


@dataclass(frozen=True)
class Command:
    kind: str
    token: Token
    identity: Identity | None = None
    route_scope: str | None = None
