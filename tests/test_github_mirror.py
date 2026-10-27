"""가짜 SSD와 임시 로컬 Git만 사용하는 미러 검사. 실제 Pi/GitHub 검증이 아니다."""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "FAKE_PRIVATE_SENTINEL"
REAL_RUN = subprocess.run


def load_mirror():
    spec = importlib.util.spec_from_file_location(
        "github_mirror_test_helper", ROOT / "scripts/github_mirror.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]), \
         patch("subprocess.run", side_effect=AssertionError("import cannot execute Git")), \
         patch.object(Path, "mkdir", side_effect=AssertionError("import cannot write files")):
        spec.loader.exec_module(module)
    return module


def inventory(path):
    """임시 fixture의 파일 내용과 링크만 비교한다. 실제 백업을 조사하지 않는다."""
    result = {}
    for item in sorted(path.rglob("*")):
        relative = str(item.relative_to(path))
        if item.is_symlink():
            result[relative] = ("symlink", os.readlink(item))
        elif item.is_file():
            result[relative] = ("file", hashlib.sha256(item.read_bytes()).hexdigest())
        elif item.is_dir():
            result[relative] = ("directory",)
    return result


class MirrorFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mirror = load_mirror()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # 실제 /mnt/ssd 대신 NEXUS 저장소 안의 임시 디렉터리만 사용한다.
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mirror-test-", dir=ROOT))
        self.work = Path(directory)
        self.mount = self.work / "fake-ssd"
        self.mount.mkdir(mode=0o700)
        self.settings = self.mirror.cloud.Settings(
            mount=self.mount,
            uuid="11111111-2222-3333-4444-555555555555",
        )
        device = self.mount.stat().st_dev
        self.signature = ("/dev/nvme0n1p1", f"{os.major(device)}:{os.minor(device)}", self.settings.uuid)
        self.stack.enter_context(patch.object(self.mirror, "FIXED_MOUNT", self.mount))
        self.stack.enter_context(patch.object(self.mirror, "OWNER_UID", os.getuid()))
        self.stack.enter_context(patch.object(self.mirror, "OWNER_GID", os.getgid()))
        self.storage = self.stack.enter_context(patch.object(
            self.mirror.cloud, "check_storage", return_value=self.signature))
        self.bmo = self.stack.enter_context(patch.object(self.mirror.cloud, "check_bmo_stopped"))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.output))
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)

    @property
    def base(self):
        return self.mount / self.mirror.BASE_NAME

    def generations(self):
        return sorted(item for item in self.base.iterdir() if item.name.startswith("gen-") and item.is_dir())

    def sync(self, runner, *, confirmed=True):
        return self.mirror.sync(self.settings, confirmed=confirmed, runner=runner)


