"""가상 시계/증거만 사용하는 제어기 회귀. 실제 Pi 검증이 아니다."""

from dataclasses import replace
import unittest

from nexus_mock import (
    Ack, AckKind, CleanupReport, FakeClock, FakeWorld, Health, Mode,
    ModeController, Policy, Release, REQUIRED, SSDEvidence, State, Step,
    Tunnel, TunnelEvidence,
)


def fixture():
    world = FakeWorld(FakeClock())
    world.observe_inventory()
    world.baseline_resources_verified = True
    world.owner_policy_verified = True
    world.robot_profile_verified = True
    world.tunnel = TunnelEvidence(Tunnel.CLOSED, 0, True, False, True)
    world.ssd = SSDEvidence(0, 1, True, True, True, True, True, True, True, True)
    controller = ModeController(world)
    assert controller.reconcile()
    return world, controller


def ack(controller, kind, *, seq=1, cleanup=None, **changes):
    message = Ack(
        controller.token, kind, seq, controller.world.clock.now,
        controller.active, cleanup,
    )
    return controller.handle(replace(message, **changes))


def ready(world, controller, target, command_id="start"):
    assert controller.request(target, command_id) is not None
    assert ack(controller, AckKind.CLOSED)
    assert ack(controller, AckKind.START_ACCEPTED)
    world.health[controller.active] = Health(world.clock.now, True)
    assert ack(controller, AckKind.READY)


def report(identity, status=Release.RELEASED):
    names = REQUIRED[identity.mode]
    return CleanupReport(
        identity, tuple((name, status) for name in sorted(names)),
        names if status == Release.NOT_ACQUIRED else frozenset(),
    )


def stop_to_cleanup(world, controller, target, command_id="switch"):
    assert controller.request(target, command_id) is not None
    assert ack(controller, AckKind.CLOSED)
    assert ack(controller, AckKind.STOP_ACCEPTED)
    return controller.active


def complete_cleanup(world, controller, identity, *, cleanup_first=False):
    if cleanup_first:
        assert ack(controller, AckKind.CLEANUP_COMPLETE, cleanup=report(identity))
        world.root_exited(identity, reaped=True)
        assert ack(controller, AckKind.ROOT_EXITED, seq=2)
    else:
        world.root_exited(identity, reaped=True)
        assert ack(controller, AckKind.ROOT_EXITED)
        assert ack(controller, AckKind.CLEANUP_COMPLETE, seq=2, cleanup=report(identity))
    assert controller.step == Step.FINAL_CLOSED


def start_count(world, mode=None):
    return sum(c.kind == "start" and (mode is None or c.identity.mode == mode)
               for c in world.commands)


