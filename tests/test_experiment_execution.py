"""One claim survives lost acknowledgments, interruption, drift and revocation."""

import base64
from contextlib import nullcontext
import json
from unittest.mock import patch

from nexus import experiment_execution, measurement, reasoning, reasoning_service as service
from nexus.coordinator import Coordinator, OPERATOR, Rejected
from nexus.linux_worker import LinuxRunner
from nexus.storage import InjectedCrash, digest, encode
from test_linux_worker import LinuxState
from test_pilot import PilotTests as _Fixtures

provision, authorized = _Fixtures.provision, _Fixtures.authorized
del _Fixtures


def observed(value):
    data = base64.b64encode(json.dumps(value).encode()).decode()
    return {"status": "test_exited", "exit": 0, "elapsed_ms": 3, "streams": {"stderr": "", "stdout": data}}


class ExperimentTests(LinuxState):
    provision = provision
    authorized = authorized

    def setUp(self):
        super().setUp()
        self.checkout, self.runtime, manifest, args = self.authorized()
        self.work = self.ok("submit-pilot", manifest=manifest, **args)["work"]
        with Coordinator(self.root, runner=LinuxRunner()) as runner, patch.object(
                LinuxRunner, "launch", return_value=nullcontext(None)), patch.object(
                LinuxRunner, "wait", return_value=observed({})):
            self.ok("run", self.work, coordinator=runner)
        self.c = self.coordinator
        self.inv = service.start(self.c, OPERATOR, "Measure the fixture box", [self.work], "fixture-reason")
        request = service._request(self.c, service._invocation(self.c, self.inv))
        result = reasoning.investigate(request)
        result["experiment"] = {"profile": reasoning.PROFILE, "target": self.work, "purpose": "Compare volume",
                                "value": "Check geometry", "evidence": request["evidence"]}
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)), patch.object(
                LinuxRunner, "wait", return_value={"status": "observed", "output": result}):
            self.key = service.run(self.c, OPERATOR, self.inv)["proposal_digest"]

    def approve(self):
        return service.decide(self.c, OPERATOR, self.inv, self.key, True)["grant"]

    def execute(self):
        return service.execute(self.c, OPERATOR, self.inv, self.key)["execution"]

    def test_exact_authority_and_legacy_proposal(self):
        with self.assertRaisesRegex(Rejected, "approval_required"):
            self.execute()
        self.approve()
        with self.assertRaisesRegex(Rejected, "operator_required"):
            experiment_execution.dispatch(self.c, object(), self.inv, self.key)
        with self.assertRaisesRegex(Rejected, "digest_mismatch"):
            service.execute(self.c, OPERATOR, self.inv, "0" * 64)
        with patch("nexus.measurement.script_digest", return_value="0" * 64):
            with self.assertRaisesRegex(Rejected, "no_compatible_execution_profile"):
                self.execute()
        self.assertIsNone(experiment_execution.execution_report(self.c, self.inv))
        # Represent a genuinely old approved, informational-only NX-007 grant.
        p = service.report(self.c, OPERATOR, self.inv)
        old = dict(p["proposal"])
        del old["execution_plan"]
        self.key = digest(old)
        self.c.db.execute("UPDATE experiment_proposals SET spec=?,digest=?", (encode(old), self.key))
        self.c.db.execute("UPDATE grants SET binding=? WHERE id=?", (encode(old), p["grant"]))
        with self.assertRaisesRegex(Rejected, "no_compatible_execution_profile"):
            self.execute()

    def test_revoked_and_expired_grants_never_launch(self):
        self.ok("revoke", self.work, grant=self.approve())
        with patch.object(LinuxRunner, "launch") as launch, self.assertRaisesRegex(Rejected, "stale_experiment"):
            self.execute()
        launch.assert_not_called()

    def test_restored_backup_cannot_reuse_an_older_grant(self):
        self.approve()
        self.ok("recover", restore=True)
        with self.assertRaisesRegex(Rejected, "stale_experiment_approval"):
            self.execute()
        self.assertEqual(service.report(self.c, OPERATOR, self.inv)["grant_status"], "revoked")

    def test_expiry(self):
        self.approve()
        self.ok("advance", ticks=10)
        with self.assertRaisesRegex(Rejected, "stale_experiment"):
            self.execute()
        self.assertIsNone(experiment_execution.execution_report(self.c, self.inv))

    def test_dependency_drift_consumes_claim_without_launch(self):
        self.approve()
        (self.runtime / "bin/python3").write_text("changed")
        with patch.object(LinuxRunner, "launch") as launch:
            initial = self.execute()
            self.assertEqual(initial["reason"], "dependency_digest_mismatch")
            self.assertEqual(self.execute(), initial)
            launch.assert_not_called()

    def test_source_drift_consumes_claim_without_launch(self):
        self.approve()
        (self.checkout / ".git/HEAD").write_text("b" * 40 + "\n")
        with patch.object(LinuxRunner, "launch") as launch:
            self.assertEqual(self.execute()["status"], "failed")
            launch.assert_not_called()

    def test_accepted_replay_is_verified_without_dispatch_or_new_reasoning(self):
        self.approve()
        before = self.c.engineering_handoff(OPERATOR, [self.work])
        raw = observed({"version": 1, "status": "measured", "mesh_mm3": measurement.EXPECTED_MM3,
                        "reported_mm3": measurement.EXPECTED_MM3})
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)) as launch, patch.object(
                LinuxRunner, "wait", return_value=raw):
            original = self.execute()
            self.assertEqual(original["status"], "accepted", original)
            with Coordinator(self.root) as another:
                replay = service.execute(another, OPERATOR, self.inv, self.key)
                self.assertEqual(replay["execution"], original)
            launch.assert_called_once()
            self.assertEqual(self.c._row("SELECT count(*) FROM reasoning_invocations")[0], 1)
            self.assertEqual(self.c._row("SELECT count(*) FROM experiment_runs")[0], 1)
            (self.c.artifacts.root / original["artifact"]).unlink()
            self.assertEqual(self.execute()["status"], "evidence_invalid")
        damaged = self.c.engineering_handoff(OPERATOR, [self.work])
        self.assertTrue(damaged["evidence_issues"])
        self.assertEqual(damaged["observations"], before["observations"])

    def test_concurrent_dispatch_observes_the_same_durable_claim(self):
        self.approve()

        def observe(_process, active, **kwargs):
            self.assertTrue(active())
            with Coordinator(self.root) as another:
                other = service.execute(another, OPERATOR, self.inv, self.key)["execution"]
                self.assertEqual(other["status"], "launched")
            return observed({"version": 1, "status": "measurement_unavailable"})

        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)) as launch, patch.object(
                LinuxRunner, "wait", side_effect=observe):
            final = self.execute()
            self.assertEqual(final["status"], "accepted")
            self.assertEqual(final["evidence"]["worker_result"]["outcome"], "measurement_unavailable")
            launch.assert_called_once()

    def test_loss_after_launch_is_fenced_and_never_retried(self):
        self.approve()
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)) as launch, patch.object(
                LinuxRunner, "wait", side_effect=InjectedCrash):
            with self.assertRaises(InjectedCrash):
                self.execute()
            with Coordinator(self.root) as reopened:
                self.ok("recover", coordinator=reopened)
                replay = service.execute(reopened, OPERATOR, self.inv, self.key)["execution"]
                self.assertEqual(replay["status"], "uncertain")
                self.assertIsNone(replay["artifact"])
            launch.assert_called_once()

    def test_revocation_while_running_fences_result(self):
        grant = self.approve()
        def revoke(_process, active, **kwargs):
            with Coordinator(self.root) as reopened:
                self.ok("revoke", self.work, coordinator=reopened, grant=grant)
            self.assertFalse(active())
            return observed({})
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)), patch.object(
                LinuxRunner, "wait", side_effect=revoke):
            self.assertEqual(self.execute()["status"], "failed")
            self.assertIsNone(self.execute()["artifact"])

    def test_lost_supervisor_response_preserves_uncertainty(self):
        self.approve()
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)), patch.object(
                LinuxRunner, "wait", return_value={"status": "supervisor_failed"}):
            self.assertEqual(self.execute()["status"], "uncertain")

    def accepted_followup(self):
        grant = self.approve()
        raw = observed({"version": 1, "status": "measurement_unavailable"})
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)), patch.object(
                LinuxRunner, "wait", return_value=raw):
            run = self.execute()
        self.assertEqual(run["status"], "accepted", run)
        return run, grant

    def test_historical_capture_survives_grant_and_parent_state_changes(self):
        run, grant = self.accepted_followup()
        self.ok("advance", ticks=10)
        self.ok("revoke", self.work, grant=grant)
        self.ok("accept", self.work, handoff=self.c._work(self.work)["data"]["handoff"])
        packet = self.c.engineering_handoff(OPERATOR, [self.work])
        self.assertEqual(packet["evidence_issues"], [])
        item, = packet["followup_observations"]
        self.assertEqual(item["reference"]["sha256"], run["artifact"])
        self.assertEqual(item["worker_result"]["outcome"], "measurement_unavailable")
        self.assertEqual(item["worker_result"]["provenance"], "worker_report_unverified")
        self.assertEqual(item["process"]["provenance"], "supervisor_process_observation")
        self.assertNotIn("streams", item["process"])
        self.assertEqual(len(packet["observations"]), 1)

    def test_foreign_capture_and_missing_acceptance_never_export(self):
        run, _ = self.accepted_followup()
        parent = self.c.engineering_handoff(OPERATOR, [self.work])["observations"][0]["reference"]["sha256"]
        self.c.db.execute("UPDATE experiment_runs SET artifact=? WHERE id=?", (parent, run["attempt"]))
        packet = self.c.engineering_handoff(OPERATOR, [self.work])
        self.assertNotIn("followup_observations", packet)
        self.assertEqual(packet["evidence_issues"][0]["reason"], "experiment_acceptance_mismatch")
        self.c.db.execute("DELETE FROM experiment_runs WHERE id=?", (run["attempt"],))
        packet = self.c.engineering_handoff(OPERATOR, [self.work])
        self.assertEqual(packet["evidence_issues"][0]["reason"], "experiment_acceptance_without_run")

    def test_corrupted_parent_invalidates_derived_evidence(self):
        run, _ = self.accepted_followup()
        packet = self.c.engineering_handoff(OPERATOR, [self.work])
        key = packet["observations"][0]["reference"]["sha256"]
        (self.c.artifacts.root / key).write_bytes(b"altered")
        packet = self.c.engineering_handoff(OPERATOR, [self.work])
        self.assertNotIn("followup_observations", packet)
        reasons = {issue["reason"] for issue in packet["evidence_issues"]}
        self.assertIn("experiment_parent_evidence_invalid", reasons)
        self.assertIn("evidence_corrupt", reasons)
        self.assertTrue((self.c.artifacts.root / run["artifact"]).is_file())
