"""Trusted Linux supervisor. The worker receives only the fixture and its script."""

import base64
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import subprocess
import sys
import time
from contextlib import ExitStack

BWRAP = "/usr/bin/bwrap"
UNSHARE = "/usr/bin/unshare"
PRLIMIT = "/usr/bin/prlimit"
SYSTEM_PYTHON = "/usr/bin/python3"
MAX_OUTPUT = 8192
TIMEOUT = 2.0
ENV = {"LANG": "C", "LC_ALL": "C"}
# A second network guard covers socket families across namespace boundaries.
DENIED_SYSCALLS = (
    "socket", "socketpair", "io_uring_setup", "io_uring_register", "io_uring_enter",
    "prctl", "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
    "keyctl", "add_key", "request_key", "bpf", "perf_event_open",
)
LIBRARIES = ("libc.so.6", "libm.so.6", "libz.so.1", "libexpat.so.1")
TRIPLET = "x86_64-linux-gnu"
LOADER = "ld-linux-x86-64.so.2"


class SandboxUnavailable(Exception):
    pass


def require_host():
    if sys.platform != "linux" or platform.machine() != "x86_64" or os.geteuid() == 0:
        raise SandboxUnavailable("unsupported_host")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise SandboxUnavailable("pidfd_unavailable")
    try:
        trial = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(trial, 0)
        finally:
            os.close(trial)
    except OSError as exc:
        raise SandboxUnavailable("pidfd_unavailable") from exc
    for path in (BWRAP, UNSHARE, PRLIMIT, SYSTEM_PYTHON):
        if not os.access(path, os.X_OK):
            raise SandboxUnavailable(f"missing_{Path(path).name}")
    python = Path(SYSTEM_PYTHON).resolve()
    if python.parent != Path("/usr/bin") or not re.fullmatch(r"python3\.(1[0-9]|[2-9][0-9])", python.name):
        raise SandboxUnavailable("unsupported_system_python")
    stdlib = Path("/usr/lib") / python.name
    if not stdlib.is_dir():
        raise SandboxUnavailable("missing_python_stdlib")
    return python, stdlib


def _filter(fd):
    try:
        lib = ctypes.CDLL(f"/usr/lib/{TRIPLET}/libseccomp.so.2")
    except OSError as exc:
        raise SandboxUnavailable("missing_libseccomp") from exc
    allow = 0x7FFF0000
    deny = 0x00050000 | errno.EPERM
    signature = {
        "seccomp_init": (ctypes.c_void_p, (ctypes.c_uint32,)),
        "seccomp_syscall_resolve_name": (ctypes.c_int, (ctypes.c_char_p,)),
        "seccomp_rule_add": (ctypes.c_int, (ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_int, ctypes.c_uint)),
        "seccomp_export_bpf": (ctypes.c_int, (ctypes.c_void_p, ctypes.c_int)),
        "seccomp_release": (None, (ctypes.c_void_p,)),
    }
    for name, (restype, argtypes) in signature.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    ctx = lib.seccomp_init(allow)
    if not ctx:
        raise SandboxUnavailable("seccomp_init_failed")
    try:
        for name in DENIED_SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0 or lib.seccomp_rule_add(ctx, deny, number, 0):
                raise SandboxUnavailable("seccomp_rule_failed")
        if lib.seccomp_export_bpf(ctx, fd):
            raise SandboxUnavailable("seccomp_export_failed")
        os.lseek(fd, 0, os.SEEK_SET)
    finally:
        lib.seccomp_release(ctx)


