"""고정 PUBLIC 프로젝트의 수동 SSD 미러. 기존 세대와 원본 checkout은 수정하지 않는다."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

try:
    from . import private_cloud as cloud
except ImportError:
    import private_cloud as cloud


SOURCE = "https://github.com/GUNOBAE/BGH_BMO_NEXUS.git"
FIXED_MOUNT = Path("/mnt/ssd")
BASE_NAME = "nexus-git-mirrors"
OWNER_UID = 0
OWNER_GID = 0
STORE_MARKER = ".nexus-mirror-store"
STORE_ID = b"NEXUS public Git mirror store v1\n"
SAMPLES = ("README.md", "AGENTS.md")
REPO_NAMES = ("BGH_BMO_NEXUS", "BGH_BMO")
GENERATION = re.compile(r"gen-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_FILE = 1024 * 1024
REASONS = {
    "root_required": "승인 후 sudo로 실행하세요",
    "confirmation_required": "BMO 정상 종료 후 --confirm-bmo-stopped를 지정하세요",
    "repo_rejected": "허용된 PUBLIC 저장소 이름만 선택할 수 있습니다",
    "settings_rejected": "/mnt/ssd와 BMO_SSD 기존 설정만 사용할 수 있습니다",
    "ssd_rejected": "기존 SSD mount·identity·ext4/rw·NVMe live·공간 검사를 확인하세요",
    "ssd_changed": "검증한 SSD identity 또는 고정 경로가 바뀌었습니다",
    "path_rejected": "기존 경로·소유권·권한·링크가 안전 조건과 다릅니다",
    "git_failed": "Git 조회/전송이 실패했습니다. 네트워크·SSD 상태를 로컬에서 확인하세요",
    "git_missing": "표준 git을 찾지 못했습니다. 자동 설치하지 않습니다",
    "git_timeout": "Git 작업 시간이 초과됐습니다",
    "mirror_invalid": "미러의 bare·객체·refs·HEAD 검증을 통과하지 못했습니다",
    "restore_invalid": "새 경로의 파일 복원 검증을 통과하지 못했습니다",
    "manifest_invalid": "완료 기록이나 복원 파일이 검증 결과와 다릅니다",
    "space_or_io_error": "SSD 공간·읽기 전용 전환·I/O 오류로 중단했습니다",
    "operation_busy": "다른 NEXUS 작업이나 클라우드 세션이 진행 중입니다",
    "lock_invalid": "기존 NEXUS lock 소유권을 확인하세요",
    "bmo_busy_or_unknown": "BMO 또는 해당 사용자의 앱이 남아 있거나 미확인입니다",
    "bmo_unknown": "BMO 실행 상태를 확인할 수 없습니다",
    "config_permissions": "기존 .env의 root 소유권·권한 600을 확인하세요",
    "config_invalid": "기존 .env 형식·UUID를 확인하세요",
    "config_missing": "기존 .env의 필수 항목을 확인하세요",
    "interrupted": "작업이 중단됐습니다",
    "operation_failed": "관측·권한·작업 상태를 로컬에서 확인하세요",
}


class MirrorError(RuntimeError):
    """민감한 URL·환경·Git 원문 대신 고정 이유만 전달한다."""


@dataclass(frozen=True)
class Repository:
    source: str
    base_name: str
    store_id: bytes
    samples: tuple[str, str]


def repository(repo="BGH_BMO_NEXUS"):
    # URL/경로 입력을 받지 않는다. 기존 NEXUS 경로·마커·복원 파일은 그대로 유지한다.
    if type(repo) is str:
        if repo == "BGH_BMO_NEXUS":
            return Repository(SOURCE, BASE_NAME, STORE_ID, SAMPLES)
        if repo == "BGH_BMO":
            return Repository("https://github.com/GUNOBAE/BGH_BMO.git", "bmo-git-mirrors",
                              b"NEXUS BGH_BMO public Git mirror store v1\n",
                              ("README.md", "README_KR.md"))
    raise MirrorError("repo_rejected")


def safe_reason(error):
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EIO, errno.EROFS, errno.ESTALE):
        return "space_or_io_error"
    code = str(error).partition(":")[0]
    return code if code in REASONS else "operation_failed"


def run_git(args, *, cwd_fd, input=None, timeout=180):
    """전용 세대의 고정 fd에서 실행한다. 개인 Git 설정·netrc·hook은 사용하지 않는다."""
    workspace = f"/proc/self/fd/{cwd_fd}"
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
        "HOME": workspace + "/git-home", "TMPDIR": workspace,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false", "GIT_SSH_COMMAND": "/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
    }
    argv = ["git", "--no-pager", "--no-optional-locks",
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "credential.helper=", "-c", "credential.interactive=false",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "http.followRedirects=false", "-c", "core.fsync=committed",
            "-c", "gc.auto=0", "-c", "maintenance.auto=false",
            "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true"] + list(args)
    try:
        return subprocess.run(argv, cwd=workspace, pass_fds=(cwd_fd,), env=environment,
                              input=input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, check=True).stdout
    except FileNotFoundError:
        raise MirrorError("git_missing") from None
    except subprocess.TimeoutExpired:
        raise MirrorError("git_timeout") from None
    except OSError as error:
        if error.errno in (errno.EIO, errno.ENOSPC, errno.EROFS, errno.ESTALE):
            raise MirrorError("space_or_io_error") from None
        raise MirrorError("git_failed") from None
    except subprocess.SubprocessError:
        raise MirrorError("git_failed") from None


def _device(signature):
    return os.makedev(*map(int, signature[1].split(":")))


def verify_fd(fd, signature, *, private=True):
    info = os.fstat(fd)
    if info.st_dev != _device(signature):
        raise MirrorError("ssd_changed")
    if not stat.S_ISDIR(info.st_mode):
        raise MirrorError("path_rejected")
    if private and (info.st_uid != OWNER_UID or info.st_gid != OWNER_GID
                    or stat.S_IMODE(info.st_mode) != 0o700):
        raise MirrorError("path_rejected")


def open_directory(parent_fd, name, signature):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        code = "space_or_io_error" if error.errno in (errno.EIO, errno.ENOSPC, errno.EROFS, errno.ESTALE) else "path_rejected"
        raise MirrorError(code) from None
    try:
        verify_fd(fd, signature)
        return fd
    except BaseException:
        os.close(fd)
        raise


def new_directory(parent_fd, name, signature):
    # exist_ok를 쓰지 않아 기존 디렉터리/부분 사본과 충돌하면 즉시 거부한다.
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        raise MirrorError("path_rejected") from None
    return open_directory(parent_fd, name, signature)


def read_file(parent_fd, name, signature, *, maximum=MAX_FILE):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_dev != _device(signature) or info.st_uid != OWNER_UID
                or info.st_gid != OWNER_GID or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > maximum):
            raise MirrorError("path_rejected")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(maximum + 1)
    finally:
        os.close(fd)


def write_file(parent_fd, name, data, signature):
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
    except FileExistsError:
        raise MirrorError("path_rejected") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_dev != _device(signature) or info.st_uid != OWNER_UID
                or info.st_gid != OWNER_GID):
            raise MirrorError("path_rejected")
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def audit_tree(fd, signature):
    """Git이 읽는 세대에 symlink·공유 inode·다른 파일시스템이 섞이면 거부한다."""
    verify_fd(fd, signature)
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = open_directory(fd, name, signature)
            try:
                audit_tree(child, signature)
            finally:
                os.close(child)
        elif (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
              or info.st_dev != _device(signature) or info.st_uid != OWNER_UID
              or info.st_gid != OWNER_GID or stat.S_IMODE(info.st_mode) & 0o077):
            raise MirrorError("path_rejected")


def gate(settings, signature, *, writing=False):
    if settings.mount != FIXED_MOUNT or settings.label != "BMO_SSD":
        raise MirrorError("settings_rejected")
    if cloud.check_storage(settings) != signature:
        raise MirrorError("ssd_changed")
    if writing:
        cloud.check_bmo_stopped(settings)


@contextmanager
def store(settings, signature, *, create=False, repo="BGH_BMO_NEXUS"):
    selected = repository(repo)
    # mount fd를 고정한 뒤 상대경로만 사용하므로 경로 교체로 SD에 대체 생성하지 않는다.
    with cloud.directory_fd(settings.mount) as mount_fd:
        verify_fd(mount_fd, signature, private=False)
        gate(settings, signature, writing=create)
        created = False
        if create:
            try:
                os.mkdir(selected.base_name, mode=0o700, dir_fd=mount_fd)
                created = True
            except FileExistsError:
                pass
        try:
            base_fd = open_directory(mount_fd, selected.base_name, signature)
        except MirrorError:
            if not create and not os.path.lexists(settings.mount / selected.base_name):
                yield None
                return
            raise
        try:
            if created:
                write_file(base_fd, STORE_MARKER, selected.store_id, signature)
                os.fsync(base_fd)
            else:
                try:
                    if read_file(base_fd, STORE_MARKER, signature) != selected.store_id:
                        raise MirrorError("path_rejected")
                except OSError as error:
                    code = "space_or_io_error" if error.errno in (errno.EIO, errno.ENOSPC, errno.EROFS, errno.ESTALE) else "path_rejected"
                    raise MirrorError(code) from None
            yield base_fd
        finally:
            os.close(base_fd)


def _git(runner, fd, *args, **kwargs):
    return runner(["--git-dir=mirror.git", *args], cwd_fd=fd, **kwargs)


def inspect_mirror(fd, signature, runner):
    audit_tree(fd, signature)
    home = open_directory(fd, "git-home", signature)
    try:
        if os.listdir(home):
            raise MirrorError("path_rejected")
    finally:
        os.close(home)
    if _git(runner, fd, "rev-parse", "--is-bare-repository").strip() != b"true":
        raise MirrorError("mirror_invalid")
    _git(runner, fd, "fsck", "--full", "--strict", "--no-reflogs")
    try:
        head = _git(runner, fd, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
        refs = _git(runner, fd, "for-each-ref", "--format=%(objectname) %(refname)")
        parsed = [line.decode("utf-8").split(" ", 1) for line in refs.splitlines()]
        if not OID.fullmatch(head) or not parsed:
            raise ValueError
        if any(not OID.fullmatch(oid) or not name.startswith("refs/") for oid, name in parsed):
            raise ValueError
        names = [name for _oid, name in parsed]
        if len(set(names)) != len(names):
            raise ValueError
        return {
            "head": head, "refs": len(names),
            "heads": sum(name.startswith("refs/heads/") for name in names),
            "tags": sum(name.startswith("refs/tags/") for name in names),
            "other": sum(not name.startswith(("refs/heads/", "refs/tags/")) for name in names),
            "refs_sha256": hashlib.sha256(refs).hexdigest(),
        }
    except (UnicodeError, ValueError, TypeError):
        raise MirrorError("mirror_invalid") from None


def sample_blob(fd, name, head, runner):
    try:
        rows = _git(runner, fd, "ls-tree", "-z", head, "--", name).split(b"\0")
        if len(rows) != 2 or rows[-1] != b"":
            raise ValueError
        fields, path = rows[0].split(b"\t", 1)
        mode, kind, raw_oid = fields.split()
        oid = raw_oid.decode("ascii")
        if mode not in (b"100644", b"100755") or kind != b"blob" or path != name.encode() or not OID.fullmatch(oid):
            raise ValueError
        size = int(_git(runner, fd, "cat-file", "-s", oid))
        if size < 1 or size > MAX_FILE:
            raise ValueError
        blob = _git(runner, fd, "cat-file", "blob", oid)
        if len(blob) != size:
            raise ValueError
        algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        if algorithm(b"blob " + str(size).encode() + b"\0" + blob).hexdigest() != oid:
            raise ValueError
        return blob
    except (UnicodeError, ValueError, TypeError):
        raise MirrorError("restore_invalid") from None


def sync(settings, *, confirmed, runner=run_git, repo="BGH_BMO_NEXUS"):
    selected = repository(repo)
    if not confirmed:
        raise MirrorError("confirmation_required")
    if settings.mount != FIXED_MOUNT or settings.label != "BMO_SSD":
        raise MirrorError("settings_rejected")
    signature = cloud.check_storage(settings)
    gate(settings, signature, writing=True)
    with store(settings, signature, create=True, repo=repo) as base_fd:
        name = "gen-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
        gate(settings, signature, writing=True)
        generation_fd = new_directory(base_fd, name, signature)
        try:
            home = new_directory(generation_fd, "git-home", signature)
            os.close(home)
            gate(settings, signature, writing=True)
            # --mirror의 강제 refspec으로 기존 사본을 갱신하지 않고 매번 새 사본을 만든다.
            runner(["clone", "--mirror", "--no-hardlinks", "--no-local", "--template=", "--", selected.source, "mirror.git"],
                   cwd_fd=generation_fd, timeout=300)
            gate(settings, signature, writing=True)
            report = inspect_mirror(generation_fd, signature, runner)
            restore_fd = new_directory(generation_fd, "restore-test", signature)
            try:
                hashes = {}
                for sample in selected.samples:
                    gate(settings, signature, writing=True)
                    blob = sample_blob(generation_fd, sample, report["head"], runner)
                    write_file(restore_fd, sample, blob, signature)
                    restored = read_file(restore_fd, sample, signature)
                    if restored != blob:
                        raise MirrorError("restore_invalid")
                    hashes[sample] = hashlib.sha256(restored).hexdigest()
                os.fsync(restore_fd)
            finally:
                os.close(restore_fd)
            mirror_fd = open_directory(generation_fd, "mirror.git", signature)
            try:
                report["config_sha256"] = hashlib.sha256(read_file(mirror_fd, "config", signature)).hexdigest()
            finally:
                os.close(mirror_fd)
            report.update({"generation": name, "restored": len(selected.samples), "files": hashes,
                           "completed_ns": time.time_ns()})
            gate(settings, signature, writing=True)
            write_file(generation_fd, "complete.json", json.dumps(report, sort_keys=True).encode() + b"\n", signature)
            os.fsync(generation_fd)
            os.fsync(base_fd)
            gate(settings, signature, writing=True)
            return report
        finally:
            os.close(generation_fd)


def status(settings, *, runner=run_git, repo="BGH_BMO_NEXUS"):
    selected = repository(repo)
    if settings.mount != FIXED_MOUNT or settings.label != "BMO_SSD":
        raise MirrorError("settings_rejected")
    signature = cloud.check_storage(settings)
    result = {"complete": 0, "partial": 0, "latest": None}
    with store(settings, signature, repo=repo) as base_fd:
        if base_fd is None:
            return result
        for name in sorted(os.listdir(base_fd)):
            if name == STORE_MARKER:
                continue
            if not GENERATION.fullmatch(name):
                raise MirrorError("path_rejected")
            generation_fd = open_directory(base_fd, name, signature)
            try:
                if "complete.json" not in os.listdir(generation_fd):
                    result["partial"] += 1
                    continue
                try:
                    saved = json.loads(read_file(generation_fd, "complete.json", signature))
                    mirror_fd = open_directory(generation_fd, "mirror.git", signature)
                    try:
                        actual = hashlib.sha256(read_file(mirror_fd, "config", signature)).hexdigest()
                    finally:
                        os.close(mirror_fd)
                    if (actual != saved["config_sha256"] or saved["generation"] != name
                            or type(saved["completed_ns"]) is not int or saved["completed_ns"] <= 0):
                        raise ValueError
                    gate(settings, signature)
                    actual_report = inspect_mirror(generation_fd, signature, runner)
                    if any(saved[key] != value for key, value in actual_report.items()):
                        raise ValueError
                    restore_fd = open_directory(generation_fd, "restore-test", signature)
                    try:
                        for sample in selected.samples:
                            data = read_file(restore_fd, sample, signature)
                            if (hashlib.sha256(data).hexdigest() != saved["files"][sample]
                                    or data != sample_blob(generation_fd, sample, saved["head"], runner)):
                                raise ValueError
                    finally:
                        os.close(restore_fd)
                    if saved["restored"] != len(selected.samples):
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise MirrorError("manifest_invalid") from None
                result["complete"] += 1
                if result["latest"] is None or saved["completed_ns"] > result["latest"]["completed_ns"]:
                    result["latest"] = saved
            finally:
                os.close(generation_fd)
    gate(settings, signature)
    return result


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, _message):
            self.exit(2, "MIRROR ERROR: arguments_invalid: 사용법을 확인하세요\n")

    parser = Parser(description="고정 PUBLIC GitHub 프로젝트의 SSD 미러·새 경로 복원 검증")
    parser.add_argument("--repo", choices=REPO_NAMES, default="BGH_BMO_NEXUS",
                        help="허용된 PUBLIC 대상 선택 (기본값: BGH_BMO_NEXUS)")
    parser.add_argument("--status", action="store_true", help="저장된 세대를 읽기 전용으로 재검증")
    parser.add_argument("--confirm-bmo-stopped", action="store_true")
    parser.add_argument("--config", type=Path, default=cloud.COMPOSE.parent / ".env")
    args = parser.parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise MirrorError("root_required")
        if not args.status and not args.confirm_bmo_stopped:
            raise MirrorError("confirmation_required")
        with cloud.operation_lock():
            settings = cloud.load_settings(args.config)
            if args.status:
                report = status(settings, repo=args.repo)
                print(f"MIRROR STATUS: complete={report['complete']} partial={report['partial']}")
                current = report["latest"]
            else:
                current = sync(settings, confirmed=True, repo=args.repo)
                print("MIRROR OK: 새 세대 복제·객체·복원 검증 완료. 이전 세대는 그대로 유지합니다")
            if current:
                print(f"HEAD={current['head']} refs={current['refs']} "
                      f"heads={current['heads']} tags={current['tags']} other={current['other']} restored={current['restored']}")
    except (Exception, KeyboardInterrupt) as error:
        code = safe_reason(error)
        print(f"MIRROR ERROR: {code}: {REASONS[code]}. 부분 사본은 삭제하지 않았습니다", file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    sys.exit(main())