class MirrorPreflightTests(MirrorFixture):
    def test_bmo_confirmation_precedes_every_observation_and_write(self):
        runner = Mock(side_effect=AssertionError("no Git before confirmation"))
        with self.assertRaises(self.mirror.MirrorError):
            self.sync(runner, confirmed=False)
        self.storage.assert_not_called()
        self.bmo.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(list(self.mount.iterdir()), [])

    def test_wrong_mount_or_label_never_starts_a_backup(self):
        for settings in (
            self.mirror.cloud.Settings(self.work / "other", self.settings.uuid),
            self.mirror.cloud.Settings(self.mount, self.settings.uuid, label="OTHER_DISK"),
        ):
            with self.subTest(settings=settings):
                runner = Mock(side_effect=AssertionError("invalid destination cannot run Git"))
                with self.assertRaises(self.mirror.MirrorError):
                    self.mirror.sync(settings, confirmed=True, runner=runner)
                runner.assert_not_called()
                self.assertEqual(list(self.mount.iterdir()), [])

    def test_storage_missing_zero_dead_read_only_or_low_space_cannot_write(self):
        for scenario in ("missing mount", "capacity zero", "controller dead", "read only", "low space"):
            with self.subTest(scenario=scenario):
                self.storage.side_effect = self.mirror.cloud.PrivateCloudError("ssd_rejected: " + scenario)
                runner = Mock(side_effect=AssertionError("bad SSD cannot run Git"))
                with self.assertRaises(self.mirror.cloud.PrivateCloudError):
                    self.sync(runner)
                runner.assert_not_called()
                self.assertEqual(list(self.mount.iterdir()), [])

    def test_bmo_busy_does_not_start_git_or_change_existing_robot_data(self):
        robot = self.mount / "existing-robot-data"
        robot.mkdir()
        (robot / "keep.txt").write_bytes(b"unchanged robot data")
        before = inventory(self.mount)
        self.bmo.side_effect = self.mirror.cloud.PrivateCloudError("bmo_busy_or_unknown")
        runner = Mock(side_effect=AssertionError("busy BMO cannot run Git"))
        with self.assertRaises(self.mirror.cloud.PrivateCloudError):
            self.sync(runner)
        runner.assert_not_called()
        self.assertEqual(inventory(self.mount), before)

    def test_existing_unclaimed_directory_is_preserved_without_adoption(self):
        self.base.mkdir(mode=0o700)
        (self.base / "keep.txt").write_bytes(b"not a NEXUS mirror store")
        before = inventory(self.base)
        runner = Mock(side_effect=AssertionError("unclaimed store cannot run Git"))
        with self.assertRaises((self.mirror.MirrorError, FileNotFoundError)):
            self.sync(runner)
        runner.assert_not_called()
        self.assertEqual(inventory(self.base), before)

    def test_symlink_store_never_reaches_other_checkout_or_cloud_data(self):
        outside = self.mount / "nexus-private-cloud-test"
        outside.mkdir(mode=0o700)
        (outside / "keep.txt").write_bytes(b"existing cloud database stand-in")
        self.base.symlink_to(outside, target_is_directory=True)
        before = inventory(outside)
        runner = Mock(side_effect=AssertionError("symlink cannot run Git"))
        with self.assertRaises(self.mirror.MirrorError):
            self.sync(runner)
        runner.assert_not_called()
        self.assertEqual(inventory(outside), before)
        self.assertTrue(self.base.is_symlink())

    def test_changed_mount_identity_is_rejected_before_a_git_process(self):
        self.storage.side_effect = [self.signature, ("/dev/nvme1n1p1", "259:2", self.settings.uuid)]
        runner = Mock(side_effect=AssertionError("changed SSD cannot run Git"))
        with self.assertRaisesRegex(self.mirror.MirrorError, "ssd_changed"):
            self.sync(runner)
        runner.assert_not_called()
        self.assertEqual(list(self.mount.iterdir()), [])

    def test_status_with_no_store_does_not_create_a_directory(self):
        runner = Mock(side_effect=AssertionError("empty status cannot execute Git"))
        with patch.object(self.mirror.os, "mkdir", side_effect=AssertionError("status cannot mkdir")):
            result = self.mirror.status(self.settings, runner=runner)
        self.assertEqual(result, {"complete": 0, "partial": 0, "latest": None})
        runner.assert_not_called()
        self.assertEqual(list(self.mount.iterdir()), [])

    def test_exclusive_file_write_preserves_a_preexisting_file(self):
        target = self.mount / "keep"
        target.write_bytes(b"keep original")
        fd = os.open(self.mount, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(self.mirror.MirrorError, "path_rejected"):
                self.mirror.write_file(fd, "keep", b"must never replace", self.signature)
        finally:
            os.close(fd)
        self.assertEqual(target.read_bytes(), b"keep original")

    def test_pinned_directory_device_change_cannot_write_a_fallback_file(self):
        fd = os.open(self.mount, os.O_RDONLY | os.O_DIRECTORY)
        try:
            info = os.fstat(fd)
            other = SimpleNamespace(st_dev=info.st_dev + 1, st_mode=info.st_mode,
                                    st_uid=info.st_uid, st_gid=info.st_gid)
            with patch.object(self.mirror.os, "fstat", return_value=other):
                with self.assertRaisesRegex(self.mirror.MirrorError, "ssd_changed"):
                    self.mirror.verify_fd(fd, self.signature)
        finally:
            os.close(fd)
        self.assertEqual(list(self.mount.iterdir()), [])


class MirrorLocalGitTests(MirrorFixture):
    """고정 HTTPS 입력만 로컬 fixture로 치환한다. 네트워크·인증은 사용하지 않는다."""

    def setUp(self):
        super().setUp()
        self.git = shutil.which("git")
        if self.git is None:
            self.skipTest("Git이 없어 임시 로컬 Git fixture 검사를 생략")
        self.source = self.work / "source"
        self.source.mkdir(mode=0o700)
        self.git_env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
            "HOME": str(self.work / "empty-home"),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        }
        self.git_fixture("init", "--initial-branch=main", str(self.source))
        (self.source / "README.md").write_bytes("첫 번째 복원 내용\r\n".encode())
        (self.source / "AGENTS.md").write_bytes(b"fixture instructions\n")
        self.git_fixture("add", "README.md", "AGENTS.md")
        self.git_fixture("-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-m", "first fixture commit")
        self.git_fixture("tag", "fixture-v1")
        self.git_fixture("branch", "removed-later")
        self.calls = []

    def git_fixture(self, *args):
        result = REAL_RUN([self.git, "-c", "core.hooksPath=/dev/null", *args],
                          cwd=self.source, env=self.git_env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, timeout=20)
        return result.stdout

    def local_runner(self, args, **options):
        self.calls.append(list(args))

        def local_only(argv, **kwargs):
            argv = list(argv)
            if self.mirror.SOURCE in argv:
                argv[argv.index(self.mirror.SOURCE)] = str(self.source)
            kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}
            # production의 HTTPS 전용 옵션은 이 임시 source 치환에만 한정해 허용한다.
            argv[1:1] = ["-c", "protocol.file.allow=always"]
            self.assertFalse(any("github.com" in part for part in argv))
            return REAL_RUN(argv, **kwargs)

        with patch.object(self.mirror.subprocess, "run", side_effect=local_only):
            return self.mirror.run_git(args, **options)

    def test_first_backup_restores_real_blob_bytes_and_complete_marker(self):
        protected = self.mount / "nexus-private-cloud-test"
        protected.mkdir(mode=0o700)
        (protected / "state").mkdir(mode=0o700)
        (protected / "state" / "existing-db").write_bytes(b"preserve existing cloud state")
        before_protected = inventory(protected)
        before_source = inventory(self.source)
        report = self.sync(self.local_runner)
        generations = self.generations()
        self.assertEqual(len(generations), 1)
        generation = generations[0]
        self.assertTrue((generation / "mirror.git").is_dir())
        self.assertTrue((generation / "complete.json").is_file())
        for name in ("README.md", "AGENTS.md"):
            self.assertEqual((generation / "restore-test" / name).read_bytes(), (self.source / name).read_bytes())
        self.assertEqual(inventory(self.source), before_source)
        self.assertEqual(inventory(protected), before_protected)
        self.assertEqual(report["restored"], 2)
        self.assertGreaterEqual(report["heads"], 2)
        self.assertGreaterEqual(report["tags"], 1)
        self.assertTrue(any("clone" in args and "--mirror" in args and self.mirror.SOURCE in args
                            for args in self.calls))
        for args in self.calls:
            self.assertFalse(any(token in args for token in (
                "push", "--force", "--prune", "--prune-tags", "checkout", "reset", "clean",
            )))

    def test_update_creates_new_generation_and_retains_deleted_or_rewritten_refs(self):
        fixed_time = Mock()
        fixed_time.now.return_value.strftime.return_value = "20260101T000000Z"
        self.stack.enter_context(patch.object(self.mirror, "datetime", fixed_time))
        self.stack.enter_context(patch.object(self.mirror.uuid, "uuid4", side_effect=[
            SimpleNamespace(hex="f" * 32), SimpleNamespace(hex="0" * 32),
        ]))
        first_report = self.sync(self.local_runner)
        first = self.generations()[0]
        before_first = inventory(first)
        first_marker = (first / "complete.json").read_bytes()
        (self.source / "README.md").write_bytes(b"second fixture bytes\n")
        self.git_fixture("add", "README.md")
        self.git_fixture("-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-m", "second fixture commit")
        self.git_fixture("branch", "-d", "removed-later")
        head = self.git_fixture("rev-parse", "HEAD").strip().decode()
        self.git_fixture("update-ref", "refs/tags/fixture-v1", head)
        second_report = self.sync(self.local_runner)
        generations = self.generations()
        self.assertEqual(len(generations), 2)
        second = next(item for item in generations if item != first)
        self.assertEqual(inventory(first), before_first)
        self.assertEqual((first / "complete.json").read_bytes(), first_marker)
        self.assertEqual((second / "restore-test" / "README.md").read_bytes(), b"second fixture bytes\n")
        self.assertNotEqual((first / "restore-test" / "README.md").read_bytes(),
                            (second / "restore-test" / "README.md").read_bytes())
        self.assertEqual(first_report["heads"], 2)
        self.assertEqual(second_report["heads"], 1)
        self.assertNotEqual(first_report["refs_sha256"], second_report["refs_sha256"])
        status = self.mirror.status(self.settings, runner=self.local_runner)
        self.assertEqual(status["complete"], 2)
        self.assertEqual(status["partial"], 0)
        self.assertEqual(status["latest"]["head"], second_report["head"])
        self.assertNotEqual(status["latest"]["head"], first_report["head"])
        self.assertEqual(inventory(first), before_first)

    def test_status_revalidates_without_writing_or_starting_a_network_clone(self):
        report = self.sync(self.local_runner)
        before = inventory(self.base)
        self.calls.clear()
        with patch.object(self.mirror.os, "mkdir", side_effect=AssertionError("status cannot create folders")), \
             patch.object(self.mirror, "write_file", side_effect=AssertionError("status cannot write reports")):
            status = self.mirror.status(self.settings, runner=self.local_runner)
        self.assertEqual(status["latest"]["head"], report["head"])
        self.assertEqual(status["complete"], 1)
        self.assertEqual(inventory(self.base), before)
        self.assertFalse(any("clone" in args or "fetch" in args for args in self.calls))

    def test_network_failure_preserves_partial_generation_and_prior_success(self):
        self.sync(self.local_runner)
        first = self.generations()[0]
        before = inventory(first)

        def failed_clone(args, **options):
            self.assertIn("clone", args)
            os.mkdir("mirror.git", mode=0o700, dir_fd=options["cwd_fd"])
            raise self.mirror.MirrorError("git_failed")

        with self.assertRaisesRegex(self.mirror.MirrorError, "git_failed"):
            self.sync(failed_clone)
        generations = self.generations()
        self.assertEqual(len(generations), 2)
        partial = next(item for item in generations if item != first)
        self.assertTrue((partial / "mirror.git").is_dir())
        self.assertFalse((partial / "complete.json").exists())
        self.assertEqual(inventory(first), before)
        status = self.mirror.status(self.settings, runner=self.local_runner)
        self.assertEqual((status["complete"], status["partial"]), (1, 1))

    def test_corrupt_blob_result_cannot_be_reported_as_a_restored_backup(self):
        def corrupt_blob(args, **options):
            result = self.local_runner(args, **options)
            if "cat-file" in args and "blob" in args:
                return result + b"unexpected bytes"
            return result

        with self.assertRaisesRegex(self.mirror.MirrorError, "restore_invalid"):
            self.sync(corrupt_blob)
        self.assertEqual(len(self.generations()), 1)
        self.assertFalse((self.generations()[0] / "complete.json").exists())

    def test_restored_file_tampering_is_refused_by_read_only_status(self):
        self.sync(self.local_runner)
        generation = self.generations()[0]
        (generation / "restore-test" / "README.md").write_bytes(b"tampered fixture restore")
        before = inventory(generation)
        with self.assertRaisesRegex(self.mirror.MirrorError, "manifest_invalid"):
            self.mirror.status(self.settings, runner=self.local_runner)
        self.assertEqual(inventory(generation), before)

    def test_changed_mirror_config_is_rejected_before_git_reads(self):
        self.sync(self.local_runner)
        generation = self.generations()[0]
        with (generation / "mirror.git" / "config").open("ab") as config:
            config.write(b"\n[include]\n path = fake-private-config\n")
        runner = Mock(side_effect=AssertionError("changed config cannot invoke Git"))
        with self.assertRaisesRegex(self.mirror.MirrorError, "manifest_invalid"):
            self.mirror.status(self.settings, runner=runner)
        runner.assert_not_called()

    def test_hardlinked_marker_is_not_adopted_or_modified(self):
        self.sync(self.local_runner)
        marker = self.base / self.mirror.STORE_MARKER
        outside = self.mount / "outside-marker"
        os.link(marker, outside)
        before = outside.read_bytes()
        runner = Mock(side_effect=AssertionError("shared inode cannot run Git"))
        with self.assertRaisesRegex(self.mirror.MirrorError, "path_rejected"):
            self.sync(runner)
        runner.assert_not_called()
        self.assertEqual(outside.read_bytes(), before)
        self.assertEqual(outside.stat().st_nlink, 2)

    def test_changed_permissions_are_not_automatically_repaired(self):
        self.sync(self.local_runner)
        self.base.chmod(0o755)
        runner = Mock(side_effect=AssertionError("unsafe permissions cannot run Git"))
        with self.assertRaisesRegex(self.mirror.MirrorError, "path_rejected"):
            self.sync(runner)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o755)
        runner.assert_not_called()

    def test_generation_name_collision_never_overwrites_or_removes_a_prior_backup(self):
        fixed_time = Mock()
        fixed_time.now.return_value.strftime.return_value = "20260101T000000Z"
        with patch.object(self.mirror, "datetime", fixed_time), \
             patch.object(self.mirror.uuid, "uuid4", return_value=SimpleNamespace(hex="0" * 32)):
            self.sync(self.local_runner)
            before = inventory(self.base)
            runner = Mock(side_effect=AssertionError("collision cannot invoke Git"))
            with self.assertRaisesRegex(self.mirror.MirrorError, "path_rejected"):
                self.sync(runner)
        runner.assert_not_called()
        self.assertEqual(inventory(self.base), before)

    def test_io_failure_after_clone_keeps_partial_objects_without_completion(self):
        def disk_fails_after_clone(args, **options):
            result = self.local_runner(args, **options)
            if "clone" in args:
                self.storage.side_effect = OSError(errno.EIO, PRIVATE)
            return result

        with self.assertRaises(OSError):
            self.sync(disk_fails_after_clone)
        generation = self.generations()[0]
        self.assertTrue((generation / "mirror.git" / "objects").is_dir())
        self.assertFalse((generation / "complete.json").exists())
        self.assertEqual(self.mirror.safe_reason(OSError(errno.EIO, PRIVATE)), "space_or_io_error")
        self.assertNotIn(PRIVATE, self.output.getvalue())

    def test_full_disk_during_restore_never_marks_a_partial_copy_complete(self):
        original_write = self.mirror.write_file

        def limited_disk(parent_fd, name, data, signature):
            if name == "README.md":
                raise OSError(errno.ENOSPC, PRIVATE)
            return original_write(parent_fd, name, data, signature)

        with patch.object(self.mirror, "write_file", side_effect=limited_disk), self.assertRaises(OSError):
            self.sync(self.local_runner)
        generation = self.generations()[0]
        self.assertTrue((generation / "mirror.git").is_dir())
        self.assertFalse((generation / "complete.json").exists())
        self.assertEqual(self.mirror.safe_reason(OSError(errno.ENOSPC, PRIVATE)), "space_or_io_error")

    def test_source_symlink_is_not_checked_out_or_restored_as_an_ordinary_file(self):
        (self.source / "README.md").unlink()
        (self.source / "README.md").symlink_to("AGENTS.md")
        self.git_fixture("add", "README.md")
        self.git_fixture("-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-m", "symlink fixture")
        with self.assertRaisesRegex(self.mirror.MirrorError, "restore_invalid"):
            self.sync(self.local_runner)
        generation = self.generations()[0]
        self.assertFalse((generation / "restore-test" / "README.md").exists())
        self.assertFalse((generation / "complete.json").exists())


