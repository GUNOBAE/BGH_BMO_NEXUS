"""실제 장치/서비스를 조회하지 않는 후속 점검 모의 테스트."""

import json
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

from scripts import inspect_pi_followup as inspection


FAKE_PRIVATE = "FAKE_PRIVATE_SENTINEL"


def encoded(key, records):
    return inspection.Probe("ok", json.dumps({key: records}))


class ReportTests(unittest.TestCase):
    def setUp(self):
        # 명령 목록에 없는 호출은 KeyError로 실패하므로 실제 명령에 빠질 수 없다.
        self.results = {
            inspection.COMMANDS["block"]: encoded("blockdevices", [{
                "name": "nvme0n1", "type": "disk", "size": 0,
                "fstype": None, "children": [{
                    "name": "nvme0n1p1", "type": "part",
                    "size": 1000200000000, "fstype": None,
                }],
            }]),
            inspection.COMMANDS["root"]: encoded("filesystems", [{
                "source": "/dev/mmcblk0p2", "fstype": "ext4",
            }]),
            inspection.COMMANDS["mounts"]: encoded("filesystems", [{
                "source": "/dev/mmcblk0p2", "fstype": "ext4",
            }]),
            inspection.COMMANDS["process"]: inspection.Probe(
                "ok", "python3\nnode\nchromium\n"),
            inspection.COMMANDS["system_running"]: inspection.Probe(
                "ok", "ollama.service loaded active running Local model\n"
                "bmo-case-fan.service loaded active running Cooling\n"),
            inspection.COMMANDS["system_files"]: inspection.Probe(
                "ok", "ollama.service enabled enabled\n"
                "bmo-case-fan.service enabled enabled\n"),
            inspection.COMMANDS["user_running"]: inspection.Probe("ok", ""),
            inspection.COMMANDS["user_files"]: inspection.Probe("ok", ""),
        }
        self.runner = Mock(side_effect=lambda argv: self.results[tuple(argv)])
        self.reader = Mock(return_value=0)

    def report(self):
        return inspection.build_report(run=self.runner, read_size=self.reader)

    def set_result(self, key, result):
        self.results[inspection.COMMANDS[key]] = result

    def test_zero_device_and_nonzero_partition_remain_unresolved(self):
        report = self.report()
        self.assertIn("nvme0n1", report)
        self.assertIn("nvme0n1p1", report)
        self.assertIn("1000200000000", report)
        self.assertIn("/dev/mmcblk0p2", report)
        self.assertIn("미확인", report)
        self.assertRegex(report, r"nvme0n1[^\n]*0")
        self.reader.assert_called_once_with("nvme0n1")
        self.assertEqual(
            {call.args[0] for call in self.runner.call_args_list},
            set(inspection.COMMANDS.values()),
        )

    def test_nonzero_size_and_ext4_do_not_prove_data_integrity(self):
        self.set_result("block", encoded("blockdevices", [{
            "name": "nvme0n1", "type": "disk", "size": 1000200000000,
            "fstype": None, "children": [{
                "name": "nvme0n1p1", "type": "part",
                "size": 1000199000000, "fstype": "ext4",
            }],
        }]))
        self.set_result("mounts", encoded("filesystems", [{
            "source": "/dev/nvme0n1p1", "fstype": "ext4",
            "target": "/private/" + FAKE_PRIVATE,
        }]))
        self.reader.return_value = 1953515625
        report = self.report()
        self.assertIn("1000200000000", report)
        self.assertIn("ext4", report)
        self.assertIn("미확인", report)
        self.assertNotIn(FAKE_PRIVATE, report)

    def test_missing_nvme_is_not_an_error_or_permission_to_initialize(self):
        self.set_result("block", encoded("blockdevices", [{
            "name": "mmcblk0", "type": "disk", "size": 512000000000,
            "fstype": None,
        }]))
        report = self.report()
        self.reader.assert_not_called()
        self.assertIn("미확인", report)

    def test_unreadable_sysfs_keeps_remaining_inventory(self):
        self.reader.return_value = None
        report = self.report()
        self.assertIn("sysfs sectors=미확인", report)
        self.assertIn("NAME=python3", report)
        self.assertIn("ollama.service: running", report)

    def test_failed_mount_query_is_unknown_instead_of_zero_mounts(self):
        self.set_result("mounts", inspection.Probe("error", FAKE_PRIVATE))
        report = self.report()
        self.assertIn("마운트 조회:", report)
        self.assertIn("미확인", report)
        self.assertNotIn("마운트 레코드=0", report)
        self.assertNotIn(FAKE_PRIVATE, report)

    def test_sysfs_and_lsblk_size_disagreement_is_reported(self):
        self.reader.return_value = 16
        report = self.report()
        self.assertIn("sysfs bytes (sectors × 512)=8192", report)
        self.assertIn("용량 관측 불일치", report)

    def test_missing_tools_and_unavailable_user_bus_are_nonfatal(self):
        for key in ("block", "root", "mounts", "process", "system_running", "system_files"):
            self.set_result(key, inspection.Probe("missing", FAKE_PRIVATE))
        for key in ("user_running", "user_files"):
            self.set_result(key, inspection.Probe("error", FAKE_PRIVATE))
        report = self.report()
        self.assertIn("미확인", report)
        self.assertNotIn(FAKE_PRIVATE, report)

    def test_partial_stdout_from_failed_or_timed_out_commands_is_withheld(self):
        for status in ("error", "timeout"):
            with self.subTest(status=status):
                for command in self.results:
                    self.results[command] = inspection.Probe(status, FAKE_PRIVATE)
                report = self.report()
                self.assertNotIn(FAKE_PRIVATE, report)
                self.assertIn("미확인", report)

    def test_malformed_json_and_nonlist_records_are_nonfatal(self):
        for payload in (
            "{", "null", "[]", '{"blockdevices": null}',
            json.dumps({"blockdevices": [{"name": "nvme0n1", "children": FAKE_PRIVATE}]}),
        ):
            with self.subTest(payload=payload):
                self.set_result("block", inspection.Probe("ok", payload))
                report = self.report()
                self.assertNotIn(FAKE_PRIVATE, report)
                self.assertIn("미확인", report)

    def test_root_and_mount_sources_are_never_raw_private_paths(self):
        for source in (
            "/dev/mapper/" + FAKE_PRIVATE,
            "//" + FAKE_PRIVATE + "/share",
            "/dev/nvme0n1p1[/" + FAKE_PRIVATE + "]",
        ):
            with self.subTest(source=source):
                self.set_result("root", encoded("filesystems", [{
                    "source": source, "fstype": "ext4",
                }]))
                self.set_result("mounts", encoded("filesystems", [{
                    "source": source, "fstype": "ext4",
                }]))
                report = self.report()
                self.assertNotIn(FAKE_PRIVATE, report)
                self.assertIn("미확인", report)

    def test_unknown_device_name_and_filesystem_are_withheld(self):
        self.set_result("block", encoded("blockdevices", [{
            "name": "nvme0n1", "type": FAKE_PRIVATE, "size": 0,
            "fstype": FAKE_PRIVATE, "label": FAKE_PRIVATE,
            "serial": FAKE_PRIVATE, "mountpoints": ["/private/" + FAKE_PRIVATE],
        }, {
            "name": "nvme0n1-" + FAKE_PRIVATE, "type": "disk",
            "size": 1, "fstype": "ext4",
        }]))
        self.set_result("root", encoded("filesystems", [{
            "source": "/dev/mmcblk0p2", "fstype": FAKE_PRIVATE,
        }]))
        self.assertNotIn(FAKE_PRIVATE, self.report())

    def test_only_known_process_names_and_service_names_survive(self):
        self.set_result("process", inspection.Probe("ok",
            "python3\n"
            "python3 --token=" + FAKE_PRIVATE + "\n"
            "bmo-" + FAKE_PRIVATE + "\n"
            "/private/" + FAKE_PRIVATE + "\n"))
        self.set_result("system_running", inspection.Probe("ok",
            "ollama.service loaded active running " + FAKE_PRIVATE + "\n"
            "bmo-" + FAKE_PRIVATE + ".service loaded active running Private\n"))
        self.set_result("system_files", inspection.Probe("ok",
            "ollama.service enabled " + FAKE_PRIVATE + "\n"
            "bmo-" + FAKE_PRIVATE + ".service enabled enabled\n"))
        report = self.report()
        self.assertIn("python3", report)
        self.assertIn("ollama.service", report)
        self.assertNotIn(FAKE_PRIVATE, report)
        self.assertNotIn("--token", report)

    def test_known_unit_with_unexpected_state_does_not_leak_state(self):
        self.set_result("system_files", inspection.Probe(
            "ok", "ollama.service " + FAKE_PRIVATE + " enabled\n"))
        self.assertNotIn(FAKE_PRIVATE, self.report())

    def test_malformed_unit_rows_cannot_forge_running_state(self):
        self.set_result("system_running", inspection.Probe("ok",
            "ollama.service running\n"
            "ollama.service loaded inactive dead\n"
            "ollama.service loaded active " + FAKE_PRIVATE + "\n"))
        # 다른 섹션의 정상 유닛 파일 상태와 분리해 실행 상태 투영만 확인한다.
        rows = inspection.service_lines(self.results[inspection.COMMANDS["system_running"]], True)
        self.assertFalse(any("ollama.service" in row for row in rows))
        self.assertFalse(any(FAKE_PRIVATE in row for row in rows))

    def test_process_names_are_exact_and_arguments_never_count_as_names(self):
        self.set_result("process", inspection.Probe("ok",
            "python3\npython3\npython3 --key=" + FAKE_PRIVATE + "\n"))
        report = self.report()
        self.assertIn("NAME=python3 COUNT=2", report)
        self.assertNotIn(FAKE_PRIVATE, report)
        self.assertNotIn("PID=", report)


