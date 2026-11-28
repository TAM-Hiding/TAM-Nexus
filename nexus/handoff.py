"""Read-only v1 engineering handoff over existing coordinator evidence."""

import json
import re

from . import pilot, source
from .storage import EvidenceError, digest, encode

SCHEMA = "nexus.investigation-handoff"
MAX_WORKS = 8
MAX_NOTES = 16
ACCEPTABLE = {"awaiting_acceptance", "completed"}
PERMISSIONS = {
    "inspect_evidence": ["read_approved_evidence"],
    "request_experiment": ["approve_read_only_experiment"],
}


class HandoffError(ValueError):
    pass


def _id(value):
    return type(value) is int and 0 < value < 2**63


def _reference(work, attempt, key):
    return {"work": work, "attempt": attempt, "sha256": key}


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 600 and value.isprintable() and bool(value.strip())


def _references(values, available):
    if not isinstance(values, list) or len(values) > MAX_WORKS:
        raise HandoffError("invalid_evidence_references")
    seen = set()
    for ref in values:
        if (not isinstance(ref, dict) or ref.keys() != {"work", "attempt", "sha256"}
                or not _id(ref["work"]) or not _id(ref["attempt"])
                or not isinstance(ref["sha256"], str)):
            raise HandoffError("invalid_evidence_references")
        key = (ref["work"], ref["attempt"], ref["sha256"])
        if key not in available:
            raise HandoffError("evidence_reference_not_verified_in_handoff")
        if key in seen:
            raise HandoffError("duplicate_evidence_reference")
        seen.add(key)


def _notes(value, available):
    if not isinstance(value, dict) or value.keys() != {"hypotheses", "recommendations"}:
        raise HandoffError("invalid_handoff_notes")
    hypotheses, recommendations = value["hypotheses"], value["recommendations"]
    if any(not isinstance(items, list) or len(items) > MAX_NOTES for items in (hypotheses, recommendations)):
        raise HandoffError("invalid_handoff_notes")
    for item in hypotheses:
        fields = {"explanation", "uncertainty", "verification", "supporting", "contradictory"}
        if (not isinstance(item, dict) or item.keys() != fields
                or any(not _text(item[key]) for key in ("explanation", "uncertainty", "verification"))):
            raise HandoffError("invalid_hypothesis")
        _references(item["supporting"], available)
        _references(item["contradictory"], available)
    result = []
    for item in recommendations:
        fields = {"action", "proposal", "permissions", "value", "risks", "dependencies", "evidence"}
        if (not isinstance(item, dict) or item.keys() != fields
                or any(not _text(item[key]) for key in ("proposal", "value", "risks", "dependencies"))):
            raise HandoffError("invalid_recommendation")
        action = item["action"]
        if not isinstance(action, str) or action not in PERMISSIONS or item["permissions"] != PERMISSIONS[action]:
            raise HandoffError("unsupported_follow_up_authority")
        _references(item["evidence"], available)
        result.append({**item, "authorization": "not_granted"})
    return hypotheses, result


