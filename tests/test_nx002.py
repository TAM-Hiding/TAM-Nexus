"""NX-001 acceptance cases and focused NX-002 boundary regressions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from nexus.coordinator import OPERATOR, Coordinator, Rejected, render_report
from nexus.fixtures import BASE, RESOURCE
from nexus.storage import EvidenceError, InjectedCrash, transaction


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.coordinator = Coordinator(self.state)
        self.addCleanup(lambda: self.coordinator.__exit__())
        self.command_count = 0

    def reload(self):
        self.coordinator.__exit__()
        self.coordinator = Coordinator(self.state)

    def report(self, work):
        return self.coordinator.report(OPERATOR, work)

    def cmd(self, action, work=None, *, version=None, principal=OPERATOR, command=None, **args):
        self.command_count += 1
        if work is not None and version is None:
            version = self.report(work)["work"]["version"]
        return self.coordinator.execute(principal, command or f"cmd-{self.command_count}",
                                        action, work=work, version=version, **args)

    def ok(self, action, work=None, **args):
        result = self.cmd(action, work, **args)
        self.assertTrue(result["ok"], result)
        return result

    def denied(self, reason, action, work=None, **args):
        result = self.cmd(action, work, **args)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], reason)
        return result

    def investigate(self, *, run=True, text="X/Y fails, X/Y/Z passes", **args):
        message = self.ok("message", text=text)["message"]
        work = self.ok("submit", message=message, **args)["work"]
        if run:
            self.ok("run", work)
        return work

    def propose(self, **args):
        return self.ok("propose", self.investigate(), **args)["work"]

    def grant(self, work, *, allow=True, ttl=10):
        binding = self.report(work)["pending_approval"]
        self.assertIsNotNone(binding)
        return self.ok("decide", work, binding=binding, allow=allow, ttl=ttl)

    def reviewed(self, **args):
        work = self.propose(**args)
        self.grant(work)
        for _ in range(3):
            self.ok("run", work)
        self.assertEqual(self.report(work)["work"]["phase"], "publish")
        return work

    def publishable(self, **args):
        work = self.reviewed(**args)
        self.grant(work)
        return work

    def accept(self, work, *, allow=True):
        return self.ok("accept", work, handoff=self.report(work)["work"]["data"]["handoff"], allow=allow)

    def test_n01_message_never_schedules_work(self):
        text = "X then Y fails. X,Y,Z works. Please investigate."
        message = self.ok("message", text=text)["message"]
        for table in ("work", "attempts"):
            self.assertEqual(self.coordinator.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        work = self.ok("submit", message=message)["work"]
        report = self.report(work)
        self.assertEqual(report["request"]["message"], message)
        self.assertEqual(report["request"]["base"], BASE)
        self.assertEqual(report["request"]["scope"], ["read", "sandbox_fixture"])
        self.assertEqual(self.coordinator.message(OPERATOR, message)["text"], text)
        self.assertEqual(report["attempts"], [])

    def test_n02_deterministic_eligible_worker_and_lease(self):
        work = self.investigate(run=False)
        for name, allowed, schema in (("forbidden", False, 1), ("incompatible", True, 2)):
            self.ok("register", worker=name, capabilities=["investigate"],
                    allowed=allowed, priority=-10, schema=schema)
        for name in ("b-worker", "a-worker"):
            self.ok("register", worker=name, capabilities=["investigate"], allowed=True, priority=0)
        claim = self.ok("claim", work)
        self.assertEqual(claim["worker"], "a-worker")
        self.assertEqual(self.report(work)["attempts"][0]["expires"], 3)
        self.ok("run", work)
        for name in ("investigator", "a-worker", "b-worker"):
            self.ok("register", worker=name, capabilities=["investigate"], allowed=False)
        blocked = self.investigate(run=False)
        self.denied("no_eligible_worker", "claim", blocked)
        self.assertEqual(self.report(blocked)["work"]["state"], "blocked")

    def test_n03_observations_and_inconclusive_control(self):
        work = self.investigate()
        report = self.report(work)
        findings = report["evidence"][0]["result"]
        self.assertEqual([item["sequence"] for item in findings["observations"]],
                         [["X", "Y"], ["X", "Y", "Z"]])
        self.assertEqual(findings["hypothesis"]["classification"], "inferred")
        self.assertTrue(any(event["kind"] == "tool_start" for event in report["events"]))
        inconclusive = self.investigate(mode="inconclusive")
        result = self.report(inconclusive)["evidence"][0]["result"]
        self.assertFalse(result["reproduced"])
        self.assertEqual(result["observations"][-1]["classification"], "inconclusive")
        self.denied("confirmed_evidence_required", "propose", inconclusive)

    def test_n04_only_exact_authenticated_approval_enables_edit(self):
        work = self.propose()
        binding = self.report(work)["pending_approval"]
        self.denied("not_runnable", "claim", work)
        for key in ("resource", "base", "revision", "scope"):
            self.denied("binding_mismatch", "decide", work,
                        binding=binding | {key: "wrong"}, allow=True)
        self.denied("operator_required", "decide", work, principal="operator",
                    binding=binding, allow=True)
        self.denied("stale_work_version", "decide", work, version=0, binding=binding, allow=True)
        report = self.report(work)
        self.assertEqual(report["grants"], [])
        self.assertIsNone(report["work"]["data"].get("candidate"))
        self.assertEqual(self.grant(work, allow=False)["state"], "declined")
        self.denied("approval_gate_required", "decide", work, binding=binding, allow=True)
        approved = self.propose()
        self.grant(approved)
        self.ok("run", approved)
        self.assertEqual(self.report(approved)["work"]["phase"], "verify")

    def test_n05_separate_grants_expiry_revocation_and_forbidden_actions(self):
        work = self.propose()
        grant = self.grant(work)["grant"]
        self.ok("revoke", work, grant=grant)
        self.denied("not_runnable", "claim", work)
        self.ok("resume", work)
        self.grant(work)
        for _ in range(3):
            self.ok("run", work)
        self.denied("publication_gate_required", "publish", work)
        self.grant(work, ttl=1)
        self.ok("advance", ticks=1)
        self.denied("publication_grant_inactive", "publish", work)
        self.assertEqual(self.report(work)["effects"], [])
        self.ok("resume", work)
        self.grant(work)
        self.ok("publish", work)
        for action in ("merge", "deploy"):
            self.denied("unsupported_action", action, work)
        self.ok("publish", work)
        self.assertEqual(self.report(work)["work"]["state"], "awaiting_acceptance")

    def test_n06_restart_fences_result_and_preserves_prior_checkpoint(self):
        work = self.propose()
        self.grant(work)
        self.ok("run", work)
        claim = self.ok("claim", work)
        job = self.coordinator._job(claim["attempt"])
        self.reload()
        self.ok("recover")
        result = self.coordinator.finish(job.worker, job, self.coordinator._fixture(job))
        self.assertEqual(result["reason"], "stale_or_wrong_attempt")
        self.ok("resume", work)
        self.ok("run", work)
        self.ok("run", work)
        report = self.report(work)
        phases = [item["phase"] for item in report["evidence"]]
        self.assertEqual(phases, ["investigate", "implement", "verify", "review"])
        self.assertEqual(report["work"]["data"]["used"], 4)
        self.assertTrue(any(event["data"].get("reason") == "restart_fenced"
                            for event in report["events"]))
        baseline = self.reviewed()
        self.assertEqual([item["digest"] for item in report["evidence"]],
                         [item["digest"] for item in self.report(baseline)["evidence"]])

    def test_n07_replay_duplicate_callback_and_concurrent_claim(self):
        message = self.ok("message", text="reference")["message"]
        first = self.ok("submit", command="same", message=message)
        self.assertEqual(first, self.ok("submit", command="same", message=message))
        self.denied("command_conflict", "submit", command="same", message=message, budget=2)
        work = first["work"]
        barrier = Barrier(2)

        def claim(index):
            with Coordinator(self.state) as coordinator:
                barrier.wait(timeout=5)
                return coordinator.execute(OPERATOR, f"concurrent-{index}", "claim", work=work,
                                           version=first["version"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, range(2)))
        self.assertEqual(sum(result["ok"] for result in outcomes), 1)
        job = self.coordinator._job(next(result["attempt"] for result in outcomes if result["ok"]))
        contents = self.coordinator._fixture(job)
        self.assertTrue(self.coordinator.finish(job.worker, job, contents)["ok"])
        self.assertFalse(self.coordinator.finish(job.worker, job, contents)["ok"])
        self.assertEqual(len(self.report(work)["evidence"]), 1)

    def test_n08_staging_and_database_faults_are_atomic(self):
        for fault in ("artifact_crash", "database_crash"):
            with self.subTest(fault=fault):
                work = self.investigate(run=False)
                job = self.coordinator._job(self.ok("claim", work)["attempt"])
                before = self.report(work)
                with self.assertRaises(InjectedCrash):
                    self.coordinator.finish(job.worker, job, self.coordinator._fixture(job), fault=fault)
                self.assertEqual(self.report(work), before)
                self.assertTrue(any(self.coordinator.artifacts.root.iterdir()))
                self.reload()
                self.ok("recover")
                self.ok("resume", work)
                self.ok("run", work)
                self.assertEqual(len(self.report(work)["evidence"]), 1)

    def test_n08_corrupt_or_missing_evidence_blocks_acceptance(self):
        for corruption, mode in ((True, "reproduced"), (False, "inconclusive")):
            with self.subTest(corruption=corruption):
                work = self.investigate(mode=mode)
                report = self.report(work)
                content = self.coordinator.artifacts.root / report["evidence"][0]["digest"]
                if corruption:
                    content.write_bytes(b"corrupt")
                else:
                    content.unlink()
                self.denied("evidence_unavailable", "accept", work,
                            handoff=report["work"]["data"]["handoff"])
                self.assertFalse(self.report(work)["evidence"][0]["available"])

    def test_n09_lost_ack_after_each_effect_recovers_without_resend(self):
        work = self.publishable()
        for operation in ("push", "draft"):
            self.denied("effect_unknown", "publish", work, fault="lost_after")
            effect = self.report(work)["effects"][-1]
            self.assertEqual(effect["operation"], operation)
            self.reload()
            self.ok("recover")
            self.ok("reconcile", work)
            self.assertEqual(self.report(work)["effects"][-1]["status"], "confirmed")
        self.assertEqual(self.coordinator.broker.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 2)
        self.accept(work)

    def test_n09_lost_before_quarantines_across_work_cancel_and_restart(self):
        work = self.publishable()
        self.denied("effect_unknown", "publish", work, fault="lost_before")
        self.assertEqual(self.ok("cancel", work)["state"], "cancelling")
        self.assertEqual(self.ok("cancel", work, acknowledge_unknown=True)["state"], "cancelled")
        self.reload()
        self.ok("recover")
        other = self.publishable()
        self.denied("target_quarantined", "publish", other)
        self.ok("reconcile", work)
        self.assertEqual(self.report(work)["effects"][0]["status"], "unknown")
        self.assertEqual(self.coordinator.broker.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_n09_drift_stops_draft_and_mismatched_draft_is_partial(self):
        work = self.publishable()
        self.ok("publish", work)
        self.coordinator.broker.drift(self.report(work)["request"]["target"])
        self.denied("effect_ref_drift", "publish", work)
        self.assertEqual(self.report(work)["effects"][-1]["status"], "ref_drift")
        other = self.publishable(topic="fixture-other")
        self.ok("publish", other)
        self.denied("effect_mismatch", "publish", other, fault="mismatch")
        report = self.report(other)
        self.assertFalse(report["effects"][-1]["receipt"]["draft"])
        self.assertNotIn("handoff", report["work"]["data"])

    def test_n10_exact_candidate_and_different_reviewer(self):
        work = self.propose()
        self.ok("register", worker="implementer", capabilities=["implement", "review"],
                allowed=True, priority=-10)
        self.grant(work)
        for _ in range(3):
            self.ok("run", work)
        report = self.report(work)
        candidate = report["work"]["data"]["candidate"]
        self.assertEqual(candidate, report["work"]["data"]["reviewed"])
        self.assertNotEqual(report["attempts"][-1]["worker"], "implementer")
        old_binding = report["pending_approval"]
        self.grant(work)
        self.ok("rework", work)
        self.denied("publication_gate_required", "publish", work)
        for _ in range(3):
            self.ok("run", work)
        changed = self.report(work)["work"]["data"]
        self.assertNotEqual(candidate, changed["candidate"])
        self.assertEqual(changed["candidate"], changed["reviewed"])
        self.denied("binding_mismatch", "decide", work, binding=old_binding, allow=True)

    def test_n11_redacted_reports_and_exact_acceptance_or_rejection(self):
        work = self.investigate(text="private-customer-material SECRET-123")
        proposal = self.ok("propose", work)["work"]
        self.grant(proposal)
        for _ in range(3):
            self.ok("run", proposal)
        self.grant(proposal, allow=False)
        self.denied("unsupported_action", "future", proposal, command="SECRET-123")
        report = self.report(proposal)
        for representation in (json.dumps(report), render_report(report)):
            self.assertNotIn("SECRET-123", representation)
            self.assertIn("review", representation)
        self.assertTrue(any(item["phase"] == "verify" and item["result"]["passed"] == 2
                            for item in report["evidence"]))
        handoff = report["work"]["data"]["handoff"]
        self.denied("operator_required", "accept", proposal, principal="worker", handoff=handoff)
        self.denied("handoff_mismatch", "accept", proposal, handoff="wrong")
        self.assertEqual(self.accept(proposal, allow=False)["state"], "rejected")
        followup = self.ok("propose", work, prior_work=proposal)["work"]
        self.assertEqual(self.report(followup)["request"]["prior_work"], proposal)
        self.assertEqual(self.accept(work)["state"], "completed")

    def test_n12_archive_statuses_do_not_inflate_evidence(self):
        work = self.investigate(run=False)
        for ref, revision, expected in (("example", "r1", "resolved"),
                                        ("example", "r0", "stale"),
                                        ("unlisted", "r1", "missing"),
                                        ("restricted", "r2", "denied"),
                                        ("offline", "r1", "unavailable")):
            result = self.ok("archive", work, reference=ref, revision=revision)
            self.assertEqual(result["outcome"], expected)
            self.assertEqual(result["satisfied"], expected == "resolved")
        self.assertEqual(self.report(work)["evidence"], [])
        self.ok("policy")
        self.assertEqual(self.ok("archive", work, reference="example", revision="r1")["outcome"], "denied")

    def test_n13_exhausted_budget_survives_restart(self):
        work = self.propose(budget=2)
        self.grant(work)
        self.ok("run", work)
        job = self.coordinator._job(self.ok("claim", work)["attempt"])
        failure = self.coordinator.finish(job.worker, job, {}, fault="worker_failure")
        self.assertEqual(failure["reason"], "fixture_worker_failed")
        self.reload()
        self.ok("recover")
        self.denied("budget_exhausted", "resume", work)
        terminal = self.report(work)
        self.assertEqual(terminal["work"]["state"], "failed")
        self.assertEqual(terminal["work"]["data"]["used"], 2)
        self.assertEqual(terminal["evidence"][-1]["phase"], "implement")
        self.assertEqual(terminal["effects"], [])
        self.denied("not_blocked", "resume", work)

    def test_archive_lookup_denies_after_work_expiry_or_cancellation(self):
        expired = self.investigate(run=False, ttl=1)
        cancelled = self.investigate(run=False)
        self.ok("advance", ticks=1)
        self.ok("cancel", cancelled)
        for work in (expired, cancelled):
            lookup = self.ok("archive", work, reference="example", revision="r1")
            self.assertEqual(lookup["outcome"], "denied")
            self.assertFalse(lookup["satisfied"])

    def test_elapsed_deadline_and_expired_worker(self):
        work = self.investigate(run=False, ttl=2)
        claim = self.ok("claim", work)
        self.ok("advance", ticks=2)
        self.denied("lease_or_authorization_inactive", "run", work)
        self.denied("budget_exhausted", "resume", work)
        self.assertEqual(self.report(work)["attempts"][0]["id"], claim["attempt"])

    def test_policy_change_and_approval_refresh_stay_at_checkpoint(self):
        work = self.propose()
        self.grant(work)
        self.ok("run", work)
        self.ok("policy")
        self.ok("resume", work)
        self.grant(work)
        self.assertEqual(self.report(work)["work"]["phase"], "verify")
        self.ok("run", work)
        self.assertEqual([row["phase"] for row in self.report(work)["evidence"]],
                         ["investigate", "implement", "verify"])

    def test_wrong_worker_and_injected_payload_cannot_change_policy_or_checkpoint(self):
        work = self.investigate(run=False)
        job = self.coordinator._job(self.ok("claim", work)["attempt"])
        forged = replace(job, worker="reviewer")
        self.assertFalse(self.coordinator.finish("reviewer", forged, {})["ok"])
        result = self.coordinator.finish(job.worker, job, {"approval": True, "secret": "never-export"})
        self.assertEqual(result["reason"], "invalid_fixture_result")
        report = self.report(work)
        self.assertNotIn("never-export", json.dumps(report))
        self.assertEqual(report["evidence"], [])
        self.assertEqual(report["grants"], [])

    def test_provider_commit_gap_is_reconciled_from_durable_intent(self):
        work = self.publishable()
        version = self.report(work)["work"]["version"]
        with self.assertRaises(InjectedCrash):
            self.cmd("publish", work, command="gap", fault="commit_gap")
        self.assertEqual(self.report(work)["effects"][0]["status"], "armed")
        self.assertIsNotNone(self.coordinator.broker.observe(self.report(work)["effects"][0]["id"]))
        self.reload()
        self.ok("recover")
        self.assertEqual(self.report(work)["effects"][0]["status"], "confirmed")
        self.denied("reconcile_report", "publish", work, command="gap", version=version, fault="commit_gap")
        self.assertEqual(self.report(work)["work"]["data"]["used"], 4)
        self.ok("publish", work)

    def interleave_publication(self, work, command, during):
        version = self.report(work)["work"]["version"]
        armed = Event()
        proceed = Event()
        first_db = None

        @contextmanager
        def pause_first_send(db, *, write=True):
            with transaction(db, write=write):
                yield
            if db is first_db and not armed.is_set():
                armed.set()
                if not proceed.wait(timeout=5):
                    raise TimeoutError("second command did not reach the armed intent")

        def first_publish():
            nonlocal first_db
            with Coordinator(self.state) as first:
                first_db = first.db
                return first.execute(OPERATOR, command, "publish", work=work, version=version)

        with patch("nexus.coordinator.transaction", pause_first_send), ThreadPoolExecutor() as pool:
            sent = pool.submit(first_publish)
            try:
                self.assertTrue(armed.wait(timeout=5))
                during(version)
            finally:
                proceed.set()
            return sent.result(timeout=5)

    def test_duplicate_publish_cannot_steal_armed_command(self):
        work = self.publishable()
        version = self.report(work)["work"]["version"]

        def duplicate(armed_version):
            self.denied("operation_in_progress", "publish", work, command="shared-publish",
                        version=armed_version)
            self.denied("command_conflict", "publish", work, command="shared-publish",
                        version=armed_version, fault="lost_before")

        first = self.interleave_publication(work, "shared-publish", duplicate)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first, self.ok("publish", work, command="shared-publish", version=version))
        self.assertEqual(len(self.report(work)["effects"]), 1)

    def test_repaired_handoff_does_not_repeat_checkpoint(self):
        work = self.investigate()
        report = self.report(work)
        handoff = report["work"]["data"]["handoff"]
        content = self.coordinator.artifacts.root / report["evidence"][0]["digest"]
        recovered = content.read_bytes()
        content.unlink()
        self.denied("evidence_unavailable", "accept", work, handoff=handoff)
        content.write_bytes(recovered)
        content.chmod(0o600)
        self.ok("resume", work)
        self.assertEqual(self.report(work)["work"]["data"]["handoff"], handoff)
        self.assertEqual(len(self.report(work)["attempts"]), 1)
        self.accept(work)

    def test_restore_blocks_all_provider_writes(self):
        self.ok("recover", restore=True)
        work = self.publishable()
        self.denied("publication_frozen", "publish", work)
        self.assertEqual(self.report(work)["effects"], [])

    def test_malformed_ids_and_transcript_are_rejected_without_mutation(self):
        self.denied("invalid_message", "message", text="\ud800")
        self.denied("unknown_message", "submit", message=2**90)
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.coordinator.db.execute("SELECT count(*) FROM work").fetchone()[0], 0)

    def test_revocation_between_effect_intent_and_dispatch(self):
        work = self.publishable()
        grant = self.report(work)["grants"][-1]["id"]

        def revoke(_version):
            self.ok("revoke", work, grant=grant)

        result = self.interleave_publication(work, "revoked-publish", revoke)
        self.assertFalse(result["ok"])
        self.assertEqual(self.report(work)["effects"][-1]["status"], "not_dispatched")
        self.assertEqual(self.coordinator.broker.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)
        self.ok("resume", work)
        self.grant(work)
        self.ok("publish", work)

    def test_owner_only_paths_and_artifact_traversal(self):
        with self.assertRaises(EvidenceError):
            self.coordinator.artifacts.get("../nexus.sqlite3")
        shared = Path(self.temp.name) / "shared"
        shared.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            Coordinator(shared)

    def test_fixture_path_uses_no_sockets(self):
        with patch("socket.socket", side_effect=AssertionError("socket opened")):
            work = self.publishable()
            self.ok("publish", work)
            self.ok("publish", work)
            self.accept(work)
        self.assertEqual(self.report(work)["work"]["state"], "completed")

    def test_cli_uses_fresh_processes_and_persists_reports(self):
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "nexus", "--state", str(self.state), *args],
                                    check=True, capture_output=True, text=True)
            return json.loads(result.stdout)

        message = cli("command", "message", "--id", "cli-message", "--args", '{"text":"test"}')
        work = cli("command", "submit", "--id", "cli-submit", "--args",
                   json.dumps({"message": message["message"]}))["work"]
        cli("command", "claim", "--id", "cli-claim", "--work", str(work), "--version", "1")
        cli("command", "recover", "--id", "cli-recover")
        report = cli("report", str(work), "--json")
        self.assertEqual(report["work"]["state"], "blocked")
        self.assertEqual(report["attempts"][0]["status"], "interrupted")
        with self.assertRaises(Rejected):
            self.coordinator.report(None, work)


if __name__ == "__main__":
    unittest.main()
