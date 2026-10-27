"""두 고정 원격의 분리를 임시 로컬 Git으로 검사한다. 실제 BMO/SSD에는 접근하지 않는다."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_github_mirror as baseline


NEXUS = "BGH_BMO_NEXUS"
BMO = "BGH_BMO"
URLS = {
    NEXUS: "https://github.com/GUNOBAE/BGH_BMO_NEXUS.git",
    BMO: "https://github.com/GUNOBAE/BGH_BMO.git",
}
BASES = {NEXUS: "nexus-git-mirrors", BMO: "bmo-git-mirrors"}
BMO_SAMPLES = ("README.md", "README_KR.md")


class RepositoryFixture(baseline.MirrorFixture):
    """SSD 검사는 가짜로 주입하고 Git은 NEXUS 안의 임시 저장소만 사용한다."""

    def setUp(self):
        super().setUp()
        self.git = shutil.which("git")
        if self.git is None:
            self.skipTest("Git이 없어 임시 로컬 Git 검사를 생략")
        fixture_home = self.work / "fixture-home"
        fixture_home.mkdir(mode=0o700)
        self.git_env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
            "HOME": str(fixture_home), "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.sources = {}
        for repo in (NEXUS, BMO):
            source = self.work / (repo + "-fixture-source")
            source.mkdir(mode=0o700)
            self.sources[repo] = source
            self.fixture_git(repo, "init", "--initial-branch=main", str(source))
            samples = ("README.md", "AGENTS.md") if repo == NEXUS else BMO_SAMPLES
            for name in samples:
                (source / name).write_bytes((repo + " " + name + "\r\n").encode())
            (source / "unselected.txt").write_bytes(b"tracked but not restored\n")
            self.fixture_git(repo, "add", *samples, "unselected.txt")
            self.fixture_git(repo, "-c", "user.name=Offline Fixture",
                             "-c", "user.email=fixture@example.invalid",
                             "commit", "-m", "first fixture commit")
            self.fixture_git(repo, "tag", "fixture-v1")
        self.calls = []

    def fixture_git(self, repo, *args):
        return baseline.REAL_RUN(
            [self.git, "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.sources[repo], env=self.git_env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, timeout=20,
        ).stdout

    def runner(self, args, **options):
        self.calls.append(list(args))

        def local_only(argv, **kwargs):
            argv = list(argv)
            for repo, url in URLS.items():
                if url in argv:
                    argv[argv.index(url)] = str(self.sources[repo])
            self.assertFalse(any("github.com" in part for part in argv))
            argv[1:1] = ["-c", "protocol.file.allow=always"]
            kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}
            return baseline.REAL_RUN(argv, **kwargs)

        with patch.object(self.mirror.subprocess, "run", side_effect=local_only):
            return self.mirror.run_git(args, **options)

    def repo_base(self, repo):
        return self.mount / BASES[repo]

    def repo_generations(self, repo):
        return sorted(path for path in self.repo_base(repo).iterdir()
                      if path.name.startswith("gen-") and path.is_dir())

    def backup(self, repo=NEXUS):
        return self.mirror.sync(self.settings, confirmed=True, repo=repo, runner=self.runner)

    def check_status(self, repo=NEXUS):
        return self.mirror.status(self.settings, repo=repo, runner=self.runner)


class RepositoryIsolationTests(RepositoryFixture):
    def test_bmo_backup_restores_its_own_files_without_changing_nexus_or_robot_source(self):
        # 기본 API도 기존 CLI처럼 NEXUS를 선택해야 한다.
        nexus_report = self.mirror.sync(self.settings, confirmed=True, runner=self.runner)
        before_nexus = baseline.inventory(self.repo_base(NEXUS))
        before_robot_source = baseline.inventory(self.sources[BMO])
        before_nexus_source = baseline.inventory(self.sources[NEXUS])
        bmo_report = self.backup(BMO)
        bmo_generation = self.repo_generations(BMO)[0]
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before_nexus)
        self.assertEqual(baseline.inventory(self.sources[BMO]), before_robot_source)
        self.assertEqual(baseline.inventory(self.sources[NEXUS]), before_nexus_source)
        self.assertNotEqual(nexus_report["head"], bmo_report["head"])
        self.assertEqual(bmo_report["restored"], 2)
        self.assertEqual({path.name for path in (bmo_generation / "restore-test").iterdir()},
                         set(BMO_SAMPLES))
        for name in BMO_SAMPLES:
            self.assertEqual((bmo_generation / "restore-test" / name).read_bytes(),
                             (self.sources[BMO] / name).read_bytes())
        clones = [args for args in self.calls if "clone" in args]
        self.assertEqual([next(url for url in URLS.values() if url in args) for args in clones],
                         [URLS[NEXUS], URLS[BMO]])

    def test_bmo_update_and_status_preserve_previous_generations_and_nexus_markers(self):
        self.backup(NEXUS)
        self.backup(BMO)
        before_nexus = baseline.inventory(self.repo_base(NEXUS))
        old_bmo = self.repo_generations(BMO)[0]
        before_old_bmo = baseline.inventory(old_bmo)
        (self.sources[BMO] / "README.md").write_bytes(b"second robot fixture commit\n")
        self.fixture_git(BMO, "add", "README.md")
        self.fixture_git(BMO, "-c", "user.name=Offline Fixture",
                         "-c", "user.email=fixture@example.invalid",
                         "commit", "-m", "second robot fixture commit")
        before_source = baseline.inventory(self.sources[BMO])
        updated = self.backup(BMO)
        before_bmo = baseline.inventory(self.repo_base(BMO))
        self.calls.clear()
        status = self.check_status(BMO)
        self.assertEqual((status["complete"], status["partial"]), (2, 0))
        self.assertEqual(status["latest"]["head"], updated["head"])
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before_nexus)
        self.assertEqual(baseline.inventory(old_bmo), before_old_bmo)
        self.assertEqual(baseline.inventory(self.repo_base(BMO)), before_bmo)
        self.assertEqual(baseline.inventory(self.sources[BMO]), before_source)
        self.assertFalse(any("clone" in args or "fetch" in args or "push" in args for args in self.calls))
        self.assertEqual(self.check_status(NEXUS)["complete"], 1)
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before_nexus)

    def test_existing_legacy_nexus_manifest_is_readable_and_never_migrated(self):
        self.backup(NEXUS)
        generation = self.repo_generations(NEXUS)[0]
        marker = generation / "complete.json"
        saved = json.loads(marker.read_bytes())
        legacy_keys = {"head", "refs", "heads", "tags", "other", "refs_sha256",
                       "generation", "restored", "files", "completed_ns", "config_sha256"}
        saved = {key: value for key, value in saved.items() if key in legacy_keys}
        marker.write_bytes(json.dumps(saved, sort_keys=True).encode() + b"\n")
        before = baseline.inventory(self.repo_base(NEXUS))
        self.backup(BMO)
        status = self.mirror.status(self.settings, runner=self.runner)
        self.assertEqual(status["complete"], 1)
        self.assertEqual(status["latest"]["head"], saved["head"])
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before)

    def test_bmo_status_does_not_read_or_repair_a_damaged_nexus_store(self):
        self.backup(NEXUS)
        self.backup(BMO)
        nexus_marker = self.repo_base(NEXUS) / self.mirror.STORE_MARKER
        nexus_marker.write_bytes(b"different store identity\n")
        before_nexus = baseline.inventory(self.repo_base(NEXUS))
        before_bmo = baseline.inventory(self.repo_base(BMO))
        self.assertEqual(self.check_status(BMO)["complete"], 1)
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before_nexus)
        with self.assertRaises(self.mirror.MirrorError):
            self.check_status(NEXUS)
        self.assertEqual(baseline.inventory(self.repo_base(BMO)), before_bmo)

    def test_bmo_store_cannot_be_a_symlink_to_the_existing_nexus_store(self):
        self.backup(NEXUS)
        self.repo_base(BMO).symlink_to(self.repo_base(NEXUS), target_is_directory=True)
        before = baseline.inventory(self.repo_base(NEXUS))
        runner = Mock(side_effect=AssertionError("linked store cannot run Git"))
        with self.assertRaises(self.mirror.MirrorError):
            self.mirror.sync(self.settings, confirmed=True, repo=BMO, runner=runner)
        runner.assert_not_called()
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before)
        self.assertTrue(self.repo_base(BMO).is_symlink())

    def test_nexus_store_identity_is_not_accepted_as_the_bmo_store(self):
        self.backup(NEXUS)
        self.backup(BMO)
        nexus_bytes = (self.repo_base(NEXUS) / self.mirror.STORE_MARKER).read_bytes()
        bmo_marker = self.repo_base(BMO) / self.mirror.STORE_MARKER
        self.assertNotEqual(bmo_marker.read_bytes(), nexus_bytes)
        bmo_marker.write_bytes(nexus_bytes)
        before = baseline.inventory(self.mount)
        runner = Mock(side_effect=AssertionError("wrong identity cannot run Git"))
        for action in ("sync", "status"):
            with self.subTest(action=action), self.assertRaises(self.mirror.MirrorError):
                options = {"confirmed": True} if action == "sync" else {}
                getattr(self.mirror, action)(self.settings, repo=BMO, runner=runner, **options)
        runner.assert_not_called()
        self.assertEqual(baseline.inventory(self.mount), before)

    def test_failed_bmo_clone_keeps_nexus_and_previous_bmo_backups(self):
        self.backup(NEXUS)
        self.backup(BMO)
        old_bmo = self.repo_generations(BMO)[0]
        before_old_bmo = baseline.inventory(old_bmo)
        before_nexus = baseline.inventory(self.repo_base(NEXUS))

        def failed_clone(args, **options):
            self.assertIn(URLS[BMO], args)
            os.mkdir("mirror.git", mode=0o700, dir_fd=options["cwd_fd"])
            raise self.mirror.MirrorError("git_failed")

        with self.assertRaisesRegex(self.mirror.MirrorError, "git_failed"):
            self.mirror.sync(self.settings, confirmed=True, repo=BMO, runner=failed_clone)
        self.assertEqual(self.check_status(BMO)["partial"], 1)
        self.assertEqual(baseline.inventory(old_bmo), before_old_bmo)
        self.assertEqual(baseline.inventory(self.repo_base(NEXUS)), before_nexus)

    def test_identical_generation_names_are_independent_between_repositories(self):
        fixed_time = Mock()
        fixed_time.now.return_value.strftime.return_value = "20260101T000000Z"
        with patch.object(self.mirror, "datetime", fixed_time), \
             patch.object(self.mirror.uuid, "uuid4", return_value=SimpleNamespace(hex="a" * 32)):
            self.backup(NEXUS)
            self.backup(BMO)
        self.assertEqual(self.repo_generations(NEXUS)[0].name, self.repo_generations(BMO)[0].name)
        self.assertEqual(self.check_status(NEXUS)["complete"], 1)
        self.assertEqual(self.check_status(BMO)["complete"], 1)


class RepositorySelectionSafetyTests(baseline.MirrorFixture):
    def test_unknown_repository_is_rejected_before_storage_process_or_git_observation(self):
        values = ("other", "bgh_bmo", "BGH_BMO\n", "../BGH_BMO", "/tmp/robot",
                  "file:///tmp/robot", "https://github.com/GUNOBAE/BGH_BMO.git",
                  "token-" + baseline.PRIVATE, None, True, [], {})
        for value in values:
            for action in ("sync", "status"):
                with self.subTest(value=value, action=action):
                    runner = Mock(side_effect=AssertionError("invalid repo cannot run Git"))
                    options = {"confirmed": True} if action == "sync" else {}
                    with self.assertRaises(self.mirror.MirrorError) as error:
                        getattr(self.mirror, action)(self.settings, repo=value, runner=runner, **options)
                    self.assertNotIn(baseline.PRIVATE, str(error.exception))
                    runner.assert_not_called()
        self.storage.assert_not_called()
        self.bmo.assert_not_called()
        self.assertEqual(list(self.mount.iterdir()), [])

    def test_bmo_target_preserves_existing_mount_and_manual_bmo_gates(self):
        cases = ("ssd_rejected: controller dead", "ssd_rejected: zero capacity")
        runner = Mock(side_effect=AssertionError("rejected SSD cannot run Git"))
        for failure in cases:
            with self.subTest(failure=failure):
                self.storage.side_effect = self.mirror.cloud.PrivateCloudError(failure)
                with self.assertRaises(self.mirror.cloud.PrivateCloudError):
                    self.mirror.sync(self.settings, confirmed=True, repo=BMO, runner=runner)
        self.storage.side_effect = None
        self.bmo.side_effect = self.mirror.cloud.PrivateCloudError("bmo_busy_or_unknown")
        with self.assertRaises(self.mirror.cloud.PrivateCloudError):
            self.mirror.sync(self.settings, confirmed=True, repo=BMO, runner=runner)
        runner.assert_not_called()
        self.assertEqual(list(self.mount.iterdir()), [])


class RepositoryCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mirror = baseline.load_mirror()

    def test_repo_urls_tokens_and_local_paths_are_never_echoed_or_used(self):
        for value in (URLS[BMO], "/tmp/robot", "other", "token-" + baseline.PRIVATE):
            output = io.StringIO()
            with self.subTest(value=value), patch.object(self.mirror.cloud, "operation_lock") as lock, \
                 patch.object(self.mirror.cloud, "load_settings") as settings, \
                 redirect_stdout(output), redirect_stderr(output):
                with self.assertRaises(SystemExit) as error:
                    self.mirror.main(["--repo", value, "--confirm-bmo-stopped"])
            self.assertEqual(error.exception.code, 2)
            self.assertNotIn(value, output.getvalue())
            lock.assert_not_called()
            settings.assert_not_called()

    def test_default_and_bmo_selection_reach_their_actions_inside_the_shared_lock(self):
        for repo in (None, NEXUS, BMO):
            for status_only in (False, True):
                held = []

                @contextmanager
                def lock():
                    held.append(True)
                    try:
                        yield
                    finally:
                        held.pop()

                def action(*_args, **options):
                    self.assertTrue(held)
                    self.assertEqual(options.get("repo", NEXUS), repo or NEXUS)
                    return {"complete": 0, "partial": 0, "latest": None} if status_only else {
                        "head": "a" * 40, "refs": 1, "heads": 1,
                        "tags": 0, "other": 0, "restored": 2,
                    }

                argv = ["--status"] if status_only else ["--confirm-bmo-stopped"]
                if repo is not None:
                    argv.extend(["--repo", repo])
                with self.subTest(repo=repo, status=status_only), \
                     patch.object(self.mirror.os, "geteuid", return_value=0), \
                     patch.object(self.mirror.cloud, "operation_lock", lock), \
                     patch.object(self.mirror.cloud, "load_settings", return_value=object()), \
                     patch.object(self.mirror, "status" if status_only else "sync", side_effect=action), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(self.mirror.main(argv), 0)
                self.assertEqual(held, [])


if __name__ == "__main__":
    unittest.main()
