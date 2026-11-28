"""Interpret one untrusted, bounded box measurement against the fixed profile."""

import base64
import hashlib
import json
import math
from pathlib import Path

ARGV = ["/runtime/bin/python3", "-I", "-S", "-B", "/input/hero_cad_volume.py"]
EXPECTED_MM3 = 12.345 * 23.456 * 34.567
WORKER = Path(__file__).with_name("hero_cad_volume.py")


def script_digest():
    return hashlib.sha256(WORKER.read_bytes()).hexdigest()


def _decode(encoded):
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate field")
        return value

    if type(encoded) is not str or len(encoded) > 2048:
        raise ValueError("invalid measurement length")
    raw = base64.b64decode(encoded, validate=True)
    value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    if type(value) is not dict or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("unsupported measurement")
    status = value.get("status")
    if status == "measured":
        if value.keys() != {"version", "status", "mesh_mm3", "reported_mm3"}:
            raise ValueError("invalid measurement fields")
        for key in ("mesh_mm3", "reported_mm3"):
            n = value[key]
            if n is None and key == "reported_mm3":
                continue
            if type(n) not in (int, float) or not -1e12 <= n <= 1e12:
                raise ValueError("invalid numerical measurement")
    elif (value.keys() != {"version", "status"} or status not in
          ("invalid_solid", "unexpected_geometry", "regeneration_failed", "measurement_unavailable")):
        raise ValueError("invalid measurement status")
    return value


def interpret(capture):
    """Called only on a validated supervisor capture, including on handoff reload."""
    result = {"measurement": None, "provenance": "worker_report_unverified"}
    if capture["source_unchanged"] is not True:
        return {**result, "outcome": "source_changed_during_run"}
    if capture["status"] == "test_exited" and capture["exit_status"] == 78:
        return {**result, "outcome": "dependency_unavailable"}
    if capture["status"] != "test_exited" or capture["exit_status"] != 0:
        return {**result, "outcome": capture["outcome"]}
    try:
        value = _decode(capture["streams"]["stdout"])
    except (ValueError, RecursionError, KeyError):
        return {**result, "outcome": "invalid_measurement_output"}
    if value["status"] != "measured":
        return {**result, "outcome": value["status"]}
    mesh, reported = value["mesh_mm3"], value["reported_mm3"]
    if not math.isclose(mesh, EXPECTED_MM3, rel_tol=1e-9, abs_tol=1e-7):
        outcome = "geometry_volume_mismatch"
    elif reported is None:
        outcome = "reported_volume_unavailable"
    elif not math.isclose(mesh, reported, rel_tol=1e-9, abs_tol=1e-7):
        outcome = "reported_volume_mismatch"
    else:
        outcome = "volumes_agree"
    return {**result, "outcome": outcome, "measurement": {
        "method": "oriented_triangle_tetrahedra", "expected_mm3": EXPECTED_MM3,
        "mesh_mm3": mesh, "reported_mm3": reported}}
