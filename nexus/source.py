"""Bounded reader for the pilot's loose-object, three-file Git fixture."""

import base64
import binascii
from contextlib import contextmanager
import hashlib
import os
import re
import stat
import zlib

from .storage import digest

FILES = ("NEXUS_FIXTURE.json", "model.py", "test_model.py")
MAX_FILE = 12000
PROFILE = "python-unittest-fixture-v1"
SANDBOX_PROFILE = "nx-003-bubblewrap"
PIN = re.compile(r"[0-9a-f]{40}")


class SourceError(Exception):
    pass


def canonical(path):
    if (not isinstance(path, str) or len(path) > 2048 or "\0" in path
            or not path.startswith("/") or any(p in ("", ".", "..") for p in path[1:].split("/"))):
        raise SourceError("noncanonical_checkout")
    try:
        path.encode("utf-8")
    except UnicodeError as exc:
        raise SourceError("noncanonical_checkout") from exc
    return path


@contextmanager
def directory(parent, parts):
    fd = os.open("/" if parent is None else ".", os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=parent)
    try:
        for part in parts:
            child = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_file(parent, name, limit):
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise SourceError("unsafe_or_oversized_source")
        with open(f"/proc/self/fd/{fd}", "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise SourceError("unsafe_or_oversized_source")
        return data
    finally:
        os.close(fd)


def revision(git):
    head = read_file(git, "HEAD", 256).strip()
    if head.startswith(b"ref: "):
        ref = head[5:]
        if not re.fullmatch(rb"refs/heads/[A-Za-z0-9_-]{1,80}", ref):
            raise SourceError("unsupported_head")
        with directory(git, ("refs", "heads")) as refs:
            head = read_file(refs, ref.split(b"/")[-1], 64).strip()
    if not re.fullmatch(rb"[0-9a-f]{40}", head):
        raise SourceError("unsupported_head")
    return head.decode("ascii")


def git_object(git, oid, kind):
    if not PIN.fullmatch(oid):
        raise SourceError("invalid_git_object")
    with directory(git, ("objects", oid[:2])) as folder:
        compressed = read_file(folder, oid[2:], MAX_FILE + 2048)
    stream = zlib.decompressobj()
    try:
        raw = stream.decompress(compressed, MAX_FILE + 128)
    except zlib.error as exc:
        raise SourceError("invalid_git_object") from exc
    if not stream.eof or stream.unused_data or hashlib.sha1(raw).hexdigest() != oid:
        raise SourceError("invalid_git_object")
    header, marker, data = raw.partition(b"\0")
    if (not marker or header != f"{kind} {len(data)}".encode("ascii") or len(data) > MAX_FILE):
        raise SourceError("invalid_git_object")
    return data


def source_files(git, commit):
    tree = git_object(git, commit, "commit").split(b"\n", 1)[0]
    if not re.fullmatch(rb"tree [0-9a-f]{40}", tree):
        raise SourceError("invalid_git_commit")
    remaining = git_object(git, tree[5:].decode("ascii"), "tree")
    objects = {}
    names = {name.encode("ascii"): name for name in FILES}
    while remaining:
        entry, marker, tail = remaining.partition(b"\0")
        mode, _, name = entry.partition(b" ")
        if not marker or len(tail) < 20 or mode != b"100644" or name not in names:
            raise SourceError("source_manifest_mismatch")
        objects[names.pop(name)] = tail[:20].hex()
        remaining = tail[20:]
    if names:
        raise SourceError("source_manifest_mismatch")
    return {name: git_object(git, oid, "blob") for name, oid in objects.items()}


def _manifest(data):
    # Canonical literal bytes avoid ambiguity around duplicate fields and coercion.
    if data == b'{"fixture":"nx-004","dependency":"python-stdlib"}\n':
        return
    if re.fullmatch(rb'{"fixture":"nx-004","dependency":"[a-z][a-z0-9-]{0,39}"}\n', data):
        return
    raise SourceError("source_manifest_mismatch")


def snapshot(path, pin, identity=None):
    path = canonical(path)
    if not isinstance(pin, str) or not PIN.fullmatch(pin):
        raise SourceError("invalid_revision")
    try:
        with directory(None, path[1:].split("/")) as root, directory(root, (".git",)) as git:
            root_stat, git_stat = os.fstat(root), os.fstat(git)
            actual = digest([path, root_stat.st_dev, root_stat.st_ino,
                             git_stat.st_dev, git_stat.st_ino])
            if identity is not None and actual != identity:
                raise SourceError("checkout_identity_changed")
            if revision(git) != pin:
                raise SourceError("source_revision_drift")
            files = source_files(git, pin)
            _manifest(files["NEXUS_FIXTURE.json"])
            if any(read_file(root, name, MAX_FILE) != content for name, content in files.items()):
                raise SourceError("source_worktree_drift")
            if revision(git) != pin:
                raise SourceError("source_revision_drift")
    except OSError as exc:
        raise SourceError("source_path_or_object_unavailable") from exc
    return actual, files


def snapshot_digest(files):
    return digest({name: hashlib.sha256(data).hexdigest() for name, data in files.items()})


def capture(observed, request, job):
    """Validate trusted supervisor metadata without interpreting test text."""
    status = observed.get("status")
    if status not in ("test_exited", "worker_timeout", "worker_output_oversized"):
        raise SourceError("invalid_test_observation")
    elapsed, exit_code, streams = (observed.get(key) for key in ("elapsed_ms", "exit", "streams"))
    if (type(elapsed) is not int or elapsed < 0
            or (status == "test_exited" and (type(exit_code) is not int or not 0 <= exit_code <= 255))
            or (status != "test_exited" and exit_code is not None)
            or not isinstance(streams, dict) or streams.keys() != {"stdout", "stderr"}):
        raise SourceError("invalid_test_observation")
    summary = {}
    missing_reported = False
    for key, value in streams.items():
        if not isinstance(value, str) or len(value) > 10924:
            raise SourceError("invalid_test_observation")
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SourceError("invalid_test_observation") from exc
        if key == "stderr":
            missing_reported = bool(re.search(rb"^ModuleNotFoundError:", data, re.M))
        summary[key] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if sum(value["bytes"] for value in summary.values()) > 8192:
        raise SourceError("invalid_test_observation")
    outcome = {
        0: "command_passed", 1: "command_exit_one",
    }.get(exit_code, "command_nonstandard_exit") if status == "test_exited" else status
    limitations = ["Output is controlled by the test process.",
                   "The supervisor observes process exit, not the truth of test assertions."]
    if exit_code == 1 and missing_reported:
        outcome = "dependency_reported_missing"
        limitations.append("The import error is test-reported and unverified.")
    extra = {}
    if "manifest" in request:
        manifest = request["manifest"]
        unchanged = observed.get("source_unchanged")
        if type(unchanged) is not bool:
            raise SourceError("invalid_test_observation")
        extra = {"manifest_digest": digest(manifest), "repository": manifest["repository"],
                 "dependency_digest": manifest["dependency_digest"], "source_unchanged": unchanged}
        if not unchanged:
            outcome = "source_changed_during_run"
        elif exit_code == 78:
            outcome = "dependency_unavailable"
        elif exit_code in (2, 3, 4, 5, 79, 126, 127):
            outcome = "test_infrastructure_failure"
    return {
        **extra, "kind": "source_test_capture", "resource": request["resource"], "base": request["base"],
        "snapshot": request["snapshot"], "scope": request["scope"],
        "command": request.get("manifest", {}).get("profile", PROFILE),
        "sandbox": SANDBOX_PROFILE, "worker": job.worker, "attempt": job.id,
        "authorization": request["source_authorization"], "revision": job.revision,
        "status": status, "exit_status": exit_code, "elapsed_ms": elapsed,
        "outcome": outcome, "provenance": "supervisor_process_observation",
        "observations": summary, "streams": streams,
        "limitations": limitations,
    }
