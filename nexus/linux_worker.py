"""Fixture launcher for the Linux worker adapter."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import time

from .fixtures import BASE, SCOPE
from .sandbox import ENV, MAX_OUTPUT, TIMEOUT

FINDINGS = Path(__file__).with_name("worker_fixture.py")


class RunnerBusy(Exception):
    pass


@contextmanager
def dispatch_lock(root):
    fd = os.open(root / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        file = os.fstat(fd)
        if (not stat.S_ISREG(file.st_mode) or file.st_uid != os.getuid()
                or file.st_mode & 0o077 or file.st_nlink != 1):
            raise OSError("unsafe worker lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerBusy from exc
        yield fd
    finally:
        # No explicit unlock: the supervisor's duplicate holds the open
        # file description across coordinator death until teardown.
        os.close(fd)


class LinuxRunner:
    def __init__(self, *, script=None, timeout=TIMEOUT, checkpoint_fault=None):
        # Replacing the built-in script and injecting commit faults are test
        # seams for trusted Python callers. The CLI accepts no executable input.
        self.script = FINDINGS.read_bytes() if script is None else script
        self.timeout = timeout
        self.checkpoint_fault = checkpoint_fault

    @contextmanager
    def launch(self, fd, job, *, files=None, pilot=None, reasoning_input=None, crosscheck=False,
               crosscheck_sha256=None):
        if crosscheck and (not pilot or pilot[1] is None or files or reasoning_input is not None):
            raise ValueError("invalid_crosscheck_profile")
        payload = "" if reasoning_input is not None or crosscheck else json.dumps({
            "phase": job.phase, "mode": job.mode, "candidate": job.candidate,
            "iteration": job.iteration, "base": BASE, "scope": SCOPE,
        }).encode("ascii").hex()
        supervisor = Path(__file__).with_name("sandbox.py").resolve()
        source_fd = None
        pilot_fds = []
        script = self.script
        timeout = self.timeout
        try:
            if files:
                source_fd = os.memfd_create("source-snapshot", os.MFD_CLOEXEC)
                with os.fdopen(os.dup(source_fd), "wb") as stream:
                    stream.write(json.dumps({name: data.hex() for name, data in files.items()})
                                 .encode("ascii"))
                os.lseek(source_fd, 0, os.SEEK_SET)
            extra = [] if source_fd is None else [str(source_fd)]
            descriptors = (fd,) if source_fd is None else (fd, source_fd)
            if reasoning_input is not None:
                source_fd = os.memfd_create("reasoning-input", os.MFD_CLOEXEC)
                with os.fdopen(os.dup(source_fd), "wb") as stream:
                    stream.write(reasoning_input)
                os.lseek(source_fd, 0, os.SEEK_SET)
                extra = ["reasoning", str(source_fd)]
                descriptors = (fd, source_fd)
            if pilot:
                runtime, tree = pilot
                for directory in (runtime, tree):
                    if directory is not None:
                        pilot_fds.append(os.open(directory, os.O_DIRECTORY | os.O_NOFOLLOW))
                extra = ["crosscheck" if crosscheck else "pilot", str(pilot_fds[0]), str(pilot_fds[1]) if tree is not None else "-"]
                descriptors += tuple(pilot_fds)
                script_name = "hero_cad_volume.py" if crosscheck else "hero_cad_test.py"
                script = Path(__file__).with_name(script_name).read_bytes()
                if crosscheck and hashlib.sha256(script).hexdigest() != crosscheck_sha256:
                    raise ValueError("crosscheck_script_digest_mismatch")
                timeout = 15
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", supervisor, str(fd), str(timeout),
                 script.hex(), payload, *extra],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=descriptors, cwd="/", env=ENV,
            )
        finally:
            if source_fd is not None:
                os.close(source_fd)
            for handle in pilot_fds:
                os.close(handle)
        try:
            yield process
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # The supervisor still holds the lock while cleaning up.
                pass
            process.stdout.close()

    def wait(self, process, current, *, pilot=False):
        output = bytearray()
        deadline = time.monotonic() + (15 if pilot else self.timeout) + 3
        with selectors.DefaultSelector() as select:
            select.register(process.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() >= deadline:
                    return {"status": "worker_cleanup_pending"}
                if not process.stdin.closed and not current():
                    process.stdin.close()
                if not select.select(0.02):
                    continue
                block = os.read(process.stdout.fileno(), 4096)
                if not block:
                    break
                output.extend(block)
                if len(output) > MAX_OUTPUT * 8:
                    return {"status": "supervisor_output_invalid"}
        if process.wait():
            return {"status": "supervisor_failed"}
        try:
            result = json.loads(output)
        except (ValueError, RecursionError):
            return {"status": "supervisor_output_invalid"}
        return result
