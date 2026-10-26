"""가짜 CLI·프로세스·상태만 쓰는 수동 클라우드 세션 검사. Pi 실행 증거가 아니다."""

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "FAKE_PRIVATE_SENTINEL"


def load_session():
    spec = importlib.util.spec_from_file_location(
        "cloud_session_test_helper", ROOT / "scripts/cloud_session.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]), \
         patch("subprocess.run", side_effect=AssertionError("import cannot execute CLI")), \
         patch("subprocess.Popen", side_effect=AssertionError("import cannot start processes")):
        spec.loader.exec_module(module)
    return module


def foreground(session_id="fake-owned-session", host="fake-node.example.ts.net:443"):
    return {"Foreground": {session_id: {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
    }}}


class FakeRuntime:
    """모든 동작은 이벤트 기록 또는 예외이며 호스트 자원을 사용하지 않는다."""

    def __init__(self, session):
        self.session = session
        self.events = []
        self.failures = {}
        self.snapshots = [({}, {})]
        self.child = object()
        self.lease = session.Lease("fake-owned-session", "fake-node.example.ts.net")

    def action(self, name):
        self.events.append(name)
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def snapshot(self):
        self.action("snapshot")
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def check_tailscale(self):
        self.action("check_tailscale")

    def containers_absent(self):
        self.action("containers_absent")

    def cloud_up(self, settings):
        self.action("cloud_up")

    def health(self):
        self.action("health")

    def start_proxy(self):
        self.action("start_proxy")
        return self.child

    def proxy_ready(self, child):
        assert child is self.child
        self.action("proxy_ready")
        return self.lease

    def watch(self, child, settings, lease):
        assert child is self.child
        assert lease is self.lease
        self.action("watch")

    def stop_proxy(self, child):
        assert child is self.child
        self.action("stop_proxy")

    def verify_closed(self, lease):
        assert lease is None or lease is self.lease
        self.action("verify_closed")

    def cloud_down(self):
        self.action("cloud_down")


class SessionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = load_session()

    def setUp(self):
        self.runtime = FakeRuntime(self.session)
        self.settings = object()

    def run_session(self, *, confirmed=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch("subprocess.run", side_effect=AssertionError("fake session cannot run CLI")), \
             patch("subprocess.Popen", side_effect=AssertionError("fake session cannot spawn")):
            self.session.run_session(self.settings, confirmed=confirmed, runtime=self.runtime)
        return out.getvalue() + err.getvalue()

    def test_manual_bmo_confirmation_precedes_any_observation_or_mutation(self):
        with self.assertRaises(self.session.SessionError):
            self.run_session(confirmed=False)
        self.assertEqual(self.runtime.events, [])

    def test_one_session_orders_health_share_close_verification_and_scoped_down(self):
        self.run_session()
        events = self.runtime.events
        ordered = ("check_tailscale", "containers_absent", "cloud_up", "health", "start_proxy",
                   "proxy_ready", "watch", "stop_proxy", "verify_closed", "cloud_down")
        self.assertEqual([name for name in events if name != "snapshot"], list(ordered))
        self.assertGreaterEqual(events.count("snapshot"), 2)
        self.assertLess(events.index("snapshot"), events.index("cloud_up"))

    def test_existing_serve_funnel_background_or_unknown_configuration_is_not_adopted(self):
        existing = (
            {"TCP": {"443": {"HTTPS": True}}},
            {"Web": {"other.example.test:443": {"Handlers": {}}}},
            {"AllowFunnel": {"other.example.test:443": True}},
            foreground("another-session"),
            {"Services": {"svc:other": {}}},
            {"Unexpected": {"secret": PRIVATE}},
        )
        for config in existing:
            for side in (0, 1):
                with self.subTest(config=config, side=side):
                    self.runtime = FakeRuntime(self.session)
                    snapshot = [{}, {}]
                    snapshot[side] = config
                    self.runtime.snapshots = [tuple(snapshot)]
                    with self.assertRaises(self.session.SessionError):
                        self.run_session()
                    self.assertNotIn("cloud_up", self.runtime.events)
                    self.assertNotIn("cloud_down", self.runtime.events)
                    self.assertNotIn("start_proxy", self.runtime.events)

    def test_existing_nexus_containers_are_not_taken_over_or_stopped(self):
        self.runtime.failures["containers_absent"] = self.session.SessionError("existing_project")
        with self.assertRaises(self.session.SessionError):
            self.run_session()
        self.assertNotIn("cloud_up", self.runtime.events)
        self.assertNotIn("cloud_down", self.runtime.events)

    def test_bmo_busy_or_ssd_rejection_never_starts_serve(self):
        for code in ("bmo_busy_or_unknown", "ssd_rejected"):
            with self.subTest(code=code):
                self.runtime = FakeRuntime(self.session)
                self.runtime.failures["cloud_up"] = self.session.SessionError(code)
                with self.assertRaises(self.session.SessionError):
                    self.run_session()
                self.assertNotIn("start_proxy", self.runtime.events)
                self.assertNotIn("stop_proxy", self.runtime.events)

    def test_health_failure_cleans_backend_without_starting_a_proxy(self):
        self.runtime.failures["health"] = self.session.SessionError("health_failed")
        with self.assertRaises(self.session.SessionError):
            self.run_session()
        self.assertNotIn("start_proxy", self.runtime.events)
        self.assertIn("cloud_down", self.runtime.events)

    def test_external_config_appearing_before_serve_launch_is_preserved(self):
        self.runtime.snapshots = [({}, {}), (foreground("external-session"), foreground("external-session"))]
        with self.assertRaises(self.session.SessionError):
            self.run_session()
        self.assertIn("cloud_up", self.runtime.events)
        self.assertIn("cloud_down", self.runtime.events)
        self.assertNotIn("start_proxy", self.runtime.events)
        self.assertNotIn("stop_proxy", self.runtime.events)

    def test_exec_failure_still_closes_the_backend_and_never_signals_an_unowned_child(self):
        self.runtime.failures["start_proxy"] = self.session.SessionError("exec_failed")
        with self.assertRaises(self.session.SessionError):
            self.run_session()
        self.assertIn("cloud_down", self.runtime.events)
        self.assertNotIn("stop_proxy", self.runtime.events)

    def test_failed_readiness_stops_created_child_and_backend(self):
        self.runtime.failures["proxy_ready"] = self.session.SessionError("proxy_not_ready")
        with self.assertRaises(self.session.SessionError):
            self.run_session()
        self.assertIn("stop_proxy", self.runtime.events)
        self.assertIn("verify_closed", self.runtime.events)
        self.assertIn("cloud_down", self.runtime.events)

    def test_cleanup_failure_never_reports_session_off_and_still_removes_the_backend(self):
        for stage in ("stop_proxy", "verify_closed", "cloud_down"):
            with self.subTest(stage=stage):
                self.runtime = FakeRuntime(self.session)
                self.runtime.failures[stage] = self.session.SessionError("cleanup_failed")
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output), self.assertRaises(self.session.SessionError):
                    self.run_session()
                self.assertIn("cloud_down", self.runtime.events)
                self.assertNotIn("SESSION OFF", output.getvalue())

    def test_ctrl_c_after_ready_closes_proxy_before_backend(self):
        self.runtime.failures["watch"] = KeyboardInterrupt()
        output = self.run_session()
        self.assertIn("SESSION OFF", output)
        self.assertLess(self.runtime.events.index("stop_proxy"), self.runtime.events.index("verify_closed"))
        self.assertLess(self.runtime.events.index("verify_closed"), self.runtime.events.index("cloud_down"))

    def test_interruption_during_startup_still_checks_backend_shutdown(self):
        self.runtime.failures["cloud_up"] = KeyboardInterrupt()
        self.run_session()
        self.assertNotIn("start_proxy", self.runtime.events)
        self.assertIn("cloud_down", self.runtime.events)

    def test_interruption_before_readiness_still_stops_created_child(self):
        self.runtime.failures["proxy_ready"] = KeyboardInterrupt()
        self.run_session()
        self.assertIn("stop_proxy", self.runtime.events)
        self.assertIn("verify_closed", self.runtime.events)
        self.assertIn("cloud_down", self.runtime.events)

    def test_signal_during_spawn_is_deferred_until_owned_child_handle_is_saved(self):
        installed = {}

        def install(number, handler):
            previous = installed.get(number, signal.SIG_DFL)
            installed[number] = handler
            return previous

        def interrupted_spawn():
            self.runtime.action("start_proxy")
            installed[signal.SIGTERM](signal.SIGTERM, None)
            return self.runtime.child

        self.runtime.start_proxy = interrupted_spawn
        with patch.object(self.session.signal, "signal", side_effect=install), \
             patch.object(self.session.signal, "pthread_sigmask", side_effect=AssertionError("child must not inherit a blocked signal mask")):
            self.run_session()
        self.assertNotIn("proxy_ready", self.runtime.events)
        self.assertIn("stop_proxy", self.runtime.events)
        self.assertIn("verify_closed", self.runtime.events)
        self.assertIn("cloud_down", self.runtime.events)
        self.assertTrue(all(value == signal.SIG_DFL for value in installed.values()))


class StatusValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = load_session()

    def test_both_official_empty_json_forms_are_accepted(self):
        for raw in ("null", "{}"):
            with self.subTest(raw=raw):
                self.assertEqual(self.session.parse_status(raw), {})

    def test_invalid_or_unknown_status_never_becomes_an_empty_configuration(self):
        values = ("not-json " + PRIVATE, "[]", '"' + PRIVATE + '"',
                  '{"TCP":[]}', '{"Foreground":[]}', '{"Unexpected":null}')
        for raw in values:
            with self.subTest(raw=raw):
                with self.assertRaises(self.session.SessionError) as error:
                    self.session.parse_status(raw)
                self.assertNotIn(PRIVATE, str(error.exception))

    def test_foreground_lease_requires_one_https_root_route_to_the_loopback_backend(self):
        snapshot = foreground()
        lease = self.session.inspect_lease((snapshot, snapshot))
        self.assertEqual(lease.session_id, "fake-owned-session")
        self.assertEqual(lease.host, "fake-node.example.ts.net")

    def test_other_route_proxy_tcp_funnel_or_multiple_sessions_cannot_become_our_lease(self):
        cases = []
        config = foreground()
        config["Foreground"]["second-session"] = {}
        cases.append(config)
        config = foreground()
        config["AllowFunnel"] = {"fake-node.example.ts.net:443": True}
        cases.append(config)
        config = foreground()
        config["Foreground"]["fake-owned-session"]["Web"]["fake-node.example.ts.net:443"]["Handlers"]["/other"] = {"Proxy": "http://127.0.0.1:9999"}
        cases.append(config)
        config = foreground()
        config["Foreground"]["fake-owned-session"]["Web"]["fake-node.example.ts.net:443"]["Handlers"]["/"]["Proxy"] = "http://remote.example.test:8080"
        cases.append(config)
        config = foreground()
        config["Foreground"]["fake-owned-session"]["TCP"] = {"443": {"TCPForward": "127.0.0.1:8080"}}
        cases.append(config)
        for config in cases:
            with self.subTest(config=config), self.assertRaises(self.session.SessionError):
                self.session.inspect_lease((config, config))


class RuntimeAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = load_session()

    def setUp(self):
        self.runtime = self.session.Runtime()

    def test_status_queries_are_read_only_and_both_json_views_must_succeed(self):
        with patch.object(self.session, "command", side_effect=["null", "{}"]) as command:
            self.assertEqual(self.runtime.snapshot(), ({}, {}))
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ["tailscale", "serve", "status", "--json"],
            ["tailscale", "funnel", "status", "--json"],
        ])
        with patch.object(self.session, "command", side_effect=["{}", "malformed " + PRIVATE]):
            with self.assertRaises(self.session.SessionError) as error:
                self.runtime.snapshot()
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_tailscale_https_must_be_preapproved_without_changing_it(self):
        ready = {"BackendState": "Running", "Self": {
            "DNSName": "fake-node.example.ts.net.", "CapMap": {"https": []},
        }}
        with patch.object(self.session, "command", return_value=json.dumps(ready)) as command:
            self.runtime.check_tailscale()
        self.assertEqual(self.runtime.host, "fake-node.example.ts.net")
        self.assertEqual(command.call_args.args[0], ["tailscale", "status", "--json", "--peers=false"])
        for bad in (
            {"BackendState": "NeedsLogin", "Self": ready["Self"]},
            {"BackendState": "Running", "Self": {"DNSName": "fake-node.example.ts.net."}},
            {"BackendState": "Running", "Self": {"DNSName": PRIVATE, "CapMap": {"https": []}}},
        ):
            with self.subTest(bad=bad), patch.object(self.session, "command", return_value=json.dumps(bad)) as command:
                with self.assertRaises(self.session.SessionError) as error:
                    self.runtime.check_tailscale()
                self.assertNotIn(PRIVATE, str(error.exception))
                self.assertEqual(command.call_count, 1)

    def test_cli_error_discards_raw_stderr_arguments_and_inherited_environment(self):
        result = subprocess.CompletedProcess([], 0, stdout="{}")
        with patch.dict(self.session.os.environ, {"TS_SOCKET": PRIVATE, "TS_AUTHKEY": PRIVATE}), \
             patch.object(self.session.subprocess, "run", return_value=result) as run:
            self.assertEqual(self.session.command(["tailscale", "serve", "status", "--json"]), "{}")
        options = run.call_args.kwargs
        self.assertFalse(options.get("shell", False))
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertNotIn("TS_SOCKET", options["env"])
        self.assertNotIn("TS_AUTHKEY", options["env"])
        self.assertLessEqual(options["timeout"], 10)
        failure = subprocess.CalledProcessError(1, ["fake", PRIVATE], stderr=PRIVATE)
        with patch.object(self.session.subprocess, "run", side_effect=failure):
            with self.assertRaises(self.session.SessionError) as error:
                self.session.command(["tailscale", "serve", "status", "--json"])
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_proxy_is_foreground_and_only_the_owned_child_receives_sigint(self):
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(self.session.subprocess, "Popen", return_value=child) as popen:
            self.assertIs(self.runtime.start_proxy(), child)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["tailscale", "serve", "--https=443", "http://127.0.0.1:8080"])
        self.assertNotIn("--bg", argv)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(popen.call_args.kwargs[name], subprocess.DEVNULL)
        self.runtime.stop_proxy(child)
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.wait.assert_called_once_with(timeout=10)
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_unconfirmed_proxy_exit_never_escalates_to_broad_kill(self):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired(PRIVATE, 10, stderr=PRIVATE)
        with self.assertRaises(self.session.SessionError) as error:
            self.runtime.stop_proxy(child)
        self.assertNotIn(PRIVATE, str(error.exception))
        child.send_signal.assert_called_once_with(signal.SIGINT)
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_residual_or_foreign_proxy_is_preserved_and_refused_as_closed(self):
        config = foreground("residual-session")
        lease = self.session.Lease("fake-owned-session", "fake-node.example.ts.net")
        with patch.object(self.runtime, "snapshot", return_value=(config, config)), \
             patch.object(self.session.time, "monotonic", side_effect=[0, 11]), \
             patch.object(self.session, "command", side_effect=AssertionError("cleanup cannot mutate other Serve config")):
            with self.assertRaises(self.session.SessionError):
                self.runtime.verify_closed(lease)

    def test_closure_waits_for_observed_empty_status_instead_of_only_child_exit(self):
        config = foreground()
        with patch.object(self.runtime, "snapshot", side_effect=[(config, config), ({}, {})]) as snapshot, \
             patch.object(self.session.time, "monotonic", side_effect=[0, 1]), \
             patch.object(self.session.time, "sleep"):
            self.runtime.verify_closed(self.session.Lease("fake-owned-session", "fake-node.example.ts.net"))
        self.assertEqual(snapshot.call_count, 2)

    def test_backend_presence_check_is_only_the_fixed_local_nexus_project(self):
        with patch.object(self.session.cloud, "run", return_value="") as run:
            self.runtime.containers_absent()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assertIn("--all", argv)
        self.assertIn("label=com.docker.compose.project=nexus-private-cloud", argv)
        self.assertEqual(argv[-2:], ["--format", "{{.ID}}"])
        self.assertFalse(any(part in argv for part in ("down", "stop", "rm", "prune")))
        with patch.object(self.session.cloud, "run", return_value="fake-existing-container"):
            with self.assertRaises(self.session.SessionError):
                self.runtime.containers_absent()

    def test_backend_adapter_does_not_print_individual_off_before_session_cleanup(self):
        output = io.StringIO()
        with patch.object(self.session.cloud, "down", side_effect=lambda: print("SERVER OFF")) as down, \
             patch.object(self.runtime, "containers_absent") as absent, redirect_stdout(output):
            self.runtime.cloud_down()
        down.assert_called_once_with()
        absent.assert_called_once_with()
        self.assertEqual(output.getvalue(), "")

    def test_actual_backend_teardown_leaves_a_different_docker_project_unchanged(self):
        projects = {"nexus-private-cloud": {"fake-nexus-id"}, "other-project": {"fake-other-id"}}
        before_other = projects["other-project"].copy()
        commands = []

        def fake_docker(argv, **_options):
            commands.append(argv)
            self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            if "compose" in argv and "down" in argv:
                project = argv[argv.index("--project-name") + 1]
                self.assertNotIn("--volumes", argv)
                self.assertNotIn("-v", argv)
                projects[project].clear()
                return ""
            if "ps" in argv:
                selector = argv[argv.index("--filter") + 1]
                self.assertEqual(selector, "label=com.docker.compose.project=nexus-private-cloud")
                return "\n".join(projects["nexus-private-cloud"])
            raise AssertionError("unexpected fake Docker command")

        with patch.object(self.session.cloud, "check_docker"), \
             patch.object(self.session.cloud, "run", side_effect=fake_docker), \
             redirect_stdout(io.StringIO()):
            self.runtime.cloud_down()
        self.assertEqual(projects["other-project"], before_other)
        self.assertEqual(projects["nexus-private-cloud"], set())
        self.assertEqual(len(commands), 3)

    def test_health_uses_bounded_direct_localhost_request_without_redirect_or_proxy(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        with patch.object(self.session.http.client, "HTTPConnection", return_value=connection) as constructor:
            self.runtime.health()
        constructor.assert_called_once_with("127.0.0.1", 8080, timeout=3)
        connection.request.assert_called_once_with("GET", "/healthz")
        connection.close.assert_called_once_with()
        connection.getresponse.return_value.status = 302
        with patch.object(self.session.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(self.session.SessionError):
                self.runtime.health()

    def test_running_session_detects_ssd_identity_change_without_controlling_bmo(self):
        child = SimpleNamespace(poll=lambda: None)
        config = foreground()
        lease = self.session.inspect_lease((config, config))
        signature = ("/dev/nvme0n1p1", "259:1", "fake-id")
        with patch.object(self.runtime, "snapshot", return_value=(config, config)), \
             patch.object(self.runtime, "health"), \
             patch.object(self.session.cloud, "check_storage", side_effect=[signature, ("changed",)]), \
             patch.object(self.session.cloud, "check_bmo_stopped") as bmo:
            with self.assertRaisesRegex(self.session.SessionError, "ssd_changed"):
                self.runtime.watch(child, object(), lease)
        bmo.assert_not_called()


class EntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = load_session()

    def test_entire_session_runs_inside_the_shared_operation_lock(self):
        held = []

        @contextmanager
        def fake_lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def run(settings, *, confirmed):
            self.assertTrue(held)
            self.assertTrue(confirmed)

        with patch.object(self.session.os, "geteuid", return_value=0), \
             patch.object(self.session.cloud, "operation_lock", fake_lock), \
             patch.object(self.session.cloud, "load_settings", return_value=object()), \
             patch.object(self.session, "signals", return_value=nullcontext()), \
             patch.object(self.session, "run_session", side_effect=run):
            self.assertEqual(self.session.main(["--confirm-bmo-stopped"]), 0)
        self.assertEqual(held, [])

    def test_main_errors_do_not_echo_private_messages_or_claim_off(self):
        output = io.StringIO()
        with patch.object(self.session.os, "geteuid", return_value=0), \
             patch.object(self.session.cloud, "operation_lock", side_effect=self.session.cloud.PrivateCloudError(PRIVATE)), \
             redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(self.session.main(["--confirm-bmo-stopped"]), 1)
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertNotIn("SESSION OFF", output.getvalue())
        self.assertIn("BMO", output.getvalue())

    def test_non_root_or_missing_confirmation_cannot_take_a_lock_or_load_settings(self):
        for uid, argv in ((1000, ["--confirm-bmo-stopped"]), (0, [])):
            with self.subTest(uid=uid), patch.object(self.session.os, "geteuid", return_value=uid), \
                 patch.object(self.session.cloud, "operation_lock") as lock, \
                 patch.object(self.session.cloud, "load_settings") as settings, \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(self.session.main(argv), 1)
            lock.assert_not_called()
            settings.assert_not_called()

    def test_unknown_cli_arguments_are_not_echoed_or_used_for_host_actions(self):
        output = io.StringIO()
        with patch.object(self.session.cloud, "operation_lock") as lock, \
             patch.object(self.session.cloud, "load_settings") as settings, \
             redirect_stdout(output), redirect_stderr(output):
            try:
                result = self.session.main(["--unknown-option", PRIVATE])
            except SystemExit as error:
                result = error.code
        self.assertEqual(result, 2)
        self.assertNotIn(PRIVATE, output.getvalue())
        lock.assert_not_called()
        settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
