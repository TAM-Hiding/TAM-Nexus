"""One approved follow-up, one durable dispatch claim, one fixed Linux profile."""

import json
from contextlib import ExitStack

from . import pilot, reasoning, source
from .coordinator import Rejected
from .linux_worker import LinuxRunner, RunnerBusy, dispatch_lock
from .storage import EvidenceError, digest, transaction


def plan_for(c, row, spec):
    request = c._revision(c._work(row["origin"]))
    experiment = spec["request"]
    if (request.get("mode") != "pilot" or experiment["profile"] != reasoning.PROFILE
            or experiment["target"] != row["origin"]):
        return None
    from . import measurement
    manifest = pilot.manifest(request["manifest"])
    return {"version": 1, "profile": reasoning.PROFILE, "manifest_sha256": digest(manifest),
            "resource": request["resource"], "source_revision": request["base"],
            "source_sha256": request["snapshot"], "dependency_sha256": manifest["dependency_digest"],
            "argv": measurement.ARGV, "script_sha256": measurement.script_digest(),
            "limits": dict(pilot.LIMITS)}


def _row(c, invocation):
    return c._row("SELECT * FROM experiment_runs WHERE invocation=?", (invocation,))


def execution_report(c, invocation):
    run = _row(c, invocation)
    if run is None:
        return None
    result = {"attempt": run["id"], "status": run["status"], "reason": run["reason"],
              "artifact": run["artifact"]}
    if run["status"] == "accepted":
        from .experiment_evidence import verify
        try:
            result["evidence"] = verify(c, run)
        except (ValueError, EvidenceError):
            result.update(status="evidence_invalid", reason="experiment_evidence_invalid")
    return result


def _event(c, origin, run, kind, **extra):
    c._event(kind, origin, attempt=run["id"], invocation=run["invocation"],
             proposal_digest=run["proposal_digest"], grant=run["grant_id"], **extra)


def _finish(c, origin, run, status, reason):
    c.db.execute("UPDATE experiment_runs SET status=?,reason=? WHERE id=?", (status, reason, run["id"]))
    _event(c, origin, run, "experiment_failed", status=status, reason=reason)


def fence_runs(c, reason):
    rows = c.db.execute("SELECT r.*,i.origin FROM experiment_runs r JOIN reasoning_invocations i "
                        "ON r.invocation=i.id WHERE r.status IN ('claimed','launched')").fetchall()
    for run in rows:
        _finish(c, run["origin"], run, "uncertain", reason)


def _authorized(c, invocation, expected):
    from . import reasoning_service as service

    row = service._invocation(c, invocation)
    proposal = service._proposal(c, row, expected)
    c._require(proposal["state"] == "approved", "experiment_approval_required")
    c._require(service._grant_status(c, row, proposal) == "current", "stale_experiment_approval")
    spec = json.loads(proposal["spec"])
    plan = spec.get("execution_plan")
    c._require(plan is not None and plan == plan_for(c, row, spec), "no_compatible_execution_profile")
    return row, proposal, spec


def _live(c, run):
    # This cheap gate also runs while the supervisor waits and copies staged inputs.
    return c._row(
        "SELECT 1 FROM experiment_runs r JOIN experiment_proposals p ON p.invocation=r.invocation "
        "JOIN grants g ON g.id=r.grant_id JOIN settings s ON s.id=1 "
        "WHERE r.id=? AND r.status IN ('claimed','launched') AND p.state='approved' "
        "AND p.grant_id=g.id AND p.digest=r.proposal_digest AND p.spec=g.binding "
        "AND g.revoked=0 AND g.policy=s.policy AND g.expiry>s.tick", (run["id"],)) is not None


def dispatch(c, principal, invocation, expected):
    from . import reasoning_service as service

    service._operator(principal)
    # The proposal digest is also the one-use effect identity. A replay only reads
    # the existing attempt, including uncertain or failed outcomes.
    with transaction(c.db, write=False):
        service._proposal(c, service._invocation(c, invocation), expected)
        exists = _row(c, invocation) is not None
    if not exists:
        try:
            with dispatch_lock(c.root) as lock, ExitStack() as stack:
                with transaction(c.db):
                    if _row(c, invocation) is None:
                        row, proposal, spec = _authorized(c, invocation, expected)
                        c.db.execute("INSERT INTO experiment_runs(invocation,proposal_digest,grant_id,status) "
                                     "VALUES(?,?,?,'claimed')", (invocation, expected, proposal["grant_id"]))
                        run = _row(c, invocation)
                        _event(c, row["origin"], run, "experiment_claimed")
                    else:
                        run = None
                if run is not None:
                    _perform(c, stack, lock, row["origin"], run, spec)
        except RunnerBusy as exc:
            raise Rejected("worker_dispatch_busy") from exc
    return service.report(c, principal, invocation)


def _perform(c, stack, lock, origin, run, spec):
    from . import experiment_evidence

    invocation, expected = run["invocation"], run["proposal_digest"]
    runner = LinuxRunner()
    def current():
        return _live(c, run)

    try:
        request, staged = c._stage_pilot(stack, origin, current)
        with transaction(c.db):
            c._require(current(), "experiment_fenced")
            _authorized(c, invocation, expected)
            c._pilot_inputs(request)
            # Mark potentially launched before Popen. A hard crash on either
            # side of process creation leaves the durable claim non-repeatable.
            c.db.execute("UPDATE experiment_runs SET status='launched' WHERE id=?", (run["id"],))
            process = stack.enter_context(runner.launch(
                lock, None, pilot=staged, crosscheck=True,
                crosscheck_sha256=spec["execution_plan"]["script_sha256"]))
            _event(c, origin, run, "experiment_launched")
        observed = runner.wait(process, current, pilot=True)
        with transaction(c.db):
            c._require(current(), "experiment_fenced")
            _authorized(c, invocation, expected)
            c._pilot_inputs(request)
            observed["source_unchanged"] = True
            if observed["status"] != "test_exited":
                # Supervisor outcome is typed. Do not accept a timeout, crash or setup failure.
                status = ("uncertain" if observed["status"] in
                          ("worker_cleanup_pending", "supervisor_failed", "supervisor_output_invalid",
                           "sandbox_status_invalid") else "failed")
                _finish(c, origin, run, status, observed["status"])
                return
            key = experiment_evidence.capture(c, invocation, run["id"], spec, run["grant_id"], observed)
            c.db.execute("UPDATE experiment_runs SET status='accepted',artifact=? WHERE id=?", (key, run["id"]))
            c.db.execute("UPDATE experiment_proposals SET state='executed' WHERE invocation=?", (invocation,))
            _event(c, origin, run, "experiment_accepted", artifact=key)
    except (source.SourceError, Rejected, EvidenceError, ValueError, OSError) as exc:
        with transaction(c.db):
            latest = _row(c, invocation)
            if latest["status"] in ("claimed", "launched"):
                reason = "experiment_io_failure" if isinstance(exc, OSError) else str(exc)
                status = "uncertain" if latest["status"] == "launched" and isinstance(exc, OSError) else "failed"
                _finish(c, origin, run, status, reason)
