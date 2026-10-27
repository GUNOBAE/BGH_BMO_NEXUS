"""가짜 Docker/HTTP와 임시 파일만 사용하는 Web Lab 검사. Pi 실행 증거가 아니다."""

from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import copy
import errno
import hashlib
import http.client
import importlib
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "FAKE_PRIVATE_WEB_LAB_SENTINEL"


def helper():
    with patch("subprocess.run", side_effect=AssertionError("import cannot run commands")), \
         patch("socket.socket", side_effect=AssertionError("import cannot use network")):
        return importlib.import_module("scripts.web_lab")


def digest(body):
    return hashlib.sha256(body).hexdigest()


class WebLabCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = helper()

    def setUp(self):
        self.settings = self.lab.cloud.Settings(
            Path("/mnt/ssd"), "11111111-2222-3333-4444-555555555555")
        self.signature = ("/dev/nvme0n1p1", "259:1", self.settings.uuid)
        self.expected = {"site-a": digest(b"Test site A"), "site-b": digest(b"Test site B")}

    def mocked_up(self, stack):
        doubles = {"output": stack.enter_context(patch("builtins.print"))}
        for name in ("check_docker", "check_storage", "check_bmo_stopped", "run"):
            doubles[name] = stack.enter_context(patch.object(self.lab.cloud, name))
        for name in ("container_rows", "require_running", "check_ports", "prepare_sites", "inspect_sites", "check_health", "down"):
            doubles[name] = stack.enter_context(patch.object(self.lab, name))
        doubles["check_storage"].return_value = self.signature
        doubles["container_rows"].return_value = []
        doubles["prepare_sites"].return_value = self.expected
        doubles["inspect_sites"].return_value = self.expected
        doubles["run"].return_value = ""
        return doubles

    def test_confirmation_precedes_any_host_observation_or_creation(self):
        with ExitStack() as stack:
            doubles = self.mocked_up(stack)
            with self.assertRaises(RuntimeError):
                self.lab.up(self.settings, confirmed=False)
            for double in doubles.values():
                double.assert_not_called()

    def test_preflight_failures_never_prepare_or_start_or_stop_anything(self):
        for name in ("check_docker", "check_storage", "check_bmo_stopped", "check_ports", "container_rows"):
            with self.subTest(name=name), ExitStack() as stack:
                doubles = self.mocked_up(stack)
                doubles[name].side_effect = self.lab.cloud.PrivateCloudError("fake preflight")
                with self.assertRaises(RuntimeError):
                    self.lab.up(self.settings, confirmed=True)
                doubles["prepare_sites"].assert_not_called()
                doubles["run"].assert_not_called()
                doubles["down"].assert_not_called()

    def test_existing_project_is_not_adopted_or_stopped(self):
        with ExitStack() as stack:
            doubles = self.mocked_up(stack)
            doubles["container_rows"].return_value = [{"ID": "existing"}]
            with self.assertRaises(RuntimeError):
                self.lab.up(self.settings, confirmed=True)
            doubles["prepare_sites"].assert_not_called()
            doubles["run"].assert_not_called()
            doubles["down"].assert_not_called()

    def test_start_uses_only_fixed_project_and_two_named_services(self):
        with ExitStack() as stack:
            doubles = self.mocked_up(stack)
            self.lab.up(self.settings, confirmed=True)
            starts = [call.args[0] for call in doubles["run"].call_args_list if "up" in call.args[0]]
            self.assertEqual(len(starts), 1)
            argv = starts[0]
            self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            self.assertIn("nexus-web-lab", argv)
            self.assertIn(str(ROOT / "deploy/web-lab/compose.yaml"), argv)
            self.assertIn("/dev/null", argv)
            self.assertEqual(argv[-2:], ["site-a", "site-b"])
            for flag in ("--detach", "--wait", "--no-build"):
                self.assertIn(flag, argv)
            self.assertNotIn("nexus-private-cloud", argv)
            doubles["check_health"].assert_called_once_with(self.expected)
            self.assertGreaterEqual(doubles["check_storage"].call_count, 3)
            self.assertGreaterEqual(doubles["check_bmo_stopped"].call_count, 2)
            doubles["down"].assert_not_called()

    def test_ssd_identity_change_before_start_does_not_run_compose(self):
        with ExitStack() as stack:
            doubles = self.mocked_up(stack)
            doubles["check_storage"].side_effect = [self.signature, ("/dev/nvme1n1p1", "260:1", self.settings.uuid)]
            with self.assertRaises(RuntimeError):
                self.lab.up(self.settings, confirmed=True)
            doubles["run"].assert_not_called()
            doubles["down"].assert_not_called()

    def test_partial_start_or_unhealthy_response_triggers_scoped_cleanup(self):
        for name in ("run", "check_health"):
            with self.subTest(name=name), ExitStack() as stack:
                doubles = self.mocked_up(stack)
                doubles[name].side_effect = self.lab.cloud.PrivateCloudError("fake post-start failure")
                with self.assertRaises(RuntimeError):
                    self.lab.up(self.settings, confirmed=True)
                doubles["down"].assert_called_once_with(announce=False)

    def test_post_start_ssd_or_bmo_change_cleans_up_and_never_reports_on(self):
        for failure in ("ssd", "bmo", "sites"):
            with self.subTest(failure=failure), ExitStack() as stack:
                doubles = self.mocked_up(stack)
                if failure == "ssd":
                    doubles["check_storage"].side_effect = [self.signature] * 3 + [self.lab.cloud.PrivateCloudError("ssd_rejected")]
                elif failure == "bmo":
                    doubles["check_bmo_stopped"].side_effect = [None, None, self.lab.cloud.PrivateCloudError("bmo_busy_or_unknown")]
                else:
                    doubles["inspect_sites"].return_value = {**self.expected, "site-b": "changed"}
                with self.assertRaises(RuntimeError):
                    self.lab.up(self.settings, confirmed=True)
                doubles["down"].assert_called_once_with(announce=False)
                self.assertFalse(any("WEB LAB ON" in str(call) for call in doubles["output"].call_args_list))

    def test_cleanup_failure_is_distinct_from_success(self):
        with ExitStack() as stack:
            doubles = self.mocked_up(stack)
            doubles["check_health"].side_effect = self.lab.WebLabError("localhost_unhealthy")
            doubles["down"].side_effect = self.lab.WebLabError("down_incomplete")
            with self.assertRaisesRegex(self.lab.WebLabError, "start_cleanup_failed"):
                self.lab.up(self.settings, confirmed=True)

    def test_down_requires_neither_ssd_nor_bmo_nor_settings_and_keeps_data(self):
        with ExitStack() as stack:
            output = stack.enter_context(patch("builtins.print"))
            docker = stack.enter_context(patch.object(self.lab.cloud, "check_docker"))
            storage = stack.enter_context(patch.object(self.lab.cloud, "check_storage"))
            bmo = stack.enter_context(patch.object(self.lab.cloud, "check_bmo_stopped"))
            run = stack.enter_context(patch.object(self.lab.cloud, "run", return_value=""))
            stack.enter_context(patch.object(self.lab, "container_rows", return_value=[]))
            ports = stack.enter_context(patch.object(self.lab, "check_ports"))
            self.lab.down()
            docker.assert_called_once_with(for_start=False)
            storage.assert_not_called()
            bmo.assert_not_called()
            argv = run.call_args.args[0]
            self.assertIn("nexus-web-lab", argv)
            self.assertIn("down", argv)
            self.assertEqual(argv[-2:], ["--timeout", "30"])
            self.assertFalse(set(argv) & {"--volumes", "-v", "--rmi", "--remove-orphans", "prune"})
            ports.assert_called_once()
            self.assertIn("WEB LAB OFF", str(output.call_args))

    def test_down_never_reports_off_if_containers_or_ports_remain(self):
        for remains in ("container", "port"):
            with self.subTest(remains=remains), patch("builtins.print") as output, \
                 patch.object(self.lab.cloud, "check_docker"), \
                 patch.object(self.lab.cloud, "run", return_value=""), \
                 patch.object(self.lab, "container_rows", return_value=[{}] if remains == "container" else []), \
                 patch.object(self.lab, "check_ports", side_effect=RuntimeError("busy") if remains == "port" else None):
                with self.assertRaises(RuntimeError):
                    self.lab.down()
                self.assertFalse(any("WEB LAB OFF" in str(call) for call in output.call_args_list))

    def test_both_ports_are_probed_only_on_loopback(self):
        probe = MagicMock()
        probe.__enter__.return_value = probe
        probe.connect_ex.return_value = errno.ECONNREFUSED
        with patch.object(self.lab.socket, "socket", return_value=probe):
            self.lab.check_ports()
        self.assertEqual([call.args[0] for call in probe.bind.call_args_list],
                         [("127.0.0.1", 8090), ("127.0.0.1", 8091)])
        self.assertEqual([call.args[0] for call in probe.connect_ex.call_args_list],
                         [("127.0.0.1", 8090), ("127.0.0.1", 8091)])
        self.assertEqual(probe.settimeout.call_args.args, (2,))

    def test_busy_port_fails_without_touching_other_services(self):
        for busy in (1, 2):
            probe = MagicMock()
            probe.__enter__.return_value = probe
            probe.connect_ex.return_value = errno.ECONNREFUSED
            probe.bind.side_effect = [None] * (busy - 1) + [OSError(PRIVATE)]
            with self.subTest(busy=busy), patch.object(self.lab.socket, "socket", return_value=probe):
                with self.assertRaises(RuntimeError):
                    self.lab.check_ports()

    def test_nat_listener_or_unknown_tcp_state_cannot_be_reported_closed(self):
        for result in (0, errno.ETIMEDOUT, errno.EHOSTUNREACH):
            probe = MagicMock()
            probe.__enter__.return_value = probe
            probe.connect_ex.return_value = result
            with self.subTest(result=result), patch.object(self.lab.socket, "socket", return_value=probe):
                with self.assertRaises(self.lab.WebLabError):
                    self.lab.check_ports()


class WebLabHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = helper()

    def health(self, bodies, statuses=(200, 200)):
        connections = [MagicMock(), MagicMock()]
        for connection, body, status in zip(connections, bodies, statuses):
            response = connection.getresponse.return_value
            response.status = status
            response.read.return_value = body
        return connections

    def test_only_both_exact_ssd_index_bodies_allow_health_success(self):
        bodies = (b"Test site A\n", b"Test site B\n")
        expected = dict(zip(("site-a", "site-b"), map(digest, bodies)))
        connections = self.health(bodies)
        with patch.object(self.lab.http.client, "HTTPConnection", side_effect=connections) as factory:
            self.lab.check_health(expected)
        self.assertEqual([call.args[:2] for call in factory.call_args_list],
                         [("127.0.0.1", 8090), ("127.0.0.1", 8091)])
        for connection in connections:
            self.assertEqual(connection.request.call_args.args[:2], ("GET", "/"))
            self.assertTrue(connection.close.called)

    def test_redirect_errors_wrong_site_or_large_body_do_not_pass(self):
        expected = {"site-a": digest(b"A"), "site-b": digest(b"B")}
        scenarios = (((b"A", b"B"), (302, 200)),
                     ((b"A", b"B"), (200, 503)),
                     ((b"B", b"A"), (200, 200)),
                     ((b"A", b"x" * (1024 * 1024 + 1)), (200, 200)))
        for bodies, statuses in scenarios:
            with self.subTest(statuses=statuses, sizes=tuple(map(len, bodies))):
                connections = self.health(bodies, statuses)
                with patch.object(self.lab.http.client, "HTTPConnection", side_effect=connections):
                    with self.assertRaises(RuntimeError):
                        self.lab.check_health(expected)

    def test_connection_failure_is_not_a_success_and_does_not_echo_private_error(self):
        expected = {"site-a": digest(b"A"), "site-b": digest(b"B")}
        connection = MagicMock()
        connection.request.side_effect = OSError(PRIVATE)
        with patch.object(self.lab.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(RuntimeError) as error:
                self.lab.check_health(expected)
        self.assertNotIn(PRIVATE, str(error.exception))
        connection.close.assert_called_once()


class WebLabCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = helper()

    def cli(self, argv, *, uid=0):
        output, error = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(error))
            stack.enter_context(patch.object(self.lab.os, "geteuid", return_value=uid))
            doubles = {}
            doubles["operation_lock"] = stack.enter_context(
                patch.object(self.lab, "operation_lock", return_value=nullcontext()))
            doubles["load_settings"] = stack.enter_context(patch.object(self.lab.cloud, "load_settings"))
            for name in ("up", "down", "status"):
                doubles[name] = stack.enter_context(patch.object(self.lab, name))
            try:
                result = self.lab.main(argv)
            except SystemExit as exit_error:
                result = exit_error.code
        return result, output.getvalue() + error.getvalue(), doubles

    def test_up_requires_root_and_passes_explicit_manual_confirmation(self):
        result, _, doubles = self.cli(["up", "--confirm-bmo-stopped"])
        self.assertEqual(result, 0)
        doubles["operation_lock"].assert_called_once()
        doubles["up"].assert_called_once_with(doubles["load_settings"].return_value, confirmed=True)
        doubles["down"].assert_not_called()
        doubles["status"].assert_not_called()
        result, _, doubles = self.cli(["up", "--confirm-bmo-stopped"], uid=1000)
        self.assertNotEqual(result, 0)
        for double in doubles.values():
            double.assert_not_called()

    def test_down_does_not_read_ssd_config_and_uses_lab_lock(self):
        result, _, doubles = self.cli(["down"])
        self.assertEqual(result, 0)
        doubles["load_settings"].assert_not_called()
        doubles["down"].assert_called_once_with()
        doubles["operation_lock"].assert_called_once()

    def test_status_uses_existing_settings_and_does_not_start_or_stop(self):
        result, _, doubles = self.cli(["status"])
        self.assertEqual(result, 0)
        doubles["status"].assert_called_once_with()
        doubles["load_settings"].assert_not_called()
        doubles["up"].assert_not_called()
        doubles["down"].assert_not_called()

    def test_unknown_arguments_do_not_leak_values_or_perform_work(self):
        for argv in (["up", "--url", PRIVATE], [PRIVATE], ["down", "--repo", PRIVATE]):
            with self.subTest(argv=argv):
                result, output, doubles = self.cli(argv)
                self.assertNotEqual(result, 0)
                self.assertNotIn(PRIVATE, output)
                for double in doubles.values():
                    double.assert_not_called()

    def test_missing_confirmation_never_reads_config_or_acquires_lock(self):
        result, _, doubles = self.cli(["up"])
        self.assertNotEqual(result, 0)
        for double in doubles.values():
            double.assert_not_called()

    def test_foreign_docker_environment_cannot_change_runtime_target(self):
        result = subprocess.CompletedProcess(["fake"], 0, stdout="", stderr="")
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://private.invalid:2375", "COMPOSE_FILE": PRIVATE}), \
             patch.object(self.lab.cloud.subprocess, "run", return_value=result) as runner:
            self.lab.cloud.run(self.lab.compose_args() + ["down", "--timeout", "30"])
        args, kwargs = runner.call_args
        self.assertEqual(args[0][:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assertNotIn("DOCKER_HOST", kwargs["env"])
        self.assertNotIn("COMPOSE_FILE", kwargs["env"])
        self.assertNotIn(PRIVATE, str(args))
        self.assertNotIn("shell", kwargs)


def running_rows():
    rows = []
    for index, (site, port) in enumerate(zip(("site-a", "site-b"), (8090, 8091)), start=1):
        bindings = {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
        rows.append({
            "Id": str(index) * 64,
            "Config": {"Labels": {"com.docker.compose.project": "nexus-web-lab", "com.docker.compose.service": site}},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "HostConfig": {"PortBindings": bindings},
            "NetworkSettings": {"Ports": copy.deepcopy(bindings)},
            "Mounts": [{"Type": "bind", "Source": f"/mnt/ssd/nexus-web-lab/{site}",
                        "Destination": "/usr/share/nginx/html", "RW": False},
                       {"Type": "tmpfs", "Destination": "/tmp"}],
        })
    return rows


class WebLabObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = helper()

    def test_container_observation_includes_stopped_containers_and_only_own_project(self):
        rows = running_rows()
        with patch.object(self.lab.cloud, "run", side_effect=["\n".join(row["Id"] for row in rows), json.dumps(rows)]) as runner:
            self.assertEqual(self.lab.container_rows(), rows)
        first, second = [call.args[0] for call in runner.call_args_list]
        self.assertIn("--all", first)
        self.assertIn("label=com.docker.compose.project=nexus-web-lab", first)
        self.assertEqual(second[-2:], [row["Id"] for row in rows])
        self.assertIn("inspect", second)

    def test_malformed_ids_foreign_labels_or_unexpected_inspect_fail_closed(self):
        rows = running_rows()
        foreign = copy.deepcopy(rows)
        foreign[0]["Config"]["Labels"]["com.docker.compose.project"] = "nexus-private-cloud"
        cases = [(PRIVATE, None), (" ".join(["1" * 64] * 2), None),
                 ("1" * 64, "not JSON"), ("1" * 64, "{}"),
                 ("\n".join(row["Id"] for row in rows), json.dumps(foreign))]
        for ids, inspection in cases:
            with self.subTest(ids=ids), patch.object(self.lab.cloud, "run", side_effect=[ids, inspection]):
                with self.assertRaises(RuntimeError) as error:
                    self.lab.container_rows()
                self.assertNotIn(PRIVATE, str(error.exception))

    def test_running_requires_both_healthy_separate_readonly_binds_and_exact_loopback_ports(self):
        self.lab.require_running(running_rows())
        mutations = (
            lambda rows: rows.pop(),
            lambda rows: rows[1]["Config"]["Labels"].update({"com.docker.compose.service": "site-a"}),
            lambda rows: rows[0]["State"].update({"Running": False}),
            lambda rows: rows[1]["State"]["Health"].update({"Status": "unhealthy"}),
            lambda rows: rows[0]["HostConfig"]["PortBindings"]["8080/tcp"][0].update({"HostIp": "0.0.0.0"}),
            lambda rows: rows[1]["NetworkSettings"]["Ports"]["8080/tcp"][0].update({"HostPort": "8080"}),
            lambda rows: rows[0]["Mounts"][0].update({"Source": "/mnt/ssd/bmo-git-mirrors"}),
            lambda rows: rows[0]["Mounts"][0].update({"RW": True}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                rows = running_rows()
                mutate(rows)
                with self.assertRaises(RuntimeError):
                    self.lab.require_running(rows)

    def test_off_status_is_config_independent_and_readonly(self):
        with patch("builtins.print") as output, \
             patch.object(self.lab.cloud, "check_docker"), \
             patch.object(self.lab.cloud, "load_settings") as settings, \
             patch.object(self.lab.cloud, "check_storage") as storage, \
             patch.object(self.lab, "container_rows", return_value=[]), \
             patch.object(self.lab, "check_ports") as ports, \
             patch.object(self.lab.cloud, "run") as runner:
            self.lab.status()
        settings.assert_not_called()
        storage.assert_not_called()
        runner.assert_not_called()
        ports.assert_called_once()
        self.assertIn("WEB LAB OFF", str(output.call_args))

    def test_running_status_checks_ssd_bmo_and_http_but_does_not_create_or_stop(self):
        settings = self.lab.cloud.Settings(Path("/mnt/ssd"), "11111111-2222-3333-4444-555555555555")
        signature = ("/dev/nvme0n1p1", "259:1", settings.uuid)
        expected = {"site-a": digest(b"A"), "site-b": digest(b"B")}
        with ExitStack() as stack:
            output = stack.enter_context(patch("builtins.print"))
            stack.enter_context(patch.object(self.lab.cloud, "check_docker"))
            storage = stack.enter_context(patch.object(self.lab.cloud, "check_storage", return_value=signature))
            bmo = stack.enter_context(patch.object(self.lab.cloud, "check_bmo_stopped"))
            stack.enter_context(patch.object(self.lab, "container_rows", return_value=running_rows()))
            inspect = stack.enter_context(patch.object(self.lab, "inspect_sites", return_value=expected))
            health = stack.enter_context(patch.object(self.lab, "check_health"))
            prepare = stack.enter_context(patch.object(self.lab, "prepare_sites"))
            down = stack.enter_context(patch.object(self.lab, "down"))
            runner = stack.enter_context(patch.object(self.lab.cloud, "run"))
            self.lab.status(settings)
        storage.assert_called()
        bmo.assert_called_once_with(settings)
        inspect.assert_called_once_with(settings, signature)
        health.assert_called_once_with(expected)
        prepare.assert_not_called()
        down.assert_not_called()
        runner.assert_not_called()
        self.assertIn("WEB LAB ON", str(output.call_args))


class WebLabFilesTests(unittest.TestCase):
    """실제 fd/링크와 임시 디렉터리 사용. root 소유권만 가짜로 보정한다."""

    @classmethod
    def setUpClass(cls):
        cls.lab = helper()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="web-lab-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.mount = Path(self.temporary.name)
        self.settings = self.lab.cloud.Settings(
            self.mount, "11111111-2222-3333-4444-555555555555")
        device = self.mount.stat().st_dev
        self.signature = ("/dev/nvme0n1p1", f"{os.major(device)}:{os.minor(device)}", self.settings.uuid)
        self.real_stat, self.real_fstat = os.stat, os.fstat

    @staticmethod
    def as_root(info):
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_uid": 0, "st_gid": 0})

    def root_ownership(self, stack):
        stack.enter_context(patch.object(self.lab, "fixed_settings"))
        stack.enter_context(patch.object(self.lab.os, "fstat", side_effect=lambda fd: self.as_root(self.real_fstat(fd))))
        stack.enter_context(patch.object(self.lab.os, "stat", side_effect=lambda *args, **kwargs: self.as_root(self.real_stat(*args, **kwargs))))
        stack.enter_context(patch.object(self.lab.os, "fchown"))

    def prepare(self):
        with ExitStack() as stack:
            self.root_ownership(stack)
            return self.lab.prepare_sites(self.settings, self.signature)

    def inspect(self):
        with ExitStack() as stack:
            self.root_ownership(stack)
            return self.lab.inspect_sites(self.settings, self.signature)

    def inventory(self):
        return {str(path.relative_to(self.mount)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
                for path in self.mount.rglob("*") if path.is_file()}

    def test_only_new_lab_directory_is_created_and_existing_cloud_mirrors_are_preserved(self):
        originals = ("nexus-private-cloud-test", "nexus-git-mirrors", "bmo-git-mirrors")
        for name in originals:
            directory = self.mount / name
            directory.mkdir(mode=0o700)
            (directory / "sentinel").write_bytes(name.encode())
        before = self.inventory()
        expected = self.prepare()
        self.assertEqual(set(expected), {"site-a", "site-b"})
        self.assertNotEqual(expected["site-a"], expected["site-b"])
        for name, value in before.items():
            self.assertEqual(self.inventory()[name], value)
        for site in ("site-a", "site-b"):
            path = self.mount / "nexus-web-lab" / site / "index.html"
            self.assertEqual(digest(path.read_bytes()), expected[site])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.mount / "nexus-web-lab").stat().st_mode), 0o700)

    def test_repeated_prepare_and_readonly_inspection_never_overwrite_site_edits(self):
        self.prepare()
        path = self.mount / "nexus-web-lab/site-a/index.html"
        path.write_bytes(b"Owner supplied replacement site A\n")
        before = self.inventory()
        expected = self.prepare()
        self.assertEqual(expected["site-a"], digest(path.read_bytes()))
        self.assertEqual(self.inventory(), before)
        self.assertEqual(self.inspect(), expected)
        self.assertEqual(self.inventory(), before)

    def test_unclaimed_directory_is_not_adopted_or_changed(self):
        directory = self.mount / "nexus-web-lab"
        directory.mkdir(mode=0o700)
        (directory / "owner-data").write_bytes(b"keep")
        before = self.inventory()
        with self.assertRaises((RuntimeError, OSError)):
            self.prepare()
        self.assertEqual(self.inventory(), before)

    def test_lab_base_or_site_symlinks_cannot_redirect_into_existing_data(self):
        target = self.mount / "nexus-git-mirrors"
        target.mkdir(mode=0o700)
        (target / "sentinel").write_bytes(b"keep")
        base = self.mount / "nexus-web-lab"
        base.symlink_to(target, target_is_directory=True)
        with self.assertRaises((RuntimeError, OSError)):
            self.prepare()
        self.assertEqual((target / "sentinel").read_bytes(), b"keep")
        self.assertEqual(list(target.iterdir()), [target / "sentinel"])
        base.unlink()
        self.prepare()
        site = base / "site-a"
        (site / "index.html").unlink()
        site.rmdir()
        site.symlink_to(target, target_is_directory=True)
        with self.assertRaises((RuntimeError, OSError)):
            self.prepare()
        self.assertEqual(list(target.iterdir()), [target / "sentinel"])

    def test_index_symlink_or_hardlink_is_rejected_without_modifying_target(self):
        self.prepare()
        index = self.mount / "nexus-web-lab/site-a/index.html"
        target = self.mount / "private-file"
        target.write_bytes(b"private owner content")
        index.unlink()
        for link in ("symlink", "hardlink"):
            with self.subTest(link=link):
                if link == "symlink":
                    index.symlink_to(target)
                else:
                    os.link(target, index)
                with self.assertRaises((RuntimeError, OSError)):
                    self.prepare()
                self.assertEqual(target.read_bytes(), b"private owner content")
                index.unlink()

    def test_nested_asset_symlinks_are_rejected_without_following_them(self):
        self.prepare()
        asset = self.mount / "nexus-web-lab/site-b/assets"
        asset.symlink_to(self.mount, target_is_directory=True)
        with self.assertRaises((RuntimeError, OSError)):
            self.inspect()

    def test_special_asset_file_is_rejected_without_opening_it(self):
        self.prepare()
        asset = self.mount / "nexus-web-lab/site-a/pipe"
        os.mkfifo(asset, mode=0o644)
        with self.assertRaises(RuntimeError):
            self.inspect()

    def test_existing_bad_marker_permissions_or_identical_empty_indexes_are_not_repaired(self):
        self.prepare()
        base = self.mount / "nexus-web-lab"
        marker = base / self.lab.MARKER
        index = base / "site-a/index.html"
        original_marker, original_index = marker.read_bytes(), index.read_bytes()
        cases = (
            lambda: marker.write_bytes(b"foreign store\n"),
            lambda: marker.chmod(0o644),
            lambda: index.chmod(0o666),
            lambda: index.write_bytes(b""),
            lambda: index.write_bytes((base / "site-b/index.html").read_bytes()),
        )
        for number, mutate in enumerate(cases):
            with self.subTest(case=number):
                mutate()
                before = self.inventory()
                with self.assertRaises(RuntimeError):
                    self.prepare()
                self.assertEqual(self.inventory(), before)
                marker.write_bytes(original_marker)
                marker.chmod(0o600)
                index.write_bytes(original_index)
                index.chmod(0o644)

    def test_existing_nonroot_owner_or_group_is_rejected_without_chown(self):
        self.prepare()
        for owner, group in ((1000, 0), (0, 1000)):
            with self.subTest(owner=owner, group=group), ExitStack() as stack:
                stack.enter_context(patch.object(self.lab, "fixed_settings"))

                def metadata(fd):
                    result = self.as_root(self.real_fstat(fd))
                    result.st_uid, result.st_gid = owner, group
                    return result

                stack.enter_context(patch.object(self.lab.os, "fstat", side_effect=metadata))
                chown = stack.enter_context(patch.object(self.lab.os, "fchown"))
                with self.assertRaises(RuntimeError):
                    self.lab.inspect_sites(self.settings, self.signature)
                chown.assert_not_called()

    def test_restrictive_umask_is_normalized_only_for_new_lab_files(self):
        old_mask = os.umask(0o077)
        try:
            self.prepare()
        finally:
            os.umask(old_mask)
        for site in ("site-a", "site-b"):
            directory = self.mount / "nexus-web-lab" / site
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((directory / "index.html").stat().st_mode), 0o644)

    def test_cross_device_identity_is_rejected_before_any_directory_creation(self):
        major, minor = map(int, self.signature[1].split(":"))
        signature = (self.signature[0], f"{major}:{minor + 1}", self.signature[2])
        with ExitStack() as stack:
            self.root_ownership(stack)
            with self.assertRaises(RuntimeError):
                self.lab.prepare_sites(self.settings, signature)
        self.assertEqual(list(self.mount.iterdir()), [])


class WebLabIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = helper()

    def test_compose_publishes_only_two_loopback_ports_and_only_readonly_lab_binds(self):
        compose = (ROOT / "deploy/web-lab/compose.yaml").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'"(127\.0\.0\.1:[0-9]+:[0-9]+)"', compose),
                         ["127.0.0.1:8090:8080", "127.0.0.1:8091:8080"])
        sources = re.findall(r"(?m)^\s*source:\s*(.*)$", compose)
        self.assertEqual(sources, ["${NEXUS_SSD_MOUNT:?NEXUS_SSD_MOUNT is required}/nexus-web-lab/site-a",
                                   "${NEXUS_SSD_MOUNT:?NEXUS_SSD_MOUNT is required}/nexus-web-lab/site-b"])
        self.assertEqual(len(re.findall(r"(?m)^\s*create_host_path:\s*false\s*$", compose)), 2)
        self.assertEqual(len(re.findall(r"(?m)^\s*read_only:\s*true\s*$", compose)), 3)
        self.assertRegex(compose, r'(?m)^\s*restart:\s*"no"\s*$')
        self.assertRegex(compose, r'(?m)^\s*user:\s*"[1-9][0-9]*:[1-9][0-9]*"\s*$')
        self.assertRegex(compose, r"@sha256:[0-9a-f]{64}")
        self.assertIn("no-new-privileges:true", compose)
        self.assertRegex(compose, r"(?m)^\s*- ALL\s*$")
        self.assertNotRegex(compose, r"(?m)^\s*(privileged|devices|device_cgroup_rules|cap_add|network_mode|pid|ipc):")
        self.assertNotIn("docker.sock", compose)
        self.assertNotIn("/nexus-private-cloud-test", compose)
        self.assertNotIn("/nexus-git-mirrors", compose)
        self.assertNotIn("/bmo-git-mirrors", compose)

    def test_wrappers_only_delegate_to_fixed_actions(self):
        for action in ("up", "down", "status"):
            wrapper = (ROOT / "scripts" / f"web-lab-{action}.sh").read_text(encoding="utf-8")
            self.assertIn("set -euo pipefail", wrapper)
            self.assertRegex(wrapper, r'exec python3 -B .*web_lab\.py"\s+' + action + r'\s+"\$@"')
            code = "\n".join(line for line in wrapper.splitlines() if not line.lstrip().startswith("#"))
            self.assertNotRegex(code, r"(?m)(?:^|[;&|])\s*(?:source|sudo|kill|killall|pkill|systemctl|mount|umount|rm|docker)\s")

    def test_lab_lock_is_private_independent_and_contention_fails_before_body(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0, st_nlink=1)
        for busy in (False, True):
            with self.subTest(busy=busy), \
                 patch.object(self.lab.os, "open", return_value=42) as opened, \
                 patch.object(self.lab.os, "fstat", return_value=metadata), \
                 patch.object(self.lab.os, "close") as closed, \
                 patch.object(self.lab.fcntl, "flock", side_effect=BlockingIOError if busy else None):
                entered = False
                try:
                    with self.lab.operation_lock():
                        entered = True
                except self.lab.WebLabError as error:
                    self.assertTrue(busy)
                    self.assertEqual(str(error), "operation_busy")
                self.assertEqual(entered, not busy)
                self.assertEqual(opened.call_args.args[0], "/run/lock/nexus-web-lab.lock")
                self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
                closed.assert_called_once_with(42)

    def test_signals_interrupt_start_and_are_ignored_during_cleanup_then_restored(self):
        numbers = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        for cleanup in (False, True):
            current = {number: object() for number in numbers}
            previous = dict(current)

            def install(number, handler):
                old = current[number]
                current[number] = handler
                return old

            with self.subTest(cleanup=cleanup), patch.object(self.lab.signal, "signal", side_effect=install):
                with self.lab.termination_signals(cleanup=cleanup):
                    for number in numbers:
                        if cleanup:
                            self.assertEqual(current[number], signal.SIG_IGN)
                        else:
                            with self.assertRaises(KeyboardInterrupt):
                                current[number](number, None)
                self.assertEqual(current, previous)