class CommandTests(unittest.TestCase):
    def test_command_set_is_observation_only(self):
        commands = inspection.COMMANDS
        self.assertEqual(set(commands), {
            "block", "root", "mounts", "process", "system_running",
            "system_files", "user_running", "user_files",
        })
        self.assertEqual(commands["process"], ("ps", "-eo", "comm="))
        self.assertEqual(set(commands["block"]), {
            "lsblk", "--bytes", "--json", "--output", "NAME,TYPE,SIZE,FSTYPE",
        })
        self.assertEqual(commands["root"], (
            "findmnt", "--json", "--mountpoint", "/", "--output", "SOURCE,FSTYPE",
        ))
        self.assertEqual(commands["mounts"], (
            "findmnt", "--json", "--list", "--output", "SOURCE,FSTYPE",
        ))
        for name in ("system_running", "system_files", "user_running", "user_files"):
            with self.subTest(name=name):
                self.assertEqual(commands[name][0], "systemctl")
                self.assertEqual(
                    sum(part in ("list-units", "list-unit-files") for part in commands[name]), 1)
                self.assertEqual("--user" in commands[name], name.startswith("user_"))
        for argv in commands.values():
            self.assertNotIn("sudo", argv)
            self.assertNotIn("journalctl", argv)

    @patch.object(inspection.subprocess, "run")
    def test_runner_never_uses_shell_and_discards_stderr(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="{}")
        argv = inspection.COMMANDS["block"]
        result = inspection.run_command(argv)
        self.assertEqual(result.status, "ok")
        positional, keyword = run.call_args
        self.assertEqual(tuple(positional[0]), argv)
        self.assertFalse(keyword.get("shell", False))
        self.assertEqual(keyword["stderr"], subprocess.DEVNULL)
        self.assertEqual(keyword["stdout"], subprocess.PIPE)
        self.assertGreater(keyword["timeout"], 0)
        self.assertLessEqual(keyword["timeout"], 10)

    @patch.object(inspection.subprocess, "run")
    def test_command_errors_discard_partial_stdout(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, stdout=FAKE_PRIVATE)
        result = inspection.run_command(inspection.COMMANDS["block"])
        self.assertEqual(result.status, "error")
        self.assertNotIn(FAKE_PRIVATE, result.stdout)

    @patch.object(inspection.subprocess, "run")
    def test_missing_command_is_static_nonfatal_status(self, run):
        run.side_effect = FileNotFoundError(FAKE_PRIVATE)
        result = inspection.run_command(inspection.COMMANDS["block"])
        self.assertEqual(result.status, "missing")
        self.assertNotIn(FAKE_PRIVATE, result.stdout)

    @patch.object(inspection.subprocess, "run")
    def test_timeout_discards_exception_output(self, run):
        run.side_effect = subprocess.TimeoutExpired(
            FAKE_PRIVATE, 1, output=FAKE_PRIVATE, stderr=FAKE_PRIVATE)
        result = inspection.run_command(inspection.COMMANDS["block"])
        self.assertEqual(result.status, "timeout")
        self.assertNotIn(FAKE_PRIVATE, result.stdout)

    @patch.object(inspection.subprocess, "run")
    def test_permission_failure_does_not_escape_as_traceback(self, run):
        run.side_effect = PermissionError(FAKE_PRIVATE)
        result = inspection.run_command(inspection.COMMANDS["block"])
        self.assertEqual(result.status, "error")
        self.assertNotIn(FAKE_PRIVATE, result.stdout)

    @patch.object(inspection.subprocess, "run")
    def test_unlisted_command_never_reaches_subprocess(self, run):
        result = inspection.run_command(("unexpected-command", FAKE_PRIVATE))
        run.assert_not_called()
        self.assertEqual(result.status, "error")