def _capture(coordinator, request, work, row, observed, checkpoints):
    attempt, key = row["attempt"], row["artifact"]
    if (work["state"] not in ACCEPTABLE or row["work"] != work["id"] or row["owner"] != work["id"]
            or row["attempt_status"] != "accepted" or row["run_status"] != "accepted"
            or row["revision"] != work["revision"] or row["generation"] != work["data"]["generation"]
            or row["phase"] != "investigate"
            or row["attempt_phase"] != "investigate" or (attempt, key) not in observed
            or (attempt, key) not in checkpoints):
        raise HandoffError("evidence_not_current_checkpoint")
    try:
        value = coordinator.artifacts.get(key)
    except EvidenceError as exc:
        state = "corrupt" if str(exc) in ("corrupt_artifact", "invalid_artifact_encoding") else "missing_or_unsafe"
        raise HandoffError(f"evidence_{state}") from exc
    if not isinstance(value, dict):
        raise HandoffError("evidence_capture_invalid")
    raw = {"status": value.get("status"), "elapsed_ms": value.get("elapsed_ms"),
           "exit": value.get("exit_status"), "streams": value.get("streams"),
           "source_unchanged": value.get("source_unchanged")}
    try:
        rebuilt = source.capture(raw, request, coordinator._job(attempt))
    except source.SourceError as exc:
        raise HandoffError("evidence_capture_invalid") from exc
    environment = value.get("host_platform")
    if "host_platform" in value:
        if (not isinstance(environment, dict) or environment.keys() != {"kernel", "architecture", "coordinator_python"}
                or any(not isinstance(text, str) or not re.fullmatch(r"[a-zA-Z0-9_.+~-]{1,100}", text)
                       for text in environment.values())):
            raise HandoffError("evidence_environment_invalid")
        rebuilt["host_platform"] = environment
    if (encode(value) != encode(rebuilt) or rebuilt["status"] != "test_exited"
            or rebuilt["outcome"] not in ("command_passed", "command_exit_one")):
        raise HandoffError("evidence_capture_mismatch")
    return rebuilt


def _acceptance_packet(coordinator, work):
    key = work["data"].get("handoff")
    try:
        packet = coordinator.artifacts.get(key)
    except EvidenceError as exc:
        state = "corrupt" if str(exc) in ("corrupt_artifact", "invalid_artifact_encoding") else "missing_or_unsafe"
        raise HandoffError(f"acceptance_packet_{state}") from exc
    if (not isinstance(packet, dict) or packet.get("work") != work["id"]
            or packet.get("revision") != work["revision"]):
        raise HandoffError("acceptance_packet_mismatch")
    ready = accepted = False
    for event in coordinator.db.execute(
        "SELECT kind,actor,data FROM events WHERE work=? AND kind IN ('handoff_ready','handoff_decision')",
        (work["id"],),
    ):
        data = json.loads(event["data"])
        if data.get("handoff") != key:
            continue
        if event["kind"] == "handoff_ready" and event["actor"] == "coordinator":
            ready |= data.get("outcome") == packet.get("outcome")
        if event["kind"] == "handoff_decision" and event["actor"] == "operator":
            accepted = data.get("accepted") is True
    if not ready or (work["state"] == "completed" and not accepted):
        raise HandoffError("acceptance_packet_event_mismatch")
    return packet


