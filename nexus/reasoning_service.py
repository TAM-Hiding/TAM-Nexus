"""Operator-only coordination of one bounded, informational reasoning invocation."""

import json
from contextlib import ExitStack
from pathlib import Path

from . import handoff, reasoning
from .coordinator import OPERATOR, Rejected, _valid_id
from .linux_worker import LinuxRunner, RunnerBusy, dispatch_lock
from .storage import digest, encode, transaction

WORKER = "deterministic-investigator-v1"
ACTION = "approve_read_only_experiment"


def _operator(principal):
    if principal is not OPERATOR:
        raise Rejected("operator_required")


def _invocation(c, invocation):
    row = c._row("SELECT * FROM reasoning_invocations WHERE id=?", (invocation,)) if _valid_id(invocation) else None
    c._require(row is not None, "unknown_reasoning_invocation")
    return row


def _request(c, row):
    works = json.loads(row["works"])
    policy = c._settings()["policy"]
    if row["policy"] != policy or any(c._revision(c._work(work))["policy"] != policy for work in works):
        raise Rejected("stale_reasoning_policy")
    request = reasoning.make_input(row["objective"], row["id"], handoff.build(c, works, include_followups=False))
    if row["input_digest"] is not None and digest(request) != row["input_digest"]:
        raise Rejected("reasoning_input_changed")
    return request


def start(c, principal, objective, works, command, implementation=WORKER):
    # Preserve fingerprints for NX-007 commands retried after migration.
    options = {} if implementation == WORKER else {"implementation": implementation}
    result = c.execute(principal, command, "start-reasoning", works=works, objective=objective, **options)
    if not result["ok"]:
        raise Rejected(result["reason"])
    return result["invocation"]


def _issue(c, objective, works, implementation=WORKER):
    """Operator command handler. All writes share its deduplication transaction."""
    c._require(type(works) is list and 1 <= len(works) <= handoff.MAX_WORKS
               and all(_valid_id(w) for w in works) and len(set(works)) == len(works),
               "invalid_reasoning_work_selection")
    for work in works:
        c._work(work)
        count = c._row("SELECT COUNT(*) FROM reasoning_charges WHERE work=?", (work,))[0]
        c._require(count < reasoning.MAX_INVOCATIONS, "reasoning_budget_exhausted")
    c._require(isinstance(objective, str) and 0 < len(objective) <= 600, "invalid_reasoning_objective")
    from . import model_disclosure
    c._require(type(implementation) is str and implementation in (WORKER, *model_disclosure.IMPLEMENTATIONS),
               "unsupported_reasoning_implementation")
    invocation = c.db.execute(
        "INSERT INTO reasoning_invocations(origin,objective,works,policy,implementation,status) VALUES(?,?,?,?,?,'issued')",
        (works[0], objective, encode(works), c._settings()["policy"], implementation)).lastrowid
    request = _request(c, _invocation(c, invocation))
    if implementation != WORKER:
        from . import demo_disclosure
        demo_disclosure.validate(request)
    key = digest(request)
    for work in works:
        rows = c.db.execute(
            "SELECT i.* FROM reasoning_invocations i JOIN reasoning_charges r ON i.id=r.invocation "
            "WHERE r.work=? AND i.status IN ('issued','running')", (work,)).fetchall()
        for previous in rows:
            _stop(c, previous, "superseded")
    c.db.execute("UPDATE reasoning_invocations SET input_digest=? WHERE id=?", (key, invocation))
    c.db.executemany("INSERT INTO reasoning_charges VALUES(?,?)", [(invocation, w) for w in works])
    c._event("reasoning_authorized", works[0], actor="operator", invocation=invocation,
             input_digest=key, works=works, worker=implementation)
    return invocation


def _stop(c, row, reason):
    c.db.execute("UPDATE reasoning_invocations SET status='interrupted',reason=? WHERE id=?",
                 (reason, row["id"]))
    c._event("reasoning_interrupted", row["origin"], invocation=row["id"], reason=reason)


def fence_active(c, reason):
    """Called inside existing operator recovery and policy transactions."""
    from .experiment_execution import fence_runs
    fence_runs(c, reason)
    for row in c.db.execute("SELECT * FROM reasoning_invocations WHERE status IN ('issued','running')").fetchall():
        _stop(c, row, reason)


def cancel(c, principal, invocation):
    _operator(principal)
    with transaction(c.db):
        row = _invocation(c, invocation)
        c._require(row["status"] in ("issued", "running"), "reasoning_not_active")
        _stop(c, row, "operator_cancelled")
    return report(c, principal, invocation)


def _accept(c, invocation, output):
    row = _invocation(c, invocation)
    c._require(row["status"] == "running", "reasoning_lease_fenced")
    request = _request(c, row)
    result = reasoning.validate(request, output)
    if result["experiment"] is not None:
        # Origin, authority, limits and all source identities come from the coordinator.
        target = request["handoff"]["investigations"][0]
        spec = {"version": 1, "invocation": invocation, "worker": row["implementation"], "origin": row["origin"],
                "input_digest": row["input_digest"], "target_source": target,
                "permission": ACTION, "request": result["experiment"],
                "limits": {"max_runs": 1, "wall_seconds": 15, "output_bytes": 8192}}
        from .experiment_execution import plan_for
        plan = plan_for(c, row, spec)
        if plan is not None:
            spec["execution_plan"] = plan
        key = digest(spec)
        c.db.execute("INSERT INTO experiment_proposals VALUES(?,?,?,'proposed',NULL)",
                     (invocation, encode(spec), key))
        c._event("experiment_proposed", row["origin"], invocation=invocation, proposal_digest=key)
    c.db.execute("UPDATE reasoning_invocations SET status='completed',output=? WHERE id=?",
                 (encode(result), invocation))
    c._event("reasoning_accepted", row["origin"], invocation=invocation,
             output_digest=digest(result), classification="unverified_reasoning")