class SysfsTests(unittest.TestCase):
    def test_sector_read_uses_only_fixed_sysfs_namespace_path(self):
        handle = mock_open(read_data="1953515625\n").return_value
        with patch.object(Path, "open", autospec=True, return_value=handle) as opened:
            self.assertEqual(inspection.read_sysfs_sectors("nvme0n1"), 1953515625)
        opened.assert_called_once_with(Path("/sys/class/block/nvme0n1/size"), encoding="ascii")
        handle.read.assert_called_once_with(64)

    @patch.object(Path, "open", new_callable=mock_open)
    def test_untrusted_or_partition_names_never_open_a_file(self, opened):
        for name in ("../" + FAKE_PRIVATE, "nvme0n1p1", "/dev/nvme0n1", "nvme0n1/size"):
            with self.subTest(name=name):
                self.assertIsNone(inspection.read_sysfs_sectors(name))
        opened.assert_not_called()

    @patch.object(Path, "open", new_callable=mock_open)
    def test_missing_or_unreadable_sysfs_remains_unknown(self, opened):
        for error in (FileNotFoundError(FAKE_PRIVATE), PermissionError(FAKE_PRIVATE)):
            with self.subTest(error=type(error).__name__):
                opened.side_effect = error
                self.assertIsNone(inspection.read_sysfs_sectors("nvme0n1"))

    def test_size_conversion_rejects_non_numeric_and_boolean_values(self):
        for value in (True, False, -1, "-1", "1.5", FAKE_PRIVATE, {}, None):
            with self.subTest(value=value):
                self.assertIsNone(inspection.nonnegative_int(value))
        self.assertEqual(inspection.nonnegative_int("0"), 0)
        self.assertEqual(inspection.nonnegative_int(1024), 1024)


