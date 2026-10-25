"""실제 호스트 접근 없이 계약 경계에 잘못된 가상 증거를 주입한다."""

from dataclasses import replace
import unittest

from nexus_mock.controller import ModeController
from nexus_mock.fakes import FakeWorld
from nexus_mock.models import (
    Ack, AckKind, CleanupReport, Health, Mode, Release, REQUIRED,
    SSDEvidence, State, Step, Tunnel, TunnelEvidence,
)


def world_fixture():
    world = FakeWorld()
    world.observe_inventory()
    world.baseline_resources_verified = True
    world.owner_policy_verified = True
    world.robot_profile_verified = True
    world.tunnel = TunnelEvidence(Tunnel.CLOSED, 0, True, False, True)
    world.ssd = SSDEvidence(
        at=0, revision=1, mounted=True, identity_verified=True,
        filesystem_verified=True, writable=True, healthy=True,
        capacity_ok=True, permissions_ok=True, backup_approved=True,
    )
    return world


def message(controller, kind, *, seq=1, at=None, identity=None, cleanup=None):
    return Ack(
        controller.token, kind, seq,
        controller.world.clock.now if at is None else at,
        controller.active if identity is None else identity, cleanup,
    )


def send(controller, kind, **kwargs):
    return controller.handle(message(controller, kind, **kwargs))


def ready_fixture(mode=Mode.ROBOT):
    world = world_fixture()
    controller = ModeController(world)
    assert controller.reconcile()
    assert controller.request(mode, "fixture-start") is not None
    assert send(controller, AckKind.CLOSED)
    assert send(controller, AckKind.START_ACCEPTED)
    world.health[controller.active] = Health(world.clock.now, True)
    assert send(controller, AckKind.READY)
    return world, controller


def cleanup_fixture(mode=Mode.ROBOT):
    world, controller = ready_fixture(mode)
    target = Mode.SERVER if mode == Mode.ROBOT else Mode.ROBOT
    assert controller.request(target, "fixture-switch") is not None
    assert send(controller, AckKind.CLOSED)
    assert send(controller, AckKind.STOP_ACCEPTED)
    return world, controller


def released_report(identity):
    return CleanupReport(identity, tuple(
        (name, Release.RELEASED) for name in sorted(REQUIRED[identity.mode])
    ))


def start_count(world, mode):
    return sum(command.kind == "start" and command.identity.mode == mode
               for command in world.commands)


