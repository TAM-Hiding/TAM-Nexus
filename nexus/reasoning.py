"""NX-007 bounded JSON contract. This module accepts data and has no execution hooks."""

import json
import re

PROFILE = "hero-cad-volume-crosscheck-v1"
PERMISSION = "approve_read_only_experiment"
MAX_INPUT = 65536
MAX_OUTPUT = 8192
MAX_SECONDS = 2
MAX_INVOCATIONS = 3
MAX_HYPOTHESES = 3
MAX_REFERENCES = 8

LIMITS = {"input_bytes": MAX_INPUT, "output_bytes": MAX_OUTPUT, "seconds": MAX_SECONDS,
          "hypotheses": MAX_HYPOTHESES, "references_per_list": MAX_REFERENCES,
          "experiment_requests": 1, "invocations_per_work": MAX_INVOCATIONS}


def _record(value, fields):
    if type(value) is not dict or value.keys() != set(fields.split()):
        raise ValueError("invalid_reasoning_fields")


def _identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("invalid_reasoning_identifier")


def _text(value):
    if type(value) is not str or not 0 < len(value) <= 600 or not value.strip() or not value.isprintable():
        raise ValueError("invalid_reasoning_text")


def _bounded(value, limit):
    try:
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("invalid_reasoning_json") from exc
    if len(data) > limit:
        raise ValueError("reasoning_size_exceeded")


def _version(record, schema):
    if record.get("schema") != schema or type(record.get("version")) is not int or record["version"] != 1:
        raise ValueError("unsupported_reasoning_schema")


def _key(ref):
    _record(ref, "work attempt sha256")
    _identifier(ref["work"])
    _identifier(ref["attempt"])
    if type(ref["sha256"]) is not str or not re.fullmatch("[a-f0-9]{64}", ref["sha256"]):
        raise ValueError("invalid_reasoning_reference")
    return ref["work"], ref["attempt"], ref["sha256"]


def _refs(refs):
    if type(refs) is not list or len(refs) > MAX_REFERENCES:
        raise ValueError("reasoning_reference_limit")
    keys = {_key(ref) for ref in refs}
    if len(keys) != len(refs):
        raise ValueError("duplicate_reasoning_reference")
    return keys


def make_input(objective, invocation, handoff):
    """The caller must build the handoff from authorized coordinator records."""
    _identifier(invocation)
    _text(objective)
    if type(handoff) is not dict:
        raise ValueError("invalid_reasoning_handoff")
    _version(handoff, "nexus.investigation-handoff")
    if handoff.get("authority") != "informational_only" or handoff.get("evidence_issues") != []:
        raise ValueError("reasoning_requires_verified_handoff")
    # Only the coordinator-built observations and subjects cross this boundary.
    # Notes are optional operator text and never necessary for this worker.
    redacted = {**handoff, "hypotheses": [], "recommendations": [], "notes_provenance": None}
    _record(redacted, "schema version investigations observations evidence_issues hypotheses recommendations notes_provenance authority")
    observations = redacted["observations"]
    subjects = redacted["investigations"]
    if (type(observations) is not list or not 1 <= len(observations) <= MAX_REFERENCES
            or type(subjects) is not list or len(subjects) != len(observations)):
        raise ValueError("invalid_reasoning_evidence_selection")
    for subject in subjects:
        _record(subject, "work work_revision state authorization approved_scope repository source_revision "
                         "source_identity snapshot_sha256 manifest_sha256 execution_profile dependency_sha256 "
                         "acceptance_packet_sha256")
        if subject["state"] not in ("awaiting_acceptance", "completed"):
            raise ValueError("unaccepted_reasoning_subject")
    for item in observations:
        _record(item, "reference provenance test worker sandbox host_platform status exit_status elapsed_ms "
                      "outcome source_unchanged stream_fingerprints limitations")
        if item["provenance"] != "supervisor_process_observation" or item["status"] != "test_exited":
            raise ValueError("unaccepted_reasoning_observation")
    references = [o["reference"] for o in observations]
    works = [s["work"] for s in subjects]
    for work in works:
        _identifier(work)
    if len(set(works)) != len(works) or {key[0] for key in _refs(references)} != set(works):
        raise ValueError("reasoning_subject_mismatch")
    profiles = [PROFILE] if all(s["execution_profile"] == "hero-cad-box-v1" for s in subjects) else []
    request = {"schema": "nexus.reasoning-input", "version": 1, "invocation": invocation,
               "objective": objective, "investigation": works[0], "handoff": redacted,
               "evidence": references, "profiles": profiles, "limits": dict(LIMITS)}
    _bounded(request, MAX_INPUT)
    return request


