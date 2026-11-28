"""Kernel isolation and durable subprocess recovery, with no live resources."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from nexus.coordinator import Coordinator, OPERATOR
from nexus.linux_worker import LinuxRunner, dispatch_lock
from nexus.sandbox import supervise
from nexus.storage import InjectedCrash

FIXTURES = Path(__file__).with_name("fixtures")


@contextmanager
def control_pipe():
    read, write = os.pipe()
    try:
        yield read
    finally:
        os.close(write)
        os.close(read)


def probe(script, fixture=b'{"phase":"investigate"}', timeout=2):
    with control_pipe() as control:
        return supervise(script, fixture, timeout, control)


def process_markers(marker, operation):
    """Prove both descendants existed, then pin identities with pidfds."""
    deadline = time.monotonic() + 2.5
    fds = {}
    try:
        while len(fds) < 2 and time.monotonic() < deadline:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdecimal():
                    continue
                try:
                    argv = (entry / "cmdline").read_bytes().split(b"\0")
                    for suffix in ("-child", "-grandchild"):
                        if suffix not in fds and (marker + suffix).encode() in argv:
                            fds[suffix] = os.pidfd_open(int(entry.name))
                except OSError:
                    continue
            if len(fds) < 2:
                time.sleep(0.01)
        if len(fds) != 2:
            raise AssertionError(f"found {len(fds)} of two expected descendants")
        operation()
        for fd in fds.values():
            if not select.select([fd], [], [], 4)[0]:
                raise AssertionError("descendant survived process cleanup")
    finally:
        for fd in fds.values():
            os.close(fd)


class LinuxState(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.coordinator = Coordinator(self.root)
        self.addCleanup(self.coordinator.__exit__)
        self.counter = 0

    def request(self, action, work=None, *, command=None, version=None, coordinator=None, **args):
        coordinator = coordinator or self.coordinator
        self.counter += 1
        if work is not None and version is None:
            version = coordinator.report(OPERATOR, work)["work"]["version"]
        return coordinator.execute(OPERATOR, command or f"command-{self.counter}", action,
                                   work=work, version=version, **args)

    def ok(self, *args, **kwargs):
        result = self.request(*args, **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def submit(self, **args):
        message = self.ok("message", text="Synthetic X/Y fails")["message"]
        return self.ok("submit", message=message, **args)["work"]


class FailClosedTests(LinuxState):
    def test_missing_required_sandbox_dependency_refuses_execution(self):
        with patch("nexus.sandbox.BWRAP", "/no/such/bwrap"), control_pipe() as control:
            result = supervise(b"raise RuntimeError('worker ran')", b"{}", 0.2, control)
        self.assertEqual(result["status"], "sandbox_unavailable")

    def test_migration_preserves_existing_state(self):
        work = self.submit()
        before = self.coordinator.report(OPERATOR, work)
        with sqlite3.connect(self.root / "nexus.sqlite3") as old:
            for table in ("experiment_runs", "experiment_proposals", "reasoning_charges", "reasoning_invocations"):
                old.execute(f"DROP TABLE {table}")
            old.execute("DROP TABLE pilot_authorizations")
            old.execute("DROP TABLE source_authorizations")
            old.execute("DROP TABLE linux_runs")
            old.execute("PRAGMA user_version=1")
        with Coordinator(self.root) as reopened:
            self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(reopened.report(OPERATOR, work), before)

    def test_linux_arm_prevents_fallback_and_preserves_budget(self):
        work = self.submit(budget=1)
        with Coordinator(self.root, runner=LinuxRunner(checkpoint_fault="before_launch")) as linux:
            with self.assertRaises(InjectedCrash):
                self.ok("run", work, coordinator=linux)
        self.assertEqual(self.request("run", work)["reason"], "linux_profile_required")
        self.ok("recover")
        self.assertEqual(self.request("resume", work)["reason"], "budget_exhausted")
        self.assertFalse(self.coordinator.report(OPERATOR, work)["evidence"])

    def test_dispatch_lock_blocks_fake_work_and_mock_publication_until_cleanup(self):
        work = self.submit()
        self.ok("run", work)
        implementation = self.ok("propose", work)["work"]
        for action in ("implement", "publish"):
            binding = self.coordinator.report(OPERATOR, implementation)["pending_approval"]
            self.ok("decide", implementation, binding=binding, allow=True)
            if action == "implement":
                for _ in range(3):
                    self.ok("run", implementation)
        other = self.submit()
        with dispatch_lock(self.root):
            self.assertEqual(self.request("run", other)["reason"], "runner_busy")
            self.assertEqual(self.request("publish", implementation)["reason"], "runner_busy")
        self.ok("run", other)
        self.ok("publish", implementation)

    def test_fake_run_cannot_satisfy_explicit_linux_replay(self):
        work = self.submit()
        self.ok("run", work, command="profile", version=1)
        with Coordinator(self.root, runner=LinuxRunner()) as linux:
            result = self.request("run", work, coordinator=linux, command="profile", version=1)
        self.assertEqual(result["reason"], "command_conflict")
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM linux_runs").fetchone()[0], 0)


class KernelTests(LinuxState):
    @classmethod
    def setUpClass(cls):
        result = probe(b'print("{}")')
        if result["status"] == "observed":
            return
        if result["status"] in ("sandbox_unavailable", "sandbox_setup_failed"):
            reason = f"required sandbox profile unavailable: {result}"
            if os.environ.get("NEXUS_REQUIRE_LINUX") != "1":
                raise unittest.SkipTest(reason)
            raise AssertionError(reason)
        raise AssertionError(f"unexpected sandbox probe result: {result}")

    def test_kernel_filesystem_network_fd_environment_and_limits(self):
        private = Path(self.temp.name) / "private-home"
        private.mkdir()
        home = private / "private-value"
        credentials = private / "credential"
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        for path in (home, credentials):
            path.write_text("synthetic-only")
        sentinels = {
            "state": str(self.root / "nexus.sqlite3"), "private": str(home),
            "credential": str(credentials), "outside": str(outside),
        }
        script = ("SENTINELS = " + repr(sentinels) + "\n").encode() + (
            FIXTURES / "isolation_probe.py").read_bytes()
        with home.open("rb") as inherited, patch.dict(os.environ, {
            "NX_PRIVATE_TOKEN": "synthetic", "HOME": str(private),
            "SSH_AUTH_SOCK": str(private / "agent.sock"), "PYTHONPATH": str(private),
        }):
            os.set_inheritable(inherited.fileno(), True)
            result = probe(script)
        self.assertEqual(result["status"], "observed", result)
        observed = result["output"]
        self.assertEqual(observed["fixture"], "investigate")
        self.assertEqual(observed["scratch"], "scratch")
        self.assertEqual(observed["readonly"], errno.EROFS)
        for path in ("state", "private", "credential", "traversal", "symlink", "outside_write"):
            value = observed["denied_reads"][path] if path in observed["denied_reads"] else observed[path]
            self.assertEqual(value, errno.ENOENT, path)
        self.assertEqual(observed["root_write"], errno.EROFS)
        self.assertEqual(observed["network"], [errno.EPERM] * 3)
        self.assertEqual(observed["connection"], errno.EPERM)
        self.assertEqual(observed["descriptors"], [0, 1, 2])
        self.assertEqual(observed["environment"], ["HOME", "LANG", "LC_ALL", "PWD"])
        self.assertEqual(observed["pid"], 1)
        self.assertEqual(observed["memory_budget"], [268435456] * 2)
        self.assertEqual(observed["memory_denied"], errno.ENOMEM)
        self.assertEqual(observed["process_budget"], [64, 64])
        self.assertFalse((outside / "must-not-be-written").exists())

    def test_kernel_timeout_kills_descendants_in_other_sessions(self):
        work = self.submit()
        marker = f"nx-tree-{os.getpid()}-timeout"
        script = (f"MARKER = {marker!r}\n".encode() + (FIXTURES / "worker_tree.py").read_bytes())

        def run():
            with Coordinator(self.root, runner=LinuxRunner(script=script, timeout=2.0)) as linux:
                return linux.execute(OPERATOR, "tree", "run", work=work, version=1)

        with ThreadPoolExecutor() as pool:
            launched = pool.submit(run)
            process_markers(marker, lambda: None)
            result = launched.result(timeout=5)
        self.assertEqual(result["reason"], "worker_timeout")
        self.assertFalse(self.coordinator.report(OPERATOR, work)["evidence"])

    def test_kernel_fork_and_scratch_limits_are_enforced(self):
        script = b"""
