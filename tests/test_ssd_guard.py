"""Offline recovery tests. No network, reboot, Docker or target SSD access."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ssd_guard as guard
from ssd_guard_state import Observation, advance, approve_five, initial


def armed():
    state = initial()
    state.update(phase="WATCHING", incident="incident-1", notification_test="tested")
    return state


def config():
    return dict(version=1, mount="/mnt/ssd", uuid="00000000-1111-2222-3333-444444444444",
                controller="nvme0", namespace="nvme0n1", partition="nvme0n1p1",
                serial_sha256=hashlib.sha256(b"fixture").hexdigest(), bmo_uid=1000,
                ntfy_url="https://ntfy.example", ntfy_topic="random_topic_1234567890", ntfy_token="")


def failing_boot(s, boot):
    for t in (180, 210, 240):
        s, action = advance(s, Observation(boot, t, "dead"))
    return s, action


class PolicyTests(unittest.TestCase):
    def test_exactly_five_reboots_then_pause_on_sixth_boot(self):
        s = armed()
        actions = []
        for index in range(6):
            s, action = failing_boot(s, f"boot-{index}")
            actions.append(action)
            # Simulate disk persistence and a fresh process at every boot.
            s = guard.validate_state(json.loads(json.dumps(s)))
        self.assertEqual(actions, ["reboot"] * 5 + ["stop"])
        self.assertEqual((s["attempts"], s["total_attempts"], s["phase"]), (5, 5, "BLOCKED"))
        self.assertIn("Would you like me to try 5 more?", s["outbox"][0]["message"])
        for index in range(100):
            s, action = advance(s, Observation(f"later-{index}", 300, "dead"))
            self.assertEqual(action, "none")
        self.assertEqual(len(s["outbox"]), 1)

    def test_no_repeated_reboot_in_one_boot(self):
        s, _ = failing_boot(armed(), "one")
        for t in (250, 280, 310, 400, 500):
            s, action = advance(s, Observation("one", t, "dead"))
            self.assertEqual(action, "none")
        self.assertEqual(s["attempts"], 1)
        self.assertEqual(s["reason"], "reboot_not_completed")

    def test_grace_and_spaced_confirmation(self):
        s = armed()
        for t in (1, 100, 179, 180, 180, 190, 209, 210, 239):
            s, action = advance(s, Observation("b", t, "dead"))
            self.assertEqual(action, "none")
        s, action = advance(s, Observation("b", 240, "dead"))
        self.assertEqual(action, "reboot")

    def test_good_sample_breaks_failure_streak(self):
        s = armed()
        for t, health in ((180, "dead"), (210, "dead"), (240, "healthy"), (270, "dead"), (300, "dead")):
            s, action = advance(s, Observation("b", t, health))
            self.assertEqual(action, "none")

    def test_brief_live_does_not_refill_budget(self):
        s, _ = failing_boot(armed(), "a")
        for t in (180, 300, 1700):
            s, action = advance(s, Observation("b", t, "healthy"))
        self.assertEqual(s["attempts"], 1)
        s, _ = advance(s, Observation("b", 1730, "dead"))
        s, _ = advance(s, Observation("b", 1760, "healthy"))
        s, _ = advance(s, Observation("b", 2000, "healthy"))
        self.assertEqual(s["attempts"], 1)

    def test_30_min_stable_resets_consecutive_not_total(self):
        s, _ = failing_boot(armed(), "a")
        for t in range(180, 1981, 30):
            s, _ = advance(s, Observation("b", t, "healthy"))
        self.assertEqual((s["phase"], s["attempts"], s["total_attempts"]), ("WATCHING", 0, 1))
        self.assertEqual(len(s["outbox"]), 1)

    def test_fifth_attempt_can_recover_without_failure_alert(self):
        s = armed()
        for index in range(5):
            s, _ = failing_boot(s, str(index))
        for t in range(180, 1981, 30):
            s, _ = advance(s, Observation("fifth-reboot", t, "healthy"))
        self.assertEqual(s["phase"], "WATCHING")
        self.assertFalse(any("5 more" in e["message"] for e in s["outbox"]))

    def test_boot_change_resets_stability_window(self):
        s, _ = failing_boot(armed(), "a")
        for boot, t in (("b", 180), ("b", 1900), ("c", 180), ("c", 500)):
            s, _ = advance(s, Observation(boot, t, "healthy"))
        self.assertEqual(s["attempts"], 1)

    def test_long_observation_gap_cannot_claim_stability(self):
        s, _ = failing_boot(armed(), "a")
        for t in (180, 1980, 4000):
            s, _ = advance(s, Observation("b", t, "healthy"))
        self.assertEqual(s["attempts"], 1)

    def test_long_observation_gap_breaks_dead_streak(self):
        s = armed()
        for t in (180, 600, 1200):
            s, action = advance(s, Observation("a", t, "dead"))
        self.assertEqual((s["attempts"], action), (0, "none"))

    def test_disarm_cannot_refill_exhausted_budget(self):
        s = armed()
        for i in range(6):
            s, _ = failing_boot(s, str(i))
        s["phase"] = "DISARMED"
        s = approve_five(s, "incident-1")
        self.assertEqual(s["total_attempts"], 5)
        self.assertEqual(s["batch"], 2)

    def test_manual_batch_and_stale_duplicate_approval(self):
        s = armed()
        for i in range(6):
            s, _ = failing_boot(s, str(i))
        with self.assertRaises(ValueError):
            approve_five(s, "old-incident")
        s = approve_five(s, "incident-1")
        self.assertEqual((s["attempts"], s["total_attempts"], s["batch"]), (0, 5, 2))
        with self.assertRaises(ValueError):
            approve_five(s, "incident-1")
        for i in range(6, 12):
            s, action = failing_boot(s, str(i))
        self.assertEqual((s["total_attempts"], action), (10, "stop"))
        with self.assertRaises(ValueError):
            approve_five(s, "incident-1")
        self.assertEqual(approve_five(s, s["incident"])["batch"], 3)

    def test_disarmed_never_reboots(self):
        s = initial()
        for i in range(10):
            s, action = failing_boot(s, str(i))
            self.assertEqual(action, "none")
        self.assertEqual(s["attempts"], 0)

    def test_unsafe_unknown_and_robot_do_not_reboot(self):
        for health in ("unsafe", "unknown"):
            s, action = advance(armed(), Observation("b", 180, health))
            self.assertEqual((s["phase"], action), ("BLOCKED", "stop"))
        s, action = advance(armed(), Observation("b", 180, "dead", False))
        self.assertEqual((s["phase"], action), ("BLOCKED", "none"))

    def test_missing_and_zero_size_use_same_budget(self):
        for health in ("missing", "zero_size"):
            s = armed()
            for t in (180, 210, 240):
                s, action = advance(s, Observation("b", t, health))
            self.assertEqual((s["attempts"], action), (1, "reboot"))


class EffectsTests(unittest.TestCase):
    def ready(self):
        s = armed()
        for t in (180, 210):
            s, _ = advance(s, Observation("b", t, "dead"))
        return s

    def test_persist_before_stop_and_reboot(self):
        events = []
        s = guard.tick(config(), self.ready(), Observation("b", 240, "dead"),
                       save=lambda value: events.append(("save", deepcopy(value))),
                       stop=lambda: events.append(("stop", None)) or True,
                       reboot=lambda: events.append(("reboot", None)), clear=lambda: True)
        self.assertEqual([e[0] for e in events], ["save", "stop", "reboot"])
        self.assertEqual(events[0][1]["attempts"], 1)
        self.assertEqual(events[0][1]["pending_boot"], "b")
        self.assertEqual(s["total_attempts"], 1)

    def test_failed_persistence_prevents_all_side_effects(self):
        stop, reboot = Mock(), Mock()
        with self.assertRaises(OSError):
            guard.tick(config(), self.ready(), Observation("b", 240, "dead"),
                       save=Mock(side_effect=OSError()), stop=stop, reboot=reboot)
        stop.assert_not_called()
        reboot.assert_not_called()

    def test_robot_race_prevents_reboot(self):
        reboot = Mock()
        s = guard.tick(config(), self.ready(), Observation("b", 240, "dead"),
                       save=Mock(), stop=lambda: True, reboot=reboot, clear=lambda: False)
        reboot.assert_not_called()
        self.assertEqual((s["phase"], s["attempts"]), ("BLOCKED", 1))

    def test_reboot_command_failure_latches(self):
        s = guard.tick(config(), self.ready(), Observation("b", 240, "dead"),
                       save=Mock(), stop=lambda: True,
                       reboot=Mock(side_effect=guard.GuardError()), clear=lambda: True)
        self.assertEqual((s["phase"], s["reason"]), ("BLOCKED", "reboot_command_failed"))

    def test_stop_failure_still_uses_bounded_orderly_reboot(self):
        reboot = Mock()
        s = guard.tick(config(), self.ready(), Observation("b", 240, "dead"),
                       save=Mock(), stop=lambda: False, reboot=reboot, clear=lambda: True)
        reboot.assert_called_once()
        self.assertIn("stop_unconfirmed", s["outbox"][0]["id"])

    def test_outbox_survives_offline_and_throttles(self):
        s = armed()
        s["outbox"] = [{"id": "event", "message": "Alert"}]
        save = Mock()
        with patch.object(guard, "notify", return_value=False) as notify:
            guard.flush(config(), s, "b", 300, save)
            guard.flush(config(), s, "b", 330, save)
            self.assertEqual(notify.call_count, 1)
        self.assertEqual(len(s["outbox"]), 1)
        with patch.object(guard, "notify", return_value=True):
            guard.flush(config(), s, "b", 600, save)
        self.assertEqual(s["outbox"], [])

    def test_notify_does_not_send_raw_paths_tokens_in_body(self):
        c = config()
        c["ntfy_token"] = "fake_secret"
        response = Mock(status=200)
        response.read.return_value = json.dumps({"event": "message", "id": "one", "topic": c["ntfy_topic"]}).encode()
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(guard.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(guard.notify(c, "Alert"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://ntfy.example/")
        self.assertNotIn(b"fake_secret", request.data)
        self.assertNotIn(b"/mnt/ssd", request.data)
        self.assertEqual(request.headers["Authorization"], "Bearer fake_secret")

    def test_redirects_disabled(self):
        self.assertIsNone(guard.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))


class ObservationTests(unittest.TestCase):
    def files(self):
        return {"/sys/class/nvme/nvme0/state": "live\n", "/sys/class/nvme/nvme0/serial": "fixture\n",
                "/sys/class/block/nvme0n1/size": "1953525168\n",
                "/sys/class/block/nvme0n1p1/dev": "259:1\n",
                "/proc/self/mountinfo": "44 1 259:1 / /mnt/ssd rw - ext4 /dev/nvme0n1p1 rw\n"}

    def observe(self, files, target="../../nvme0n1p1"):
        def read(path):
            if path not in files:
                raise FileNotFoundError
            return files[path]
        return guard.observe_storage(config(), read=read, readlink=lambda _: target)

    def test_healthy_requires_expected_uuid_mount_and_serial(self):
        self.assertEqual(self.observe(self.files()), "healthy")
        self.assertEqual(self.observe(self.files(), "../../nvme1n1p1"), "unsafe")
        files = self.files()
        files["/sys/class/nvme/nvme0/serial"] = "replacement"
        self.assertEqual(self.observe(files), "unsafe")

    def test_dead_never_reads_ssd_files_or_block_device(self):
        files = self.files()
        files["/sys/class/nvme/nvme0/state"] = "dead"
        allowed = {k: v for k, v in files.items() if k.startswith("/sys/class/nvme/")}
        self.assertEqual(self.observe(allowed), "dead")

    def test_missing_and_zero(self):
        self.assertEqual(self.observe({}), "missing")
        files = self.files()
        files["/sys/class/block/nvme0n1/size"] = "0"
        self.assertEqual(self.observe(files), "zero_size")

    def test_no_mount_wrong_mount_and_readonly_are_not_dead(self):
        for mount in ("", "44 1 179:2 / /mnt/ssd rw - ext4 /dev/mmcblk0p2 rw",
                      "44 1 259:1 / /mnt/ssd ro - ext4 /dev/nvme0n1p1 ro"):
            files = self.files()
            files["/proc/self/mountinfo"] = mount
            self.assertEqual(self.observe(files), "unsafe")

    def test_transient_reset_and_read_error_do_not_reboot(self):
        files = self.files()
        files["/sys/class/nvme/nvme0/state"] = "resetting"
        self.assertEqual(self.observe(files), "unknown")
        self.assertEqual(guard.observe_storage(config(), read=Mock(side_effect=PermissionError())), "unknown")

    def test_sd_root_required_and_nested_ssd_mount_rejected(self):
        root = "1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n"
        guard.check_sd_paths(guard.mount_rows(root), [guard.STATE, guard.CONFIG])
        with self.assertRaises(guard.GuardError):
            guard.check_sd_paths(guard.mount_rows(root + "2 1 259:1 / /var/lib rw - ext4 /dev/nvme0n1p1 rw\n"), [guard.STATE])
        with self.assertRaises(guard.GuardError):
            guard.check_sd_paths(guard.mount_rows(root.replace("mmcblk0p2", "nvme0n1p1")), [guard.STATE])

    def test_corrupt_state_rejected_not_reset(self):
        for key, value in (("attempts", -1), ("attempts", 6), ("attempts", True),
                           ("sample_at", float("nan")), ("outbox", ["bad"]), ("phase", "whatever")):
            s = initial()
            s[key] = value
            with self.assertRaises(guard.GuardError):
                guard.validate_state(s)

    def test_bad_url_and_topic_rejected(self):
        for key, value in (("ntfy_url", "http://ntfy.example"), ("ntfy_url", "https://user:password@ntfy.example"),
                           ("ntfy_url", "https://ntfy.example/topic"), ("ntfy_topic", "short"),
                           ("ntfy_token", "secret\nHeader: value")):
            c = config()
            c[key] = value
            with self.assertRaises(guard.GuardError):
                guard.validate_config(c)

    def test_fingerprint_invalidates_changed_topic(self):
        a, b = config(), config()
        b["ntfy_topic"] += "x"
        self.assertNotEqual(guard.config_fingerprint(a), guard.config_fingerprint(b))

    def test_atomic_write_and_read_with_temp_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            # /tmp is intentionally writable, unlike the real fixed state location.
            with patch.object(guard, "secure_directory"):
                guard.atomic_save(path, initial())
                if os.geteuid() == 0:
                    self.assertEqual(guard.read_json(path), initial())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
