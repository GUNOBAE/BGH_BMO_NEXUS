"""수동 PRIVATE CLOUD의 안전 조건을 가짜 관측으로 검사한다. Pi 검증이 아니다."""

import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from contextlib import ExitStack


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/private-cloud/compose.yaml"


def load_helper():
    """import 중 프로세스·소켓·파일 변경이 일어나도 테스트가 즉시 실패한다."""
    spec = importlib.util.spec_from_file_location(
        "private_cloud_safety_test_helper", ROOT / "scripts/private_cloud.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch("subprocess.run", side_effect=AssertionError("import must not run commands")), \
         patch("socket.socket", side_effect=AssertionError("import must not bind sockets")), \
         patch.object(Path, "mkdir", side_effect=AssertionError("import must not create directories")):
        spec.loader.exec_module(module)
    return module


def yaml_block(text, key):
    """고정한 Compose의 한 블록만 찾는다. 일반 YAML 파서로 사용하지 않는다."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r"(\s*)" + re.escape(key) + r":\s*", line)
        if match:
            indent = len(match.group(1))
            block = []
            for candidate in lines[index + 1:]:
                if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                    break
                block.append(candidate)
            found.append("\n".join(block))
    return found


class PrivateCloudComposeTests(unittest.TestCase):
    def setUp(self):
        self.compose = COMPOSE.read_text(encoding="utf-8")

    def test_only_http_on_loopback_is_published(self):
        blocks = yaml_block(self.compose, "ports")
        self.assertEqual(len(blocks), 1)
        published = [line.strip().lstrip("- ").strip("\"'")
                     for line in blocks[0].splitlines() if line.strip()]
        self.assertEqual(published, ["127.0.0.1:8080:8080"])
        self.assertNotRegex(self.compose, r"(?m)^\s*(network_mode|pid|ipc):\s*['\"]?host")

    def test_pinned_nonroot_service_has_no_automatic_restart_or_host_devices(self):
        self.assertRegex(self.compose, r"(?m)^\s*image:\s*ghcr\.io/drakkan/sftpgo:v2\.7\.6\s*$")
        self.assertRegex(self.compose, r"(?m)^\s*restart:\s*[\"']no[\"']\s*$")
        self.assertRegex(self.compose, r"(?m)^\s*user:\s*[\"']1000:1000[\"']\s*$")
        self.assertRegex(self.compose, r"(?m)^\s*read_only:\s*true\s*$")
        self.assertIn("no-new-privileges:true", self.compose)
        self.assertIn("ALL", "\n".join(yaml_block(self.compose, "cap_drop")))
        self.assertNotRegex(self.compose, r"(?m)^\s*(privileged|devices|device_cgroup_rules|cap_add):")
        self.assertNotIn("docker.sock", self.compose)
        self.assertNotRegex(self.compose, r"(?m)^\s*source:\s*['\"]?/(?:home|root|dev|sys|proc)(?:/|\s|['\"])" )

    def test_binds_are_only_explicit_test_data_and_state_without_auto_creation(self):
        sources = re.findall(r"(?m)^\s*source:\s*(.*?)\s*$", self.compose)
        targets = re.findall(r"(?m)^\s*target:\s*[\"']?(/[^\"'\s]+)[\"']?\s*$", self.compose)
        self.assertEqual(len(sources), 2)
        self.assertTrue(any(source.strip("\"'").endswith("/nexus-private-cloud-test/data") for source in sources))
        self.assertTrue(any(source.strip("\"'").endswith("/nexus-private-cloud-test/state") for source in sources))
        for source in sources:
            self.assertIn("${NEXUS_SSD_MOUNT:?", source)
        self.assertEqual(set(targets), {"/srv/sftpgo/data", "/var/lib/sftpgo"})
        self.assertEqual(len(re.findall(r"(?m)^\s*create_host_path:\s*false\s*$", self.compose)), 2)

    def test_wrappers_only_delegate_and_never_source_the_settings(self):
        for action in ("up", "down"):
            with self.subTest(action=action):
                wrapper = (ROOT / "scripts" / f"server-{action}.sh").read_text(encoding="utf-8")
                self.assertIn("set -euo pipefail", wrapper)
                self.assertRegex(wrapper, r"exec python3 -B .*private_cloud\.py[\"']?\s+" + action + r"\s+\"\$@\"")
                code = "\n".join(line for line in wrapper.splitlines() if not line.lstrip().startswith("#"))
                self.assertNotRegex(code, r"(?m)(?:^|[;&|])\s*(?:source|\.|sudo|kill|killall|pkill|systemctl|mount|umount|rm|docker)\s")


class PrivateCloudCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = load_helper()

    def setUp(self):
        self.settings = self.cloud.Settings(
            mount=Path("/mnt/test-ssd"),
            uuid="11111111-2222-3333-4444-555555555555",
        )
        self.signature = ("/dev/nvme0n1p1", "259:1", self.settings.uuid)

    def patched_up(self, stack):
        stack.enter_context(patch("builtins.print"))
        doubles = {}
        for name in ("check_docker", "check_storage", "check_bmo_stopped",
                     "check_port", "prepare_directories", "run"):
            doubles[name] = stack.enter_context(patch.object(self.cloud, name))
        doubles["check_storage"].return_value = self.signature
        doubles["run"].return_value = ""
        return doubles

    def test_up_requires_manual_bmo_confirmation_before_any_host_action(self):
        with ExitStack() as stack:
            doubles = self.patched_up(stack)
            with self.assertRaises(self.cloud.PrivateCloudError):
                self.cloud.up(self.settings, confirmed=False)
            for double in doubles.values():
                double.assert_not_called()

    def test_preflight_failures_cannot_create_directories_or_start_compose(self):
        for failed in ("check_docker", "check_storage", "check_bmo_stopped", "check_port"):
            with self.subTest(failed=failed), ExitStack() as stack:
                doubles = self.patched_up(stack)
                doubles[failed].side_effect = self.cloud.PrivateCloudError("fake preflight rejected")
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.cloud.up(self.settings, confirmed=True)
                doubles["prepare_directories"].assert_not_called()
                self.assertFalse(any("up" in call.args[0] for call in doubles["run"].call_args_list))

    def test_successful_up_uses_only_the_named_sftpgo_service_and_fixed_project(self):
        with ExitStack() as stack:
            doubles = self.patched_up(stack)
            self.cloud.up(self.settings, confirmed=True)
            calls = [call.args[0] for call in doubles["run"].call_args_list]
            starts = [argv for argv in calls if "up" in argv]
            self.assertEqual(len(starts), 1)
            argv = starts[0]
            self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            self.assertIn("nexus-private-cloud", argv)
            self.assertIn(str(COMPOSE), argv)
            self.assertIn("/dev/null", argv)
            self.assertEqual(argv[-1], "sftpgo")
            self.assertIn("--detach", argv)
            self.assertIn("--wait", argv)
            self.assertIn("--no-build", argv)
            doubles["prepare_directories"].assert_called_once_with(self.settings, self.signature)
            self.assertGreaterEqual(doubles["check_storage"].call_count, 2)
            self.assertGreaterEqual(doubles["check_bmo_stopped"].call_count, 2)

    def test_down_only_removes_this_project_without_volume_or_data_deletion(self):
        with patch("builtins.print"), \
             patch.object(self.cloud, "check_docker"), \
             patch.object(self.cloud, "check_storage") as storage, \
             patch.object(self.cloud, "check_bmo_stopped") as bmo, \
             patch.object(self.cloud, "prepare_directories") as directories, \
             patch.object(self.cloud, "run", return_value="") as run:
            self.cloud.down()
            storage.assert_not_called()
            bmo.assert_not_called()
            directories.assert_not_called()
            self.assertEqual(run.call_count, 2)
            argv = run.call_args_list[0].args[0]
            self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            self.assertIn("nexus-private-cloud", argv)
            self.assertIn("down", argv)
            self.assertNotIn("-v", argv)
            self.assertNotIn("--volumes", argv)
            self.assertNotIn("--rmi", argv)
            self.assertFalse(any(token in ("rm", "umount", "mount", "systemctl", "kill") for token in argv))
            remaining = run.call_args_list[1].args[0]
            self.assertIn("ps", remaining)
            self.assertIn("label=com.docker.compose.project=nexus-private-cloud", remaining)

    def test_down_cannot_report_off_while_this_projects_container_is_running(self):
        with patch("builtins.print") as output, \
             patch.object(self.cloud, "check_docker") as docker, \
             patch.object(self.cloud, "run", side_effect=["", "fake-running-container\n"]):
            with self.assertRaisesRegex(self.cloud.PrivateCloudError, "down_incomplete"):
                self.cloud.down()
        output.assert_not_called()
        docker.assert_called_once_with(for_start=False)

    def test_busy_loopback_port_is_rejected_without_real_network_io(self):
        fake_socket = MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        fake_socket.bind.side_effect = OSError("fake address busy")
        with patch.object(self.cloud.socket, "socket", return_value=fake_socket):
            with self.assertRaises(self.cloud.PrivateCloudError):
                self.cloud.check_port()
        fake_socket.bind.assert_called_once_with(("127.0.0.1", 8080))

    def test_free_loopback_port_is_only_probed_locally(self):
        fake_socket = MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        with patch.object(self.cloud.socket, "socket", return_value=fake_socket):
            self.cloud.check_port()
        fake_socket.bind.assert_called_once_with(("127.0.0.1", 8080))

    def test_run_has_no_shell_and_does_not_inherit_remote_docker_settings(self):
        result = subprocess.CompletedProcess(["fake-tool"], 0, stdout="ok", stderr="")
        with patch.dict(self.cloud.os.environ, {
            "DOCKER_HOST": "tcp://invalid.test:2375",
            "DOCKER_CONTEXT": "unsafe-context",
            "COMPOSE_FILE": "/fake/untrusted.yaml",
            "COMPOSE_PROJECT_NAME": "other-project",
        }), patch.object(self.cloud.subprocess, "run", return_value=result) as run:
            self.assertEqual(self.cloud.run(["fake-tool"]), "ok")
        self.assertEqual(run.call_args.args[0], ["fake-tool"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        environment = run.call_args.kwargs["env"]
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
            self.assertNotIn(key, environment)

    def test_changed_storage_before_creation_or_launch_never_starts_compose(self):
        changed = ("/dev/nvme1n1p1", "259:2", self.settings.uuid)
        for before_creation in (False, True):
            with self.subTest(before_creation=before_creation), ExitStack() as stack:
                doubles = self.patched_up(stack)
                same = [self.signature] if before_creation else [self.signature, self.signature]
                doubles["check_storage"].side_effect = same + [changed]
                with self.assertRaisesRegex(self.cloud.PrivateCloudError, "ssd_changed"):
                    self.cloud.up(self.settings, confirmed=True)
                doubles["run"].assert_not_called()
                self.assertEqual(doubles["prepare_directories"].call_count, 0 if before_creation else 1)

    def test_start_or_post_start_failure_closes_only_this_project(self):
        for failure in ("compose", "storage_after_up", "bmo_after_up"):
            with self.subTest(failure=failure), ExitStack() as stack:
                doubles = self.patched_up(stack)
                shutdown = stack.enter_context(patch.object(self.cloud, "down"))
                if failure == "compose":
                    doubles["run"].side_effect = self.cloud.PrivateCloudError("fake start failed")
                elif failure == "storage_after_up":
                    doubles["check_storage"].side_effect = [
                        self.signature, self.signature, self.signature,
                        self.cloud.PrivateCloudError("fake storage disappeared"),
                    ]
                else:
                    doubles["check_bmo_stopped"].side_effect = [
                        None, None, self.cloud.PrivateCloudError("fake BMO reappeared"),
                    ]
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.cloud.up(self.settings, confirmed=True)
                shutdown.assert_called_once_with()
                self.assertEqual(doubles["run"].call_count, 1)
                self.assertIn("up", doubles["run"].call_args.args[0])

    def test_run_command_failure_does_not_expose_arguments_or_raw_logs(self):
        failure = subprocess.CalledProcessError(1, ["fake", "SECRET_VALUE"], stderr="SECRET_VALUE")
        with patch.object(self.cloud.subprocess, "run", side_effect=failure):
            with self.assertRaises(self.cloud.PrivateCloudError) as caught:
                self.cloud.run(["fake", "SECRET_VALUE"])
        self.assertNotIn("SECRET_VALUE", str(caught.exception))

    def test_docker_requires_linux_and_loopback_safe_server_version(self):
        cases = (("linux", "28.0.0", True), ("linux", "29.1.3", True),
                 ("linux", "27.5.1", False), ("linux", "unknown", False),
                 ("windows", "28.0.0", False), ("", "28.0.0", False))
        for operating_system, version, accepted in cases:
            with self.subTest(operating_system=operating_system, version=version):
                with patch.object(self.cloud, "run", side_effect=[
                    "Docker Compose version v2.39.4", operating_system, version,
                ]) as run:
                    if accepted:
                        self.cloud.check_docker()
                    else:
                        with self.assertRaises(self.cloud.PrivateCloudError):
                            self.cloud.check_docker()
                for call in run.call_args_list:
                    self.assertEqual(call.args[0][:3],
                                     ["docker", "--host", "unix:///var/run/docker.sock"])

    def test_old_docker_can_still_stop_this_project_without_start_version_gate(self):
        with patch.object(self.cloud, "run", side_effect=[
            "Docker Compose version v2.39.4", "linux",
        ]) as run:
            self.cloud.check_docker(for_start=False)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(any("compose" in call.args[0] for call in run.call_args_list))
        self.assertTrue(any("info" in call.args[0] for call in run.call_args_list))
        self.assertFalse(any("{{.Server.Version}}" in call.args[0] for call in run.call_args_list))


class PrivateCloudSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = load_helper()

    def config(self, changes=None):
        values = {
            "NEXUS_SSD_MOUNT": "/mnt/test-ssd",
            "NEXUS_SSD_UUID": "11111111-2222-3333-4444-555555555555",
            "NEXUS_SSD_LABEL": "BMO_SSD",
            "NEXUS_MIN_FREE_BYTES": "1073741824",
            "BMO_SCAN_UID": "1000",
        }
        values.update(changes or {})
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"

    def read(self, text, *, owner=0, mode=stat.S_IFREG | 0o600):
        metadata = SimpleNamespace(st_mode=mode, st_uid=owner, st_gid=0,
                                   st_nlink=1, st_size=len(text))
        with patch.object(Path, "read_text", return_value=text), \
             patch.object(Path, "stat", return_value=metadata), \
             patch.object(self.cloud, "run", side_effect=AssertionError("config must not run commands")):
            return self.cloud.load_settings(Path("/fake/private-cloud.env"))

    def test_explicit_settings_load_without_shell_expansion(self):
        settings = self.read("# local test settings\n" + self.config())
        self.assertEqual(settings.mount, Path("/mnt/test-ssd"))
        self.assertEqual(settings.uuid, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(settings.label, "BMO_SSD")
        self.assertEqual(settings.min_free_bytes, 1073741824)
        self.assertEqual(settings.bmo_uid, 1000)

    def test_settings_require_private_root_owned_regular_file(self):
        for owner, mode in ((1000, stat.S_IFREG | 0o600),
                            (0, stat.S_IFREG | 0o644),
                            (0, stat.S_IFREG | 0o666),
                            (0, stat.S_IFLNK | 0o600)):
            with self.subTest(owner=owner, mode=oct(mode)):
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.read(self.config(), owner=owner, mode=mode)

    def test_invalid_missing_or_shell_like_settings_are_rejected(self):
        invalid = (
            self.config({"NEXUS_SSD_UUID": "CHANGE_ME"}),
            self.config({"NEXUS_SSD_UUID": "$(fake-command)"}),
            self.config({"NEXUS_SSD_MOUNT": "relative/path"}),
            self.config({"NEXUS_SSD_MOUNT": "/"}),
            self.config({"NEXUS_MIN_FREE_BYTES": "0"}),
            self.config({"NEXUS_MIN_FREE_BYTES": "not-a-number"}),
            self.config() + "UNKNOWN_OPTION=yes\n",
            self.config() + "NEXUS_SSD_UUID=11111111-2222-3333-4444-555555555555\n",
            self.config().replace("NEXUS_SSD_UUID=11111111-2222-3333-4444-555555555555\n", ""),
            self.config() + "export EXTRA=value\n",
        )
        for index, text in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.read(text)


class PrivateCloudStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = load_helper()

    def setUp(self):
        self.settings = self.cloud.Settings(
            mount=Path("/mnt/test-ssd"),
            uuid="11111111-2222-3333-4444-555555555555",
        )
        self.filesystem = {
            "target": str(self.settings.mount), "source": "/dev/nvme0n1p1",
            "fstype": "ext4", "uuid": self.settings.uuid, "label": "BMO_SSD",
            "options": "rw,relatime", "maj:min": "259:1",
        }

    def storage_environment(self, stack, *, filesystem=None, size="1953525168\n",
                            state="live\n", free=10737418240):
        """findmnt/sysfs/stat/공간 확인 모두 가짜이며 실제 장치에 접근하지 않는다."""
        info = {"filesystems": [self.filesystem if filesystem is None else filesystem]}
        runner = stack.enter_context(patch.object(self.cloud, "run", return_value=json.dumps(info)))
        device_number = self.cloud.os.makedev(259, 1)

        def fake_stat(path, *args, **kwargs):
            path = str(path)
            is_block = path == "/dev/nvme0n1p1"
            return SimpleNamespace(
                st_mode=(stat.S_IFBLK if is_block else stat.S_IFDIR) | 0o700,
                st_rdev=device_number if is_block else 0,
                st_dev=device_number if path != "/" else self.cloud.os.makedev(179, 2),
                st_uid=0, st_gid=0,
            )

        def fake_resolve(path, *args, **kwargs):
            if str(path) == "/sys/dev/block/259:1":
                return Path("/sys/devices/pci0000/nvme/nvme0/nvme0n1/nvme0n1p1")
            return path

        def fake_read(path, *args, **kwargs):
            if path.name == "size":
                return size
            if path.name == "state":
                return state
            if path.name == "dev":
                return "259:1\n"
            raise AssertionError(f"unexpected fake storage read: {path}")

        stack.enter_context(patch.object(self.cloud.os, "stat", side_effect=fake_stat))
        stack.enter_context(patch.object(self.cloud.os, "access", return_value=True))
        stack.enter_context(patch.object(Path, "resolve", autospec=True, side_effect=fake_resolve))
        stack.enter_context(patch.object(Path, "read_text", autospec=True, side_effect=fake_read))
        stack.enter_context(patch.object(self.cloud.shutil, "disk_usage", return_value=SimpleNamespace(free=free)))
        stack.enter_context(patch.object(self.cloud.os, "open", side_effect=AssertionError("storage check must not open writable files")))
        return runner

    def test_valid_storage_requires_exact_mount_and_returns_identity_signature(self):
        with ExitStack() as stack:
            runner = self.storage_environment(stack)
            signature = self.cloud.check_storage(self.settings)
        self.assertEqual(signature, ("/dev/nvme0n1p1", "259:1", self.settings.uuid))
        argv = runner.call_args.args[0]
        self.assertEqual(argv[0], "findmnt")
        self.assertIn("--mountpoint", argv)
        self.assertIn(str(self.settings.mount), argv)

    def test_wrong_mount_filesystem_or_disk_identity_cannot_pass(self):
        changes = (
            {"target": "/"}, {"target": "/mnt/test-ssd/subdirectory"},
            {"fstype": "tmpfs"}, {"fstype": "ext3"},
            {"uuid": "66666666-7777-8888-9999-000000000000"},
            {"label": "OTHER_DISK"}, {"options": "ro,relatime"},
            {"source": "/dev/mmcblk0p2"},
            {"source": "/dev/nvme0n1p1[/unapproved-subdir]"},
        )
        for change in changes:
            with self.subTest(change=change), ExitStack() as stack:
                self.storage_environment(stack, filesystem={**self.filesystem, **change})
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.cloud.check_storage(self.settings)

    def test_missing_or_malformed_mount_observation_fails_closed(self):
        for output in ("not-json", "{}", '{"filesystems": []}'):
            with self.subTest(output=output), ExitStack() as stack:
                runner = self.storage_environment(stack)
                runner.return_value = output
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.cloud.check_storage(self.settings)

    def test_zero_capacity_dead_controller_and_low_space_fail_closed(self):
        for scenario in ({"size": "0\n"}, {"state": "dead\n"},
                         {"free": self.settings.min_free_bytes - 1}):
            with self.subTest(scenario=scenario), ExitStack() as stack:
                self.storage_environment(stack, **scenario)
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.cloud.check_storage(self.settings)


class PrivateCloudDirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = load_helper()

    def setUp(self):
        self.settings = self.cloud.Settings(
            mount=Path("/mnt/test-ssd"),
            uuid="11111111-2222-3333-4444-555555555555",
        )
        self.signature = ("/dev/nvme0n1p1", "259:1", self.settings.uuid)

    def test_directory_walk_never_follows_symlinks_and_closes_its_descriptors(self):
        with patch.object(self.cloud.os, "open", side_effect=[10, 11, 12]) as opened, \
             patch.object(self.cloud.os, "close") as closed:
            with self.cloud.directory_fd(self.settings.mount) as fd:
                self.assertEqual(fd, 12)
        self.assertEqual([call.args[0] for call in opened.call_args_list], ["/", "mnt", "test-ssd"])
        for call in opened.call_args_list[1:]:
            self.assertTrue(call.args[1] & self.cloud.os.O_NOFOLLOW)
            self.assertTrue(call.args[1] & self.cloud.os.O_DIRECTORY)
        self.assertEqual([call.args[0] for call in closed.call_args_list], [10, 11, 12])

    def test_symlink_in_mount_path_cannot_create_any_test_folder(self):
        with patch.object(self.cloud.os, "open", side_effect=[10, OSError("fake symlink rejected")]), \
             patch.object(self.cloud.os, "close") as closed, \
             patch.object(self.cloud.os, "mkdir") as mkdir:
            with self.assertRaises(OSError):
                self.cloud.prepare_directories(self.settings, self.signature)
        mkdir.assert_not_called()
        closed.assert_called_once_with(10)

    def test_mount_changed_to_sd_is_rejected_before_directory_creation(self):
        mount_context = MagicMock()
        mount_context.__enter__.return_value = 30
        sd = SimpleNamespace(st_dev=self.cloud.os.makedev(179, 2))
        with patch.object(self.cloud, "directory_fd", return_value=mount_context), \
             patch.object(self.cloud.os, "fstat", return_value=sd), \
             patch.object(self.cloud.os, "mkdir") as mkdir, \
             patch.object(self.cloud.os, "open") as opened:
            with self.assertRaisesRegex(self.cloud.PrivateCloudError, "ssd_changed"):
                self.cloud.prepare_directories(self.settings, self.signature)
        mkdir.assert_not_called()
        opened.assert_not_called()

    def existing_folders(self, stack, *, bad_owner=False, bad_mode=False):
        mount_context = MagicMock()
        mount_context.__enter__.return_value = 30
        stack.enter_context(patch.object(self.cloud, "directory_fd", return_value=mount_context))
        stack.enter_context(patch.object(self.cloud.os, "mkdir", side_effect=FileExistsError))
        opened = stack.enter_context(patch.object(self.cloud.os, "open", side_effect=[41, 42, 43]))
        closed = stack.enter_context(patch.object(self.cloud.os, "close"))
        chown = stack.enter_context(patch.object(self.cloud.os, "fchown"))
        chmod = stack.enter_context(patch.object(self.cloud.os, "fchmod"))

        def info(fd):
            owner = 0 if fd in (30, 41) else 1000
            if fd == 42 and bad_owner:
                owner = 0
            mode = 0o755 if fd == 42 and bad_mode else 0o700
            return SimpleNamespace(st_dev=self.cloud.os.makedev(259, 1),
                                   st_uid=owner, st_gid=owner, st_mode=stat.S_IFDIR | mode)

        stack.enter_context(patch.object(self.cloud.os, "fstat", side_effect=info))
        return opened, closed, chown, chmod

    def test_existing_valid_data_is_preserved_without_chown_or_chmod(self):
        with ExitStack() as stack:
            opened, closed, chown, chmod = self.existing_folders(stack)
            self.cloud.prepare_directories(self.settings, self.signature)
            self.assertEqual([call.args[0] for call in opened.call_args_list],
                             ["nexus-private-cloud-test", "data", "state"])
            self.assertEqual(sorted(call.args[0] for call in closed.call_args_list), [41, 42, 43])
            chown.assert_not_called()
            chmod.assert_not_called()
            for call in opened.call_args_list:
                self.assertTrue(call.args[1] & self.cloud.os.O_NOFOLLOW)

    def test_existing_wrong_owner_or_mode_is_rejected_without_permission_repair(self):
        for bad in ({"bad_owner": True}, {"bad_mode": True}):
            with self.subTest(bad=bad), ExitStack() as stack:
                _, _, chown, chmod = self.existing_folders(stack, **bad)
                with self.assertRaisesRegex(self.cloud.PrivateCloudError, "data_permissions"):
                    self.cloud.prepare_directories(self.settings, self.signature)
                chown.assert_not_called()
                chmod.assert_not_called()


class PrivateCloudBmoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = load_helper()

    def setUp(self):
        self.settings = self.cloud.Settings(
            mount=Path("/mnt/test-ssd"),
            uuid="11111111-2222-3333-4444-555555555555",
        )

    def scan(self, command, *, uid=1000, parent=1, comm=None, error=None, vanished=False):
        """한 가상 PID만 공급한다. 호스트 /proc 조회와 신호 전송은 허용하지 않는다."""
        proc_root = Path("/fake/proc")
        pid = proc_root / "123"
        executable = comm or (Path(command[0]).name if command else "unknown")
        status = (f"Name:\t{executable}\nState:\tS (sleeping)\n"
                  f"PPid:\t{parent}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")

        def read_text(path, *args, **kwargs):
            if error is not None:
                raise error
            if path.name == "status":
                return status
            if path.name == "comm":
                return executable + "\n"
            raise AssertionError(f"unexpected fake process read: {path}")

        def read_bytes(path, *args, **kwargs):
            if error is not None:
                raise error
            if path.name != "cmdline":
                raise AssertionError(f"unexpected fake process bytes: {path}")
            return b"\x00".join(arg.encode() for arg in command) + (b"\x00" if command else b"")

        with patch.object(Path, "iterdir", autospec=True, return_value=iter([pid])), \
             patch.object(Path, "read_text", autospec=True, side_effect=read_text), \
             patch.object(Path, "read_bytes", autospec=True, side_effect=read_bytes), \
             patch.object(Path, "exists", return_value=not vanished), \
             patch.object(Path, "is_dir", return_value=True), \
             patch.object(Path, "readlink", return_value=Path("/fake/non-bmo")), \
             patch.object(self.cloud.os, "getpid", return_value=9999), \
             patch.object(self.cloud.os, "kill", side_effect=AssertionError("never signal BMO")), \
             patch.object(self.cloud.subprocess, "run", side_effect=AssertionError("scan must not run commands")):
            return self.cloud.check_bmo_stopped(self.settings, proc_root=proc_root)

    def test_bmo_markers_and_unidentified_robot_uid_workloads_are_rejected(self):
        commands = (
            (["python3", "-m", "bmo_ai.desktop.main"], 0),
            (["python3", "/fake/worker.py"], 1000),
            (["node", "/fake/worker.js"], 1000),
            (["chromium", "--no-sandbox"], 1000),
            (["aplay", "fake.wav"], 1000),
            (["arecord"], 1000),
            (["rpicam-hello"], 1000),
        )
        for command, uid in commands:
            with self.subTest(program=command[0], uid=uid):
                with self.assertRaises(self.cloud.PrivateCloudError):
                    self.scan(command, uid=uid)

    def test_unreadable_or_empty_userspace_process_is_unknown_and_rejected(self):
        with self.assertRaises(self.cloud.PrivateCloudError):
            self.scan(["python3"], error=PermissionError("fake inaccessible process"))
        with self.assertRaises(self.cloud.PrivateCloudError):
            self.scan([], comm="python3", parent=1)

    def test_protected_service_and_kernel_thread_do_not_trigger_process_changes(self):
        self.scan(["sshd", "-D"], uid=0)
        self.scan([], uid=0, parent=2, comm="kworker")

    def test_process_that_disappeared_during_scan_is_not_assumed_running(self):
        self.scan(["python3"], error=FileNotFoundError("fake exited process"), vanished=True)


if __name__ == "__main__":
    unittest.main()
