"""Capture and verify historical evidence for the fixed, operator-approved follow-up."""

import json

from . import handoff, measurement, reasoning, source
from .storage import EvidenceError, digest, encode


def _record(invocation, attempt, spec, grant_id, observed):
    plan = spec["execution_plan"]
    try:
        process = source.process_observation(observed)
    except source.SourceError as exc:
        raise handoff.HandoffError("invalid_experiment_observation") from exc
    if observed.get("source_unchanged") is not True:
        raise handoff.HandoffError("experiment_source_changed")
    process["source_unchanged"] = True
    interpretation = measurement.interpret(process)
    if interpretation["outcome"] == "invalid_measurement_output":
        raise handoff.HandoffError("invalid_measurement_output")
    return {
        "schema": "nexus.experiment-capture", "version": 1, "origin": spec["origin"],
        "invocation": invocation, "attempt": attempt, "grant": grant_id,
        "proposal_digest": digest(spec), "execution_plan": plan,
        "evidence_references": spec["request"]["evidence"],
        "sandbox": source.SANDBOX_PROFILE, "process": process, "worker_result": interpretation,
    }


def capture(c, invocation, attempt, spec, grant_id, observed):
    """Persist the immutable capture. The caller commits its accepted reference atomically."""
    return c.artifacts.put(_record(invocation, attempt, spec, grant_id, observed))


def _historical_events(c, run, spec, grant):
    expected = {"invocation": run["invocation"], "attempt": run["id"],
                "proposal_digest": run["proposal_digest"], "grant": run["grant_id"]}
    order = []
    for event in c.db.execute(
        "SELECT kind,actor,tick,policy,data FROM events WHERE work=? "
        "AND kind IN ('experiment_decision','experiment_claimed','experiment_launched','experiment_accepted','approval_revoked') "
        "ORDER BY seq", (spec["origin"],),
    ):
        data = json.loads(event["data"])
        if event["kind"] == "approval_revoked":
            if data.get("grant") == grant["id"] and "experiment_accepted" not in order:
                raise handoff.HandoffError("experiment_grant_revoked_before_acceptance")
            continue
        if data.get("invocation") != run["invocation"]:
            continue
        kind = event["kind"]
        if kind == "experiment_decision":
            if (event["actor"] != "operator" or data.get("state") != "approved"
                    or data.get("proposal_digest") != run["proposal_digest"]
                    or data.get("grant") != grant["id"]):
                raise handoff.HandoffError("experiment_decision_mismatch")
        else:
            if event["actor"] != "coordinator" or any(data.get(k) != v for k, v in expected.items()):
                raise handoff.HandoffError("experiment_event_mismatch")
            if kind == "experiment_accepted" and data.get("artifact") != run["artifact"]:
                raise handoff.HandoffError("experiment_acceptance_mismatch")
        if event["policy"] != grant["policy"] or event["tick"] >= grant["expiry"]:
            raise handoff.HandoffError("experiment_event_grant_mismatch")
        order.append(kind)
    if order != ["experiment_decision", "experiment_claimed", "experiment_launched", "experiment_accepted"]:
        raise handoff.HandoffError("experiment_event_chain_incomplete")


def verify(c, run):
    """Reconstruct the measurement from checked bytes and the durable authorization chain."""
    if run["status"] != "accepted":
        raise handoff.HandoffError("experiment_not_accepted")
    invocation = c._row("SELECT * FROM reasoning_invocations WHERE id=?", (run["invocation"],))
    p = c._row("SELECT * FROM experiment_proposals WHERE invocation=?", (run["invocation"],))
    grant = c._row("SELECT * FROM grants WHERE id=?", (run["grant_id"],))
    if (not invocation or invocation["status"] != "completed" or not p or p["state"] != "executed"
            or run["proposal_digest"] != p["digest"] or run["grant_id"] != p["grant_id"] or not grant
            or grant["work"] != invocation["origin"] or grant["action"] != reasoning.PERMISSION
            or grant["binding"] != p["spec"] or grant["principal"] != "operator"):
        raise handoff.HandoffError("experiment_binding_mismatch")
    try:
        spec = json.loads(p["spec"])
        if (digest(spec) != p["digest"] or spec["origin"] != invocation["origin"]
                or spec["invocation"] != invocation["id"] or spec["input_digest"] != invocation["input_digest"]
                or spec["request"] != json.loads(invocation["output"])["experiment"]
                or spec["request"]["target"] != spec["origin"]
                or spec["request"]["profile"] != reasoning.PROFILE
                or spec["execution_plan"]["profile"] != reasoning.PROFILE):
            raise handoff.HandoffError("experiment_specification_mismatch")
        original = handoff.build(c, json.loads(invocation["works"]), include_followups=False)
        if original["evidence_issues"]:
            raise handoff.HandoffError("experiment_parent_evidence_invalid")
        target = original["investigations"][0].copy()
        # Human acceptance after execution only advances the original work's state.
        target["state"] = spec["target_source"]["state"]
        if target != spec["target_source"]:
            raise handoff.HandoffError("experiment_source_binding_mismatch")
        refs = {encode(o["reference"]) for o in original["observations"]}
        if not all(encode(ref) in refs for ref in spec["request"]["evidence"]):
            raise handoff.HandoffError("experiment_parent_reference_mismatch")
        _historical_events(c, run, spec, grant)
        value = c.artifacts.get(run["artifact"])
        process = value["process"]
        raw = {"status": process["status"], "elapsed_ms": process["elapsed_ms"], "exit": process["exit_status"],
               "streams": process["streams"], "source_unchanged": process["source_unchanged"]}
        rebuilt = _record(invocation["id"], run["id"], spec, run["grant_id"], raw)
        if encode(value) != encode(rebuilt):
            raise handoff.HandoffError("experiment_capture_mismatch")
    except EvidenceError as exc:
        raise handoff.HandoffError(f"experiment_{exc}") from exc
    except handoff.HandoffError:
        raise
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise handoff.HandoffError("experiment_record_invalid") from exc

    # Preserve the supervisor/worker boundary in public exports. Raw streams stay private.
    return {**rebuilt, "reference": {"invocation": invocation["id"], "attempt": run["id"],
                                    "sha256": run["artifact"]},
            "process": {k: v for k, v in process.items() if k != "streams"}}