class AdversarialEvidenceTests(unittest.TestCase):
    def assert_blocked(self, world, controller, target):
        self.assertEqual(controller.state, State.ERROR)
        self.assertFalse(controller.public_permission)
        self.assertEqual(start_count(world, target), 0)

    def test_ack_timestamp_before_current_step_is_stale(self):
        world = world_fixture()
        controller = ModeController(world)
        self.assertTrue(controller.reconcile())
        world.clock.advance(1)
        controller.request(Mode.ROBOT, "later-start")
        self.assertFalse(send(controller, AckKind.CLOSED, at=0))
        self.assert_blocked(world, controller, Mode.ROBOT)

    def test_future_timestamp_cannot_supply_evidence(self):
        world = world_fixture()
        controller = ModeController(world)
        controller.reconcile()
        controller.request(Mode.ROBOT, "future")
        self.assertFalse(send(controller, AckKind.CLOSED, at=1))
        self.assert_blocked(world, controller, Mode.ROBOT)

    def test_current_pid_slot_with_changed_identity_cannot_be_stopped(self):
        for field, value in (
            ("birth", 77), ("epoch", 77), ("artifact", "fixture-other"),
            ("run", "fixture-other-run"), ("owner", "external"),
        ):
            with self.subTest(field=field):
                world, controller = ready_fixture()
                original = controller.active
                world.roots[Mode.ROBOT] = replace(original, **{field: value})
                self.assertIsNone(controller.request(Mode.SERVER, "switch"))
                self.assert_blocked(world, controller, Mode.SERVER)
                self.assertFalse(any(c.kind == "stop" for c in world.commands))

    def test_stop_acceptance_does_not_release_resources(self):
        world, controller = cleanup_fixture()
        self.assertEqual(controller.state, State.ROBOT_STOPPING)
        self.assertIn(Mode.ROBOT, world.roots)
        self.assertEqual(start_count(world, Mode.SERVER), 0)
        world.clock.advance(controller.policy.timeout)
        self.assertFalse(controller.tick())
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_complete_resource_ack_does_not_replace_root_reap(self):
        world, controller = cleanup_fixture()
        self.assertTrue(send(
            controller, AckKind.CLEANUP_COMPLETE,
            cleanup=released_report(controller.active),
        ))
        self.assertEqual(controller.step, Step.CLEANUP)
        self.assertIn(Mode.ROBOT, world.roots)
        self.assertEqual(start_count(world, Mode.SERVER), 0)
        world.clock.advance(controller.policy.timeout)
        self.assertFalse(controller.tick())
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_exit_without_reap_is_insufficient(self):
        world, controller = cleanup_fixture()
        world.root_exited(controller.active, reaped=False)
        self.assertFalse(send(controller, AckKind.ROOT_EXITED))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_root_exit_ack_requires_supervisor_observation(self):
        world, controller = cleanup_fixture()
        self.assertFalse(send(controller, AckKind.ROOT_EXITED))
        self.assert_blocked(world, controller, Mode.SERVER)
        self.assertIn(Mode.ROBOT, world.roots)

    def test_reaped_root_without_resource_report_does_not_start_target(self):
        world, controller = cleanup_fixture()
        world.root_exited(controller.active, reaped=True)
        self.assertTrue(send(controller, AckKind.ROOT_EXITED))
        self.assertEqual(controller.step, Step.CLEANUP)
        self.assertEqual(start_count(world, Mode.SERVER), 0)
        world.clock.advance(controller.policy.timeout)
        self.assertFalse(controller.tick())
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_root_and_cleanup_can_arrive_in_either_order(self):
        for root_first in (False, True):
            with self.subTest(root_first=root_first):
                world, controller = cleanup_fixture()
                identity = controller.active
                if root_first:
                    world.root_exited(identity, reaped=True)
                    self.assertTrue(send(controller, AckKind.ROOT_EXITED))
                    self.assertTrue(send(
                        controller, AckKind.CLEANUP_COMPLETE, seq=2,
                        cleanup=released_report(identity),
                    ))
                else:
                    self.assertTrue(send(
                        controller, AckKind.CLEANUP_COMPLETE,
                        cleanup=released_report(identity),
                    ))
                    world.root_exited(identity, reaped=True)
                    self.assertTrue(send(controller, AckKind.ROOT_EXITED, seq=2))
                self.assertEqual(controller.step, Step.FINAL_CLOSED)
                self.assertEqual(start_count(world, Mode.SERVER), 0)
                self.assertTrue(send(controller, AckKind.CLOSED))
                self.assertEqual(start_count(world, Mode.SERVER), 1)
                self.assertEqual(set(world.roots), {Mode.SERVER})

    def test_missing_or_duplicate_resource_is_not_complete(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                world, controller = cleanup_fixture()
                report = released_report(controller.active)
                rows = report.resources
                rows = rows + (rows[0],) if duplicate else rows[1:]
                self.assertFalse(send(
                    controller, AckKind.CLEANUP_COMPLETE,
                    cleanup=replace(report, resources=rows),
                ))
                self.assert_blocked(world, controller, Mode.SERVER)

    def test_any_resource_unknown_blocks_even_after_root_reaped(self):
        for resource in sorted(REQUIRED[Mode.ROBOT]):
            with self.subTest(resource=resource):
                world, controller = cleanup_fixture()
                identity = controller.active
                world.root_exited(identity, reaped=True)
                self.assertTrue(send(controller, AckKind.ROOT_EXITED))
                report = CleanupReport(identity, tuple(
                    (name, Release.UNKNOWN if name == resource else Release.RELEASED)
                    for name in sorted(REQUIRED[Mode.ROBOT])
                ))
                self.assertFalse(send(
                    controller, AckKind.CLEANUP_COMPLETE, seq=2, cleanup=report,
                ))
                self.assert_blocked(world, controller, Mode.SERVER)

    def test_not_acquired_requires_explicit_per_resource_proof(self):
        for proven in (False, True):
            with self.subTest(proven=proven):
                world, controller = cleanup_fixture()
                identity = controller.active
                report = CleanupReport(
                    identity,
                    tuple((name, Release.NOT_ACQUIRED)
                          for name in sorted(REQUIRED[Mode.ROBOT])),
                    REQUIRED[Mode.ROBOT] if proven else frozenset(),
                )
                self.assertEqual(send(
                    controller, AckKind.CLEANUP_COMPLETE, cleanup=report,
                ), proven)
                self.assertEqual(start_count(world, Mode.SERVER), 0)
                if not proven:
                    self.assert_blocked(world, controller, Mode.SERVER)

    def test_cleanup_for_another_run_cannot_complete_current_stop(self):
        world, controller = cleanup_fixture()
        wrong = replace(controller.active, run="fixture-unowned-run")
        self.assertFalse(send(
            controller, AckKind.CLEANUP_COMPLETE, cleanup=released_report(wrong),
        ))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_replayed_root_ack_with_new_sequence_is_still_rejected(self):
        world, controller = cleanup_fixture()
        world.root_exited(controller.active, reaped=True)
        self.assertTrue(send(controller, AckKind.ROOT_EXITED))
        self.assertFalse(send(controller, AckKind.ROOT_EXITED, seq=2))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_late_ready_cannot_resurrect_stopping_robot(self):
        world, controller = ready_fixture()
        old_token = controller.token
        old_identity = controller.active
        controller.request(Mode.SERVER, "stop-before-late-ready")
        self.assertFalse(controller.handle(Ack(
            old_token, AckKind.READY, 2, world.clock.now, old_identity,
        )))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_previous_close_ack_cannot_approve_final_close(self):
        world, controller = ready_fixture()
        controller.request(Mode.SERVER, "switch")
        old_close = message(controller, AckKind.CLOSED)
        self.assertTrue(controller.handle(old_close))
        self.assertTrue(send(controller, AckKind.STOP_ACCEPTED))
        identity = controller.active
        world.root_exited(identity, reaped=True)
        self.assertTrue(send(controller, AckKind.ROOT_EXITED))
        self.assertTrue(send(
            controller, AckKind.CLEANUP_COMPLETE, seq=2,
            cleanup=released_report(identity),
        ))
        self.assertEqual(controller.step, Step.FINAL_CLOSED)
        self.assertFalse(controller.handle(old_close))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_unconfigured_tunnel_requires_verified_empty_scope(self):
        world = world_fixture()
        world.tunnel = replace(world.tunnel, empty_inventory_verified=False)
        controller = ModeController(world)
        self.assertFalse(controller.reconcile())
        self.assert_blocked(world, controller, Mode.ROBOT)

    def test_unknown_tunnel_is_never_reported_closed_after_error(self):
        world, controller = ready_fixture()
        world.tunnel = replace(world.tunnel, status=Tunnel.UNKNOWN)
        self.assertFalse(controller.tick())
        self.assertEqual(world.tunnel.status, Tunnel.UNKNOWN)
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_ssd_can_fail_between_request_and_actual_start(self):
        world = world_fixture()
        controller = ModeController(world)
        self.assertTrue(controller.reconcile())
        controller.request(Mode.SERVER, "ssd-disconnect")
        world.ssd = replace(world.ssd, mounted=False)
        self.assertFalse(send(controller, AckKind.CLOSED))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_healthy_but_replaced_ssd_cannot_reuse_planned_mount_evidence(self):
        world = world_fixture()
        controller = ModeController(world)
        self.assertTrue(controller.reconcile())
        controller.request(Mode.SERVER, "ssd-replaced")
        world.ssd = replace(world.ssd, revision=2)
        self.assertFalse(send(controller, AckKind.CLOSED))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_root_exit_before_close_ack_has_no_unowned_stop_or_exception(self):
        world, controller = ready_fixture()
        controller.request(Mode.SERVER, "root-left-before-stop")
        world.root_exited(controller.active, reaped=True)
        self.assertFalse(send(controller, AckKind.CLOSED))
        self.assert_blocked(world, controller, Mode.SERVER)
        self.assertFalse(any(c.kind == "stop" for c in world.commands))

    def test_closed_observation_before_close_request_is_insufficient(self):
        world = world_fixture()
        controller = ModeController(world)
        self.assertTrue(controller.reconcile())
        world.clock.advance(1)
        controller.request(Mode.ROBOT, "old-closed-observation")
        # ACK는 새 tick이지만 observer의 CLOSED는 요청보다 오래된 관측이다.
        self.assertFalse(send(controller, AckKind.CLOSED))
        self.assert_blocked(world, controller, Mode.ROBOT)

    def test_ssd_identity_revision_change_in_ready_revokes_public(self):
        world, controller = ready_fixture(Mode.SERVER)
        self.assertTrue(controller.operator_public_approval())
        starts_before = start_count(world, Mode.SERVER)
        world.ssd = replace(world.ssd, revision=2)
        self.assertFalse(controller.tick())
        self.assertEqual(controller.state, State.ERROR)
        self.assertFalse(controller.public_permission)
        self.assertEqual(start_count(world, Mode.SERVER), starts_before)
        self.assertEqual(start_count(world, Mode.ROBOT), 0)

    def test_external_robot_before_start_is_not_adopted_or_stopped(self):
        world = world_fixture()
        controller = ModeController(world)
        self.assertTrue(controller.reconcile())
        controller.request(Mode.SERVER, "external-before-start")
        world.external_robot = True
        self.assertFalse(send(controller, AckKind.CLOSED))
        self.assert_blocked(world, controller, Mode.SERVER)
        self.assertFalse(any(c.kind == "stop" for c in world.commands))

    def test_external_robot_during_server_ready_revokes_permission_only(self):
        world, controller = ready_fixture(Mode.SERVER)
        self.assertTrue(controller.operator_public_approval())
        world.external_robot = True
        self.assertFalse(controller.tick())
        self.assertEqual(controller.state, State.ERROR)
        self.assertFalse(controller.public_permission)
        self.assertEqual(start_count(world, Mode.ROBOT), 0)
        self.assertFalse(any(c.kind == "stop" for c in world.commands))

    def test_owner_policy_loss_prevents_new_transition(self):
        world, controller = ready_fixture()
        world.owner_policy_verified = False
        self.assertIsNone(controller.request(Mode.SERVER, "policy-lost"))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_unexpected_root_disappearance_does_not_imply_cleanup(self):
        world, controller = ready_fixture()
        world.roots.clear()
        self.assertFalse(controller.tick())
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_error_reconcile_will_not_adopt_remaining_owned_root(self):
        world, controller = ready_fixture()
        world.tunnel = replace(world.tunnel, status=Tunnel.UNKNOWN)
        controller.tick()
        epoch_before = controller.epoch
        transition_before = controller.transition
        world.tunnel = TunnelEvidence(Tunnel.CLOSED, 0, True, False, True)
        self.assertFalse(controller.reconcile())
        self.assertEqual(controller.state, State.ERROR)
        self.assertGreater(controller.epoch, epoch_before)
        self.assertGreater(controller.transition, transition_before)
        self.assertFalse(any(c.kind == "stop" for c in world.commands))

    def test_root_exit_does_not_revalidate_pre_start_resource_baseline(self):
        world, controller = cleanup_fixture()
        identity = controller.active
        report = released_report(identity)
        report = replace(report, resources=tuple(
            (name, Release.UNKNOWN if name == "camera" else status)
            for name, status in report.resources
        ))
        self.assertFalse(send(
            controller, AckKind.CLEANUP_COMPLETE, cleanup=report,
        ))
        world.root_exited(identity, reaped=True)
        world.observe_inventory()
        # 첫 실행 전의 baseline=True는 미확정 camera 정리를 증명하지 못한다.
        self.assertFalse(controller.reconcile())
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_wrong_step_ack_cannot_skip_stop_acceptance(self):
        world, controller = ready_fixture()
        controller.request(Mode.SERVER, "skip-step")
        self.assertTrue(send(controller, AckKind.CLOSED))
        self.assertEqual(controller.step, Step.STOP_ACCEPTED)
        self.assertFalse(send(
            controller, AckKind.CLEANUP_COMPLETE,
            cleanup=released_report(controller.active),
        ))
        self.assert_blocked(world, controller, Mode.SERVER)

    def test_same_command_id_for_new_target_is_conflict(self):
        world = world_fixture()
        controller = ModeController(world)
        controller.reconcile()
        first = controller.request(Mode.ROBOT, "same-command")
        self.assertEqual(controller.request(Mode.ROBOT, "same-command"), first)
        self.assertIsNone(controller.request(Mode.SERVER, "same-command"))
        self.assert_blocked(world, controller, Mode.SERVER)
        self.assertEqual(start_count(world, Mode.ROBOT), 0)


if __name__ == "__main__":
    unittest.main()