def build(coordinator, works, notes=None):
    """Build from one read transaction. Supplied prose has no observation path."""
    if (not isinstance(works, list) or not 1 <= len(works) <= MAX_WORKS
            or any(not _id(work) for work in works) or len(set(works)) != len(works)):
        raise HandoffError("invalid_handoff_work_selection")

    subjects, observations, issues = [], [], []
    available = set()
    modes = set()
    for work_id in works:
        work = coordinator._work(work_id)
        request = coordinator._revision(work)
        if request["kind"] != "source_investigation" or request.get("mode") not in ("source", "pilot"):
            raise HandoffError("source_investigation_required")
        if request.get("work") != work_id or digest(request) != work["revision"]:
            raise HandoffError("work_revision_mismatch")
        modes.add(request["mode"])
        if len(modes) > 1:
            raise HandoffError("unrelated_investigation_profiles")
        spec = request.get("manifest", {})
        subject = {
            "work": work_id, "work_revision": work["revision"], "state": work["state"],
            "authorization": request["source_authorization"], "approved_scope": request["scope"],
            "repository": spec.get("repository", "nx-004-synthetic"),
            "source_revision": request["base"], "source_identity": request["resource"],
            "snapshot_sha256": request["snapshot"], "manifest_sha256": digest(spec) if spec else None,
            "execution_profile": spec.get("profile", source.PROFILE),
            "dependency_sha256": spec.get("dependency_digest"),
        }
        subjects.append(subject)
        packet = None
        if work["state"] in ACCEPTABLE:
            key = work["data"].get("handoff")
            subject["acceptance_packet_sha256"] = key
            try:
                packet = _acceptance_packet(coordinator, work)
            except HandoffError as exc:
                issues.append({"work": work_id, "reason": str(exc)})
        if spec:
            pilot.manifest(spec)
        events = {"source_observed": set(), "checkpoint": set()}
        for event in coordinator.db.execute(
            "SELECT kind,data FROM events WHERE work=? AND "
            "((kind='source_observed' AND actor='runner') OR (kind='checkpoint' AND actor='coordinator'))",
            (work_id,),
        ):
            data = json.loads(event["data"])
            events[event["kind"]].add((data["attempt"], data["artifact"]))
        committed = set()
        rows = coordinator.db.execute(
            "SELECT c.*, a.work AS owner,a.status AS attempt_status,a.revision,a.generation,"
            "a.phase AS attempt_phase,l.status AS run_status FROM checkpoints c "
            "JOIN attempts a ON a.id=c.attempt LEFT JOIN linux_runs l ON l.attempt=a.id "
            "WHERE c.work=? ORDER BY c.id", (work_id,))
        for row in rows:
            attempt, key = row["attempt"], row["artifact"]
            ref = _reference(work_id, attempt, key)
            committed.add((attempt, key))
            try:
                capture = _capture(coordinator, request, work, row, events["source_observed"], events["checkpoint"])
            except HandoffError as exc:
                issues.append({"reference": ref, "reason": str(exc)})
                continue
            if packet is None:
                continue
            if (packet.get("checkpoints") != [{"phase": "investigate", "attempt": attempt, "artifact": key}]
                    or packet.get("evidence") != [key] or packet.get("outcome") != capture["outcome"]):
                issues.append({"reference": ref, "reason": "acceptance_packet_mismatch"})
                continue
            available.add((work_id, attempt, key))
            observations.append({
                "reference": ref, "provenance": "supervisor_process_observation",
                "test": ("tests/test_document.py::test_box_regenerates_exact_solid"
                         if spec else "test_model.py via unittest discovery"),
                "worker": capture["worker"], "sandbox": capture["sandbox"],
                "host_platform": capture.get("host_platform"), "status": capture["status"],
                "exit_status": capture["exit_status"], "elapsed_ms": capture["elapsed_ms"],
                "outcome": capture["outcome"], "source_unchanged": capture.get("source_unchanged"),
                "stream_fingerprints": capture["observations"], "limitations": capture["limitations"],
            })
        for attempt, key in sorted(events["source_observed"] - committed):
            issues.append({"reference": _reference(work_id, attempt, key), "reason": "unaccepted_capture"})
        if not committed:
            issues.append({"work": work_id, "reason": "no_accepted_checkpoint"})

    hypotheses, recommendations = _notes(notes if notes is not None else
                                         {"hypotheses": [], "recommendations": []}, available)
    return {"schema": SCHEMA, "version": 1, "investigations": subjects, "observations": observations,
            "evidence_issues": issues, "hypotheses": hypotheses, "recommendations": recommendations,
            "notes_provenance": "operator_supplied_unverified" if notes is not None else None,
            "authority": "informational_only"}


def render(record):
    """Render the same bounded JSON fields in a terminal-friendly form."""
    lines = ["Engineering investigation handoff v1", "",
             "Observations and authorized scope", ""]
    sections = (
        ("Investigation", record["investigations"]), ("Observed test", record["observations"]),
        ("Evidence issue", record["evidence_issues"]),
        ("Hypothesis · unverified", record["hypotheses"]),
        ("Recommendation · approval not granted", record["recommendations"]),
    )
    for title, items in sections:
        for item in items:
            lines.extend((title, *(f"  {key.replace('_', ' ')}  " +
                (str(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=True))
                for key, value in item.items()), ""))
    lines.append("This handoff grants no authority.")
    return "\n".join(lines)
