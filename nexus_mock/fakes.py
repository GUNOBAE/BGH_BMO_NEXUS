"""직접 주입한 가상 관측과 메모리 명령 기록만 제공한다. OS 호출은 없다."""

from dataclasses import dataclass, field

from .models import Command, Health, Identity, Mode, SSDEvidence, Token, TunnelEvidence


@dataclass
class FakeClock:
    now: int = 0

    def advance(self, ticks: int):
        if ticks < 0:
            raise ValueError("fake time cannot move backwards")
        self.now += ticks


@dataclass
class FakeWorld:
    clock: FakeClock = field(default_factory=FakeClock)
    inventory_verified: bool = False
    inventory_at: int = -1
    baseline_resources_verified: bool = False
    owner_policy_verified: bool = False
    robot_profile_verified: bool = False
    external_robot: bool = False
    roots: dict[Mode, Identity] = field(default_factory=dict)
    # 둘 다 확인해야 하므로 종료와 wait/reap을 별도 가상 사실로 보존한다.
    exits: dict[Identity, tuple[bool, bool]] = field(default_factory=dict)
    health: dict[Identity, Health] = field(default_factory=dict)
    tunnel: TunnelEvidence = field(default_factory=TunnelEvidence)
    ssd: SSDEvidence = field(default_factory=SSDEvidence)
    commands: list[Command] = field(default_factory=list)
    _birth: int = 0
    _epoch: int = 0
    transition_counter: int = 0

    @property
    def current_epoch(self) -> int:
        return self._epoch

    def new_transition(self) -> int:
        self.transition_counter += 1
        return self.transition_counter

    def new_epoch(self, requested: int | None = None) -> int:
        """같은 가상 세계 안에서 재생성한 제어기의 세대 재사용을 막는다."""
        value = self._epoch + 1 if requested is None else requested
        if value <= self._epoch:
            raise ValueError("mock epoch must increase")
        self._epoch = value
        return value

    def observe_inventory(self):
        """테스트가 명시적으로 완전한 가상 목록을 관측했다고 선언한다."""
        self.inventory_verified = True
        self.inventory_at = self.clock.now

    def observe_baseline_resources(self):
        """새 전수 자원 확인을 테스트가 명시적으로 선언한다. 부재 추정이 아니다."""
        self.baseline_resources_verified = True

    def record(self, kind: str, token: Token, identity: Identity | None = None,
               *, route_scope: str | None = None):
        self.commands.append(Command(kind, token, identity, route_scope))

    def start(self, mode: Mode, token: Token) -> Identity:
        if self.roots or self.external_robot:
            raise ValueError("fake supervisor refuses overlap")
        self._birth += 1
        identity = Identity(mode, token.run, self._birth, self._birth, token.epoch)
        self.baseline_resources_verified = False
        self.roots[mode] = identity
        self.record("start", token, identity)
        return identity

    def stop(self, token: Token, identity: Identity):
        if identity.owner != "nexus" or self.roots.get(identity.mode) != identity:
            raise ValueError("fake supervisor refuses unowned stop")
        self.record("stop", token, identity)

    def root_exited(self, identity: Identity, *, reaped: bool):
        """테스트용 관측. 실행 중인 프로세스에 신호를 보내지 않는다."""
        if self.roots.get(identity.mode) == identity:
            del self.roots[identity.mode]
        self.exits[identity] = (True, reaped)