import json, os, time
from pathlib import Path

try:
    Path("one").write_bytes(bytes(3 * 1024 * 1024))
    Path("two").write_bytes(bytes(2 * 1024 * 1024))
except OSError as exc:
    scratch_error = exc.errno
else:
    scratch_error = None
for _ in range(65):
    try:
        if os.fork() == 0:
            time.sleep(30)
            os._exit(0)
    except OSError as exc:
        print(json.dumps({"fork_error": exc.errno, "scratch_error": scratch_error}))
        break
"""
        result = probe(script)
        self.assertEqual(result["status"], "observed", result)
        self.assertEqual(result["output"], {"fork_error": errno.EAGAIN, "scratch_error": errno.ENOSPC})

    def test_kernel_coordinator_sigkill_closes_liveness_and_recovery_fences(self):
        work = self.submit()
        marker = f"nx-tree-{os.getpid()}-restart"
        script = f"MARKER = {marker!r}\n".encode() + (FIXTURES / "worker_tree.py").read_bytes()
        source = """
import sys
from nexus.coordinator import Coordinator, OPERATOR
from nexus.linux_worker import LinuxRunner
with Coordinator(sys.argv[1], runner=LinuxRunner(script=bytes.fromhex(sys.argv[2]), timeout=8)) as c:
    c.execute(OPERATOR, 'killed', 'run', work=int(sys.argv[3]), version=1)
