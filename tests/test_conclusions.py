"""An operator can close an exact hypothesis using accepted source and follow-up evidence."""

from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch

from nexus import conclusions, reasoning, reasoning_service as service
from nexus.coordinator import Coordinator, OPERATOR
from nexus.linux_worker import LinuxRunner
from nexus.storage import digest
import test_experiment_execution as execution_fixtures


class ConclusionTests(unittest.TestCase):
    def setUp(self):
        # The earlier investigator's approved follow-up is also relevant to a second
        # investigator with the same selected evidence and original investigation.
        f = execution_fixtures.ExperimentTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        run, _ = f.accepted_followup()
        self.c, self.root, self.work = f.c, f.root, f.work
        self.inv = service.start(self.c, OPERATOR, "Assess the earlier volume measurement", [f.work], "comparison")
        req = service._request(self.c, service._invocation(self.c, self.inv))
        answer = reasoning.investigate(req)
        self.hyp = {"explanation": "A changed calculation could explain this discrepancy.",
                    "supporting": req["evidence"], "contradictory": [], "uncertainty": "Only one shape.",
                    "verification": "Compare an independent volume measurement."}
        answer["hypotheses"] = [self.hyp]
        with patch.object(LinuxRunner, "launch", return_value=nullcontext(None)), patch.object(
                LinuxRunner, "wait", return_value={"status": "observed", "output": answer}):
            service.run(self.c, OPERATOR, self.inv)
        self.args = dict(invocation=self.inv, hypothesis_digest=digest(self.hyp),
                         evidence=[*req["evidence"], run["evidence"]["reference"]],
                         disposition="unresolved", rationale="The independent run returned no measurement.",
                         uncertainty="The reported discrepancy has no second numerical value.")

    def test_exact_assessment_is_durable_and_inert(self):
        before = self.c.engineering_handoff(OPERATOR, [self.work])
        counts = lambda: tuple(self.c._row("SELECT COUNT(*) FROM " + table)[0]
                               for table in ("grants", "experiment_runs", "reasoning_invocations", "experiment_proposals"))
        initial = counts()
        reply = self.c.execute(OPERATOR, "assessment", "conclude", **self.args)
        self.assertTrue(reply["ok"], reply)
        with Coordinator(self.root) as opened:
            self.assertEqual(reply, opened.execute(OPERATOR, "assessment", "conclude", **self.args))
            report = conclusions.report(opened, service._invocation(opened, self.inv))
        self.assertEqual(report["hypothesis_digests"], [digest(self.hyp)])
        item, = report["conclusions"]
        self.assertEqual(item["digest"], reply["conclusion_digest"])
        self.assertEqual(item["assessment"]["authority"], "informational_only")
        self.assertEqual(item["assessment"]["classification"], "operator_assessment")
        self.assertEqual(item["assessment"]["evidence"], self.args["evidence"])
        self.assertEqual(self.c.engineering_handoff(OPERATOR, [self.work]), before)
        self.assertEqual(counts(), initial)
        self.assertEqual(self.c.execute(OPERATOR, "another", "conclude", **self.args)["reason"],
                         "hypothesis_already_concluded")
        self.assertEqual(self.c.execute(OPERATOR, "assessment", "conclude",
                                       **(self.args | {"rationale": "changed"}))["reason"], "command_conflict")

    def test_denies_wrong_principal_hypothesis_or_evidence(self):
        self.assertEqual(self.c.execute(object(), "untrusted", "conclude", **self.args)["reason"], "operator_required")
        for index, changed in enumerate((
            {"hypothesis_digest": "a" * 64}, {"disposition": "approve"}, {"uncertainty": ""},
            {"evidence": []}, {"evidence": [self.args["evidence"][0]] * 2},
            {"evidence": [self.args["evidence"][0] | {"work": True}]},
            {"evidence": [self.args["evidence"][1] | {"invocation": self.inv}]},
            {"evidence": [self.args["evidence"][1] | {"sha256": "f" * 64}]},
        )):
            with self.subTest(changed=changed):
                self.assertFalse(self.c.execute(OPERATOR, f"invalid-{index}", "conclude",
                                                **(self.args | changed))["ok"])
        self.assertEqual(conclusions.report(self.c, service._invocation(self.c, self.inv))["conclusions"], [])

    def test_refuses_tampered_reasoning_and_followup(self):
        row = service._invocation(self.c, self.inv)
        modified = json.loads(row["output"])
        modified["hypotheses"][0]["uncertainty"] = "I am certain."
        self.c.db.execute("UPDATE reasoning_invocations SET output=? WHERE id=?", (json.dumps(modified), self.inv))
        self.assertEqual(self.c.execute(OPERATOR, "changed", "conclude", **self.args)["reason"],
                         "reasoning_output_integrity")
        self.c.db.execute("UPDATE reasoning_invocations SET output=? WHERE id=?", (row["output"], self.inv))
        self.assertTrue(self.c.execute(OPERATOR, "intact", "conclude", **self.args)["ok"])
        (self.c.artifacts.root / self.args["evidence"][1]["sha256"]).unlink()
        item, = conclusions.report(self.c, row)["conclusions"]
        self.assertEqual(item["integrity"], "invalid_or_unavailable")
        self.assertNotIn("assessment", item)
