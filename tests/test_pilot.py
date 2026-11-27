"""Manifest authority, staged dependency identity and workstation diagnostics."""

import base64
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import shutil
from threading import Event
import unittest
from unittest.mock import patch

from nexus import pilot, sandbox, source
from nexus.coordinator import Coordinator, OPERATOR
from nexus.linux_worker import LinuxRunner
from nexus.storage import InjectedCrash
from test_linux_worker import LinuxState, control_pipe, probe


class PilotTests(LinuxState):
    def provision(self):
        root = Path(self.temp.name)
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        pin = "a" * 40
        (checkout / ".git" / "HEAD").write_text(pin + "\n")
        files = {"src/hero_cad/__init__.py": b"", "src/hero_cad/geometry.py": b"synthetic",
                 pilot.TEST: b"synthetic test"}
        for name, value in files.items():
            target = checkout / name
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_bytes(value)
        env = root / "runtime"
        for name in ("bin/python3", "lib/python3.12/encodings/__init__.py", "packages/pytest/__init__.py"):
            path = env / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text("synthetic dependency")
            path.chmod(0o755)
        spec = {"version": 1, "repository": pilot.REPOSITORY, "revision": pin, "profile": pilot.PROFILE,
                "source_digest": source.snapshot_digest(files), "source_paths": sorted(files),
                "argv": pilot.ARGV, "limits": pilot.LIMITS,
                "dependency_digest": pilot.runtime_digest(str(env)), "evidence": ["exit", "stdout", "stderr"]}
        return checkout, env, spec

    def authorized(self):
        checkout, env, spec = self.provision()
        granted = self.ok("authorize-pilot", checkout=str(checkout), environment=str(env), manifest=spec)
        message = self.ok("message", text="synthetic pilot request")["message"]
        return checkout, env, spec, {"message": message, "authorization": granted["authorization"]}

    def test_manifest_authority_and_tampering(self):
        checkout, env, spec, request = self.authorized()
        changed = spec | {"dependency_digest": "0" * 64}
        self.assertEqual(self.request("submit-pilot", manifest=changed, **request)["reason"],
                         "pilot_not_authorized")
        for key, value in (("argv", ["sh"]), ("version", True), ("limits", pilot.LIMITS | {"wall_seconds": 999}),
                           ("source_paths", ["/etc/passwd", *spec["source_paths"]]),
                           ("source_digest", "bad")):
            with self.subTest(key=key):
                with self.assertRaises(source.SourceError):
                    pilot.manifest(spec | {key: value})
        work = self.ok("submit-pilot", manifest=spec, **request)["work"]
        self.assertEqual(self.request("run", work)["reason"], "linux_profile_required")
        self.assertNotIn(str(checkout), json.dumps(self.coordinator.report(OPERATOR, work)))
        self.assertNotIn(str(env), json.dumps(self.coordinator.report(OPERATOR, work)))
        self.ok("policy")
        self.assertEqual(self.request("submit-pilot", manifest=spec, **request)["reason"],
                         "pilot_not_authorized")

    def test_symlink_and_unapproved_source_bytes(self):
        checkout, env, spec = self.provision()
        self.assertEqual(set(pilot.checkout_files(str(checkout), spec)[1]), set(spec["source_paths"]))
        forbidden = checkout / "src" / "hero_cad" / "private.txt"
        forbidden.write_text("not in source snapshot")
        self.assertNotIn("src/hero_cad/private.txt", pilot.checkout_files(str(checkout), spec)[1])
        allowed = checkout / pilot.TEST
        allowed.unlink()
        allowed.symlink_to(forbidden)
        with self.assertRaises(source.SourceError):
            pilot.checkout_files(str(checkout), spec)
        with self.assertRaises(source.SourceError):
            pilot.checkout_files(str(checkout) + "/../checkout", spec)
        target = env / "native"
        target.symlink_to(checkout)
        with self.assertRaises(source.SourceError):
            pilot.runtime_digest(str(env))

    def test_dependency_staging_pins_bytes_and_blocks_drift(self):
        checkout, env, spec, request = self.authorized()
        work = self.ok("submit-pilot", manifest=spec, **request)["work"]
        path = env / "packages" / "pytest" / "__init__.py"
        with pilot.staged_runtime(str(env), spec["dependency_digest"], self.root) as staged:
            old = path.read_bytes()
            path.write_text("changed dependency")
            self.assertEqual((staged / path.relative_to(env)).read_bytes(), old)
        with Coordinator(self.root, runner=LinuxRunner()) as runner:
            result = self.request("run", work, coordinator=runner)
        self.assertEqual(result["reason"], "dependency_digest_mismatch")
        report = self.coordinator.report(OPERATOR, work)
        self.assertEqual(report["work"]["data"]["used"], 1)
        self.assertFalse(any(event["kind"] == "linux_launched" for event in report["events"]))

    def test_restart_refuses_source_drift_and_stale_results(self):
        checkout, env, spec, request = self.authorized()
        work = self.ok("submit-pilot", manifest=spec, **request)["work"]
        with Coordinator(self.root, runner=LinuxRunner(checkpoint_fault="before_launch")) as runner:
            with self.assertRaises(InjectedCrash):
                self.request("run", work, coordinator=runner)
        job = self.coordinator._job(1)
        with Coordinator(self.root) as reopened:
            self.ok("recover", coordinator=reopened)
            self.assertEqual(reopened.finish(job.worker, job, {})["reason"], "stale_or_wrong_attempt")
            (checkout / ".git" / "HEAD").write_text("0" * 40 + "\n")
            self.assertEqual(self.request("resume", work, coordinator=reopened)["reason"],
                             "pilot_detached_revision_required")
            self.assertEqual(reopened.report(OPERATOR, work)["work"]["data"]["used"], 1)

    def test_cancellation_during_staging_does_not_wait_for_sqlite_writer(self):
        _, _, spec, request = self.authorized()
        work = self.ok("submit-pilot", manifest=spec, **request)["work"]
        started, proceed = Event(), Event()
        real_stage = pilot.staged_runtime

        @contextmanager
        def interrupted(*args, **kwargs):
            started.set()
            if not proceed.wait(5):
                raise AssertionError("cancel could not commit while staging")
            with real_stage(*args, **kwargs) as stage:
                yield stage

        def launch():
            with Coordinator(self.root, runner=LinuxRunner()) as runner:
                return runner.execute(OPERATOR, "run", "run", work=work, version=1)

        with patch("nexus.pilot.staged_runtime", interrupted), ThreadPoolExecutor() as pool:
            pending = pool.submit(launch)
            try:
                self.assertTrue(started.wait(5))
                self.ok("cancel", work)
            finally:
                proceed.set()
            self.assertEqual(pending.result(5)["reason"], "stale_or_wrong_attempt")
        report = self.coordinator.report(OPERATOR, work)
        self.assertFalse(any(event["kind"] == "linux_launched" for event in report["events"]))

    def test_abandoned_stage_reclaimed_only_at_next_dispatch(self):
        _, _, spec, request = self.authorized()
        work = self.ok("submit-pilot", manifest=spec, **request)["work"]
        stage = pilot.stage_root(self.root)
        abandoned = stage / "pilot-12345678"
        abandoned.mkdir()
        (abandoned / "bytes").write_bytes(b"prior stage")
        with patch("nexus.linux_worker.LinuxRunner.launch", side_effect=OSError):
            with Coordinator(self.root, runner=LinuxRunner()) as runner:
                result = self.request("run", work, coordinator=runner)
        self.assertEqual(result["reason"], "supervisor_launch_failed")
        self.assertFalse(abandoned.exists())
        self.assertEqual(list(stage.iterdir()), [])

    def test_preflight_fails_closed_on_missing_sandbox(self):
        with patch("nexus.sandbox.BWRAP", "/no/bubblewrap"):
            result = pilot.preflight()
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["host"], "missing_bubblewrap")

    def test_archive_manifest_rejects_missing_commit_stamp(self):
        with self.assertRaisesRegex(source.SourceError, "revision_mismatch"):
            pilot.manifest_from_archive("a" * 40, "b" * 64, io.BytesIO(bytes(10240)))

    def test_capture_reports_evidence_limits_and_source_drift(self):
        _, _, spec, request = self.authorized()
        work = self.ok("submit-pilot", manifest=spec, **request)["work"]
        claim = self.ok("claim", work)
        job = self.coordinator._job(claim["attempt"])
        body = self.coordinator.report(OPERATOR, work)["request"]
        observed = {"status": "test_exited", "exit": 1, "elapsed_ms": 100,
                    "streams": {"stdout": "", "stderr": ""}, "source_unchanged": True}
        captured = source.capture(observed, body, job)
        self.assertEqual(captured["command"], pilot.PROFILE)
        self.assertEqual(captured["dependency_digest"], spec["dependency_digest"])
        self.assertEqual(source.capture(observed | {"source_unchanged": False}, body, job)["outcome"],
                         "source_changed_during_run")
        self.assertEqual(source.capture(observed | {"exit": 79}, body, job)["outcome"],
                         "test_infrastructure_failure")


