"""Local operator assessments of specific accepted reasoning output."""

import json

from . import experiment_evidence, handoff
from .coordinator import Rejected, _valid_id
from .storage import EvidenceError, digest, encode


def _hypotheses(c, row):
    c._require(row["status"] == "completed", "completed_reasoning_required")
    output = json.loads(row["output"])
    events = c.db.execute(
        "SELECT data FROM events WHERE work=? AND kind='reasoning_accepted' AND actor='coordinator'",
        (row["origin"],))
    accepted = [data for event in events if (data := json.loads(event["data"])).get("invocation") == row["id"]]
    c._require(len(accepted) == 1 and accepted[0].get("output_digest") == digest(output)
               and accepted[0].get("classification") == "unverified_reasoning"
               and output["invocation"] == row["id"], "reasoning_output_integrity")
    return output["hypotheses"]


def _validate_evidence(c, row, hypothesis, refs):
    c._require(type(refs) is list and 1 <= len(refs) <= 8, "invalid_conclusion_references")
    works = json.loads(row["works"])
    original = handoff.build(c, works, include_followups=False)
    c._require(not original["evidence_issues"], "conclusion_parent_evidence_invalid")
    available = {encode(item["reference"]) for item in original["observations"]}
    c._require(all(encode(r) in available for r in hypothesis["supporting"] + hypothesis["contradictory"]),
               "hypothesis_evidence_unavailable")
    c._require(len({encode(r) for r in refs}) == len(refs), "duplicate_conclusion_reference")
    for ref in refs:
        if encode(ref) in available:
            continue
        c._require(isinstance(ref, dict) and ref.keys() == {"invocation", "attempt", "sha256"}
                   and _valid_id(ref["invocation"]) and _valid_id(ref["attempt"]), "unrelated_conclusion_reference")
        run = c._row("SELECT r.*, i.origin, i.works FROM experiment_runs r "
                     "JOIN reasoning_invocations i ON i.id=r.invocation WHERE r.invocation=? AND r.id=?",
                     (ref["invocation"], ref["attempt"]))
        c._require(run is not None and run["origin"] == row["origin"] and run["artifact"] == ref["sha256"]
                   and set(json.loads(run["works"])).issubset(works), "unrelated_conclusion_reference")
        verified = experiment_evidence.verify(c, run)
        c._require(encode(verified["reference"]) == encode(ref), "invalid_conclusion_reference")


def _events(c, row):
    entries = c.db.execute(
        "SELECT data FROM events WHERE work=? AND kind='hypothesis_concluded' AND actor='operator' ORDER BY seq",
        (row["origin"],))
    return [data for e in entries if (data := json.loads(e["data"])).get("invocation") == row["id"]]


def _record(c, row, hypotheses, key, refs, disposition, rationale, uncertainty):
    matching = [item for item in hypotheses if digest(item) == key]
    c._require(len(matching) == 1, "unknown_or_ambiguous_hypothesis")
    c._require(disposition in ("supported", "contradicted", "unresolved"), "invalid_conclusion_disposition")
    for text in (rationale, uncertainty):
        c._require(type(text) is str and 0 < len(text) <= 600 and text.strip() and text.isprintable(),
                   "invalid_conclusion_text")
    _validate_evidence(c, row, matching[0], refs)
    return {"schema": "nexus.hypothesis-conclusion", "version": 1,
            "origin": row["origin"], "invocation": row["id"], "hypothesis_digest": key,
            "reasoning_output_digest": digest(json.loads(row["output"])), "evidence": refs,
            "disposition": disposition, "rationale": rationale, "uncertainty": uncertainty,
            "classification": "operator_assessment", "authority": "informational_only"}


def _conclude(c, invocation, hypothesis_digest, evidence, disposition, rationale, uncertainty):
    """Called by the authenticated command handler inside its ledger transaction."""
    from .reasoning_service import _invocation

    row = _invocation(c, invocation)
    c._require(all(event["hypothesis_digest"] != hypothesis_digest for event in _events(c, row)),
               "hypothesis_already_concluded")
    try:
        record = _record(c, row, _hypotheses(c, row), hypothesis_digest, evidence, disposition, rationale, uncertainty)
        key = c.artifacts.put(record)
    except (EvidenceError, ValueError, KeyError, TypeError) as exc:
        raise Rejected("conclusion_integrity_failure") from exc
    c._event("hypothesis_concluded", row["origin"], actor="operator", invocation=invocation,
             hypothesis_digest=hypothesis_digest, artifact=key)
    return c._ok(invocation=invocation, conclusion_digest=key)


def report(c, row):
    result = {"hypothesis_digests": [], "conclusions": []}
    if row["status"] != "completed":
        return result
    hypotheses = _hypotheses(c, row)
    result["hypothesis_digests"] = [digest(h) for h in hypotheses]
    for event in _events(c, row):
        key = event["artifact"]
        try:
            record = c.artifacts.get(key)
            rebuilt = _record(c, row, hypotheses, event["hypothesis_digest"], record["evidence"],
                              record["disposition"], record["rationale"], record["uncertainty"])
            c._require(encode(rebuilt) == encode(record), "conclusion_record_mismatch")
        except (Rejected, EvidenceError, ValueError, KeyError, TypeError):
            result["conclusions"].append({"digest": key, "integrity": "invalid_or_unavailable"})
        else:
            result["conclusions"].append({"digest": key, "integrity": "verified", "assessment": record})
    return result