def validate_input(request):
    _record(request, "schema version invocation objective investigation handoff evidence profiles limits")
    _version(request, "nexus.reasoning-input")
    _bounded(request, MAX_INPUT)
    expected = make_input(request["objective"], request["invocation"], request["handoff"])
    _identifier(request["investigation"])
    _refs(request["evidence"])
    # Canonical bytes distinguish booleans and floats from integer limits.
    if json.dumps(expected, sort_keys=True) != json.dumps(request, sort_keys=True):
        raise ValueError("reasoning_envelope_mismatch")


def validate(request, output):
    """Validate returned claims against the coordinator's retained input."""
    validate_input(request)
    _bounded(output, MAX_OUTPUT)
    _record(output, "schema version invocation hypotheses experiment")
    _version(output, "nexus.reasoning-output")
    _identifier(output["invocation"])
    if output["invocation"] != request["invocation"]:
        raise ValueError("reasoning_invocation_mismatch")
    available = _refs(request["evidence"])
    hypotheses = output["hypotheses"]
    if type(hypotheses) is not list or len(hypotheses) > MAX_HYPOTHESES:
        raise ValueError("reasoning_hypothesis_limit")
    for hypothesis in hypotheses:
        _record(hypothesis, "explanation supporting contradictory uncertainty verification")
        for field in ("explanation", "uncertainty", "verification"):
            _text(hypothesis[field])
        supporting = _refs(hypothesis["supporting"])
        contrary = _refs(hypothesis["contradictory"])
        if not supporting or not (supporting | contrary) <= available or supporting & contrary:
            raise ValueError("invalid_hypothesis_evidence")
    proposal = output["experiment"]
    if proposal is not None:
        _record(proposal, "profile target purpose value evidence")
        _identifier(proposal["target"])
        if proposal["profile"] not in request["profiles"] or proposal["target"] != request["investigation"]:
            raise ValueError("unsupported_experiment_scope")
        _text(proposal["purpose"])
        _text(proposal["value"])
        refs = _refs(proposal["evidence"])
        if not refs <= available or not any(key[0] == proposal["target"] for key in refs):
            raise ValueError("invalid_experiment_evidence")
    return output


def investigate(request):
    """Deterministic stand-in. Fixture knowledge lives exclusively in this function."""
    validate_input(request)
    result = {"schema": "nexus.reasoning-output", "version": 1,
              "invocation": request["invocation"], "hypotheses": [], "experiment": None}
    subjects = {s["source_revision"]: s["work"] for s in request["handoff"]["investigations"]}
    baseline = subjects.get("b053d529c8cd32d07c5d37d060372b8bb0d85741")
    control = subjects.get("ff43cdd9ab36c24aa1ae4ff3cb42f7e35782bdea")
    observed = {o["reference"]["work"]: o["outcome"] for o in request["handoff"]["observations"]}
    if (PROFILE in request["profiles"] and request["investigation"] == baseline
            and observed.get(baseline) == "command_passed" and observed.get(control) == "command_exit_one"):
        evidence = [r for r in request["evidence"] if r["work"] in (baseline, control)]
        result["hypotheses"].append({
            "explanation": "The deliberately modified disposable control may halve the reported box volume.",
            "supporting": evidence, "contradictory": [],
            "uncertainty": "The baseline passed and the control failed. Exit status alone cannot identify the failing calculation.",
            "verification": "Compare an independent geometric volume measurement with the reported volume."})
        result["experiment"] = {"target": baseline, "profile": PROFILE, "evidence": evidence,
                                "purpose": "Independently measure volume for the pinned baseline box.",
                                "value": "Establish a reference measurement independent of the reported volume calculation."}
    return result


def loads(data, limit):
    """Decode the exact byte-limited JSON wire format, refusing ambiguous objects."""
    if type(data) is not bytes or len(data) > limit:
        raise ValueError("reasoning_size_exceeded")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_reasoning_field")
            result[key] = value
        return result

    def invalid_number(_):
        raise ValueError("invalid_reasoning_number")

    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_number)
    except RecursionError as exc:
        raise ValueError("invalid_reasoning_json") from exc
    # No version-one field approaches this depth. Check iteratively before
    # passing nested records to ordinary encoders or equality comparisons.
    pending = [(record, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 16:
            raise ValueError("reasoning_depth_exceeded")
        if isinstance(item, dict):
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
    return record


if __name__ == "__main__":
    import sys

    with open("/input/job.json", "rb") as source:
        job = loads(source.read(MAX_INPUT + 1), MAX_INPUT)
    answer = json.dumps(investigate(job), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(answer) > MAX_OUTPUT:
        raise ValueError("reasoning_size_exceeded")
    sys.stdout.write(answer)
