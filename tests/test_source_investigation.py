"""NX-004 source authorization, evidence integrity and real subprocess checks."""

import base64
import errno
import json
import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor

from fixtures.provision_engineering import provision
from nexus.coordinator import Coordinator, OPERATOR, Rejected, render_report
from nexus.linux_worker import LinuxRunner
from nexus.source import capture, SourceError
from nexus.storage import EvidenceError, InjectedCrash
from test_linux_worker import LinuxState, probe, process_markers, FIXTURES


class CheckoutTests(LinuxState):
    def setup_source(self, **options):
        checkout = Path(self.temp.name) / f"source-{self.counter}"
        revisions = provision(checkout, **options)
        return checkout, revisions

    def submit_checkout(self, checkout, commit, *, budget=3):
        authorization = self.ok("authorize-source", checkout=str(checkout), commit=commit)
        message = self.ok("message", text="private-investigation-transcript")["message"]
        work = self.ok("submit-source", message=message, authorization=authorization["authorization"],
                       checkout=str(checkout), commit=commit, budget=budget)["work"]
        return work, authorization

    def git_checkout(self, checkout, revision):
        subprocess.run(["/usr/bin/git", "checkout", "--detach", revision], cwd=checkout,
                       check=True, capture_output=True)

    def test_bound_allowlist_revision_and_identity(self):
        checkout, refs = self.setup_source()
        authorization = self.ok("authorize-source", checkout=str(checkout), commit=refs["defect"])
        message = self.ok("message", text="review")["message"]
        other, _ = self.setup_source()
        for path, commit in ((other, refs["defect"]), (checkout, refs["control"])):
            result = self.request("submit-source", message=message, authorization=authorization["authorization"],
                                  checkout=str(path), commit=commit)
            self.assertEqual(result["reason"], "source_not_authorized")
        checkout.rename(other.parent / "original")
        other.rename(checkout)
        displaced = self.request("submit-source", message=message, authorization=authorization["authorization"],
                                 checkout=str(checkout), commit=refs["defect"])
        self.assertEqual(displaced["reason"], "checkout_identity_changed")

    def test_symlinks_traversal_and_worktree_drift(self):
        checkout, refs = self.setup_source()
        alias = checkout.parent / "alias"
        alias.symlink_to(checkout, target_is_directory=True)
        for path in (str(alias), f"{checkout}/../{checkout.name}"):
            self.assertFalse(self.request("authorize-source", checkout=path, commit=refs["defect"])["ok"])
        model = checkout / "model.py"
        original = model.read_bytes()
        model.write_text("changed")
        result = self.request("authorize-source", checkout=str(checkout), commit=refs["defect"])
        self.assertEqual(result["reason"], "source_worktree_drift")
        outside = checkout.parent / "secret"
        outside.write_bytes(original)
        model.unlink()
        model.symlink_to(outside)
        result = self.request("authorize-source", checkout=str(checkout), commit=refs["defect"])
        self.assertEqual(result["reason"], "unsafe_or_oversized_source")

    def test_dependency_declared_unavailable_blocks_without_launch(self):
        checkout, refs = self.setup_source(
            manifest='{"fixture":"nx-004","dependency":"unavailable-fixture"}\n')
        work, _ = self.submit_checkout(checkout, refs["defect"])
        with Coordinator(self.root, runner=LinuxRunner()) as runner:
            outcome = self.request("run", work, coordinator=runner)
        self.assertEqual(outcome["reason"], "dependency_unavailable")
        report = self.coordinator.report(OPERATOR, work)
        self.assertEqual(report["work"]["state"], "blocked")
        self.assertFalse(any(e["kind"] == "linux_launched" for e in report["events"]))

    def test_source_drift_prevents_dispatch_and_resumption(self):
        checkout, refs = self.setup_source()
        work, _ = self.submit_checkout(checkout, refs["defect"])
        self.assertEqual(self.request("run", work)["reason"], "linux_profile_required")
        with Coordinator(self.root, runner=LinuxRunner(checkpoint_fault="before_launch")) as runner:
            with self.assertRaises(InjectedCrash):
                self.request("run", work, coordinator=runner)
        self.git_checkout(checkout, refs["control"])
        self.ok("recover")
        self.assertEqual(self.request("resume", work)["reason"], "source_revision_drift")
        self.git_checkout(checkout, refs["defect"])
        self.ok("resume", work)
        self.git_checkout(checkout, refs["control"])
        with Coordinator(self.root, runner=LinuxRunner()) as runner:
            result = self.request("run", work, coordinator=runner)
        self.assertEqual(result["reason"], "source_revision_drift")
        report = self.coordinator.report(OPERATOR, work)
        self.assertEqual(report["work"]["data"]["used"], 2)
        self.assertFalse(report["evidence"])

    def test_malformed_capture_rejected(self):
        for observation in ({}, {"status": "test_exited", "exit": True, "elapsed_ms": 1},
                            {"status": "test_exited", "exit": 0, "elapsed_ms": 1,
                             "streams": {"stdout": "not base64", "stderr": ""}}):
            with self.assertRaises(SourceError):
                capture(observation, {}, None)