def configure_public_route(world):
    """실제 주소 없이 승인된 가상 공개 경로 하나만 설정한다."""
    world.tunnel = replace(
        world.tunnel, configured=True, empty_inventory_verified=False,
        approved_route_scope="mock-approved-demo",
    )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.world, self.controller = fixture()

    def assert_failed_without_start(self, previous):
        self.assertEqual(self.controller.state, State.ERROR)
        self.assertFalse(self.controller.public_permission)
        self.assertEqual(start_count(self.world), previous)
        self.assertFalse(self.controller.tick())
        self.assertEqual(start_count(self.world), previous)

    def test_initial_unknown_does_not_assume_stopped(self):
        world = FakeWorld()
        controller = ModeController(world)
        self.assertEqual(controller.state, State.UNKNOWN)
        self.assertIsNone(controller.request(Mode.ROBOT, "unsafe"))
        self.assertEqual(controller.state, State.ERROR)
        self.assertEqual(start_count(world), 0)

    def test_unknown_reconcile_requires_all_explicit_evidence(self):
        for field, value in (("inventory_verified", False),
                             ("inventory_at", -100),
                             ("baseline_resources_verified", False),
                             ("owner_policy_verified", False),
                             ("external_robot", True)):
            with self.subTest(field=field):
                world, _ = fixture()
                setattr(world, field, value)
                controller = ModeController(world)
                self.assertFalse(controller.reconcile())
                self.assertEqual(controller.state, State.ERROR)
                self.assertEqual(start_count(world), 0)

    def test_reconcile_empty_unconfigured_verified_scope_is_closed(self):
        self.assertEqual(self.controller.state, State.MAINTENANCE)
        self.assertFalse(self.world.tunnel.configured)
        self.assertTrue(self.world.tunnel.empty_inventory_verified)
        self.assertEqual(start_count(self.world), 0)

    def test_missing_tunnel_configuration_is_not_proof_closed(self):
        self.world.tunnel = replace(self.world.tunnel, empty_inventory_verified=False)
        self.assertIsNone(self.controller.request(Mode.ROBOT, "r"))
        self.assert_failed_without_start(0)

    def test_normal_two_way_and_maintenance(self):
        w, c = self.world, self.controller
        ready(w, c, Mode.ROBOT)
        self.assertEqual(c.state, State.ROBOT_READY)
        self.assertEqual(c.desired, Mode.ROBOT)
        transition = c.transition
        old = stop_to_cleanup(w, c, Mode.SERVER)
        self.assertEqual(c.state, State.ROBOT_STOPPING)
        self.assertEqual(c.desired, Mode.SERVER)
        self.assertEqual(start_count(w, Mode.SERVER), 0)
        complete_cleanup(w, c, old, cleanup_first=True)
        self.assertEqual(start_count(w, Mode.SERVER), 0)
        self.assertTrue(ack(c, AckKind.CLOSED))
        self.assertTrue(ack(c, AckKind.START_ACCEPTED))
        w.health[c.active] = Health(w.clock.now, True)
        self.assertTrue(ack(c, AckKind.READY))
        self.assertEqual(c.state, State.SERVER_READY)
        self.assertFalse(c.public_permission)
        self.assertGreater(c.transition, transition)
        old = stop_to_cleanup(w, c, Mode.ROBOT, "back")
        complete_cleanup(w, c, old)
        self.assertTrue(ack(c, AckKind.CLOSED))
        self.assertTrue(ack(c, AckKind.START_ACCEPTED))
        w.health[c.active] = Health(w.clock.now, True)
        self.assertTrue(ack(c, AckKind.READY))
        old = stop_to_cleanup(w, c, Mode.MAINTENANCE, "maint")
        complete_cleanup(w, c, old)
        self.assertTrue(ack(c, AckKind.CLOSED))
        self.assertEqual(c.state, State.MAINTENANCE)
        self.assertEqual(w.roots, {})
        self.assertEqual(start_count(w), 3)

    def test_stop_acceptance_does_not_release_resources(self):
        ready(self.world, self.controller, Mode.ROBOT)
        identity = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.assertEqual(self.controller.active, identity)
        self.assertIn(Mode.ROBOT, self.world.roots)
        self.assertEqual(start_count(self.world, Mode.SERVER), 0)
        self.world.clock.advance(self.controller.policy.timeout)
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(1)

    def test_cleanup_without_exit_cannot_start(self):
        ready(self.world, self.controller, Mode.ROBOT)
        old = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.assertTrue(ack(self.controller, AckKind.CLEANUP_COMPLETE, cleanup=report(old)))
        self.assertEqual(self.controller.step, Step.CLEANUP)
        self.assertEqual(start_count(self.world, Mode.SERVER), 0)
        self.world.clock.advance(10)
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(1)

    def test_exit_without_cleanup_cannot_start(self):
        ready(self.world, self.controller, Mode.ROBOT)
        old = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.world.root_exited(old, reaped=True)
        self.assertTrue(ack(self.controller, AckKind.ROOT_EXITED))
        self.assertEqual(self.controller.step, Step.CLEANUP)
        self.world.clock.advance(10)
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(1)

    def test_root_exit_without_reap_is_error(self):
        ready(self.world, self.controller, Mode.ROBOT)
        old = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.world.root_exited(old, reaped=False)
        self.assertFalse(ack(self.controller, AckKind.ROOT_EXITED))
        self.assert_failed_without_start(1)

    def test_explicit_cleanup_failure_blocks_target(self):
        ready(self.world, self.controller, Mode.ROBOT)
        stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.assertFalse(ack(self.controller, AckKind.CLEANUP_FAILED))
        self.assert_failed_without_start(1)

    def test_each_unknown_resource_blocks_target(self):
        for mode in (Mode.ROBOT, Mode.SERVER):
            for resource in sorted(REQUIRED[mode]):
                with self.subTest(mode=mode, resource=resource):
                    w, c = fixture()
                    ready(w, c, mode)
                    old = stop_to_cleanup(w, c, Mode.MAINTENANCE)
                    resources = tuple((r, Release.UNKNOWN if r == resource else Release.RELEASED)
                                      for r in sorted(REQUIRED[mode]))
                    self.assertFalse(ack(c, AckKind.CLEANUP_COMPLETE,
                                         cleanup=CleanupReport(old, resources)))
                    self.assertEqual(c.state, State.ERROR)
                    self.assertEqual(start_count(w), 1)

    def test_not_acquired_requires_run_specific_proof(self):
        ready(self.world, self.controller, Mode.ROBOT)
        old = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        unproven = replace(report(old, Release.NOT_ACQUIRED), not_acquired_proven=frozenset())
        self.assertFalse(ack(self.controller, AckKind.CLEANUP_COMPLETE, cleanup=unproven))
        self.assert_failed_without_start(1)

    def test_all_proven_not_acquired_is_accepted(self):
        ready(self.world, self.controller, Mode.ROBOT)
        old = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.world.root_exited(old, reaped=True)
        self.assertTrue(ack(self.controller, AckKind.ROOT_EXITED))
        self.assertTrue(ack(self.controller, AckKind.CLEANUP_COMPLETE, seq=2,
                            cleanup=report(old, Release.NOT_ACQUIRED)))
        self.assertEqual(self.controller.step, Step.FINAL_CLOSED)

    def test_old_transition_step_run_epoch_and_sequence_are_rejected(self):
        for mutation in ({"epoch": 0}, {"transition": 0}, {"step": 0}, {"run": "old"}):
            with self.subTest(mutation=mutation):
                w, c = fixture()
                c.request(Mode.ROBOT, "r")
                self.assertFalse(ack(c, AckKind.CLOSED, token=replace(c.token, **mutation)))
                self.assertEqual(c.state, State.ERROR)
                self.assertEqual(start_count(w), 0)

    def test_replayed_accepted_message_is_error(self):
        self.controller.request(Mode.ROBOT, "r")
        self.assertTrue(ack(self.controller, AckKind.CLOSED))
        message = Ack(self.controller.token, AckKind.START_ACCEPTED, 1, 0, self.controller.active)
        self.assertTrue(self.controller.handle(message))
        self.assertFalse(self.controller.handle(message))
        self.assert_failed_without_start(1)

    def test_same_step_sequence_replay_is_error(self):
        ready(self.world, self.controller, Mode.ROBOT)
        old = stop_to_cleanup(self.world, self.controller, Mode.SERVER)
        self.assertTrue(ack(self.controller, AckKind.CLEANUP_COMPLETE, cleanup=report(old)))
        self.assertFalse(ack(self.controller, AckKind.CLEANUP_COMPLETE, cleanup=report(old)))
        self.assert_failed_without_start(1)

    def test_pid_slot_reuse_is_detected_without_kill(self):
        ready(self.world, self.controller, Mode.ROBOT)
        original = self.controller.active
        self.world.roots[Mode.ROBOT] = replace(original, birth=original.birth + 1)
        self.assertIsNone(self.controller.request(Mode.SERVER, "conflict"))
        self.assert_failed_without_start(1)
        self.assertFalse(any(c.kind == "stop" for c in self.world.commands))

    def test_external_interference_at_ready_revokes_permission(self):
        configure_public_route(self.world)
        ready(self.world, self.controller, Mode.SERVER)
        self.assertTrue(self.controller.operator_public_approval())
        self.world.tunnel = replace(self.world.tunnel, status=Tunnel.OPEN)
        self.assertTrue(ack(self.controller, AckKind.PUBLIC_OPENED))
        self.world.external_robot = True
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(1)
        self.assertEqual(self.world.tunnel.status, Tunnel.OPEN)  # 닫기 요청 != CLOSED
        self.assertFalse(any(c.kind == "stop" for c in self.world.commands))

    def test_external_interference_before_launch_starts_nothing(self):
        self.controller.request(Mode.SERVER, "s")
        self.world.external_robot = True
        self.assertFalse(ack(self.controller, AckKind.CLOSED))
        self.assert_failed_without_start(0)

    def test_public_open_requires_separate_approval(self):
        ready(self.world, self.controller, Mode.SERVER)
        self.assertFalse(self.controller.public_permission)
        self.assertFalse(any(c.kind == "open_public" for c in self.world.commands))
        self.world.tunnel = TunnelEvidence(Tunnel.OPEN, 0, True, True)
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(1)

    def test_public_approval_in_robot_is_denied(self):
        ready(self.world, self.controller, Mode.ROBOT)
        self.assertFalse(self.controller.operator_public_approval())
        self.assert_failed_without_start(1)

    def test_open_public_is_closed_and_verified_before_stop(self):
        configure_public_route(self.world)
        ready(self.world, self.controller, Mode.SERVER)
        c, w = self.controller, self.world
        self.assertTrue(c.operator_public_approval())
        w.tunnel = replace(w.tunnel, status=Tunnel.OPEN)
        self.assertTrue(ack(c, AckKind.PUBLIC_OPENED))
        self.assertIsNotNone(c.request(Mode.ROBOT, "back"))
        self.assertFalse(c.public_permission)
        self.assertFalse(any(e.kind == "stop" for e in w.commands))
        self.assertFalse(ack(c, AckKind.CLOSED))
        self.assert_failed_without_start(1)

    def test_valid_close_allows_owned_stop_then_target(self):
        configure_public_route(self.world)
        ready(self.world, self.controller, Mode.SERVER)
        c, w = self.controller, self.world
        self.assertTrue(c.operator_public_approval())
        opened = [command for command in w.commands if command.kind == "open_public"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].route_scope, "mock-approved-demo")
        w.tunnel = replace(w.tunnel, status=Tunnel.OPEN)
        self.assertTrue(ack(c, AckKind.PUBLIC_OPENED))
        c.request(Mode.ROBOT, "back")
        w.tunnel = replace(w.tunnel, status=Tunnel.CLOSED)
        self.assertTrue(ack(c, AckKind.CLOSED))
        self.assertEqual(sum(e.kind == "stop" for e in w.commands), 1)
        self.assertEqual(start_count(w, Mode.ROBOT), 0)

    def test_tunnel_unknown_before_launch_is_error(self):
        self.controller.request(Mode.ROBOT, "r")
        self.world.tunnel = TunnelEvidence()
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(0)

    def test_timeout_at_boundary_blocks_start(self):
        self.controller.request(Mode.ROBOT, "r")
        self.world.clock.advance(9)
        self.assertTrue(self.controller.tick())
        self.world.clock.advance(1)
        self.assertFalse(ack(self.controller, AckKind.CLOSED))
        self.assert_failed_without_start(0)

    def test_startup_timeout_does_not_launch_other_mode(self):
        self.controller.request(Mode.ROBOT, "r")
        self.assertTrue(ack(self.controller, AckKind.CLOSED))
        self.world.clock.advance(10)
        self.assertFalse(self.controller.tick())
        self.assertIsNone(self.controller.request(Mode.SERVER, "unsafe"))
        self.assert_failed_without_start(1)

    def test_every_ssd_gate_blocks_start(self):
        for name in ("mounted", "identity_verified", "filesystem_verified", "writable",
                     "healthy", "capacity_ok", "permissions_ok", "backup_approved"):
            with self.subTest(gate=name):
                w, c = fixture()
                w.ssd = replace(w.ssd, **{name: False})
                self.assertIsNone(c.request(Mode.SERVER, "s"))
                self.assertEqual(c.state, State.ERROR)
                self.assertEqual(start_count(w), 0)

    def test_ssd_directory_without_mount_is_not_sufficient(self):
        self.world.ssd = replace(self.world.ssd, mounted=False)
        self.assertIsNone(self.controller.request(Mode.SERVER, "s"))
        self.assert_failed_without_start(0)

    def test_ssd_disconnect_between_request_and_start_is_error(self):
        self.controller.request(Mode.SERVER, "s")
        self.world.ssd = replace(self.world.ssd, mounted=False)
        self.assertFalse(ack(self.controller, AckKind.CLOSED))
        self.assert_failed_without_start(0)

    def test_ssd_replacement_between_request_and_start_is_error(self):
        self.controller.request(Mode.SERVER, "s")
        self.world.ssd = replace(self.world.ssd, revision=2)
        self.assertFalse(ack(self.controller, AckKind.CLOSED))
        self.assert_failed_without_start(0)

    def test_ssd_disconnect_ready_revokes_public_permission(self):
        configure_public_route(self.world)
        ready(self.world, self.controller, Mode.SERVER)
        self.assertTrue(self.controller.operator_public_approval())
        self.world.ssd = replace(self.world.ssd, mounted=False)
        self.assertFalse(self.controller.tick())
        self.assert_failed_without_start(1)

    def test_expired_health_revokes_public_and_never_restarts(self):
        configure_public_route(self.world)
        ready(self.world, self.controller, Mode.SERVER)
        self.assertTrue(self.controller.operator_public_approval())
        self.world.tunnel = replace(self.world.tunnel, status=Tunnel.OPEN)
        self.assertTrue(ack(self.controller, AckKind.PUBLIC_OPENED))
        self.world.clock.advance(21)
        self.world.observe_inventory()
        self.world.tunnel = replace(self.world.tunnel, at=21)
        self.world.ssd = replace(self.world.ssd, at=21)
        self.assertFalse(self.controller.tick())
        self.assertEqual(self.controller.error, "health_unknown_or_unhealthy")
        self.assert_failed_without_start(1)

    def test_ready_requires_independent_health_evidence(self):
        self.controller.request(Mode.ROBOT, "r")
        self.assertTrue(ack(self.controller, AckKind.CLOSED))
        self.assertTrue(ack(self.controller, AckKind.START_ACCEPTED))
        self.assertFalse(ack(self.controller, AckKind.READY))
        self.assert_failed_without_start(1)

    def test_duplicate_user_request_is_idempotent(self):
        tid = self.controller.request(Mode.ROBOT, "one")
        token = self.controller.token
        self.assertEqual(self.controller.request(Mode.ROBOT, "one"), tid)
        self.assertEqual(self.controller.token, token)
        self.assertEqual(start_count(self.world), 0)
        self.assertIsNone(self.controller.request(Mode.SERVER, "one"))
        self.assert_failed_without_start(0)

    def test_command_id_after_timeout_does_not_return_old_acceptance(self):
        c, w = self.controller, self.world
        self.assertIsNotNone(c.request(Mode.ROBOT, "same"))
        w.clock.advance(c.policy.timeout)
        self.assertFalse(c.tick())
        commands = list(w.commands)
        self.assertIsNone(c.request(Mode.ROBOT, "same"))
        self.assertEqual(c.error, "stale_command_id")
        self.assertEqual(w.commands, commands)
        self.assert_failed_without_start(0)

    def test_command_id_from_error_epoch_is_rejected_after_reconcile(self):
        c, w = self.controller, self.world
        old_transition = c.request(Mode.ROBOT, "same")
        old_epoch = c.epoch
        w.clock.advance(c.policy.timeout)
        self.assertFalse(c.tick())
        w.observe_inventory()
        w.observe_baseline_resources()
        w.tunnel = replace(w.tunnel, at=w.clock.now)
        self.assertTrue(c.reconcile())
        self.assertGreater(c.epoch, old_epoch)
        self.assertGreater(c.transition, old_transition)
        commands = list(w.commands)
        self.assertIsNone(c.request(Mode.ROBOT, "same"))
        self.assertEqual(c.error, "stale_command_id")
        self.assert_failed_without_start(0)
        # 실패 응답 뒤에는 다시 전수 관측해야 하며 새 ID로만 시작한다.
        self.assertEqual(w.commands[:-1], commands)
        w.observe_inventory()
        w.observe_baseline_resources()
        self.assertTrue(c.reconcile())
        self.assertIsNotNone(c.request(Mode.ROBOT, "fresh"))
        self.assertTrue(ack(c, AckKind.CLOSED))
        self.assertEqual(start_count(w), 1)

    def test_duplicate_request_checks_timeout_before_cached_acceptance(self):
        c, w = self.controller, self.world
        self.assertIsNotNone(c.request(Mode.ROBOT, "same"))
        w.clock.advance(c.policy.timeout)
        self.assertIsNone(c.request(Mode.ROBOT, "same"))
        self.assertEqual(c.error, "timeout")
        self.assert_failed_without_start(0)

    def test_duplicate_request_in_same_live_transition_stays_idempotent(self):
        c, w = self.controller, self.world
        transition = c.request(Mode.ROBOT, "same")
        self.assertTrue(ack(c, AckKind.CLOSED))
        token = c.token
        commands = list(w.commands)
        self.assertEqual(c.request(Mode.ROBOT, "same"), transition)
        self.assertEqual(c.token, token)
        self.assertEqual(w.commands, commands)
        self.assertTrue(ack(c, AckKind.START_ACCEPTED))
        w.health[c.active] = Health(w.clock.now, True)
        self.assertTrue(ack(c, AckKind.READY))
        commands = list(w.commands)
        self.assertEqual(c.request(Mode.ROBOT, "same"), transition)
        self.assertEqual(w.commands, commands)
        self.assertEqual(c.state, State.ROBOT_READY)

    def test_cached_request_rechecks_current_generation_and_ready_evidence(self):
        cases = (
            ("new_controller", "controller_generation_superseded"),
            ("health_revoked", "health_unknown_or_unhealthy"),
            ("ssd_replaced", "ssd_lost_or_changed"),
            ("external_robot", "external_workload"),
        )
        for change, expected_error in cases:
            with self.subTest(change=change):
                w, c = fixture()
                configure_public_route(w)
                ready(w, c, Mode.SERVER, "same")
                self.assertTrue(c.operator_public_approval())
                w.tunnel = replace(w.tunnel, status=Tunnel.OPEN)
                self.assertTrue(ack(c, AckKind.PUBLIC_OPENED))
                starts = start_count(w)
                stops = sum(command.kind == "stop" for command in w.commands)
                if change == "new_controller":
                    ModeController(w)
                elif change == "health_revoked":
                    w.health[c.active] = Health(w.clock.now, False)
                elif change == "ssd_replaced":
                    w.ssd = replace(w.ssd, revision=w.ssd.revision + 1)
                else:
                    w.external_robot = True
                self.assertIsNone(c.request(Mode.SERVER, "same"))
                self.assertEqual(c.state, State.ERROR)
                self.assertEqual(c.error, expected_error)
                self.assertFalse(c.public_permission)
                self.assertEqual(start_count(w), starts)
                self.assertEqual(start_count(w, Mode.ROBOT), 0)
                self.assertEqual(sum(command.kind == "stop" for command in w.commands), stops)
                self.assertEqual(sum(command.kind == "open_public" for command in w.commands), 1)
                self.assertEqual(w.commands[-1].kind, "close_public")

    def test_completed_command_id_is_stale_after_new_transition(self):
        c, w = self.controller, self.world
        ready(w, c, Mode.ROBOT, "old")
        self.assertIsNotNone(c.request(Mode.SERVER, "new"))
        transition = c.transition
        commands = list(w.commands)
        self.assertIsNone(c.request(Mode.ROBOT, "old"))
        self.assertEqual(c.error, "stale_command_id")
        self.assertEqual(c.transition, transition)
        self.assertEqual(w.commands[:-1], commands)
        self.assert_failed_without_start(1)

    def test_unconfigured_public_route_cannot_approve_publication(self):
        ready(self.world, self.controller, Mode.SERVER)
        self.assertTrue(self.world.tunnel.empty_inventory_verified)
        self.assertFalse(self.world.tunnel.configured)
        self.assertFalse(self.controller.operator_public_approval())
        self.assertFalse(self.controller.public_permission)
        self.assertEqual(self.controller.state, State.SERVER_READY)
        self.assertEqual(self.controller.step, Step.IDLE)
        self.assertFalse(any(c.kind == "open_public" for c in self.world.commands))
        self.assertTrue(self.controller.tick())

    def test_public_route_requires_configuration_and_nonempty_approved_scope(self):
        for configured, scope in ((False, "mock-approved-demo"),
                                  (True, None), (True, ""), (True, "   "),
                                  (True, 42), (True, True)):
            with self.subTest(configured=configured, scope=scope):
                w, c = fixture()
                w.tunnel = replace(
                    w.tunnel, configured=configured, approved_route_scope=scope,
                )
                ready(w, c, Mode.SERVER)
                self.assertFalse(c.operator_public_approval())
                self.assertFalse(c.public_permission)
                self.assertEqual(c.state, State.SERVER_READY)
                self.assertEqual(c.step, Step.IDLE)
                self.assertFalse(any(e.kind == "open_public" for e in w.commands))
                self.assertTrue(c.tick())

    def test_public_route_scope_change_revokes_permission_without_reopening(self):
        for opened in (False, True):
            changes = [{"approved_route_scope": None},
                       {"approved_route_scope": "mock-other-demo"}]
            if not opened:
                changes.append({"configured": False, "empty_inventory_verified": True})
            for change in changes:
                with self.subTest(opened=opened, change=change):
                    w, c = fixture()
                    configure_public_route(w)
                    ready(w, c, Mode.SERVER)
                    self.assertTrue(c.operator_public_approval())
                    if opened:
                        w.tunnel = replace(w.tunnel, status=Tunnel.OPEN)
                        self.assertTrue(ack(c, AckKind.PUBLIC_OPENED))
                    w.tunnel = replace(w.tunnel, **change)
                    self.assertFalse(c.tick())
                    self.assertEqual(c.state, State.ERROR)
                    self.assertEqual(c.error, "public_route_scope_lost_or_changed")
                    self.assertFalse(c.public_permission)
                    self.assertEqual(sum(e.kind == "open_public" for e in w.commands), 1)
                    self.assertEqual(start_count(w), 1)

    def test_ready_robot_profile_revocation_fails_closed(self):
        ready(self.world, self.controller, Mode.ROBOT)
        self.world.robot_profile_verified = False
        self.assertFalse(self.controller.tick())
        self.assertEqual(self.controller.error, "robot_profile_unknown")
        self.assert_failed_without_start(1)

    def test_concurrent_conflicting_request_is_error(self):
        self.controller.request(Mode.ROBOT, "first")
        self.assertIsNone(self.controller.request(Mode.SERVER, "second"))
        self.assert_failed_without_start(0)

    def test_recovery_requires_empty_proven_world_and_new_epoch(self):
        c, w = self.controller, self.world
        c.request(Mode.ROBOT, "r")
        old = c.token
        self.assertFalse(ack(c, AckKind.CLOSED, token=replace(old, run="bad")))
        transition = c.transition
        w.observe_inventory()
        w.observe_baseline_resources()
        self.assertTrue(c.reconcile())
        self.assertGreater(c.epoch, old.epoch)
        self.assertGreater(c.transition, transition)
        self.assertEqual(c.state, State.MAINTENANCE)
        self.assertEqual(c.desired, Mode.MAINTENANCE)
        self.assertEqual(start_count(w), 0)
        c.request(Mode.ROBOT, "new")
        self.assertFalse(c.handle(Ack(old, AckKind.CLOSED, 1, 0)))
        self.assert_failed_without_start(0)

    def test_recovery_does_not_adopt_or_stop_surviving_root(self):
        ready(self.world, self.controller, Mode.ROBOT)
        self.world.tunnel = TunnelEvidence()
        self.assertFalse(self.controller.tick())
        self.world.tunnel = TunnelEvidence(Tunnel.CLOSED, 0, True, False, True)
        self.assertFalse(self.controller.reconcile())
        self.assert_failed_without_start(1)
        self.assertFalse(any(e.kind == "stop" for e in self.world.commands))

    def test_policy_and_clock_reject_invalid_durations(self):
        with self.assertRaises(ValueError):
            Policy(timeout=0)
        with self.assertRaises(ValueError):
            Policy(evidence_ttl=0)
        with self.assertRaises(ValueError):
            self.world.clock.advance(-1)

    def test_new_controller_uses_new_world_epoch_and_starts_unknown(self):
        second = ModeController(self.world)
        self.assertGreater(second.epoch, self.controller.epoch)
        self.assertEqual(second.state, State.UNKNOWN)
        self.assertFalse(second.public_permission)
        self.assertTrue(second.reconcile())
        self.assertGreater(second.transition, self.controller.transition)
        with self.assertRaises(ValueError):
            ModeController(self.world, epoch=self.controller.epoch)

    def test_superseded_controller_cannot_start_or_recover(self):
        ModeController(self.world)
        self.assertIsNone(self.controller.request(Mode.ROBOT, "obsolete"))
        self.assertEqual(self.controller.error, "controller_generation_superseded")
        self.assertFalse(self.controller.reconcile())
        self.assert_failed_without_start(0)

    def test_sequence_gap_and_non_integer_sequences_fail_closed(self):
        for seq in (0, 2, 99, True, "1"):
            with self.subTest(seq=seq):
                w, c = fixture()
                c.request(Mode.ROBOT, "r")
                self.assertFalse(ack(c, AckKind.CLOSED, seq=seq))
                self.assertEqual(c.state, State.ERROR)
                self.assertEqual(start_count(w), 0)

    def test_invalid_command_id_fails_without_type_error(self):
        self.assertIsNone(self.controller.request(Mode.ROBOT, ["unhashable"]))
        self.assert_failed_without_start(0)


if __name__ == "__main__":
    unittest.main()
