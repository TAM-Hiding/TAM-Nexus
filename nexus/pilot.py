"""Operator-owned, manifest-bound HERO-CAD pilot inputs."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import time
from tempfile import TemporaryDirectory

from . import source
from .storage import digest, private_directory

PROFILE = "hero-cad-box-v1"
REPOSITORY = "TAM-Hiding/hero-cad"
TEST = "tests/test_document.py"
ARGV = ["/runtime/bin/python3", "-I", "-S", "-B", "/input/hero_cad_test.py"]
LIMITS = {"wall_seconds": 15, "cpu_seconds": 12, "address_space_bytes": 2147483648,
          "processes": 64, "scratch_bytes": 4194304, "output_bytes": 8192}
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_PATH = re.compile(r"src/hero_cad/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py")
MAX_RUNTIME = 2 * 1024**3


def manifest(value):
    """Validate exact v1 fields and a single executable/evidence profile."""
    fixed = {"version": 1, "repository": REPOSITORY, "profile": PROFILE,
             "argv": ARGV, "limits": LIMITS, "evidence": ["exit", "stdout", "stderr"]}
    keys = fixed.keys() | {"revision", "source_digest", "source_paths", "dependency_digest"}
    if (not isinstance(value, dict) or value.keys() != keys
            or any(type(value[key]) is not type(expected) or value[key] != expected
                   for key, expected in fixed.items())
            or any(type(n) is not int for n in value["limits"].values())):
        raise source.SourceError("invalid_pilot_manifest")
    for key, pattern in (("revision", source.PIN), ("source_digest", SHA256),
                         ("dependency_digest", SHA256)):
        if not isinstance(value[key], str) or not pattern.fullmatch(value[key]):
            raise source.SourceError("invalid_pilot_manifest")
    paths = value["source_paths"]
    if (not isinstance(paths, list) or not 3 <= len(paths) <= 96
            or any(not isinstance(path, str) or len(path) > 150 or
                   (path != TEST and not SOURCE_PATH.fullmatch(path)) for path in paths)
            or sorted(set(paths)) != paths or TEST not in paths
            or "src/hero_cad/__init__.py" not in paths):
        raise source.SourceError("invalid_pilot_paths")
    return value


def load_manifest(path):
    def unique(pairs):
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError("duplicate manifest key")
        return values

    try:
        with open(path, "rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError("oversized manifest")
        return manifest(json.loads(data, object_pairs_hook=unique))
    except (OSError, ValueError, RecursionError) as exc:
        raise source.SourceError("invalid_pilot_manifest") from exc


def manifest_from_archive(revision, dependency_digest, stream):
    """Build an operator manifest from a local Git archive without extracting it."""
    raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise source.SourceError("pilot_archive_oversized")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            if archive.pax_headers.get("comment") != revision:
                raise source.SourceError("pilot_archive_revision_mismatch")
            for entry in archive:
                if entry.name != TEST and not SOURCE_PATH.fullmatch(entry.name):
                    continue
                if not entry.isfile() or entry.size > 131072 or entry.name in files:
                    raise source.SourceError("unsafe_pilot_archive_entry")
                with archive.extractfile(entry) as blob:
                    files[entry.name] = blob.read()
    except (tarfile.TarError, ValueError, OSError) as exc:
        raise source.SourceError("invalid_pilot_archive") from exc
    return manifest({"version": 1, "repository": REPOSITORY, "profile": PROFILE,
                     "argv": ARGV, "limits": LIMITS, "evidence": ["exit", "stdout", "stderr"],
                     "revision": revision, "source_digest": source.snapshot_digest(files),
                     "source_paths": sorted(files), "dependency_digest": dependency_digest})


def checkout_files(path, spec, identity=None):
    path = source.canonical(path)
    try:
        with source.directory(None, path[1:].split("/")) as root, source.directory(root, (".git",)) as git:
            root_stat, git_stat = os.fstat(root), os.fstat(git)
            actual = digest([path, root_stat.st_dev, root_stat.st_ino, git_stat.st_dev, git_stat.st_ino])
            if identity is not None and identity != actual:
                raise source.SourceError("checkout_identity_changed")
            pin = spec["revision"].encode("ascii")
            if source.read_file(git, "HEAD", 256).strip() != pin:
                raise source.SourceError("pilot_detached_revision_required")
            files = {}
            for name in spec["source_paths"]:
                parent, _, leaf = name.rpartition("/")
                with source.directory(root, parent.split("/")) as folder:
                    files[name] = source.read_file(folder, leaf, 131072)
            if source.read_file(git, "HEAD", 256).strip() != pin:
                raise source.SourceError("source_revision_drift")
            if source.snapshot_digest(files) != spec["source_digest"]:
                raise source.SourceError("source_snapshot_drift")
            return actual, files
    except OSError as exc:
        raise source.SourceError("source_path_or_object_unavailable") from exc


def runtime_digest(path, staging=None, current=None):
    """Hash bounded regular files and optionally stage those exact bytes."""
    path = source.canonical(path)
    records = {}
    total = 0
    visited = 0
    deadline = time.monotonic() + 90

    def check():
        if time.monotonic() >= deadline:
            raise source.SourceError("dependency_staging_timeout")
        if current is not None and not current():
            raise source.SourceError("worker_cancelled")

    def walk(folder, prefix="", depth=0):
        nonlocal visited
        if depth > 24:
            raise source.SourceError("dependency_tree_oversized")
        with os.scandir(folder) as entries:
            for entry in entries:
                name = prefix + entry.name
                visited += 1
                if visited % 128 == 1:
                    check()
                if visited > 40000 or len(name) > 512 or not re.fullmatch(r"[A-Za-z0-9_.+/ -]+", name):
                    raise source.SourceError("dependency_tree_oversized")
                if entry.is_dir(follow_symlinks=False):
                    with source.directory(folder, (entry.name,)) as child:
                        walk(child, name + "/", depth + 1)
                    continue
                fd = os.open(entry.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise source.SourceError("unsafe_dependency_entry")
                    target = staging / name if staging else None
                    if target:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    hashed = hashlib.sha256()
                    with open(f"/proc/self/fd/{fd}", "rb") as stream:
                        if target:
                            with target.open("xb") as output:
                                read_stream(stream, hashed, output)
                            target.chmod(0o755 if info.st_mode & 0o111 else 0o644)
                        else:
                            read_stream(stream, hashed)
                    records[name] = [bool(info.st_mode & 0o111), hashed.hexdigest()]
                finally:
                    os.close(fd)

    def read_stream(stream, hashed, target=None):
        nonlocal total
        while block := stream.read(1024 * 1024):
            total += len(block)
            if time.monotonic() >= deadline:
                raise source.SourceError("dependency_staging_timeout")
            if total > MAX_RUNTIME:
                raise source.SourceError("dependency_tree_oversized")
            hashed.update(block)
            if target:
                target.write(block)

    try:
        with source.directory(None, path[1:].split("/")) as folder:
            walk(folder)
    except OSError as exc:
        raise source.SourceError("dependency_environment_unavailable") from exc
    if ("bin/python3" not in records or not records["bin/python3"][0]
            or "lib/python3.12/encodings/__init__.py" not in records
            or "packages/pytest/__init__.py" not in records):
        raise source.SourceError("dependency_environment_incomplete")
    return digest(records)


@contextmanager
def staged_runtime(path, expected, root, *, current=None):
    with TemporaryDirectory(prefix="pilot-", dir=root) as directory:
        stage = Path(directory)
        if runtime_digest(path, stage, current) != expected:
            raise source.SourceError("dependency_digest_mismatch")
        yield stage


def stage_root(root):
    """Called only while the dispatch lock excludes previous supervisors."""
    stage = private_directory(root / "pilot-staging")
    for entry in stage.iterdir():
        if not re.fullmatch(r"(?:pilot|source)-[a-z0-9_]{8}", entry.name):
            raise source.SourceError("unexpected_pilot_staging_entry")
        if entry.is_symlink() or not entry.is_dir():
            raise source.SourceError("unexpected_pilot_staging_entry")
        shutil.rmtree(entry)
    return stage


def preflight(environment=None, expected=None):
    """Probe the actual sandbox without opening the coordinator database."""
    from . import sandbox

    checks = {}
    try:
        python, _ = sandbox.require_host()
        checks["host"] = {"status": "ok", "system_python": python.name}
    except sandbox.SandboxUnavailable as exc:
        return {"ok": False, "checks": {"host": str(exc)},
                "hint": "Check x86-64 Linux, system Python, Bubblewrap, util-linux and pidfds."}
    try:
        help_text = subprocess.run([sandbox.BWRAP, "--help"], env=sandbox.ENV,
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=3, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        help_text = b""
    required = ("--ro-bind-fd", "--json-status-fd", "--ro-bind-data", "--disable-userns")
    missing = [flag for flag in required if flag.encode() not in help_text]
    if missing:
        checks["bubblewrap"] = {"missing_flags": missing}
        return {"ok": False, "checks": checks,
                "hint": "Use a maintained Bubblewrap build with the required flags."}
    checks["bubblewrap"] = "ok"
    control, writer = os.pipe()
    try:
        with TemporaryDirectory(prefix="nexus-preflight-") as temporary:
            root = Path(temporary)
            (root / "probe").write_bytes(b"")
            probe = sandbox.supervise(
                b'import pathlib; pathlib.Path("/scratch/probe").write_text("ok"); print("{}")',
                b"{}", sandbox.TIMEOUT, control)
            checks["sandbox"] = probe["status"]
            if probe["status"] != "observed":
                return {"ok": False, "checks": checks,
                        "hint": "Check required Bubblewrap flags, namespaces and seccomp. Use strict NX-003 tests for detail."}
            checks["temporary_storage"] = "ok"
            if environment is None or expected is None:
                checks["dependencies"] = "not_checked"
                return {"ok": True, "ready_for_hero_cad": False, "checks": checks}
            if not SHA256.fullmatch(expected):
                raise source.SourceError("invalid_dependency_digest")
            with staged_runtime(environment, expected, root) as stage:
                fd = os.open(stage, os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    script = Path(__file__).with_name("hero_cad_test.py").read_bytes()
                    result = sandbox.supervise(script, b"", LIMITS["wall_seconds"], control,
                                               pilot=(fd, None, True))
                finally:
                    os.close(fd)
            if result.get("status") != "test_exited" or result.get("exit") != 0:
                checks["dependencies"] = ("dependency_unavailable" if result.get("exit") == 78
                                          else result["status"] + "_dependency_probe")
                return {"ok": False, "checks": checks,
                        "hint": "Check the provisioned CPython 3.12, native libraries and pinned wheels."}
            checks["dependencies"] = "ok"
            return {"ok": True, "ready_for_hero_cad": True, "checks": checks}
    except (OSError, source.SourceError) as exc:
        checks["provisioning"] = str(exc) if isinstance(exc, source.SourceError) else "temporary_storage_unavailable"
        return {"ok": False, "checks": checks,
                "hint": "Check the private temporary directory and reprovision or reapprove the dependency digest."}
    finally:
        os.close(control)
        os.close(writer)