def _command(python, stdlib, mounts, filter_fd, status_fd, source, pilot=None):
    args = [
        PRLIMIT, "--as=268435456:268435456", "--cpu=3:3", "--nproc=64:64",
        "--nofile=64:64", "--core=0:0", "--fsize=4194304:4194304", "--",
        UNSHARE, "--user", "--map-current-user", "--net", "--",
        BWRAP, "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
        "--disable-userns", "--cap-drop", "ALL", "--new-session", "--die-with-parent",
        "--as-pid-1", "--json-status-fd", str(status_fd), "--seccomp", str(filter_fd),
        "--ro-bind", str(python), "/usr/bin/python3", "--ro-bind", str(stdlib), str(stdlib),
        "--symlink", "usr/lib", "/lib", "--dir", "/lib64",
        "--symlink", f"/usr/lib/{TRIPLET}/{LOADER}", f"/lib64/{LOADER}",
    ]
    if pilot:
        args[1:3] = ["--as=2147483648:2147483648", "--cpu=12:12"]
    for name in (*LIBRARIES, LOADER):
        path = f"/usr/lib/{TRIPLET}/{name}"
        args.extend(("--ro-bind", path, path))
    for path, fd in mounts:
        args.extend(("--ro-bind-data", str(fd), path))
    if pilot:
        runtime_fd, source_fd, preflight = pilot
        args.extend(("--ro-bind-fd", str(runtime_fd), "/runtime"))
        if source_fd is not None:
            args.extend(("--ro-bind-fd", str(source_fd), "/source"))
    args.extend((
        "--perms", "0700", "--size", "4194304", "--tmpfs", "/scratch",
        "--remount-ro", "/", "--clearenv", "--setenv", "HOME", "/nonexistent",
        "--setenv", "LANG", "C", "--setenv", "LC_ALL", "C", "--chdir", "/scratch",
    ))
    if pilot:
        args.extend(("--setenv", "TMPDIR", "/scratch", "--setenv", "LD_LIBRARY_PATH",
                     "/runtime/lib:/runtime/native",
                     "--", "/runtime/bin/python3", "-I", "-S", "-B", "/input/hero_cad_test.py"))
        if preflight:
            args.append("preflight")
        return args
    args.extend(("--", "/usr/bin/python3", "-I", "-S", "-B"))
    args.extend(("-m", "unittest", "discover", "-s", "/source", "-p", "test_model.py", "-v")
                if source else ("/input/worker.py",))
    return args


def _open_memfd(stack, name, content=b""):
    fd = os.memfd_create(name, os.MFD_CLOEXEC)
    stack.callback(os.close, fd)
    if os.write(fd, content) != len(content):
        raise SandboxUnavailable("fixture_write_failed")
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _pidfd(child, launcher):
    # Bwrap reports its own direct child on a private pipe. Check lineage after
    # pinning the PID so PID reuse cannot redirect signal delivery.
    fd = os.pidfd_open(child)
    try:
        stat = Path(f"/proc/{child}/stat").read_text()
        if int(stat.rsplit(")", 1)[1].split()[1]) == launcher:
            return fd
    except (OSError, ValueError, IndexError):
        pass
    os.close(fd)
    raise ProcessLookupError(child)