class GitCommandIsolationTests(MirrorFixture):
    def test_git_has_only_private_home_and_explicit_config_without_inherited_credentials(self):
        fd = os.open(self.mount, os.O_RDONLY | os.O_DIRECTORY)
        try:
            result = subprocess.CompletedProcess([], 0, stdout=b"fixture result")
            with patch.dict(self.mirror.os.environ, {
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": PRIVATE, "GIT_DIR": PRIVATE, "GITHUB_TOKEN": PRIVATE,
                "HOME": PRIVATE, "HTTPS_PROXY": PRIVATE,
            }), patch.object(self.mirror.subprocess, "run", return_value=result) as run:
                self.assertEqual(self.mirror.run_git(["--version"], cwd_fd=fd), b"fixture result")
            args = run.call_args.args[0]
            options = run.call_args.kwargs
            self.assertEqual(args[0], "git")
            self.assertFalse(options.get("shell", False))
            self.assertEqual(options["cwd"], f"/proc/self/fd/{fd}")
            self.assertEqual(options["pass_fds"], (fd,))
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertEqual(options["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(options["env"]["GIT_CONFIG_SYSTEM"], "/dev/null")
            self.assertEqual(options["env"]["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(options["env"]["HOME"], f"/proc/self/fd/{fd}/git-home")
            self.assertEqual(options["env"]["TMPDIR"], f"/proc/self/fd/{fd}")
            self.assertNotIn(PRIVATE, repr(options["env"]))
            for required in ("core.hooksPath=/dev/null", "credential.helper=", "protocol.allow=never",
                             "protocol.https.allow=always", "http.followRedirects=false", "gc.auto=0"):
                self.assertIn(required, args)
        finally:
            os.close(fd)

    def test_git_failure_reasons_never_include_raw_stderr_or_argv(self):
        fd = os.open(self.mount, os.O_RDONLY | os.O_DIRECTORY)
        failures = (
            (FileNotFoundError(PRIVATE), "git_missing"),
            (subprocess.TimeoutExpired(PRIVATE, 1, output=PRIVATE, stderr=PRIVATE), "git_timeout"),
            (subprocess.CalledProcessError(1, ["git", PRIVATE], stderr=PRIVATE), "git_failed"),
            (OSError(errno.ENOSPC, PRIVATE), "space_or_io_error"),
            (OSError(errno.EIO, PRIVATE), "space_or_io_error"),
        )
        try:
            for failure, code in failures:
                with self.subTest(code=code), patch.object(self.mirror.subprocess, "run", side_effect=failure):
                    with self.assertRaises(self.mirror.MirrorError) as error:
                        self.mirror.run_git(["--version"], cwd_fd=fd)
                    self.assertEqual(str(error.exception), code)
                    self.assertNotIn(PRIVATE, str(error.exception))
        finally:
            os.close(fd)


class MirrorEntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mirror = load_mirror()

    def test_unknown_cli_input_is_not_echoed_or_used_as_a_remote(self):
        output = io.StringIO()
        with patch.object(self.mirror.cloud, "operation_lock") as lock, \
             patch.object(self.mirror.cloud, "load_settings") as settings, \
             redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(SystemExit) as stopped:
                self.mirror.main(["--repository", PRIVATE])
        self.assertEqual(stopped.exception.code, 2)
        self.assertNotIn(PRIVATE, output.getvalue())
        lock.assert_not_called()
        settings.assert_not_called()

    def test_nonroot_or_missing_confirmation_never_loads_private_settings(self):
        for uid, argv in ((1000, ["--confirm-bmo-stopped"]), (0, [])):
            with self.subTest(uid=uid), patch.object(self.mirror.os, "geteuid", return_value=uid), \
                 patch.object(self.mirror.cloud, "operation_lock") as lock, \
                 patch.object(self.mirror.cloud, "load_settings") as settings, \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(self.mirror.main(argv), 1)
            lock.assert_not_called()
            settings.assert_not_called()

    def test_sync_and_status_are_both_covered_by_the_existing_shared_lock(self):
        for status_only in (False, True):
            held = []

            @contextmanager
            def fake_lock():
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

            def action(*_args, **_kwargs):
                self.assertTrue(held)
                return {"complete": 0, "partial": 0, "latest": None} if status_only else {
                    "head": "a" * 40, "refs": 1, "heads": 1, "tags": 0, "other": 0, "restored": 2,
                }

            with self.subTest(status=status_only), patch.object(self.mirror.os, "geteuid", return_value=0), \
                 patch.object(self.mirror.cloud, "operation_lock", fake_lock), \
                 patch.object(self.mirror.cloud, "load_settings", return_value=object()), \
                 patch.object(self.mirror, "status" if status_only else "sync", side_effect=action), \
                 redirect_stdout(io.StringIO()):
                argv = ["--status"] if status_only else ["--confirm-bmo-stopped"]
                self.assertEqual(self.mirror.main(argv), 0)
            self.assertEqual(held, [])

    def test_busy_lock_or_unknown_exception_has_no_false_ok_or_sensitive_output(self):
        for failure in (self.mirror.cloud.PrivateCloudError("operation_busy"), RuntimeError(PRIVATE)):
            output = io.StringIO()
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(self.mirror.os, "geteuid", return_value=0), \
                 patch.object(self.mirror.cloud, "operation_lock", side_effect=failure), \
                 redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(self.mirror.main(["--confirm-bmo-stopped"]), 1)
            self.assertNotIn(PRIVATE, output.getvalue())
            self.assertNotIn("MIRROR OK", output.getvalue())
            self.assertIn("MIRROR ERROR", output.getvalue())


if __name__ == "__main__":
    unittest.main()