def run(c, principal, invocation):
    _operator(principal)
    if _invocation(c, invocation)["implementation"] != WORKER:
        from .model_disclosure import invoke
        return invoke(c, principal, invocation)
    runner = LinuxRunner(script=Path(reasoning.__file__).read_bytes(), timeout=reasoning.MAX_SECONDS)
    try:
        with dispatch_lock(c.root) as fd, ExitStack() as stack:
            with transaction(c.db):
                row = _invocation(c, invocation)
                c._require(row["status"] == "issued", "reasoning_lease_fenced")
                _request(c, row)
                c.db.execute("UPDATE reasoning_invocations SET status='running' WHERE id=?", (invocation,))
                c._event("reasoning_claimed", row["origin"], invocation=invocation)
            # The reservation survives process death after launch. Holding the short
            # launch gate serializes disclosure with a concurrent cancel or policy change.
            with transaction(c.db):
                row = _invocation(c, invocation)
                c._require(row["status"] == "running", "reasoning_lease_fenced")
                request = _request(c, row)
                process = stack.enter_context(runner.launch(
                    fd, None, reasoning_input=encode(request).encode("ascii")))
                c._event("reasoning_dispatched", row["origin"], invocation=invocation)
            outcome = runner.wait(process, lambda: _invocation(c, invocation)["status"] == "running")
            with transaction(c.db):
                c._require(_invocation(c, invocation)["status"] == "running", "reasoning_lease_fenced")
                if outcome["status"] != "observed":
                    _stop(c, row, outcome["status"])
                else:
                    try:
                        _accept(c, invocation, outcome["output"])
                    except (Rejected, ValueError):
                        _stop(c, row, "reasoning_result_rejected")
    except RunnerBusy as exc:
        raise Rejected("worker_dispatch_busy") from exc
    return report(c, principal, invocation)


def _proposal(c, row, expected):
    proposal = c._row("SELECT * FROM experiment_proposals WHERE invocation=?", (row["id"],))
    c._require(proposal is not None, "no_experiment_proposal")
    c._require(proposal["digest"] == expected == digest(json.loads(proposal["spec"])),
               "proposal_digest_mismatch")
    return proposal


def decide(c, principal, invocation, expected_digest, approve):
    _operator(principal)
    c._require(type(approve) is bool, "invalid_experiment_decision")
    with transaction(c.db):
        row = _invocation(c, invocation)
        proposal = _proposal(c, row, expected_digest)
        c._require(proposal["state"] == "proposed", "experiment_already_decided")
        grant = None
        if approve:
            _request(c, row)
            settings = c._settings()
            grant = c.db.execute("INSERT INTO grants(work,action,binding,principal,policy,expiry) "
                                 "VALUES(?,?,?,'operator',?,?)",
                                 (row["origin"], ACTION, proposal["spec"], settings["policy"],
                                  settings["tick"] + 10)).lastrowid
        state = "approved" if approve else "rejected"
        c.db.execute("UPDATE experiment_proposals SET state=?,grant_id=? WHERE invocation=?",
                     (state, grant, invocation))
        c._event("experiment_decision", row["origin"], actor="operator", invocation=invocation,
                 proposal_digest=expected_digest, state=state, grant=grant)
    return report(c, principal, invocation)


def execute(c, principal, invocation, expected_digest):
    _operator(principal)
    from .experiment_execution import dispatch
    return dispatch(c, principal, invocation, expected_digest)


def _grant_status(c, row, proposal):
    if proposal is None or proposal["grant_id"] is None:
        return "not_granted"
    grant = c._row("SELECT * FROM grants WHERE id=?", (proposal["grant_id"],))
    if (grant is None or grant["principal"] != "operator" or grant["action"] != ACTION
            or grant["work"] != row["origin"] or grant["binding"] != proposal["spec"]
            or digest(json.loads(proposal["spec"])) != proposal["digest"]):
        return "binding_mismatch"
    if grant["revoked"]:
        return "revoked"
    settings = c._settings()
    if settings["policy"] != grant["policy"]:
        return "stale_policy"
    if settings["tick"] >= grant["expiry"]:
        return "expired"
    try:
        _request(c, row)
    except (ValueError, Rejected):
        return "stale_evidence"
    return "current"


def report(c, principal, invocation):
    _operator(principal)
    with transaction(c.db, write=False):
        row = _invocation(c, invocation)
        p = c._row("SELECT * FROM experiment_proposals WHERE invocation=?", (invocation,))
        from .experiment_execution import execution_report
        execution = execution_report(c, invocation)
        supported = p is not None and "execution_plan" in json.loads(p["spec"])
        from . import conclusions, model_disclosure
        return {**conclusions.report(c, row), **model_disclosure.audit(c, row),
                "invocation": invocation, "status": row["status"], "reason": row["reason"],
                "origin": row["origin"], "input_digest": row["input_digest"],
                "reasoning": json.loads(row["output"]) if row["output"] else None,
                "classification": "unverified_reasoning", "proposal": json.loads(p["spec"]) if p else None,
                "proposal_digest": p["digest"] if p else None, "proposal_state": p["state"] if p else None,
                "grant_status": _grant_status(c, row, p),
                "grant": p["grant_id"] if p else None, "execution": execution,
                "execution_support": reasoning.PROFILE if supported else "no_compatible_execution_profile"}