def _kill_tree(process, child_fd):
    if child_fd is not None:
        try:
            signal.pidfd_send_signal(child_fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Sandbox setup has not reported a PID yet. Its parent death signal
        # handles any setup child. No worker descriptors are available to it.
        process.kill()
    process.wait()


def _collect(process, status_fd, control, timeout, source):
    start = time.monotonic()
    deadline = start + timeout
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    output = streams["stdout"]

    def captured(status, exit_code=None):
        result = {"status": status}
        if source:
            result.update(exit=exit_code, elapsed_ms=int((time.monotonic() - start) * 1000),
                          streams={key: base64.b64encode(data).decode("ascii")
                                   for key, data in streams.items()})
        return result
    metadata = bytearray()
    child_fd = None
    child = None
    code = None
    failure = None
    try:
        with selectors.DefaultSelector() as select:
            select.register(process.stdout, selectors.EVENT_READ, "stdout")
            if source:
                select.register(process.stderr, selectors.EVENT_READ, "stderr")
            select.register(status_fd, selectors.EVENT_READ, "status")
            select.register(control, selectors.EVENT_READ, "cancel")
            while len(select.get_map()) > 1 or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "worker_timeout"
                    break
                for key, _ in select.select(min(remaining, 0.05)):
                    if key.data == "cancel":
                        failure = "worker_cancelled"
                        break
                    block = os.read(key.fd, 4096)
                    if not block:
                        select.unregister(key.fileobj)
                        continue
                    if key.data in streams:
                        available = MAX_OUTPUT - sum(map(len, streams.values()))
                        streams[key.data].extend(block[:available])
                        if len(block) > available:
                            failure = "worker_output_oversized"
                            break
                        continue
                    metadata.extend(block)
                    if len(metadata) > 4096:
                        failure = "sandbox_status_invalid"
                        break
                    while b"\n" in metadata:
                        line, _, tail = metadata.partition(b"\n")
                        metadata[:] = tail
                        info = json.loads(line)
                        if "child-pid" in info:
                            child = info["child-pid"]
                            try:
                                child_fd = _pidfd(child, process.pid)
                            except ProcessLookupError:
                                # It may already have exited. The private exit
                                # status and launcher wait establish the outcome.
                                pass
                        if "exit-code" in info:
                            code = info["exit-code"]
                if failure:
                    break
        if failure:
            _kill_tree(process, child_fd)
            return captured(failure)
        exit_code = process.wait()
        if child is None or code is None or code != exit_code:
            return {"status": "sandbox_setup_failed"}
        if source:
            return captured("test_exited", code)
        if code:
            return {"status": "worker_failed", "exit": code}
        try:
            value = _parse_output(output)
        except (ValueError, UnicodeError, RecursionError):
            return {"status": "worker_output_invalid"}
        return {"status": "observed", "exit": code, "output": value}
    finally:
        if process.poll() is None:
            _kill_tree(process, child_fd)
        if child_fd is not None:
            os.close(child_fd)


def _parse_output(output):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate result field")
            result[key] = value
        return result

    def invalid(_constant):
        raise ValueError("non-finite result")

    value = json.loads(output.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid,
                       parse_float=invalid)
    if not isinstance(value, dict):
        raise ValueError("result must be an object")
    return value


def supervise(script, fixture, timeout, control, *, files=None, pilot=None):
    with ExitStack() as stack:
        try:
            python, stdlib = require_host()
            if pilot:
                inputs = {"/input/hero_cad_test.py": script, "/input/pytest.ini": b"[pytest]\n"}
            elif files is None:
                inputs = {"/input/worker.py": script, "/input/job.json": fixture}
            else:
                names = {"NEXUS_FIXTURE.json", "model.py", "test_model.py"}
                if files.keys() != names or any(len(data) > 12000 for data in files.values()):
                    raise SandboxUnavailable("source_manifest_mismatch")
                inputs = {"/source/" + name: data for name, data in files.items()}
            mounts = [(path, _open_memfd(stack, "input", data)) for path, data in inputs.items()]
            filter_fd = _open_memfd(stack, "filter")
            _filter(filter_fd)
            status_read, status_write = os.pipe()
            stack.callback(os.close, status_read)
            capture_streams = files is not None or pilot is not None
            pilot_fds = tuple(fd for fd in pilot[:2] if fd is not None) if pilot else ()
            try:
                process = subprocess.Popen(
                    _command(python, stdlib, mounts, filter_fd, status_write, files is not None, pilot),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE if capture_streams else subprocess.DEVNULL,
                    close_fds=True, pass_fds=(*[fd for _, fd in mounts], filter_fd, status_write, *pilot_fds),
                    cwd="/", env=ENV,
                )
            finally:
                os.close(status_write)
            stack.callback(process.stdout.close)
            if process.stderr:
                stack.callback(process.stderr.close)
        except SandboxUnavailable as exc:
            return {"status": "sandbox_unavailable", "detail": str(exc)}
        except OSError:
            return {"status": "sandbox_setup_failed"}
        return _collect(process, status_read, control, timeout, capture_streams)


if __name__ == "__main__":
    # The launcher passes a private liveness pipe on stdin and a dispatch lock
    # descriptor. Hold the lock until the sandbox has been reaped, even on EOF.
    lock_fd, timeout, script, fixture, *source = sys.argv[1:]
    try:
        os.fstat(int(lock_fd))
        files = None
        pilot = None
        if source and source[0] == "pilot":
            pilot = (int(source[1]), None if source[2] == "-" else int(source[2]), source[2] == "-")
        elif source:
            with os.fdopen(int(source[0]), "rb") as stream:
                serialized = stream.read(74001)
            if len(serialized) > 74000:
                raise SandboxUnavailable("source_manifest_mismatch")
            files = {key: bytes.fromhex(value) for key, value in json.loads(serialized).items()}
        result = supervise(bytes.fromhex(script), bytes.fromhex(fixture), float(timeout), 0, files=files, pilot=pilot)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    finally:
        os.close(int(lock_fd))
