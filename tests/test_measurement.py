import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nexus import measurement
from nexus.linux_worker import LinuxRunner


class MeasurementTests(unittest.TestCase):
    def interpret(self, obj, **fields):
        data = obj if type(obj) is bytes else json.dumps(obj).encode()
        return measurement.interpret({"status": "test_exited", "exit_status": 0,
                                      "source_unchanged": True, "outcome": "command_passed",
                                      "streams": {"stdout": base64.b64encode(data).decode()}, **fields})

    def test_measured_geometry_and_report_discrepancies(self):
        expected = measurement.EXPECTED_MM3
        data = {"version": 1, "status": "measured", "mesh_mm3": expected, "reported_mm3": expected}
        for change, outcome in [({}, "volumes_agree"), ({"reported_mm3": expected / 2}, "reported_volume_mismatch"),
                                ({"mesh_mm3": expected / 2}, "geometry_volume_mismatch"),
                                ({"reported_mm3": None}, "reported_volume_unavailable")]:
            result = self.interpret({**data, **change})
            self.assertEqual(result["outcome"], outcome)
            self.assertEqual(result["provenance"], "worker_report_unverified")
        for change in [{"mesh_mm3": True}, {"reported_mm3": float("nan")}, {"command": "another experiment"},
                       {"version": 2}, {"mesh_mm3": None}, {"reported_mm3": "5004"}]:
            self.assertEqual(self.interpret({**data, **change})["outcome"], "invalid_measurement_output")

    def test_digest_binds_exact_script_forwarded_to_supervisor(self):
        runner = LinuxRunner(script=b"")
        approved = measurement.script_digest()
        with tempfile.TemporaryDirectory() as root, patch("nexus.linux_worker.subprocess.Popen") as popen:
            options = {"pilot": (root, root), "crosscheck": True}
            for expected in (None, approved):
                with self.subTest(expected=expected), patch.object(Path, "read_bytes", return_value=b"changed"), \
                        self.assertRaisesRegex(ValueError, "crosscheck_script_digest_mismatch"):
                    with runner.launch(0, None, crosscheck_sha256=expected, **options):
                        self.fail("unapproved script reached launcher")
            popen.assert_not_called()
            with runner.launch(0, None, crosscheck_sha256=approved, **options):
                self.assertEqual(popen.call_args.args[0][7], measurement.WORKER.read_bytes().hex())

    def test_malformed_and_uncertain_records(self):
        for data in (b"not JSON", b'{"version":1,"status":"invalid_solid","status":"regeneration_failed"}',
                     b" " * 2000, {"version": True, "status": "invalid_solid"},
                     {"version": 1, "status": "regeneration_failed", "mesh_mm3": 3}):
            self.assertEqual(self.interpret(data)["outcome"], "invalid_measurement_output")
        for code in ("invalid_solid", "unexpected_geometry", "measurement_unavailable", "regeneration_failed"):
            self.assertEqual(self.interpret({"version": 1, "status": code})["outcome"], code)
        self.assertEqual(self.interpret({}, status="worker_timeout", outcome="worker_timeout")["outcome"],
                         "worker_timeout")
        self.assertEqual(self.interpret({}, exit_status=78)["outcome"], "dependency_unavailable")
        self.assertEqual(self.interpret({}, source_unchanged=False)["outcome"], "source_changed_during_run")