class SourceKernelTests(LinuxState):
    setup_source = CheckoutTests.setup_source
    submit_checkout = CheckoutTests.submit_checkout
    git_checkout = CheckoutTests.git_checkout

    @classmethod
    def setUpClass(cls):
        result = probe(b'print("{}")')
        if result["status"] == "observed":
            return
        if os.environ.get("NEXUS_REQUIRE_LINUX") != "1" and result["status"] in (
                "sandbox_setup_failed", "sandbox_unavailable"):
            import unittest
            raise unittest.SkipTest(str(result))
        raise AssertionError(result)

    def run_source(self, work, **options):
        with Coordinator(self.root, runner=LinuxRunner(**options)) as runner:
            return self.request("run", work, coordinator=runner)

    def test_real_failure_and_passing_control_preserve_source(self):
        checkout, refs = self.setup_source()
        for label, result, exit_code in (("defect", "command_exit_one", 1),
                                         ("control", "command_passed", 0)):
            with self.subTest(label=label):
                self.git_checkout(checkout, refs[label])
                work, authorization = self.submit_checkout(checkout, refs[label])
                before = {p.relative_to(checkout): p.read_bytes()
                          for p in checkout.rglob("*") if p.is_file()}
                with Coordinator(self.root, runner=LinuxRunner()) as runner:
                    first = self.ok("run", work, version=1, command=label, coordinator=runner)
                    self.assertEqual(first, self.ok("run", work, version=1, command=label,
                                                    coordinator=runner))
                after = {p.relative_to(checkout): p.read_bytes()
                         for p in checkout.rglob("*") if p.is_file()}
                self.assertEqual(before, after)
                report = self.coordinator.report(OPERATOR, work)
                self.assertEqual(report["work"]["state"], "awaiting_acceptance")
                item, = report["evidence"]
                proof = item["result"]
                self.assertEqual(proof["outcome"], result)
                self.assertEqual(proof["exit_status"], exit_code)
                self.assertEqual(proof["snapshot"], authorization["snapshot"])
                self.assertEqual(proof["base"], refs[label])
                private = self.coordinator.source_evidence(OPERATOR, work, item["digest"])
                stderr = base64.b64decode(private["streams"]["stderr"])
                self.assertIn(b"200 != 2000" if label == "defect" else b"OK", stderr)
                self.assertNotIn("streams", proof)
                self.assertNotIn(str(checkout), json.dumps(report))
                self.assertNotIn("private-investigation-transcript", render_report(report))
                self.assertEqual(self.request("propose", work)["reason"], "source_readonly")
                self.assertEqual(self.request("archive", work, reference="example",
                                              revision="r1")["reason"], "source_readonly")
                with self.assertRaises(Rejected):
                    self.coordinator.source_evidence("worker", work, item["digest"])

    def test_untrusted_source_mutations_and_network_are_denied(self):
        credential = Path(self.temp.name) / "PRIVATE-CREDENTIAL"
        credential.write_text("private-bytes")
        script = f"""
import errno
import os
from pathlib import Path
import socket
import unittest
try:
    Path("/source/model.py").write_text("changed")
except OSError as exc:
    assert exc.errno == {errno.EROFS}
else:
    raise AssertionError("source writable")
assert not Path({str(credential)!r}).exists()
assert "SSH_AUTH_SOCK" not in os.environ
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise AssertionError("socket available")
Path("/scratch/allowed").write_text("data")
print("PRIVATE-UNTRUSTED-MARKER")


class SandboxProbe(unittest.TestCase):
    def test_fixture_ran(self):
        self.assertTrue(Path("/scratch/allowed").is_file())
"""
        checkout, refs = self.setup_source(test=script)
        work, _ = self.submit_checkout(checkout, refs["defect"])
        self.assertTrue(self.run_source(work)["ok"])
        report = self.coordinator.report(OPERATOR, work)
        item, = report["evidence"]
        self.assertEqual(item["result"]["exit_status"], 0)
        self.assertNotIn("PRIVATE-UNTRUSTED-MARKER", json.dumps(report))
        raw = self.coordinator.source_evidence(OPERATOR, work, item["digest"])
        self.assertIn(b"PRIVATE-UNTRUSTED-MARKER", base64.b64decode(raw["streams"]["stdout"]))

    def test_import_error_blocks_with_unverified_dependency_hint(self):
        checkout, refs = self.setup_source(test="import fixture_package_not_available\n")
        work, _ = self.submit_checkout(checkout, refs["defect"])
        self.assertEqual(self.run_source(work)["reason"], "dependency_reported_missing")
        report = self.coordinator.report(OPERATOR, work)
        self.assertFalse(report["evidence"])
        self.assertEqual(report["partial"][0]["result"]["exit_status"], 1)
        self.assertIn("unverified", report["partial"][0]["result"]["limitations"][-1])

    def test_coordinator_sigkill_abandons_source_run_and_restarts(self):
        marker = f"nx004-kill-{os.getpid()}"
        code = f"MARKER = {marker!r}\n" + (FIXTURES / "worker_tree.py").read_text()
        checkout, refs = self.setup_source(test=code)
        work, _ = self.submit_checkout(checkout, refs["defect"])
        process = subprocess.Popen([
            os.sys.executable, "-m", "nexus", "--state", str(self.root), "--runner", "linux",
            "command", "run", "--id", "crashed", "--work", str(work), "--version", "1",
        ], stdout=subprocess.DEVNULL)
        try:
            process_markers(marker, process.kill)
            self.assertEqual(process.wait(timeout=4), -9)
        finally:
            process.kill()
            process.wait(timeout=4)
        with Coordinator(self.root) as restarted:
            self.ok("recover", coordinator=restarted)
            report = restarted.report(OPERATOR, work)
            self.assertEqual(report["attempts"][0]["status"], "interrupted")
            self.assertFalse(report["evidence"])
            self.assertFalse(report["partial"])
            self.assertEqual(report["work"]["data"]["used"], 1)
        self.git_checkout(checkout, refs["control"])
        self.assertEqual(self.request("resume", work)["reason"], "source_revision_drift")

    def test_output_cap_retains_bounded_partial_capture(self):
        checkout, refs = self.setup_source(test='print("output" * 3000)\n')
        work, _ = self.submit_checkout(checkout, refs["defect"])
        result = self.run_source(work)
        self.assertEqual(result["reason"], "worker_output_oversized")
        report = self.coordinator.report(OPERATOR, work)
        self.assertFalse(report["evidence"])
        partial, = report["partial"]
        self.assertFalse(partial["accepted"])
        self.assertEqual(sum(v["bytes"] for v in partial["result"]["observations"].values()), 8192)
        self.assertNotIn("streams", partial["result"])

    def test_process_tree_timeout_and_concurrent_dispatch(self):
        marker = f"nx004-tree-{os.getpid()}"
        code = f"MARKER = {marker!r}\n" + (FIXTURES / "worker_tree.py").read_text()
        checkout, refs = self.setup_source(test=code)
        work, _ = self.submit_checkout(checkout, refs["defect"])

        def competing_command():
            with Coordinator(self.root, runner=LinuxRunner()) as runner:
                duplicate = runner.execute(OPERATOR, "first-run", "run", work=work, version=1)
                self.assertEqual(duplicate["reason"], "operation_in_progress")

        def run():
            with Coordinator(self.root, runner=LinuxRunner(timeout=3)) as runner:
                return runner.execute(OPERATOR, "first-run", "run", work=work, version=1)

        with ThreadPoolExecutor() as pool:
            result = pool.submit(run)
            process_markers(marker, competing_command)
            self.assertEqual(result.result(timeout=5)["reason"], "worker_timeout")
        self.assertEqual(self.coordinator.report(OPERATOR, work)["partial"][0]["result"]["status"],
                         "worker_timeout")

    def test_post_ack_restart_fences_old_attempt_and_detects_damage(self):
        checkout, refs = self.setup_source()
        work, _ = self.submit_checkout(checkout, refs["defect"])
        with self.assertRaises(InjectedCrash):
            self.run_source(work, checkpoint_fault="after_ack")
        with Coordinator(self.root) as reopened:
            report = reopened.report(OPERATOR, work)
            prior = report["partial"][0]["digest"]
            job = reopened._job(report["attempts"][-1]["id"])
            self.ok("recover", coordinator=reopened)
            self.assertEqual(reopened.finish(job.worker, job, {})["reason"], "stale_or_wrong_attempt")
        self.ok("resume", work)
        self.assertTrue(self.run_source(work)["ok"])
        report = self.coordinator.report(OPERATOR, work)
        accepted = report["evidence"][0]["digest"]
        self.assertNotEqual(accepted, prior)
        self.assertEqual(report["work"]["data"]["used"], 2)
        handoff = report["work"]["data"]["handoff"]
        artifact = self.coordinator.artifacts.root / accepted
        original = artifact.read_bytes()
        artifact.write_bytes(b"corrupt")
        with self.assertRaises(EvidenceError):
            self.coordinator.source_evidence(OPERATOR, work, accepted)
        self.assertEqual(self.request("accept", work, handoff=handoff)["reason"], "evidence_unavailable")
        artifact.write_bytes(original)
        self.ok("resume", work)
        self.ok("accept", work, handoff=handoff)
        with Coordinator(self.root) as reopened:
            report = reopened.report(OPERATOR, work)
            self.assertEqual(report["work"]["state"], "completed")
            self.assertEqual(len(report["evidence"]), 1)
