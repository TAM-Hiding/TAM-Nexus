"""Engineering handoffs only promote current, digest-verified local observations."""

import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from fixtures.provision_engineering import provision
from nexus import handoff, pilot
from nexus.coordinator import Coordinator, OPERATOR, Rejected
from nexus.linux_worker import LinuxRunner
from nexus.storage import InjectedCrash
from test_linux_worker import LinuxState


class ObservedFixture(LinuxRunner):
    """Drive the existing coordinator capture path without requiring another kernel run."""

    @contextmanager
    def launch(self, fd, job, **kwargs):
        yield object()

    def wait(self, process, current):
        assert current()
        return {"status": "test_exited", "exit": 1, "elapsed_ms": 10,
                "streams": {"stdout": "U1lOVEhFVElDX1BSSVZBVEVfT1VUUFVU", "stderr": ""}}


class HandoffTests(LinuxState):
    def experiment(self, *, run=True, fault=None):
        checkout = Path(self.temp.name) / f"checkout-{self.counter}"
        commit = provision(checkout)["defect"]
        args = {"checkout": str(checkout), "commit": commit}
        authorization = self.ok("authorize-source", **args)["authorization"]
        note = self.ok("message", text="private objective")["message"]
        work = self.ok("submit-source", **args, message=note, authorization=authorization)["work"]
        if run:
            with Coordinator(self.root, runner=ObservedFixture(checkpoint_fault=fault)) as runner:
                if fault:
                    with self.assertRaises(InjectedCrash):
                        self.ok("run", work, coordinator=runner)
                else:
                    self.ok("run", work, coordinator=runner)
        return work

    def packet(self, *work, notes=None):
        return self.coordinator.engineering_handoff(OPERATOR, list(work), notes)

    def test_serialization_separates_observations_and_operator_notes(self):
        work = self.experiment()
        observation, = self.packet(work)["observations"]
        reference = observation["reference"]
        notes = {"hypotheses": [{
            "explanation": "Possible unit mismatch", "uncertainty": "No cross-unit control",
            "verification": "Check a separately approved control", "supporting": [reference],
            "contradictory": [],
        }], "recommendations": [{
            "action": "request_experiment", "proposal": "Compare a control", "value": "Localize the issue",
            "risks": "Scope limited to one fixture", "dependencies": "Matching runtime",
            "permissions": ["approve_read_only_experiment"], "evidence": [reference],
        }]}
        packet = self.packet(work, notes=notes)
        self.assertEqual(packet, json.loads(json.dumps(packet)))
        self.assertEqual(packet["version"], 1)
        self.assertEqual(packet["authority"], "informational_only")
        self.assertEqual(packet["notes_provenance"], "operator_supplied_unverified")
        self.assertEqual(packet["observations"], [observation])
        self.assertEqual(packet["recommendations"][0]["authorization"], "not_granted")
        self.assertNotIn("private objective", handoff.render(packet))
        self.assertNotIn("streams", observation)
        self.assertIn("Hypothesis · unverified", handoff.render(packet))
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM grants").fetchone()[0], 0)
        self.assertEqual(self.request("propose", work)["reason"], "source_readonly")
        command = [sys.executable, "-m", "nexus", "--state", str(self.root), "handoff", str(work), "--json"]
        output = subprocess.run(command, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(output.stdout), self.packet(work))

    def test_read_principal_and_unsupported_follow_up_authority(self):
        work = self.experiment()
        with self.assertRaises(Rejected):
            self.coordinator.engineering_handoff("worker", [work])
        recommendation = {
            "action": "publish", "proposal": "Pub", "value": "None", "risks": "Leaks data",
            "dependencies": "Network", "permissions": ["publish"], "evidence": [],
        }
        with self.assertRaisesRegex(handoff.HandoffError, "unsupported_follow_up_authority"):
            self.packet(work, notes={"hypotheses": [], "recommendations": [recommendation]})
        recommendation["action"] = "request_experiment"
        with self.assertRaisesRegex(handoff.HandoffError, "unsupported_follow_up_authority"):
            self.packet(work, notes={"hypotheses": [], "recommendations": [recommendation]})
        with self.assertRaisesRegex(handoff.HandoffError, "invalid_handoff_notes"):
            self.packet(work, notes={"observations": [], "hypotheses": [], "recommendations": []})

    def test_corrupt_missing_and_foreign_checkpoint_bytes_are_never_promoted(self):
        first, second = self.experiment(), self.experiment()
        reference = self.packet(first)["observations"][0]["reference"]
        other = self.packet(second)["observations"][0]["reference"]
        notes = {"hypotheses": [{"explanation": "Guess", "uncertainty": "Unknown", "verification": "Repeat",
                                 "supporting": [other], "contradictory": []}], "recommendations": []}
        with self.assertRaisesRegex(handoff.HandoffError, "reference_not_verified"):
            self.packet(first, notes=notes)
        record = self.packet(first, second, notes=notes)
        self.assertEqual(len(record["observations"]), 2)
        path = self.coordinator.artifacts.root / reference["sha256"]
        path.write_bytes(b"corrupted")
        self.assertEqual(self.packet(first)["evidence_issues"][0]["reason"], "evidence_corrupt")
        path.unlink()
        self.assertEqual(self.packet(first)["evidence_issues"][0]["reason"], "evidence_missing_or_unsafe")
        self.coordinator.db.execute("UPDATE checkpoints SET artifact=? WHERE work=?", (other["sha256"], first))
        self.assertFalse(self.packet(first)["observations"])

    def test_post_ack_restart_marks_attempt_unaccepted_and_keeps_retry_evidence(self):
        work = self.experiment(fault="after_ack")
        packet = self.packet(work)
        self.assertFalse(packet["observations"])
        self.assertEqual(packet["evidence_issues"][0]["reason"], "unaccepted_capture")
        self.ok("recover")
        self.ok("resume", work)
        with Coordinator(self.root, runner=ObservedFixture()) as runner:
            self.ok("run", work, coordinator=runner)
        packet = self.packet(work)
        self.assertEqual(len(packet["observations"]), 1)
        self.assertNotEqual(packet["observations"][0]["reference"], packet["evidence_issues"][0]["reference"])
        self.assertEqual(packet["evidence_issues"][0]["reason"], "unaccepted_capture")

    def test_superseded_generation_and_cancelled_work_cannot_be_promoted(self):
        work = self.experiment()
        self.coordinator.db.execute("UPDATE attempts SET generation=generation+1 WHERE work=?", (work,))
        self.assertEqual(self.packet(work)["evidence_issues"][0]["reason"], "evidence_not_current_checkpoint")
        self.coordinator.db.execute("UPDATE attempts SET generation=generation-1 WHERE work=?", (work,))
        self.ok("cancel", work)
        self.assertFalse(self.packet(work)["observations"])

    def test_older_nx005_capture_remains_readable_without_host_metadata(self):
        with patch("nexus.coordinator.platform.release", return_value="6.1.0"):
            work = self.experiment()
        self.assertEqual(self.packet(work)["observations"][0]["host_platform"]["kernel"], "6.1.0")
        reference = self.packet(work)["observations"][0]["reference"]
        legacy = self.coordinator.artifacts.get(reference["sha256"])
        legacy.pop("host_platform")
        prior = self.coordinator.artifacts.put(legacy)
        self.coordinator.db.execute("UPDATE checkpoints SET artifact=? WHERE work=?", (prior, work))
        self.coordinator.db.execute(
            "UPDATE events SET data=replace(data,?,?) WHERE work=? AND kind IN ('source_observed','checkpoint')",
            (reference["sha256"], prior, work))
        state = self.coordinator._work(work)
        old = state["data"]["handoff"]
        prior_packet = self.coordinator.artifacts.get(old)
        prior_packet["evidence"] = [prior]
        prior_packet["checkpoints"][0]["artifact"] = prior
        new = self.coordinator.artifacts.put(prior_packet)
        self.coordinator.db.execute("UPDATE work SET data=replace(data,?,?) WHERE id=?", (old, new, work))
        self.coordinator.db.execute(
            "UPDATE events SET data=replace(data,?,?) WHERE work=? AND kind='handoff_ready'", (old, new, work))
        self.assertIsNone(self.packet(work)["observations"][0]["host_platform"])

    def test_acceptance_packet_and_operator_decision_are_verified(self):
        work = self.experiment()
        key = self.packet(work)["investigations"][0]["acceptance_packet_sha256"]
        self.ok("accept", work, handoff=key)
        self.assertEqual(len(self.packet(work)["observations"]), 1)
        self.coordinator.db.execute("DELETE FROM events WHERE work=? AND kind='handoff_decision'", (work,))
        record = self.packet(work)
        self.assertFalse(record["observations"])
        self.assertEqual(record["evidence_issues"][0]["reason"], "acceptance_packet_event_mismatch")
        path = self.coordinator.artifacts.root / key
        path.write_bytes(b"bad packet")
        self.assertEqual(self.packet(work)["evidence_issues"][0]["reason"], "acceptance_packet_corrupt")
        path.unlink()
        self.assertEqual(self.packet(work)["evidence_issues"][0]["reason"], "acceptance_packet_missing_or_unsafe")

    def test_export_does_not_create_or_migrate_state(self):
        uninitialized = Path(self.temp.name) / "uninitialized"
        command = [sys.executable, "-m", "nexus", "--state", str(uninitialized), "handoff", "1"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(uninitialized.exists())
        version = self.coordinator.db.execute("PRAGMA user_version").fetchone()[0]
        self.coordinator.db.execute("PRAGMA user_version=1")
        with self.assertRaisesRegex(ValueError, "upgrade this state"):
            Coordinator(self.root, readonly=True)
        self.assertEqual(self.coordinator.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.coordinator.db.execute(f"PRAGMA user_version={version}")

    def test_preflight_reports_unsupported_capabilities(self):
        with patch("nexus.pilot.sys.version_info", (3, 10, 0)):
            result = pilot.preflight()
        self.assertFalse(result["ok"])
        self.assertIn("3.11", result["hint"])
        with patch("nexus.sandbox.subprocess.run") as run:
            run.return_value.stdout = b"--ro-bind-data"
            result = pilot.preflight()
        self.assertFalse(result["ok"])
        self.assertIn("--ro-bind-fd", result["checks"]["bubblewrap"]["missing_flags"])
