"""Operator disclosure and single-use dispatch for optional reasoning implementations."""

import hashlib
import json
from contextlib import contextmanager

from . import reasoning_service as service
from .coordinator import Rejected
from .storage import digest, encode, transaction

IMPLEMENTATIONS = ("mock-v1", "llama-unix-v1")
ACTION = "disclose_reasoning_input"


def _payload(c, row):
    from . import model_adapter

    implementation = row["implementation"]
    c._require(implementation in IMPLEMENTATIONS, "no_inference_disclosure")
    request = service._request(c, row)
    body = model_adapter.prepare(request)
    binding = {"version": 1, "invocation": row["id"], "work": row["origin"],
               "policy": row["policy"], "implementation": implementation,
               "destination": model_adapter.DESTINATIONS[implementation],
               "recipient_uid": model_adapter.recipient(implementation),
               "input_sha256": row["input_digest"], "payload_sha256": hashlib.sha256(body).hexdigest()}
    return binding, body


def preview(c, principal, invocation):
    service._operator(principal)
    with transaction(c.db, write=False):
        row = service._invocation(c, invocation)
        c._require(row["status"] == "issued", "reasoning_not_issued")
        binding, body = _payload(c, row)
        return {**binding, "payload": body.decode("utf-8"), "disclosure_sha256": digest(binding)}


def _grant_current(c, row, binding):
    return c._row("SELECT grants.id FROM grants JOIN settings ON settings.id=1 "
                  "WHERE grants.id=? AND grants.work=? AND grants.action=? AND grants.binding=? "
                  "AND grants.principal='operator' AND grants.revoked=0 AND grants.policy=settings.policy "
                  "AND grants.expiry>settings.tick",
                  (row["disclosure_grant"], row["origin"], ACTION, encode(binding))) is not None


def disclose(c, principal, invocation, expected):
    service._operator(principal)
    with transaction(c.db):
        row = service._invocation(c, invocation)
        c._require(row["status"] == "issued", "reasoning_not_issued")
        binding, _ = _payload(c, row)
        c._require(digest(binding) == expected, "disclosure_digest_mismatch")
        grant = row["disclosure_grant"]
        if grant is None:
            grant = c.db.execute("INSERT INTO grants(work,action,binding,principal,policy,expiry) "
                                 "VALUES(?,?,?,'operator',?,?)",
                                 (row["origin"], ACTION, encode(binding), row["policy"],
                                  c._settings()["tick"] + 10)).lastrowid
            c.db.execute("UPDATE reasoning_invocations SET disclosure_grant=? WHERE id=?", (grant, invocation))
            c._event("inference_disclosure_authorized", row["origin"], actor="operator",
                     invocation=invocation, grant=grant, disclosure_sha256=expected,
                     implementation=binding["implementation"], recipient_uid=binding["recipient_uid"],
                     destination=binding["destination"], payload_sha256=binding["payload_sha256"])
        else:
            c._require(_grant_current(c, row, binding), "stale_disclosure_grant")
        return {"invocation": invocation, "grant": grant, "disclosure_sha256": expected}


def audit(c, row):
    implementation = row["implementation"]
    grant = c._row("SELECT binding FROM grants WHERE id=?", (row["disclosure_grant"],))
    return {"implementation": implementation, "reasoning_input_sha256": row["input_digest"],
            "disclosure_grant": row["disclosure_grant"],
            "disclosure": json.loads(grant["binding"]) if grant else None,
            "transport_state": row["transport_state"],
            "inference_response": json.loads(row["response_metadata"]) if row["response_metadata"] else None}


def invoke(c, principal, invocation):
    from . import model_adapter

    service._operator(principal)
    # Commit the one-use claim before opening any transport. A lost reply or
    # process crash leaves a fenced invocation even if disclosure already occurred.
    with transaction(c.db):
        row = service._invocation(c, invocation)
        c._require(row["status"] == "issued", "reasoning_lease_fenced")
        binding, body = _payload(c, row)
        c._require(_grant_current(c, row, binding), "disclosure_approval_required")
        c.db.execute("UPDATE reasoning_invocations SET status='running',transport_state='outcome_uncertain' WHERE id=?", (invocation,))
        c._event("reasoning_claimed", row["origin"], invocation=invocation, implementation=row["implementation"])

    def current():
        latest = service._invocation(c, invocation)
        return latest["status"] == "running" and _grant_current(c, latest, binding)

    @contextmanager
    def send_gate():
        # Serialize the bounded send with cancellation, grant revocation and policy changes.
        with transaction(c.db):
            c._require(current(), "inference_fenced")
            renewed, exact = _payload(c, service._invocation(c, invocation))
            c._require(binding == renewed and body == exact, "disclosure_changed")
            yield
            c._event("inference_request_sent", row["origin"], invocation=invocation,
                     grant=row["disclosure_grant"], payload_sha256=binding["payload_sha256"])

    try:
        output, metadata = model_adapter.infer(row["implementation"], c.root, body, current,
                                               send_gate=send_gate, approved_uid=binding["recipient_uid"])
    except (Rejected, ValueError, OSError) as exc:
        # Retain only fixed diagnostic codes. The durable claim stays consumed.
        reason = str(exc) if str(exc) in ("model_timeout", "model_cancelled", "model_response_invalid",
                                         "model_transport_unavailable", "model_endpoint_identity_invalid",
                                         "model_peer_mismatch", "model_incomplete_response") else "inference_incomplete_or_uncertain"
        with transaction(c.db):
            latest = service._invocation(c, invocation)
            if latest["status"] == "running":
                if reason == "model_response_invalid":
                    c.db.execute("UPDATE reasoning_invocations SET transport_state='response_rejected' WHERE id=?",
                                 (invocation,))
                service._stop(c, latest, reason)
        return service.report(c, principal, invocation)
    with transaction(c.db):
        latest = service._invocation(c, invocation)
        if latest["status"] == "running":
            if not current():
                service._stop(c, latest, "inference_authority_lapsed")
            else:
                c.db.execute("UPDATE reasoning_invocations SET response_metadata=?,transport_state='response_received' WHERE id=?",
                             (encode(metadata), invocation))
                try:
                    service._accept(c, invocation, output)
                except (ValueError, Rejected):
                    service._stop(c, latest, "reasoning_result_rejected")
    return service.report(c, principal, invocation)
