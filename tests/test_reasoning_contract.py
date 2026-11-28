import copy
import json
import unittest
from pathlib import Path

from nexus import reasoning as protocol


class ReasoningContractTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "examples/nx-006/hero-cad-handoff.json"
        self.handoff = json.loads(path.read_text())
        self.request = protocol.make_input("Explain the volume control.", 42, self.handoff)
        self.answer = protocol.investigate(self.request)

    def test_fixture_pass_fail_and_unsupported_inputs(self):
        self.assertEqual(self.answer, protocol.validate(self.request, protocol.investigate(self.request)))
        self.assertEqual(self.answer["experiment"]["target"], self.request["investigation"])
        hypothesis, = self.answer["hypotheses"]
        self.assertIn("deliberately modified", hypothesis["explanation"])
        self.assertEqual(hypothesis["supporting"], self.request["evidence"])
        self.assertEqual(self.request["handoff"]["recommendations"], [])
        unmatched = copy.deepcopy(self.handoff)
        unmatched["investigations"][1]["source_revision"] = "d" * 40
        request = protocol.make_input("Inspect.", 43, unmatched)
        self.assertIsNone(protocol.validate(request, protocol.investigate(request))["experiment"])

    def test_reject_envelope_tampering(self):
        for key, bad in [("version", True), ("invocation", 0), ("schema", "other"),
                         ("objective", "\x1b[31m"), ("evidence", []), ("profiles", ["sh"]),
                         ("limits", {}), ("investigation", True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.investigate({**self.request, key: bad})

    def test_reject_spoofed_output_and_excess(self):
        for key, bad in [("version", 2), ("invocation", 41), ("hypotheses", self.answer["hypotheses"] * 4),
                         ("observations", []), ("next_invocation", 1), ("experiment", [self.answer["experiment"]]),
                         ("experiment", "a" * protocol.MAX_OUTPUT)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                protocol.validate(self.request, {**self.answer, key: bad})

    def test_proposals_are_narrow(self):
        proposal = self.answer["experiment"]
        for patch in [{"profile": "/bin/sh"}, {"target": True}, {"target": 2}, {"command": "date"},
                      {"grant": "operator"}, {"evidence": []}, {"evidence": [proposal["evidence"][1]]},
                      {"evidence": [{**proposal["evidence"][0], "sha256": "d" * 64}]}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                protocol.validate(self.request, {**self.answer, "experiment": {**proposal, **patch}})

    def test_hypothesis_references_and_text(self):
        hypothesis, = self.answer["hypotheses"]
        for patch in [{"supporting": []}, {"supporting": self.request["evidence"] * 5},
                      {"contradictory": hypothesis["supporting"]}, {"uncertainty": ""},
                      {"supporting": [{**hypothesis["supporting"][0], "attempt": True}]},
                      {"verification": ["free shell"]}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                protocol.validate(self.request, {**self.answer, "hypotheses": [{**hypothesis, **patch}]})

    def test_wire_format_and_byte_caps(self):
        for data in [b'{"version":2,"version":1}', b'{"x": NaN}', b'\xff', b'[' * 2000 + b']' * 2000]:
            with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                protocol.loads(data, protocol.MAX_OUTPUT)
        with self.assertRaises(ValueError):
            protocol.loads(b' ' * protocol.MAX_OUTPUT + b'1', protocol.MAX_OUTPUT)
        with self.assertRaises(ValueError):
            protocol.make_input("read", 1, {**self.handoff, "evidence_issues": ["missing"]})