class MainTests(unittest.TestCase):
    @patch.object(inspection, "build_report", side_effect=RuntimeError(FAKE_PRIVATE))
    @patch.object(inspection.sys, "argv", ["inspect_pi_followup.py"])
    def test_unexpected_exception_prints_no_traceback_or_private_message(self, _build):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(inspection.main(), 1)
        self.assertNotIn(FAKE_PRIVATE, out.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertIn("미확인", out.getvalue())

    @patch.object(inspection, "build_report")
    @patch.object(inspection.sys, "argv", ["inspect_pi_followup.py", FAKE_PRIVATE])
    def test_unexpected_arguments_never_start_observation_or_echo_input(self, build):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(inspection.main(), 2)
        build.assert_not_called()
        self.assertNotIn(FAKE_PRIVATE, out.getvalue())
        self.assertEqual(err.getvalue(), "")


class WrapperTests(unittest.TestCase):
    """빈 PATH/가짜 명령 경로에서만 실행하며 실제 점검 바이너리는 사용하지 않는다."""

    def setUp(self):
        self.bash = shutil.which("bash")
        if self.bash is None:
            self.skipTest("bash가 없어 wrapper 모의 실행 생략")
        self.wrapper = Path(__file__).resolve().parents[1] / "scripts" / "inspect_pi_followup.sh"

    def invoke(self, directory, *args):
        return subprocess.run(
            [self.bash, str(self.wrapper), *args],
            env={"PATH": directory, "LC_ALL": "C.UTF-8"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", check=False, timeout=10,
        )

    def test_python_missing_prints_only_static_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            self.assertIn("python3 없음", result.stdout)
            self.assertEqual(os.listdir(directory), [])

    def test_python_runs_with_all_observation_commands_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "python3").symlink_to(sys.executable)
            result = self.invoke(directory)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            self.assertIn("명령 없음", result.stdout)
            self.assertEqual(os.listdir(directory), ["python3"])

    def test_unexpected_cli_argument_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "python3").symlink_to(sys.executable)
            result = self.invoke(directory, FAKE_PRIVATE)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")
            self.assertNotIn(FAKE_PRIVATE, result.stdout)


if __name__ == "__main__":
    unittest.main()
