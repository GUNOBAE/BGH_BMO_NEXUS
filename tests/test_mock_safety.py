"""Mock 패키지의 정적 경계와 실행한 경로의 호스트 I/O 차단을 검사한다.

이 검사는 악의적인 Python 프로그램의 샌드박스가 아니다. 허용한 순수 표준
라이브러리와 로컬 Mock 모듈만 쓰는지 검사하고, 정해진 시나리오의 I/O 시도가
즉시 실패하도록 한다. 실제 Pi 자원 해제나 공개 경로 차단을 검증하지 않는다.
"""

import ast
import builtins
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import io
import os
from pathlib import Path
import socket
import subprocess
import time
import unittest
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "nexus_mock"
PURE_IMPORTS = {
    "__future__", "dataclasses", "enum", "typing", "math", "fractions",
    "collections", "nexus_mock",
}
FORBIDDEN_CALLS = {
    "open", "input", "eval", "exec", "compile", "__import__", "breakpoint",
    "globals", "locals", "vars", "__builtins__",
}
FORBIDDEN_ATTRIBUTES = {
    "__builtins__", "__globals__", "__subclasses__", "__getattribute__",
    "__code__", "__closure__", "__dict__",
}


def violations(source):
    """주석의 단어가 아닌 실제 import와 호출 구문만 검사한다."""
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in PURE_IMPORTS:
                    problems.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if not node.level and (node.module or "").split(".")[0] not in PURE_IMPORTS:
                problems.append((node.lineno, "from", node.module))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            # 금지 함수를 다른 이름에 대입하는 경우도 차단한다.
            problems.append((node.lineno, "builtin", node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            problems.append((node.lineno, "attribute", node.attr))
    return problems


@contextmanager
def forbidden_host_io():
    """이미 import한 Mock 실행 중 대표 호스트 I/O 함수가 불리면 실패한다."""
    boundaries = {
        builtins: ("open", "input"),
        io: ("open",),
        os: ("open", "system", "kill", "killpg", "fork", "posix_spawn",
             "listdir", "scandir", "stat", "lstat", "readlink", "getpid"),
        Path: ("open", "read_text", "write_text", "mkdir"),
        subprocess: ("Popen", "run", "call", "check_call", "check_output"),
        socket: ("socket", "create_connection", "getaddrinfo"),
        time: ("time", "monotonic", "sleep"),
    }
    with ExitStack() as stack:
        guards = []
        for owner, names in boundaries.items():
            for name in names:
                if hasattr(owner, name):
                    guards.append(stack.enter_context(patch.object(
                        owner, name,
                        side_effect=AssertionError("Mock에서 호스트 I/O 호출 금지"),
                    )))
        yield
        for guard in guards:
            guard.assert_not_called()


class MockStaticSafetyTests(unittest.TestCase):
    def test_package_uses_only_pure_stdlib_and_mock_imports(self):
        sources = sorted(PACKAGE_ROOT.rglob("*.py"))
        self.assertTrue(sources, "검사할 Mock 패키지가 필요하다")
        self.assertTrue((PACKAGE_ROOT / "controller.py").is_file())
        for source in sources:
            with self.subTest(module=source.relative_to(PACKAGE_ROOT)):
                self.assertEqual(violations(source.read_text(encoding="utf-8")), [])

    def test_guard_detects_import_aliases_and_dynamic_execution(self):
        for source in (
            "import subprocess as fake",
            "from os import kill as fake",
            "import socket",
            "import pathlib",
            "import time",
            "from importlib import import_module",
            "indirect = open",
            "indirect = __import__",
            "eval('operation')",
            "exec('operation')",
            "f.__globals__",
            "object.__subclasses__()",
        ):
            with self.subTest(source=source):
                self.assertTrue(violations(source))

    def test_guard_allows_comments_and_pure_mock_imports(self):
        source = (
            "# subprocess, socket, time, open: 문서 주석은 호출이 아니다.\n"
            "from dataclasses import dataclass\n"
            "from enum import Enum\n"
            "from .models import State\n"
            "from nexus_mock.fakes import FakeClock\n"
        )
        self.assertEqual(violations(source), [])

    def test_runtime_guard_fails_before_representative_host_calls(self):
        # 아래 호출은 모두 patch에 걸리므로 실제 파일/프로세스/소켓에 도달하지 않는다.
        operations = (
            lambda: builtins.open("FAKE_FILE"),
            lambda: subprocess.run(["FAKE_COMMAND"]),
            lambda: socket.socket(),
            lambda: os.kill("FAKE_PID", "FAKE_SIGNAL"),
            lambda: time.monotonic(),
        )
        for index, operation in enumerate(operations):
            with self.subTest(boundary=index):
                with self.assertRaisesRegex(AssertionError, "호스트 I/O 호출 금지"):
                    with forbidden_host_io():
                        operation()


class MockRuntimeSafetyTests(unittest.TestCase):
    def test_two_way_public_and_error_paths_do_not_call_host_io(self):
        # import에 필요한 소스 로딩은 허용하고, 아래 실행 구간의 호스트 I/O를 막는다.
        from nexus_mock.controller import ModeController
        from nexus_mock.fakes import FakeWorld
        from nexus_mock.models import (
            Ack, AckKind, CleanupReport, Health, Mode, Release, REQUIRED,
            SSDEvidence, State, Tunnel, TunnelEvidence,
        )

        with forbidden_host_io():
            world = FakeWorld()
            world.observe_inventory()
            world.baseline_resources_verified = True
            world.owner_policy_verified = True
            world.robot_profile_verified = True
            world.tunnel = TunnelEvidence(
                Tunnel.CLOSED, 0, True, True,
                approved_route_scope="mock-approved-demo",
            )
            world.ssd = SSDEvidence(
                at=0, revision=1, mounted=True, identity_verified=True,
                filesystem_verified=True, writable=True, healthy=True,
                capacity_ok=True, permissions_ok=True, backup_approved=True,
            )
            controller = ModeController(world)
            self.assertTrue(controller.reconcile())

            def send(kind, seq=1, *, cleanup=None):
                identity = None if kind == AckKind.CLOSED else controller.active
                self.assertTrue(controller.handle(Ack(
                    controller.token, kind, seq, world.clock.now,
                    identity=identity, cleanup=cleanup,
                )))

            def ready():
                send(AckKind.CLOSED)
                send(AckKind.START_ACCEPTED)
                world.health[controller.active] = Health(world.clock.now, True)
                send(AckKind.READY)

            def stop_and_ready(target, command_id):
                self.assertIsNotNone(controller.request(target, command_id))
                world.tunnel = replace(
                    world.tunnel, status=Tunnel.CLOSED, at=world.clock.now,
                )
                send(AckKind.CLOSED)
                send(AckKind.STOP_ACCEPTED)
                identity = controller.active
                world.root_exited(identity, reaped=True)
                send(AckKind.ROOT_EXITED)
                cleanup = CleanupReport(identity, tuple(
                    (resource, Release.RELEASED)
                    for resource in sorted(REQUIRED[identity.mode])
                ))
                send(AckKind.CLEANUP_COMPLETE, 2, cleanup=cleanup)
                ready()

            self.assertIsNotNone(controller.request(Mode.ROBOT, "fake-robot"))
            ready()
            self.assertEqual(controller.state, State.ROBOT_READY)
            stop_and_ready(Mode.SERVER, "fake-server")
            self.assertEqual(controller.state, State.SERVER_READY)
            self.assertTrue(controller.operator_public_approval())
            world.tunnel = replace(world.tunnel, status=Tunnel.OPEN)
            send(AckKind.PUBLIC_OPENED)
            stop_and_ready(Mode.ROBOT, "fake-robot-again")
            self.assertEqual(controller.state, State.ROBOT_READY)

            starts_before_error = sum(c.kind == "start" for c in world.commands)
            stops_before_error = sum(c.kind == "stop" for c in world.commands)
            world.external_robot = True
            world.clock.advance(1)
            self.assertFalse(controller.tick())
            self.assertEqual(controller.state, State.ERROR)
            self.assertFalse(controller.public_permission)
            self.assertEqual(
                sum(c.kind == "start" for c in world.commands), starts_before_error,
            )
            self.assertEqual(
                sum(c.kind == "stop" for c in world.commands), stops_before_error,
            )


if __name__ == "__main__":
    unittest.main()