class PilotMountTests(LinuxState):
    def test_kernel_mounts_dependencies_and_checkout_read_only(self):
        baseline = probe(b'print("{}")')
        if baseline["status"] != "observed":
            if os.environ.get("NEXUS_REQUIRE_LINUX") != "1" and baseline["status"] in (
                    "sandbox_unavailable", "sandbox_setup_failed"):
                self.skipTest(str(baseline))
            self.fail(str(baseline))
        root = Path(self.temp.name)
        runtime, tree = root / "runtime", root / "tree"
        (runtime / "bin").mkdir(parents=True)
        shutil.copy(sandbox.SYSTEM_PYTHON, runtime / "bin" / "python3")
        (runtime / "approved").write_text("dependency")
        tree.mkdir()
        (tree / "approved").write_text("fixture")
        secret = root / "personal"
        secret.write_text("private")
        script = f"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import errno, os, socket
assert Path("/source/approved").read_text() == "fixture"
for name in ("/source/approved", "/runtime/approved"):
    try:
        with open(name, "ab"):
            pass
    except OSError as exc:
        assert exc.errno == errno.EROFS
    else:
        raise AssertionError("writable mount")
assert not Path({str(secret)!r}).exists()
assert not any(Path("/proc/self/fd").glob("*"))
assert sorted(fd for fd in range(64) if os.access("/dev/fd/" + str(fd), os.F_OK)) == []
for fd in range(3, 64):
    try:
        os.fstat(fd)
    except OSError as exc:
        assert exc.errno == errno.EBADF
    else:
        raise AssertionError("unexpected inherited descriptor")
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError("network available")
Path("/scratch/writable").write_text("ok")
print("mounts verified")
"""
        fds = [os.open(path, os.O_DIRECTORY) for path in (runtime, tree)]
        try:
            with control_pipe() as control:
                result = sandbox.supervise(script.encode(), b"", 3, control, pilot=(*fds, False))
        finally:
            for fd in fds:
                os.close(fd)
        self.assertEqual(result["status"], "test_exited", result)
        self.assertEqual(result["exit"], 0, base64.b64decode(result["streams"]["stderr"]))
        self.assertIn(b"mounts verified", base64.b64decode(result["streams"]["stdout"]))


if __name__ == "__main__":
    unittest.main()
