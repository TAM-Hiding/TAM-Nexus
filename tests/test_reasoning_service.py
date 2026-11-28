"""Coordinator authorization and recovery tests, including an actual sandbox invocation."""

from copy import deepcopy
import subprocess
import sys
import os
from unittest.mock import patch

from nexus import reasoning, reasoning_service as service
from nexus.coordinator import Coordinator, OPERATOR, Rejected
from nexus.storage import encode
from test_handoff import HandoffTests as _Fixture
from test_linux_worker import LinuxState

# Reuse the genuine synthetic checkout/capture path, without collecting another copy of its tests.
experiment = _Fixture.experiment
del _Fixture


class ReasoningServiceTests(LinuxState):
    experiment = experiment

    def issue(self):
        self.counter += 1
        return service.start(self.coordinator, OPERATOR, "Assess the bounded fixture", [self.work], f"reason-{self.counter}")

    def setUp(self):
        super().setUp()
        self.work = self.experiment()

    def test_actual_worker_only_consumes_selected_evidence(self):
        invocation = self.issue()
        result = service.run(self.coordinator, OPERATOR, invocation)
        if result["reason"] in ("sandbox_unavailable", "sandbox_setup_failed") and os.getenv("NEXUS_REQUIRE_LINUX") != "1":
            self.skipTest(result["reason"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["classification"], "unverified_reasoning")
        self.assertIsNone(result["proposal"])
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM grants").fetchone()[0], 0)
        with self.assertRaisesRegex(Rejected, "fenced"):
            service.run(self.coordinator, OPERATOR, invocation)

    def test_budget_survives_cancel_supersession_and_restart(self):
        first, second = self.issue(), self.issue()
        with self.assertRaisesRegex(Rejected, "fenced"):
            service.run(self.coordinator, OPERATOR, first)
        service.cancel(self.coordinator, OPERATOR, second)
        third = self.issue()
        with Coordinator(self.root) as reopened:
            self.ok("recover", coordinator=reopened)
            self.assertEqual(service.report(reopened, OPERATOR, third)["reason"], "restart_fenced")
            with self.assertRaisesRegex(Rejected, "fenced"):
                service._accept(reopened, third, {})
            with self.assertRaisesRegex(Rejected, "budget_exhausted"):
                service.start(reopened, OPERATOR, "Different objective", [self.work], "retry")

    def test_issuance_is_idempotent_and_rejects_conflicts(self):
        args = (self.coordinator, OPERATOR, "Compare", [self.work], "operator-request")
        original = service.start(*args)
        self.assertEqual(service.start(*args), original)
        with self.assertRaisesRegex(Rejected, "command_conflict"):
            service.start(self.coordinator, OPERATOR, "Other", [self.work], "operator-request")
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM reasoning_charges").fetchone()[0], 1)

    def test_crash_after_launch_does_not_roll_back_running_claim(self):
        invocation = self.issue()
        script = """
import os, sys
from contextlib import contextmanager
from unittest.mock import patch
from nexus.coordinator import Coordinator, OPERATOR
from nexus import reasoning_service as service
from nexus.linux_worker import LinuxRunner
launch = LinuxRunner.launch
@contextmanager
def die(*args, **kwargs):
    with launch(*args, **kwargs):
        os._exit(88)
    yield
with Coordinator(sys.argv[1]) as c, patch.object(LinuxRunner, "launch", die):
    service.run(c, OPERATOR, int(sys.argv[2]))
"""
        crash = subprocess.run([sys.executable, "-c", script, str(self.root), str(invocation)], timeout=10)
        self.assertEqual(crash.returncode, 88)
        with Coordinator(self.root) as reopened:
            self.assertEqual(service.report(reopened, OPERATOR, invocation)["status"], "running")
            self.ok("recover", coordinator=reopened)
            self.assertEqual(service.report(reopened, OPERATOR, invocation)["reason"], "restart_fenced")
            with self.assertRaisesRegex(Rejected, "fenced"):
                service._accept(reopened, invocation, {})
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM experiment_proposals").fetchone()[0], 0)

    def test_source_acceptance_changes_digest_and_policy_fences_issued_work(self):
        invocation = self.issue()
        packet = self.coordinator._work(self.work)["data"]["handoff"]
        self.ok("accept", self.work, handoff=packet)
        with self.assertRaisesRegex(Rejected, "reasoning_input_changed"):
            service.run(self.coordinator, OPERATOR, invocation)
        current = self.issue()
        self.ok("policy")
        self.assertEqual(service.report(self.coordinator, OPERATOR, current)["reason"], "policy_changed")
        with self.assertRaisesRegex(Rejected, "stale_reasoning_policy"):
            self.issue()

    def test_inflight_cancellation_fences_even_a_well_formed_late_result(self):
        invocation = self.issue()
        result = reasoning.investigate(service._request(
            self.coordinator, service._invocation(self.coordinator, invocation)))

        def concurrent_cancel(_process, still_current):
            with Coordinator(self.root) as other:
                service.cancel(other, OPERATOR, invocation)
            self.assertFalse(still_current())
            return {"status": "observed", "output": result}

        with patch("nexus.linux_worker.LinuxRunner.wait", side_effect=concurrent_cancel):
            with self.assertRaisesRegex(Rejected, "reasoning_lease_fenced"):
                service.run(self.coordinator, OPERATOR, invocation)
        record = service.report(self.coordinator, OPERATOR, invocation)
        self.assertIsNone(record["reasoning"])
        self.assertIsNone(record["proposal"])

    def test_permissions_and_damaged_evidence_prevent_worker_disclosure(self):
        with self.assertRaisesRegex(Rejected, "operator_required"):
            service.start(self.coordinator, "worker", "Assess", [self.work], "untrusted")
        invocation = self.issue()
        with self.assertRaisesRegex(Rejected, "operator_required"):
            service.decide(self.coordinator, "worker", invocation, "0" * 64, True)
        key = self.coordinator.db.execute("SELECT artifact FROM checkpoints WHERE work=?", (self.work,)).fetchone()[0]
        (self.coordinator.artifacts.root / key).write_bytes(b"damaged")
        with patch("nexus.linux_worker.LinuxRunner.launch") as launch:
            with self.assertRaisesRegex(ValueError, "verified_handoff"):
                service.run(self.coordinator, OPERATOR, invocation)
            launch.assert_not_called()

    def test_exact_proposal_grant_and_replay_checks(self):
        # Exercise the proposal path with synthetic verified observations and an explicitly
        # available request-only profile. Production selects that profile for HERO-CAD.
        real_build = reasoning.make_input
        def selectable(*args):
            packet = deepcopy(args[2])
            for subject in packet["investigations"]:
                subject["execution_profile"] = "hero-cad-box-v1"
            return real_build(args[0], args[1], packet)

        c = self.coordinator
        def propose():
            invocation = self.issue()
            row = service._invocation(c, invocation)
            request = service._request(c, row)
            result = reasoning.investigate(request)
            result["experiment"] = {"profile": reasoning.PROFILE, "target": self.work,
                                    "purpose": "Independent measurement", "value": "Check repeatability",
                                    "evidence": request["evidence"]}
            c.db.execute("UPDATE reasoning_invocations SET status='running' WHERE id=?", (invocation,))
            service._accept(c, invocation, result)
            return invocation, service.report(c, OPERATOR, invocation)["proposal_digest"]

        with patch.object(reasoning, "make_input", side_effect=selectable):
            invocation, key = propose()
            with self.assertRaisesRegex(Rejected, "approval_required"):
                service.execute(c, OPERATOR, invocation, key)
            approved = service.decide(c, OPERATOR, invocation, key, True)
            self.assertEqual(approved["proposal_state"], "approved")
            self.assertEqual(approved["grant_status"], "current")
            with self.assertRaisesRegex(Rejected, "already_decided"):
                service.decide(c, OPERATOR, invocation, key, True)
            with self.assertRaisesRegex(Rejected, "no_compatible_execution_profile"):
                service.execute(c, OPERATOR, invocation, key)
            other, other_key = propose()
            with self.assertRaisesRegex(Rejected, "digest_mismatch"):
                service.decide(c, OPERATOR, other, key, True)
            self.assertEqual(service.decide(c, OPERATOR, other, other_key, False)["proposal_state"], "rejected")
            self.assertEqual(service.report(c, OPERATOR, invocation)["grant_status"], "current")
            revocable, revoke_key = propose()
            extra = service.decide(c, OPERATOR, revocable, revoke_key, True)
            self.ok("revoke", self.work, grant=extra["grant"])
            self.assertEqual(service.report(c, OPERATOR, revocable)["grant_status"], "revoked")
            with self.assertRaisesRegex(Rejected, "stale_experiment_approval"):
                service.execute(c, OPERATOR, revocable, revoke_key)
            self.ok("advance", ticks=10)
            with self.assertRaisesRegex(Rejected, "stale_experiment_approval"):
                service.execute(c, OPERATOR, invocation, key)
            self.assertEqual(service.report(c, OPERATOR, invocation)["grant_status"], "expired")
            altered = approved["proposal"] | {"permission": "publish"}
            c.db.execute("UPDATE experiment_proposals SET spec=? WHERE invocation=?", (encode(altered), invocation))
            with self.assertRaisesRegex(Rejected, "digest_mismatch"):
                service.execute(c, OPERATOR, invocation, key)
