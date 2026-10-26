"""Issue #7의 fail-closed 순수 Mock 상태기계. 실제 호스트 어댑터는 없다."""

from .fakes import FakeWorld
from .models import (
    Ack, AckKind, Identity, Mode, Policy, Release, REQUIRED, State, Step, Token,
    Tunnel,
)


class ModeController:
    def __init__(self, world: FakeWorld, *, epoch: int | None = None, policy: Policy = Policy()):
        self.world = world
        self.policy = policy
        self.epoch = world.new_epoch(epoch)
        self.transition = world.transition_counter
        self.step_number = 0
        self.state = State.UNKNOWN  # 관측 상태: 요청만으로 READY/정리 완료가 되지 않는다.
        self.desired = Mode.MAINTENANCE
        self.step = Step.IDLE
        self.token = Token(self.epoch, self.transition, 0, "mock-idle")
        self.active: Identity | None = None
        self.public_permission = False
        self._public_route_scope: str | None = None
        self.error: str | None = None
        self.deadline: int | None = None
        self.history: list[tuple[int, State, str]] = []
        self._last_seq = 0
        self._cleanup_ok = False
        self._root_ok = False
        self._ssd_revision: int | None = None
        self._planned_ssd_revision: int | None = None
        self._step_started_at = world.clock.now
        # 실패/복구 뒤에도 기록을 남겨 과거 ID가 새 요청으로 수락되지 않게 한다.
        self._requests: dict[str, tuple[Mode, int, int]] = {}

    def _fresh(self, at: int) -> bool:
        return 0 <= self.world.clock.now - at <= self.policy.evidence_ttl

    def _closed(self) -> bool:
        tunnel = self.world.tunnel
        return (
            tunnel.status == Tunnel.CLOSED and tunnel.scope_verified
            and self._fresh(tunnel.at)
            and (tunnel.configured or tunnel.empty_inventory_verified)
        )

    def _ssd_ok(self) -> bool:
        s = self.world.ssd
        return self._fresh(s.at) and all((
            s.mounted, s.identity_verified, s.filesystem_verified, s.writable,
            s.healthy, s.capacity_ok, s.permissions_ok, s.backup_approved,
        ))

    def _public_route_approved(self) -> bool:
        tunnel = self.world.tunnel
        scope = tunnel.approved_route_scope
        return tunnel.configured and isinstance(scope, str) and bool(scope.strip())

    def _fail(self, reason: str) -> bool:
        # 닫기 요청은 CLOSED 관측을 위조하지 않는다. 남아 있는 root도 죽이지 않는다.
        if self.state != State.ERROR:
            self.world.record("close_public", self.token)
            self.history.append((self.transition, State.ERROR, reason))
            # 기존 empty 증거가 살아 있는 worker/카메라 등의 UNKNOWN을 지우면 안 된다.
            self.world.baseline_resources_verified = False
            self.world.inventory_verified = False
        self.public_permission = False
        self._public_route_scope = None
        self.state = State.ERROR
        self.error = reason
        self.deadline = None
        return False

    def _set_step(self, step: Step, run: str):
        self.step_number += 1
        self.step = step
        self.token = Token(self.epoch, self.transition, self.step_number, run)
        self.deadline = self.world.clock.now + self.policy.timeout
        self._step_started_at = self.world.clock.now
        self._last_seq = 0

    def _idle(self):
        self.step = Step.IDLE
        self.deadline = None

    def _inventory_ok(self) -> bool:
        w = self.world
        if not w.inventory_verified or not self._fresh(w.inventory_at):
            return self._fail("inventory_unknown")
        if not w.owner_policy_verified:
            return self._fail("owner_policy_unknown")
        if w.external_robot or any(i.owner != "nexus" for i in w.roots.values()):
            return self._fail("external_workload")
        expected = {self.active.mode: self.active} if self.active else {}
        for mode, identity in w.roots.items():
            if expected.get(mode) != identity:
                return self._fail("identity_changed_or_overlap")
        if self.active and w.roots.get(self.active.mode) != self.active:
            stopping = self.state in (State.ROBOT_STOPPING, State.SERVER_STOPPING)
            if not stopping or self.world.exits.get(self.active, (False, False))[0] is not True:
                return self._fail("root_disappeared_without_evidence")
        return True

    def _guard(self) -> bool:
        if self.state == State.ERROR:
            return False
        if self.epoch != self.world.current_epoch:
            return self._fail("controller_generation_superseded")
        if not self._inventory_ok():
            return False
        tunnel = self.world.tunnel
        if (tunnel.status == Tunnel.UNKNOWN or not self._fresh(tunnel.at)
                or not tunnel.scope_verified
                or (not tunnel.configured and not tunnel.empty_inventory_verified)
                or (tunnel.status == Tunnel.OPEN and not tunnel.configured)):
            return self._fail("tunnel_unknown")
        open_allowed = (
            self.step == Step.CLOSED
            or (self.state == State.SERVER_READY and self.public_permission)
        )
        if tunnel.status == Tunnel.OPEN and not open_allowed:
            return self._fail("public_open_without_permission")
        if self.public_permission and (
                not self._public_route_approved()
                or tunnel.approved_route_scope != self._public_route_scope):
            return self._fail("public_route_scope_lost_or_changed")
        if self.active and self.active.mode == Mode.ROBOT:
            if not self.world.robot_profile_verified:
                return self._fail("robot_profile_unknown")
        if self.active and self.active.mode == Mode.SERVER:
            if not self._ssd_ok() or self.world.ssd.revision != self._ssd_revision:
                return self._fail("ssd_lost_or_changed")
        if self.state in (State.ROBOT_READY, State.SERVER_READY):
            health = self.world.health.get(self.active)
            if not health or not health.ready or not self._fresh(health.at):
                return self._fail("health_unknown_or_unhealthy")
        return True

    def tick(self) -> bool:
        """대기하지 않는다. 테스트가 시간을 진행한 후 호출한다."""
        if self.state == State.ERROR:
            return False
        if self.deadline is not None and self.world.clock.now >= self.deadline:
            return self._fail("timeout")
        return self._guard()

    def reconcile(self) -> bool:
        """새 가상 조사로만 UNKNOWN/ERROR를 벗어난다. 이전 모드 자동 재개는 없다."""
        if self.epoch != self.world.current_epoch:
            return self._fail("controller_generation_superseded")
        if self.state not in (State.UNKNOWN, State.ERROR):
            return self._fail("reconcile_while_busy")
        if self.state == State.ERROR:
            self.epoch = self.world.new_epoch()
        self.transition = self.world.new_transition()
        self.active = None
        self._ssd_revision = None
        self._planned_ssd_revision = None
        self.public_permission = False
        self._public_route_scope = None
        self.state = State.UNKNOWN
        self.desired = Mode.MAINTENANCE
        self.error = None
        self._idle()
        self._set_step(Step.CLOSED, "mock-reconcile")
        if not self._inventory_ok():
            return False
        if not self.world.baseline_resources_verified:
            return self._fail("baseline_resources_unknown")
        if not self._closed():
            return self._fail("tunnel_not_verified_closed")
        self.state = State.MAINTENANCE
        self._idle()
        self.history.append((self.transition, self.state, "reconciled"))
        return True

    def request(self, target: Mode, command_id: str) -> int | None:
        """현재 세대/전환의 유효한 요청만 멱등적이다. 과거 ID 재사용은 거부한다."""
        if not isinstance(target, Mode) or not isinstance(command_id, str) or not command_id:
            self._fail("invalid_request")
            return None
        previous = self._requests.get(command_id)
        if previous:
            previous_target, epoch, transition = previous
            if previous_target != target:
                self._fail("command_id_reused")
                return None
            if self.state == State.ERROR or epoch != self.epoch or transition != self.transition:
                self._fail("stale_command_id")
                return None
            # 캐시 hit도 deadline·세대·현재 증거를 검사해야 수락처럼 보이지 않는다.
            return transition if self.tick() else None
        if self.state not in (State.MAINTENANCE, State.ROBOT_READY, State.SERVER_READY) or self.step != Step.IDLE:
            self._fail("request_while_busy_or_unknown")
            return None
        if not self._guard():
            return None
        if target == Mode.SERVER:
            if not self._ssd_ok():
                self._fail("ssd_preflight_failed")
                return None
            self._planned_ssd_revision = self.world.ssd.revision
        self.transition = self.world.new_transition()
        self._requests[command_id] = (target, self.epoch, self.transition)
        self.desired = target
        if (target == Mode.MAINTENANCE and self.active is None
                or self.active and self.active.mode == target):
            return self.transition
        self.public_permission = False
        self._public_route_scope = None
        self._cleanup_ok = False
        self._root_ok = False
        run = self.active.run if self.active else self._new_run()
        self._set_step(Step.CLOSED, run)
        if self.active:
            self.state = State.ROBOT_STOPPING if self.active.mode == Mode.ROBOT else State.SERVER_STOPPING
        else:
            self.state = State.ROBOT_STARTING if target == Mode.ROBOT else State.SERVER_STARTING
        self.world.record("close_public", self.token)
        return self.transition

    def _new_run(self) -> str:
        return f"mock-run-{self.epoch}-{self.transition}"

    def _start_target(self) -> bool:
        if not self._closed() or not self._inventory_ok() or self.world.roots:
            return self._fail("start_gate_not_closed_or_empty")
        if self.desired == Mode.MAINTENANCE:
            self.state = State.MAINTENANCE
            self._idle()
            self.history.append((self.transition, self.state, "cleanup_verified"))
            return True
        if self.desired == Mode.SERVER:
            if (not self._ssd_ok()
                    or self.world.ssd.revision != self._planned_ssd_revision):
                return self._fail("ssd_preflight_failed")
            self._ssd_revision = self.world.ssd.revision
        elif not self.world.robot_profile_verified:
            return self._fail("robot_preflight_unknown")
        self._set_step(Step.START_ACCEPTED, self._new_run())
        self.active = self.world.start(self.desired, self.token)
        self.state = State.ROBOT_STARTING if self.desired == Mode.ROBOT else State.SERVER_STARTING
        return True

    def _cleanup_complete(self, ack: Ack) -> bool:
        report = ack.cleanup
        if not report or report.identity != self.active:
            return self._fail("cleanup_identity_unknown")
        resources = dict(report.resources)
        if len(resources) != len(report.resources) or set(resources) != REQUIRED[self.active.mode]:
            return self._fail("cleanup_resource_inventory_incomplete")
        for name, status in resources.items():
            if status == Release.RELEASED:
                continue
            if status == Release.NOT_ACQUIRED and name in report.not_acquired_proven:
                continue
            return self._fail("resource_unknown_or_unproven")
        self._cleanup_ok = True
        return self._after_cleanup()

    def _after_cleanup(self) -> bool:
        if self._cleanup_ok and self._root_ok:
            if self.world.roots or self.world.exits.get(self.active) != (True, True):
                return self._fail("root_not_terminated_and_reaped")
            self.active = None
            self._ssd_revision = None
            self._set_step(Step.FINAL_CLOSED, self._new_run())
            self.world.record("close_public", self.token)
        return True

    def handle(self, ack: Ack) -> bool:
        """현재 token/순서에 맞는 가상 메시지 한 개만 처리한다."""
        if not self.tick():
            return False
        if (self.step == Step.IDLE or ack.token != self.token
                or type(ack.seq) is not int or ack.seq != self._last_seq + 1
                or not self._fresh(ack.at)
                or ack.at < self._step_started_at):
            return self._fail("stale_or_replayed_ack")
        self._last_seq = ack.seq
        if self.step not in (Step.CLOSED, Step.FINAL_CLOSED):
            if not self.active or ack.identity != self.active:
                return self._fail("ack_identity_mismatch")
        if ack.kind == AckKind.CLEANUP_FAILED:
            return self._fail("cleanup_failed")
        if self.step in (Step.CLOSED, Step.FINAL_CLOSED) and ack.kind == AckKind.CLOSED:
            if not self._closed() or self.world.tunnel.at < self._step_started_at:
                return self._fail("close_request_is_not_closed")
            if self.active:
                if self.world.roots.get(self.active.mode) != self.active:
                    return self._fail("root_gone_before_stop_request")
                self._set_step(Step.STOP_ACCEPTED, self.active.run)
                self.world.stop(self.token, self.active)
                return True
            return self._start_target()
        if self.step == Step.STOP_ACCEPTED and ack.kind == AckKind.STOP_ACCEPTED:
            self._set_step(Step.CLEANUP, self.active.run)
            return True
        if self.step == Step.CLEANUP:
            if ack.kind == AckKind.CLEANUP_COMPLETE:
                if self._cleanup_ok:
                    return self._fail("replayed_cleanup")
                return self._cleanup_complete(ack)
            if ack.kind == AckKind.ROOT_EXITED:
                if self._root_ok or self.world.exits.get(self.active) != (True, True):
                    return self._fail("root_exit_unverified_or_replayed")
                self._root_ok = True
                return self._after_cleanup()
        if self.step == Step.START_ACCEPTED and ack.kind == AckKind.START_ACCEPTED:
            self._set_step(Step.READY, self.active.run)
            return True
        if self.step == Step.READY and ack.kind == AckKind.READY:
            health = self.world.health.get(self.active)
            if not health or not health.ready or not self._fresh(health.at):
                return self._fail("ready_without_health")
            self.state = State.ROBOT_READY if self.active.mode == Mode.ROBOT else State.SERVER_READY
            self._idle()
            self.history.append((self.transition, self.state, "ready_verified"))
            return True
        if self.step == Step.PUBLIC_OPEN and ack.kind == AckKind.PUBLIC_OPENED:
            if self.world.tunnel.status != Tunnel.OPEN or not self.public_permission:
                return self._fail("public_open_unverified")
            self._idle()
            return True
        return self._fail("unexpected_ack_kind_or_step")

    def operator_public_approval(self) -> bool:
        """별도 사용자 승인도 가상으로만 표현한다. 실제 공개 경로는 열지 않는다."""
        if not self.tick():
            return False
        if self.state != State.SERVER_READY or self.step != Step.IDLE or not self._closed():
            return self._fail("public_approval_outside_private_server_ready")
        if not self._public_route_approved():
            # 검증된 빈 경로는 비공개 SERVER_READY를 유지할 근거일 뿐이다.
            return False
        self.public_permission = True
        self._public_route_scope = self.world.tunnel.approved_route_scope
        self._set_step(Step.PUBLIC_OPEN, self.active.run)
        self.world.record("open_public", self.token, self.active,
                          route_scope=self._public_route_scope)
        return True