"""
        process = subprocess.Popen([sys.executable, "-c", source, str(self.root), script.hex(), str(work)])
        try:
            process_markers(marker, process.kill)
            self.assertEqual(process.wait(timeout=4), -9)
        finally:
            process.kill()
            process.wait(timeout=4)
        self.ok("recover")
        self.ok("resume", work)
        with Coordinator(self.root, runner=LinuxRunner()) as reopened:
            deadline = time.monotonic() + 4
            while True:
                outcome = self.request("run", work, coordinator=reopened)
                if outcome.get("reason") != "runner_busy" or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(outcome["ok"], outcome)
        report = self.coordinator.report(OPERATOR, work)
        self.assertEqual([item["status"] for item in report["attempts"]], ["interrupted", "accepted"])
        self.assertEqual(len(report["evidence"]), 1)

    def test_crash_malformed_oversized_and_spoof_are_typed(self):
        for script, reason in (
            (b"import os; os._exit(37)", "worker_failed"),
            (b"print('{broken')", "worker_output_invalid"),
            (b'''print('{"a":1,"a":2}')''', "worker_output_invalid"),
            (b'''print('{"a":1e999}')''', "worker_output_invalid"),
            (b"print('x'*9000)", "worker_output_oversized"),
            (b'''print('{"status":"observed","approval":true}')''', "invalid_fixture_result"),
        ):
            with self.subTest(reason=reason):
                work = self.submit()
                with Coordinator(self.root, runner=LinuxRunner(script=script)) as linux:
                    result = self.request("run", work, coordinator=linux)
                self.assertEqual(result["reason"], reason, result)
                report = self.coordinator.report(OPERATOR, work)
                self.assertFalse(report["evidence"])
                self.assertFalse(report["grants"])
                self.assertEqual(report["work"]["state"], "blocked")
                self.assertEqual(report["work"]["data"]["used"], 1)
                if reason == "worker_failed":
                    observed = next(e for e in report["events"] if e["kind"] == "linux_observed")
                    self.assertEqual(observed["data"]["exit_code"], 37)

    def test_checkpoint_failure_boundaries_and_late_results(self):
        for fault in ("after_worker", "after_ack", "artifact_crash", "database_crash"):
            with self.subTest(fault=fault):
                investigation = self.submit()
                with Coordinator(self.root, runner=LinuxRunner()) as linux:
                    self.ok("run", investigation, coordinator=linux)
                work = self.ok("propose", investigation)["work"]
                binding = self.coordinator.report(OPERATOR, work)["pending_approval"]
                self.ok("decide", work, binding=binding, allow=True)
                with Coordinator(self.root, runner=LinuxRunner(checkpoint_fault=fault)) as linux:
                    with self.assertRaises(InjectedCrash):
                        self.ok("run", work, coordinator=linux)
                job = self.coordinator._job(self.coordinator.report(OPERATOR, work)["attempts"][-1]["id"])
                self.assertEqual(self.coordinator.finish(job.worker, job, {})["reason"],
                                 "linux_attempt_reserved")
                with Coordinator(self.root) as recovered:
                    report = recovered.report(OPERATOR, work)
                    self.assertEqual([item["phase"] for item in report["evidence"]], ["investigate"])
                    self.assertEqual(any(e["kind"] == "linux_observed" and e["work"] == work
                                         for e in report["events"]), fault != "after_worker")
                    self.ok("recover", coordinator=recovered)
                self.assertEqual(self.coordinator.finish(job.worker, job, {})["reason"],
                                 "stale_or_wrong_attempt")
                self.ok("resume", work)
                with Coordinator(self.root, runner=LinuxRunner()) as linux:
                    self.ok("run", work, coordinator=linux)
                report = self.coordinator.report(OPERATOR, work)
                self.assertEqual([item["phase"] for item in report["evidence"]], ["investigate", "implement"])
                self.assertEqual(report["work"]["data"]["used"], 2)

    def test_concurrent_run_and_cancellation_kill_live_namespace(self):
        work = self.submit()
        marker = f"nx-tree-{os.getpid()}-cancel"
        script = f"MARKER = {marker!r}\n".encode() + (FIXTURES / "worker_tree.py").read_bytes()

        def send():
            with Coordinator(self.root, runner=LinuxRunner(script=script, timeout=8)) as linux:
                return linux.execute(OPERATOR, "live", "run", work=work, version=1)

        def cancel():
            with Coordinator(self.root, runner=LinuxRunner()) as concurrent:
                replay = concurrent.execute(OPERATOR, "live", "run", work=work, version=1)
                self.assertEqual(replay["reason"], "operation_in_progress")
                different = concurrent.execute(OPERATOR, "other", "run", work=work,
                                               version=concurrent.report(OPERATOR, work)["work"]["version"])
                self.assertEqual(different["reason"], "runner_busy")
            self.ok("cancel", work)

        with ThreadPoolExecutor() as pool:
            launched = pool.submit(send)
            process_markers(marker, cancel)
            result = launched.result(timeout=5)
        self.assertEqual(result["reason"], "stale_or_wrong_attempt")
        report = self.coordinator.report(OPERATOR, work)
        self.assertFalse(report["evidence"])
        self.assertEqual(report["work"]["state"], "cancelled")

    def test_end_to_end_fixture_and_exact_replay(self):
        work = self.submit()
        with Coordinator(self.root, runner=LinuxRunner()) as linux:
            self.ok("run", work, coordinator=linux)
            implementation = self.ok("propose", work)["work"]
            binding = self.coordinator.report(OPERATOR, implementation)["pending_approval"]
            self.assertEqual(self.request("run", implementation, coordinator=linux)["reason"],
                             "no_active_attempt")
            self.ok("decide", implementation, binding=binding, allow=True)
            for index, phase in enumerate(("implement", "verify", "review"), 1):
                version = linux.report(OPERATOR, implementation)["work"]["version"]
                command = f"stage-{index}"
                first = self.ok("run", implementation, command=command, version=version, coordinator=linux)
                self.assertEqual(first, self.ok("run", implementation, command=command,
                                               version=version, coordinator=linux))
            report = linux.report(OPERATOR, implementation)
            self.assertEqual([e["phase"] for e in report["evidence"]],
                             ["investigate", "implement", "verify", "review"])
            self.assertEqual(report["work"]["data"]["reviewed"], report["work"]["data"]["candidate"])
            self.assertEqual(len(report["attempts"]), 4)
            self.assertFalse(report["effects"])
            self.assertEqual(self.request("run", implementation, command=command,
                                          version=version)["reason"], "command_conflict")
            self.ok("decide", implementation, binding=report["pending_approval"], allow=True)
            self.ok("publish", implementation)
            self.ok("publish", implementation)
            report = linux.report(OPERATOR, implementation)
            self.assertEqual(report["work"]["state"], "awaiting_acceptance")
            self.assertEqual([item["status"] for item in report["effects"]], ["confirmed", "confirmed"])
