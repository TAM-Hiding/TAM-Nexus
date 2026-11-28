from copy import deepcopy
import json
from pathlib import Path
import unittest

from nexus import demo_disclosure, reasoning


class DemoDisclosureTests(unittest.TestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[1] / "examples/nx-008/engineering-handoff.json"
        self.packet = json.loads(example.read_text())
        # NX-007 reasoning sees the observations available when the hypothesis was formed.
        del self.packet["followup_observations"]

    def test_fixed_objective_and_locally_reconstructed_demo_pair(self):
        make = reasoning.make_input
        demo_disclosure.validate(make(demo_disclosure.OBJECTIVE, 27, self.packet))
        with self.assertRaisesRegex(ValueError, "outside_demo_disclosure_scope"):
            demo_disclosure.validate(make("Disclose my private objective", 27, self.packet))
        for group, field, other in (
            ("investigations", "manifest_sha256", "0" * 64),
            ("investigations", "source_revision", "a" * 40),
            ("investigations", "repository", "other/project"),
            ("observations", "source_unchanged", False),
            ("observations", "outcome", "inconclusive"),
        ):
            with self.subTest(field=field):
                packet = deepcopy(self.packet)
                packet[group][0][field] = other
                with self.assertRaisesRegex(ValueError, "outside_demo_disclosure_scope"):
                    demo_disclosure.validate(make(demo_disclosure.OBJECTIVE, 27, packet))
