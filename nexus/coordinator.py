"""Single-writer state machine for the NX-002 fixture workflow."""

from dataclasses import dataclass
from contextlib import ExitStack
import inspect
import json
import re

from .fixtures import (
    BASE, DEFAULT_WORKERS, PHASES, RESOURCE, SCOPE, FixtureBroker, archive_lookup,
    run_fixture,
)
from . import source
from .linux_worker import RunnerBusy, dispatch_lock
from .storage import (
    COORDINATOR_MIGRATIONS, SCHEMA, Artifacts, EvidenceError, InjectedCrash, digest, encode, open_database,
    private_directory, transaction,
)

OPERATOR = object()
TERMINAL = frozenset(("completed", "rejected", "declined", "failed", "cancelled"))
COMMANDS = frozenset((
    "message", "submit", "propose", "decide", "revoke", "claim", "run", "resume",
    "recover", "cancel", "accept", "rework", "publish", "reconcile", "advance",
    "policy", "register", "archive", "authorize-source", "submit-source",
))
GLOBAL = frozenset(("message", "submit", "recover", "advance", "policy", "register",
                    "authorize-source", "submit-source"))
LEASE_TICKS = 3


def _valid_id(value):
    return type(value) is int and 0 < value < 2**63


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: int
    work: int
    generation: int
    phase: str
    revision: str
    candidate: str | None
    worker: str
    expires: int
    mode: str
    iteration: int


class Coordinator:
    def __init__(self, root, *, runner=None):
        root = private_directory(root)
        self.root = root.resolve()
        self.runner = runner
        self.artifacts = Artifacts(root / "artifacts")
        self.db = open_database(root / "nexus.sqlite3", SCHEMA, COORDINATOR_MIGRATIONS)
        try:
            self.broker = FixtureBroker(root)
            with transaction(self.db):
                for name, priority, caps, allowed, version in DEFAULT_WORKERS:
                    self.db.execute("INSERT OR IGNORE INTO workers VALUES (?,?,?,?,?)",
                                    (name, priority, encode(caps), allowed, version))
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.broker.close()
        self.db.close()

    def _row(self, query, values=()):
        return self.db.execute(query, values).fetchone()

    def _settings(self):
        return self._row("SELECT * FROM settings")

    def _event(self, kind, work=None, *, actor="coordinator", command=None, **data):
        settings = self._settings()
        self.db.execute("INSERT INTO events(tick,kind,work,command,actor,policy,data)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (settings["tick"], kind, work, command, actor,
                         settings["policy"], encode(data)))

    def _work(self, work):
        self._require(_valid_id(work), "unknown_work")
        row = self._row("SELECT * FROM work WHERE id = ?", (work,))
        if row is None:
            raise Rejected("unknown_work")
        return dict(row) | {"data": json.loads(row["data"])}

    def _revision(self, work):
        return json.loads(self._row("SELECT body FROM revisions WHERE digest = ?",
                                    (work["revision"],))[0])

    def _save(self, work, state, *, phase=None, reason, **changes):
        work["data"].update(changes, reason=reason)
        previous = work["state"]
        work["version"] += 1
        work["state"] = state
        if phase is not None:
            work["phase"] = phase
        self.db.execute("UPDATE work SET state=?, phase=?, version=?, data=? WHERE id=?",
                        (state, work["phase"], work["version"], encode(work["data"]), work["id"]))
        self._event("transition", work["id"], previous=previous, state=state,
                    phase=work["phase"], revision=work["revision"],
                    version=work["version"], reason=reason)

    def _new(self, phase, binding, state, *, budget, ttl, profile=None):
        cursor = self.db.execute("INSERT INTO work(version,state,phase,revision,data)"
                                 " VALUES (1,?,?,?,?)", (state, phase, "", "{}"))
        work = cursor.lastrowid
        revision = self._write_revision(work, binding)
        data = {"budget": budget, "used": 0, "deadline": self._settings()["tick"] + ttl,
                "generation": 0, "iteration": 1, "reason": "created"}
        if profile:
            data["worker_profile"] = profile
        self.db.execute("UPDATE work SET revision=?,data=? WHERE id=?",
                        (revision, encode(data), work))
        self._event("work_created", work, revision=revision, state=state, phase=phase)
        return self._work(work)

    def _write_revision(self, work, body):
        body = {"work": work, **body}
        revision = digest(body)
        self.db.execute("INSERT INTO revisions VALUES (?,?,?)", (revision, work, encode(body)))
        return revision

    def execute(self, principal, command, action, *, work=None, version=None, **args):
        """Trusted caller supplies principal. Payload values cannot select a principal."""
        if (not isinstance(command, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", command)
                or not isinstance(action, str) or len(action) > 40
                or (work is not None and not _valid_id(work))):
            return {"ok": False, "reason": "invalid_command"}
        actor = "operator" if principal is OPERATOR else "untrusted"
        linux_dispatch = action == "run" and actor == "operator" and self.runner is not None
        try:
            request = [actor, action, work, version, args]
            if linux_dispatch:
                request.append("linux")
            serialized = encode(request)
            if len(serialized) > 16384:
                raise ValueError
            fingerprint = digest(serialized)
        except (TypeError, ValueError, RecursionError):
            return {"ok": False, "reason": "invalid_command"}

        if linux_dispatch:
            return self._run_linux(command, work, version, args, fingerprint)

        armed = None
        if action == "publish" and actor == "operator":
            # Arm in its own durable transaction BEFORE the fake provider can act.
            # The subsequent guarded send is still part of ordinary command handling.
            with transaction(self.db):
                armed = self._arm_for_command(command, fingerprint, work, version, args)

        with transaction(self.db):
            previous = self._row("SELECT * FROM commands WHERE id=?", (command,))
            if previous:
                if previous["fingerprint"] == fingerprint:
                    return json.loads(previous["response"])
                self._event("command_conflict", self._existing_work(work), actor=actor,
                            command=command)
                return {"ok": False, "reason": "command_conflict"}
            reservation = self._row("SELECT work,fingerprint,status FROM effects WHERE command=?",
                                    (command,))
            if reservation is not None and armed is None:
                if reservation["fingerprint"] != fingerprint:
                    self._event("command_conflict", reservation["work"], actor=actor, command=command)
                    return {"ok": False, "reason": "command_conflict"}
                reason = "operation_in_progress" if reservation["status"] == "armed" else "reconcile_report"
                return {"ok": False, "reason": reason, "effect_status": reservation["status"]}
            reserved = self._linux_replay(command, fingerprint, actor)
            if reserved is not None:
                return reserved
            self.db.execute("SAVEPOINT command_body")
            try:
                if principal is not OPERATOR:
                    raise Rejected("operator_required")
                if action not in COMMANDS:
                    raise Rejected("unsupported_action")
                handler = getattr(self, "_cmd_" + action.replace("-", "_"))
                expected = version + 1 if armed is not None else version
                params = [] if action in GLOBAL else [self._checked_work(work, expected)]
                if action == "publish":
                    params.extend((command, armed))
                try:
                    inspect.signature(handler).bind(*params, **args)
                except TypeError as exc:
                    raise Rejected("invalid_arguments") from exc
                result = handler(*params, **args)
            except Rejected as exc:
                self.db.execute("ROLLBACK TO command_body")
                if armed is not None:
                    self._stop_unsent(armed)
                result = {"ok": False, "reason": str(exc)}
            self.db.execute("RELEASE command_body")
            self._event("command", self._existing_work(work), actor=actor, command=command,
                        action=action if action in COMMANDS else "unsupported",
                        ok=result["ok"], reason=result.get("reason"))
            self.db.execute("INSERT INTO commands VALUES (?,?,?)", (command, fingerprint, encode(result)))
            return result

    def _existing_work(self, work):
        return work if self._row("SELECT 1 FROM work WHERE id = ?", (work,)) else None

    def _checked_work(self, work, version):
        current = self._work(work)
        if type(version) is not int or current["version"] != version:
            raise Rejected("stale_work_version")
        return current

    @staticmethod
    def _require(condition, reason):
        if not condition:
            raise Rejected(reason)

    @staticmethod
    def _ok(work=None, **data):
        if work:
            data.update(work=work["id"], version=work["version"], state=work["state"])
        return {"ok": True, **data}

    def _cmd_message(self, text):
        self._require(isinstance(text, str) and 0 < len(text) <= 4096, "invalid_message")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise Rejected("invalid_message") from exc
        cursor = self.db.execute("INSERT INTO messages(text,tick) VALUES (?,?)",
                                 (text, self._settings()["tick"]))
        self._event("message_recorded", actor="operator", message=cursor.lastrowid,
                    content_digest=digest(text))
        return self._ok(message=cursor.lastrowid)

    def _cmd_submit(self, message, resource=RESOURCE, base=BASE, mode="reproduced",
                    budget=10, ttl=30):
        self._require(_valid_id(message), "unknown_message")
        record = self._row("SELECT text FROM messages WHERE id=?", (message,))
        self._require(record is not None, "unknown_message")
        self._require(resource == RESOURCE and base == BASE, "resource_or_base_denied")
        self._require(mode in ("reproduced", "inconclusive"), "invalid_fixture")
        self._validate_budget(budget, ttl)
        work = self._new("investigate", {
            "kind": "investigation", "message": message, "message_digest": digest(record["text"]),
            "resource": resource, "base": base, "scope": ["read", "sandbox_fixture"],
            "policy": self._settings()["policy"], "mode": mode,
        }, "queued", budget=budget, ttl=ttl)
        return self._ok(work, revision=work["revision"])

    def _cmd_authorize_source(self, checkout, commit):
        try:
            resource, files = source.snapshot(checkout, commit)
        except source.SourceError as exc:
            raise Rejected(str(exc)) from exc
        snapshot = source.snapshot_digest(files)
        values = (checkout, resource, commit, snapshot, self._settings()["policy"])
        self.db.execute("INSERT OR IGNORE INTO source_authorizations(path,resource,revision,snapshot,policy)"
                        " VALUES (?,?,?,?,?)", values)
        row = self._row("SELECT id FROM source_authorizations WHERE path=? AND resource=? "
                        "AND revision=? AND snapshot=? AND policy=?", values)
        self._event("source_authorized", actor="operator", authorization=row["id"],
                    resource=resource, revision=commit, snapshot=snapshot)
        return self._ok(authorization=row["id"], resource=resource, snapshot=snapshot)

    def _cmd_submit_source(self, message, authorization, checkout, commit, budget=3, ttl=30):
        self._require(_valid_id(message) and _valid_id(authorization), "invalid_source_request")
        record = self._row("SELECT text FROM messages WHERE id=?", (message,))
        self._require(record is not None, "unknown_message")
        row = self._row("SELECT * FROM source_authorizations WHERE id=?", (authorization,))
        self._require(row is not None and row["policy"] == self._settings()["policy"]
                      and row["path"] == checkout and row["revision"] == commit,
                      "source_not_authorized")
        request = {"kind": "source_investigation", "message": message,
                   "message_digest": digest(record["text"]), "policy": row["policy"],
                   "resource": row["resource"], "base": commit, "snapshot": row["snapshot"],
                   "source_authorization": authorization, "mode": "source",
                   "scope": ["read_pinned_source", source.PROFILE]}
        self._validate_budget(budget, ttl)
        try:
            self._source_files(request)
        except source.SourceError as exc:
            raise Rejected(str(exc)) from exc
        work = self._new("investigate", request, "queued", budget=budget, ttl=ttl, profile="linux")
        return self._ok(work, revision=work["revision"])

    def _source_files(self, request):
        row = self._row("SELECT * FROM source_authorizations WHERE id=?",
                        (request["source_authorization"],))
        if (row is None or row["policy"] != self._settings()["policy"]
                or row["resource"] != request["resource"] or row["revision"] != request["base"]
                or row["snapshot"] != request["snapshot"]):
            raise source.SourceError("source_not_authorized")
        _, files = source.snapshot(row["path"], row["revision"], row["resource"])
        if source.snapshot_digest(files) != request["snapshot"]:
            raise source.SourceError("source_snapshot_drift")
        return files

    def _validate_budget(self, budget, ttl):
        self._require(type(budget) is int and 1 <= budget <= 100
                      and type(ttl) is int and 1 <= ttl <= 1000, "invalid_budget")

    def _cmd_propose(self, work, topic="fixture-topic", budget=10, ttl=30, prior_work=None):
        self._require(self._revision(work)["kind"] != "source_investigation", "source_readonly")
        self._require(work["phase"] == "investigate" and work["state"] in
                      ("awaiting_acceptance", "completed", "rejected"), "investigation_required")
        evidence = self._evidence(work)
        self._require(evidence is not None and any(
            item.get("reproduced") is True for item in evidence.values()), "confirmed_evidence_required")
        self._require(topic in ("fixture-topic", "fixture-other"), "invalid_topic")
        if prior_work is not None:
            self._require(_valid_id(prior_work), "invalid_prior_work")
            prior = self._work(prior_work)
            self._require(prior["state"] in TERMINAL
                          and self._revision(prior).get("investigation") == work["id"],
                          "invalid_prior_work")
        self._validate_budget(budget, ttl)
        source = self._revision(work)
        child = self._new("implement", {
            "kind": "implementation", "investigation": work["id"],
            "investigation_revision": work["revision"], "evidence": list(evidence),
            "prior_work": prior_work,
            "message": source["message"], "message_digest": source["message_digest"],
            "resource": source["resource"], "base": source["base"], "scope": SCOPE,
            "target": f"{RESOURCE}/{topic}", "mode": source["mode"],
            "expected": "X/Y matches control after refresh", "risk": "synthetic fixture only",
            "plan": ["verify", "review"],
        }, "awaiting_approval", budget=budget, ttl=ttl, profile=work["data"].get("worker_profile"))
        return self._ok(child, binding=self._binding(child, "implement"))

    def _binding(self, work, action):
        revision = self._revision(work)
        binding = {"action": action, "revision": work["revision"],
                   "resource": revision["resource"], "base": revision["base"],
                   "scope": revision["scope"]}
        if action == "implement":
            binding["operations"] = ["implement", "verify", "review"]
        if action == "publish":
            binding.update(candidate=work["data"].get("candidate"),
                           target=revision["target"], base_ref="main",
                           expected_topic_head=None, operations=["push", "draft"])
        return binding

    def _cmd_decide(self, work, binding, allow, ttl=10):
        phase = work["phase"]
        self._require(work["state"] == "awaiting_approval" and phase in ("implement", "publish"),
                      "approval_gate_required")
        self._require(type(allow) is bool and type(ttl) is int and 1 <= ttl <= 100,
                      "invalid_decision")
        self._require(encode(binding) == encode(self._binding(work, phase)), "binding_mismatch")
        if phase == "publish":
            self._require(self._review_valid(work), "review_not_current")
            self._require(self._evidence(work) is not None, "evidence_unavailable")
        self._event("approval_decision", work["id"], actor="operator", binding=binding,
                    allowed=allow)
        if not allow:
            if phase == "publish":
                return self._handoff(work, "publication_declined")
            else:
                self._save(work, "declined", reason="implementation_declined")
            return self._ok(work)
        settings = self._settings()
        cursor = self.db.execute(
            "INSERT INTO grants(work,action,binding,principal,policy,expiry) VALUES (?,?,?,?,?,?)",
            (work["id"], phase, encode(binding), "operator", settings["policy"],
             settings["tick"] + ttl))
        next_phase = work["data"].get("resume_phase") or phase
        self._save(work, "queued", phase=next_phase, reason="approved", resume_phase=None)
        return self._ok(work, grant=cursor.lastrowid)

    def _grant(self, work, action):
        settings = self._settings()
        return self._row(
            "SELECT * FROM grants WHERE work=? AND action=? AND binding=? AND principal='operator'"
            " AND policy=? AND expiry>? AND revoked=0 ORDER BY id DESC LIMIT 1",
            (work["id"], action, encode(self._binding(work, action)),
             settings["policy"], settings["tick"]))

    def _permitted(self, work):
        phase = work["phase"]
        if phase == "investigate":
            return self._revision(work)["policy"] == self._settings()["policy"]
        return self._grant(work, "publish" if phase == "publish" else "implement") is not None

    def _cmd_revoke(self, work, grant):
        self._require(_valid_id(grant), "unknown_live_grant")
        row = self._row("SELECT * FROM grants WHERE id=? AND work=?", (grant, work["id"]))
        self._require(row is not None and not row["revoked"], "unknown_live_grant")
        self.db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant,))
        self._event("approval_revoked", work["id"], actor="operator", grant=grant)
        if work["state"] in ("queued", "running") and not self._permitted(work):
            self._interrupt(work, "approval_revoked")
        return self._ok(work)

    def _cmd_register(self, worker, capabilities, allowed, priority=10, schema=1):
        self._require(isinstance(worker, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", worker)
                      and isinstance(capabilities, list) and len(capabilities) <= 4
                      and all(cap in PHASES for cap in capabilities)
                      and type(allowed) is bool and type(priority) is int and -100 <= priority <= 100
                      and type(schema) is int and 1 <= schema <= 100, "invalid_worker")
        self.db.execute("INSERT OR REPLACE INTO workers VALUES (?,?,?,?,?)",
                        (worker, priority, encode(capabilities), allowed, schema))
        self._event("worker_registered", actor="operator", worker=worker, allowed=allowed,
                    capabilities=capabilities, schema=schema)
        return self._ok()

    def _eligible(self, work, worker):
        phase = work["phase"]
        return (worker["allowed"] and worker["version"] == 1
                and phase in json.loads(worker["capabilities"])
                and (phase != "review" or worker["id"] != work["data"].get("implementer")))

    def _available_worker(self, work):
        return next((worker for worker in self.db.execute(
            "SELECT * FROM workers ORDER BY priority, id") if self._eligible(work, worker)), None)

    def _exhausted(self, work):
        data = work["data"]
        return data["used"] >= data["budget"] or self._settings()["tick"] >= data["deadline"]

    def _blocked(self, work, reason, **changes):
        self._save(work, "blocked", reason=reason, **changes)
        return {"ok": False, "reason": reason, "version": work["version"]}

    def _budget_failed(self, work):
        self._save(work, "failed", reason="budget_exhausted")
        return {"ok": False, "reason": "budget_exhausted", "version": work["version"]}

    def _evidence(self, work, *, export=False):
        rows = self.db.execute("SELECT artifact FROM checkpoints WHERE work=? ORDER BY id",
                               (work["id"],)).fetchall()
        references = self._revision(work).get("evidence", [])
        values = {}
        for key in [*references, *(row["artifact"] for row in rows)]:
            try:
                values[key] = self.artifacts.get(key)
            except EvidenceError:
                if not export:
                    return None
        return values

    def _check_dispatch_lock(self):
        try:
            with dispatch_lock(self.root):
                return
        except RunnerBusy as exc:
            raise Rejected("runner_busy") from exc
        except OSError as exc:
            raise Rejected("worker_lock_unavailable") from exc

    def _claim(self, work, *, linux=False):
        self._require(work["state"] == "queued" and work["phase"] in PHASES, "not_runnable")
        if not self._permitted(work):
            return self._blocked(work, "authorization_inactive")
        if self._evidence(work) is None:
            return self._blocked(work, "evidence_unavailable")
        if self._exhausted(work):
            return self._budget_failed(work)
        self._require(self._row("SELECT 1 FROM attempts WHERE status='active'") is None
                      and not self._armed(), "runner_busy")
        if not linux:
            self._check_dispatch_lock()
        worker = self._available_worker(work)
        if worker is None:
            return self._blocked(work, "no_eligible_worker")
        data = work["data"]
        generation = data["generation"] + 1
        phase = work["phase"]
        expires = min(self._settings()["tick"] + LEASE_TICKS, data["deadline"])
        cursor = self.db.execute(
            "INSERT INTO attempts(work,generation,phase,revision,candidate,worker,expires,status)"
            " VALUES (?,?,?,?,?,?,?,'active')",
            (work["id"], generation, phase, work["revision"], data.get("candidate"),
             worker["id"], expires))
        self._save(work, "running", reason="claimed", generation=generation, used=data["used"] + 1)
        self._event("worker_assigned", work["id"], attempt=cursor.lastrowid, worker=worker["id"],
                    generation=generation, phase=phase, expires=expires, revision=work["revision"],
                    candidate=data.get("candidate"), remaining=data["budget"] - data["used"])
        return self._ok(work, attempt=cursor.lastrowid, worker=worker["id"], generation=generation)

    def _cmd_claim(self, work):
        return self._claim(work)

    def _job(self, attempt):
        row = self._row("SELECT * FROM attempts WHERE id=?", (attempt,))
        self._require(row is not None, "unknown_attempt")
        work = self._work(row["work"])
        return Job(*(row[key] for key in ("id", "work", "generation", "phase", "revision",
                                          "candidate", "worker", "expires")),
                   self._revision(work)["mode"], work["data"]["iteration"])

    def _fixture(self, job):
        return run_fixture(job.phase, mode=job.mode, candidate=job.candidate, iteration=job.iteration)

    def _linux_replay(self, command, fingerprint, actor):
        reservation = self._row(
            "SELECT l.*,a.work FROM linux_runs l JOIN attempts a ON a.id=l.attempt WHERE l.command=?",
            (command,))
        if reservation is None:
            return None
        if reservation["fingerprint"] != fingerprint:
            self._event("command_conflict", reservation["work"], actor=actor, command=command)
            return {"ok": False, "reason": "command_conflict"}
        active = reservation["status"] in ("armed", "launched", "observed")
        return {"ok": False, "reason": "operation_in_progress" if active else "reconcile_report",
                "runner_status": reservation["status"]}

    def _linux_previous(self, command, fingerprint, work):
        previous = self._row("SELECT * FROM commands WHERE id=?", (command,))
        effect = self._row("SELECT * FROM effects WHERE command=?", (command,))
        if previous and previous["fingerprint"] == fingerprint:
            return json.loads(previous["response"])
        if previous or effect:
            self._event("command_conflict", self._existing_work(work), actor="operator",
                        command=command)
            return {"ok": False, "reason": "command_conflict"}
        return self._linux_replay(command, fingerprint, "operator")

    def _linux_response(self, command, fingerprint, work, result):
        self._event("command", self._existing_work(work), actor="operator", command=command,
                    action="run", ok=result["ok"], reason=result.get("reason"))
        self.db.execute("INSERT INTO commands VALUES (?,?,?)", (command, fingerprint, encode(result)))
        return result

    def _prepare_linux(self, command, fingerprint, work_id, version, args):
        try:
            work = self._checked_work(work_id, version)
            self._require(not args, "invalid_arguments")
            if work["state"] == "queued":
                claim = self._claim(work, linux=True)
                if not claim["ok"]:
                    return claim
                attempt = claim["attempt"]
            else:
                active = self._row("SELECT id FROM attempts WHERE work=? AND status='active'",
                                   (work_id,))
                self._require(active is not None, "no_active_attempt")
                attempt = active["id"]
            self._require(not self._row("SELECT 1 FROM linux_runs WHERE attempt=?", (attempt,)),
                          "linux_attempt_reserved")
            job = self._job(attempt)
        except Rejected as exc:
            return {"ok": False, "reason": str(exc)}
        self.db.execute("INSERT INTO linux_runs VALUES (?,?,?,'armed')", (attempt, command, fingerprint))
        if work["data"].get("worker_profile") != "linux":
            self._save(work, "running", reason="linux_profile_selected", worker_profile="linux")
        self._event("linux_armed", work_id, actor="runner", command=command, attempt=attempt,
                    generation=job.generation, revision=job.revision)
        return job

    def _run_linux(self, command, work_id, version, args, fingerprint):
        with ExitStack() as stack:
            with transaction(self.db):
                previous = self._linux_previous(command, fingerprint, work_id)
                if previous is not None:
                    return previous
                try:
                    lock = stack.enter_context(dispatch_lock(self.root))
                except RunnerBusy:
                    return {"ok": False, "reason": "runner_busy"}
                except OSError:
                    return {"ok": False, "reason": "worker_lock_unavailable"}
                job = self._prepare_linux(command, fingerprint, work_id, version, args)
                if isinstance(job, dict):
                    return self._linux_response(command, fingerprint, work_id, job)

            fault = self.runner.checkpoint_fault
            if fault == "before_launch":
                raise InjectedCrash("after durable claim, before launch")
            observed = self._execute_linux(stack, lock, job, command)
            capture_key = None
            if fault == "after_worker":
                raise InjectedCrash("after worker output, before durable acknowledgement")

            with transaction(self.db):
                if not self._linux_current(job):
                    return self._linux_stale(job, command, fingerprint, observed["status"])
                if job.mode == "source" and "streams" in observed:
                    try:
                        capture = source.capture(observed, self._revision(self._work(work_id)), job)
                        capture_key = self.artifacts.put(capture)
                    except (source.SourceError, EvidenceError):
                        observed = {"status": "invalid_test_observation"}
                    else:
                        self._event("source_observed", work_id, actor="runner", attempt=job.id,
                                    artifact=capture_key, status=capture["status"])
                self.db.execute("UPDATE linux_runs SET status='observed' WHERE attempt=?", (job.id,))
                self._event("linux_observed", work_id, actor="runner", command=command, attempt=job.id,
                            outcome=observed["status"], exit_code=observed.get("exit"),
                            detail=observed.get("detail"))
            if fault == "after_ack":
                raise InjectedCrash("after acknowledgement, before checkpoint")

            with transaction(self.db):
                if not self._linux_current(job):
                    return self._linux_stale(job, command, fingerprint, observed["status"])
                if capture_key is not None and observed["status"] == "test_exited":
                    result = self._finish_source(job, capture_key, fault)
                elif observed["status"] == "observed" and job.mode != "source":
                    result = self._finish(job.worker, job, observed["output"], fault=fault, linux=True)
                else:
                    result = self._interrupt(self._work(work_id), observed["status"])
                self.db.execute("UPDATE linux_runs SET status=? WHERE attempt=?",
                                ("accepted" if result["ok"] else "failed", job.id))
                return self._linux_response(command, fingerprint, work_id, result)

    def _execute_linux(self, stack, lock, job, command):
        with transaction(self.db):
            if not self._linux_current(job):
                return {"status": "worker_cancelled"}
            if self._evidence(self._work(job.work)) is None:
                return {"status": "evidence_unavailable"}
            files = None
            if job.mode == "source":
                try:
                    files = self._source_files(self._revision(self._work(job.work)))
                except source.SourceError as exc:
                    return {"status": str(exc)}
                if files["NEXUS_FIXTURE.json"] != b'{"fixture":"nx-004","dependency":"python-stdlib"}\n':
                    return {"status": "dependency_unavailable"}
                self._event("source_snapshot_verified", job.work, actor="runner", attempt=job.id,
                            snapshot=source.snapshot_digest(files), profile=source.PROFILE)
            try:
                launcher = self.runner.launch(lock, job, files=files) if files else self.runner.launch(lock, job)
                process = stack.enter_context(launcher)
            except OSError:
                return {"status": "supervisor_launch_failed"}
            self.db.execute("UPDATE linux_runs SET status='launched' WHERE attempt=?", (job.id,))
            self._event("linux_launched", job.work, actor="runner", command=command, attempt=job.id)
        return self.runner.wait(process, lambda: self._linux_current(job))

    def _finish_source(self, job, key, fault):
        work = self._work(job.work)
        try:
            capture = self.artifacts.get(key)
        except EvidenceError:
            return self._interrupt(work, "evidence_unavailable")
        if capture["outcome"] not in ("command_passed", "command_exit_one"):
            return self._interrupt(work, capture["outcome"])
        self.db.execute("UPDATE attempts SET status='accepted' WHERE id=?", (job.id,))
        self.db.execute("INSERT INTO checkpoints(work,attempt,phase,artifact,candidate)"
                        " VALUES (?,?,?,?,NULL)", (job.work, job.id, job.phase, key))
        self._event("checkpoint", job.work, attempt=job.id, artifact=key, phase=job.phase,
                    candidate=None)
        if fault in ("database_crash", "artifact_crash"):
            raise InjectedCrash("after source capture publication, before checkpoint commit")
        return self._handoff(work, capture["outcome"])

    def _linux_current(self, job):
        try:
            return self._active_job(job.worker, job)[1]
        except Rejected:
            return False

    def _linux_stale(self, job, command, fingerprint, runner_outcome):
        work = self._work(job.work)
        active = self._row("SELECT 1 FROM attempts WHERE id=? AND status='active'", (job.id,))
        if active:
            self._interrupt(work, "lease_or_authorization_inactive")
        self._event("result_refused", job.work, actor="runner", attempt=job.id,
                    reason="stale_or_wrong_attempt", runner_outcome=runner_outcome)
        return self._linux_response(command, fingerprint, job.work,
                                    {"ok": False, "reason": "stale_or_wrong_attempt"})

    def _fence_linux(self, work):
        fenced = self.db.execute(
            "UPDATE linux_runs SET status='interrupted' WHERE attempt IN "
            "(SELECT id FROM attempts WHERE work=? AND status='active') "
            "AND status IN ('armed','launched','observed')", (work["id"],))
        if fenced.rowcount:
            self._event("linux_fenced", work["id"], cleanup="dispatch_lock_required")

    def _cmd_run(self, work):
        self._require(work["data"].get("worker_profile") != "linux", "linux_profile_required")
        if work["state"] == "queued":
            claim = self._claim(work)
            if not claim["ok"]:
                return claim
            attempt = claim["attempt"]
        else:
            row = self._row("SELECT id FROM attempts WHERE work=? AND status='active'", (work["id"],))
            self._require(row is not None, "no_active_attempt")
            attempt = row["id"]
        job = self._job(attempt)
        return self._finish(job.worker, job, None, run=True)

    def finish(self, worker, job, output, *, fault=None):
        """Runner adapter callback. The trusted adapter supplies the worker separately."""
        with transaction(self.db):
            try:
                return self._finish(worker, job, output, fault=fault)
            except Rejected as exc:
                self._event("result_refused", self._existing_work(job.work), actor="runner",
                            reason=str(exc))
                return {"ok": False, "reason": str(exc)}

    def _active_job(self, worker, job):
        attempt = self._row("SELECT * FROM attempts WHERE id=?", (job.id,))
        work = self._work(job.work)
        self._require(attempt is not None and attempt["status"] == "active"
                      and work["state"] == "running" and job == self._job(job.id)
                      and worker == attempt["worker"] and job.revision == work["revision"]
                      and job.generation == work["data"]["generation"]
                      and job.candidate == work["data"].get("candidate"), "stale_or_wrong_attempt")
        control = self._row("SELECT * FROM workers WHERE id = ?", (job.worker,))
        return work, (job.expires > self._settings()["tick"] and self._permitted(work)
                      and control is not None and self._eligible(work, control))

    def _finish(self, worker, job, output, *, run=False, fault=None, linux=False):
        work, permitted = self._active_job(worker, job)
        self._require(linux or work["data"].get("worker_profile") != "linux",
                      "linux_attempt_reserved")
        if not permitted:
            return self._interrupt(work, "lease_or_authorization_inactive")
        if self._evidence(work) is None:
            return self._interrupt(work, "evidence_unavailable")
        if not linux:
            self._event("tool_start", job.work, actor="runner", attempt=job.id,
                        tool=f"fixture-{job.phase}", revision=job.revision, candidate=job.candidate)
        expected = self._fixture(job)
        if run:
            output = expected
        if fault == "worker_failure":
            self._event("tool_result", job.work, actor="runner", attempt=job.id, outcome="failure")
            return self._interrupt(work, "fixture_worker_failed")
        try:
            valid = encode(output) == encode(expected)
        except (ValueError, TypeError, RecursionError):
            valid = False
        self._event("tool_result", job.work, actor="runner", attempt=job.id,
                    outcome="passed" if valid else "invalid", reserved_ticks=1)
        if not valid:
            return self._interrupt(work, "invalid_fixture_result")
        try:
            artifact = self.artifacts.put(expected, crash=fault == "artifact_crash")
        except EvidenceError:
            return self._interrupt(work, "evidence_unavailable")
        candidate = artifact if job.phase == "implement" else job.candidate
        self.db.execute("UPDATE attempts SET status='accepted' WHERE id=?", (job.id,))
        self.db.execute("INSERT INTO checkpoints(work,attempt,phase,artifact,candidate)"
                        " VALUES (?,?,?,?,?)", (job.work, job.id, job.phase, artifact, candidate))
        grant = self._grant(work, "implement") if job.phase != "investigate" else None
        self._event("checkpoint", job.work, attempt=job.id, artifact=artifact, phase=job.phase,
                    candidate=candidate, grant=grant["id"] if grant else None,
                    remaining=work["data"]["budget"] - work["data"]["used"])
        if fault == "database_crash":
            raise InjectedCrash("after checkpoint/event writes, before commit")
        if job.phase == "investigate":
            handoff = self._handoff(work, "reproduced" if expected["reproduced"] else "inconclusive")
            return handoff | {"artifact": artifact}
        elif job.phase == "implement":
            self._save(work, "queued", phase="verify", reason="candidate_created",
                       candidate=candidate, implementer=job.worker, verified=None, reviewed=None)
        elif job.phase == "verify":
            self._save(work, "queued", phase="review", reason="verified", verified=candidate)
        else:
            self._require(work["data"]["verified"] == candidate, "verification_not_current")
            self._event("independent_review", job.work, actor="runner", worker=job.worker,
                        candidate=candidate, artifact=artifact)
            self._save(work, "awaiting_approval", phase="publish", reason="reviewed",
                       reviewed=candidate)
        return self._ok(work, artifact=artifact)

    def _interrupt(self, work, reason):
        self._fence_linux(work)
        self.db.execute("UPDATE attempts SET status='interrupted' WHERE work=? AND status='active'",
                        (work["id"],))
        self._event("interrupted", work["id"], reason=reason)
        return self._blocked(work, reason)

    def _cmd_advance(self, ticks):
        self._require(type(ticks) is int and 1 <= ticks <= 1000, "invalid_ticks")
        self.db.execute("UPDATE settings SET tick=tick+?", (ticks,))
        self._event("clock_advanced", actor="operator", ticks=ticks)
        return self._ok(tick=self._settings()["tick"])

    def _cmd_policy(self):
        self.db.execute("UPDATE settings SET policy=policy+1")
        for row in self.db.execute("SELECT id FROM work WHERE state IN ('queued','running')").fetchall():
            self._interrupt(self._work(row["id"]), "policy_changed")
        self._event("policy_changed", actor="operator")
        return self._ok(policy=self._settings()["policy"])

    def _armed(self):
        return self._row("SELECT id FROM effects WHERE status='armed'") is not None

    def _unresolved(self, work):
        return self._row("SELECT * FROM effects WHERE work=? AND status IN ('armed','unknown','mismatch')",
                         (work["id"],))

    def _cmd_recover(self, restore=False):
        self._require(type(restore) is bool, "invalid_restore_flag")
        if restore:
            self.db.execute("UPDATE settings SET publication_frozen=1")
            self._event("publication_frozen", actor="operator", reason="possible_missing_intents")
        active = self.db.execute("SELECT work FROM attempts WHERE status='active'").fetchall()
        for row in active:
            self._interrupt(self._work(row["work"]), "restart_fenced")
        effects = self.db.execute("SELECT * FROM effects WHERE status='armed' ORDER BY id").fetchall()
        for effect in effects:
            self._observe_effect(self._work(effect["work"]), effect, self.broker.observe(effect["id"]))
        self._event("recovery", actor="operator", attempts=len(active), effects=len(effects))
        return self._ok(attempts=len(active), effects=len(effects))

    def _cmd_resume(self, work):
        self._require(work["state"] == "blocked", "not_blocked")
        self._require(not self._unresolved(work), "effect_unresolved")
        if self._evidence(work) is None:
            return self._blocked(work, "evidence_unavailable")
        continuation = work["data"].get("resume_handoff")
        if continuation:
            return self._handoff(work, continuation)
        if self._exhausted(work):
            return self._budget_failed(work)
        request = self._revision(work)
        if request["kind"] == "source_investigation" and self._permitted(work):
            try:
                self._source_files(request)
            except source.SourceError as exc:
                return self._blocked(work, str(exc))
        if not self._permitted(work):
            phase = work["phase"]
            if phase == "investigate":
                return self._blocked(work, "submit_under_current_policy")
            gate = phase if phase == "publish" else "implement"
            self._save(work, "awaiting_approval", phase=gate, reason="approval_required",
                       resume_phase=phase)
        else:
            self._save(work, "queued", reason="resumed")
        return self._ok(work)

    def _cmd_cancel(self, work, acknowledge_unknown=False):
        self._require(type(acknowledge_unknown) is bool, "invalid_disposition")
        self._require(work["state"] not in TERMINAL, "terminal_work")
        self._fence_linux(work)
        self.db.execute("UPDATE attempts SET status='interrupted' WHERE work=? AND status='active'",
                        (work["id"],))
        effect = self._unresolved(work)
        if effect:
            self._save(work, "cancelling", reason="reconciling_cancellation")
            if effect["status"] != "mismatch":
                self._observe_effect(work, effect, self.broker.observe(effect["id"]))
            if self._unresolved(work) and not acknowledge_unknown:
                return self._ok(work)
        self._save(work, "cancelled", reason="operator_cancelled")
        self._event("cancelled", work["id"], actor="operator",
                    unresolved_retained=self._unresolved(work) is not None)
        return self._ok(work)

    def _review_valid(self, work):
        data = work["data"]
        candidate = data.get("candidate")
        return bool(candidate and data.get("verified") == candidate and data.get("reviewed") == candidate)

    def _cmd_rework(self, work):
        self._require(work["state"] in ("awaiting_approval", "queued") and work["phase"] == "publish",
                      "rework_requires_reviewed_candidate")
        self._require(self._grant(work, "implement") is not None, "authorization_inactive")
        self._require(self._row("SELECT id FROM effects WHERE work=?", (work["id"],)) is None,
                      "effects_already_started")
        self._save(work, "queued", phase="implement", reason="bounded_fixture_rework",
                   iteration=work["data"]["iteration"] + 1, candidate=None, verified=None, reviewed=None)
        return self._ok(work)

    def _arm_for_command(self, command, fingerprint, work_id, version, args):
        if (set(args) - {"fault"} or args.get("fault", "none") not in FixtureBroker.FAULTS
                or self._row("SELECT id FROM commands WHERE id=?", (command,))
                or self._row("SELECT 1 FROM linux_runs WHERE command=?", (command,))
                or self._row("SELECT id FROM effects WHERE command=?", (command,))):
            return None
        try:
            work = self._checked_work(work_id, version)
            operation, binding, grant = self._publication_plan(work)
        except Rejected:
            return None
        cursor = self.db.execute(
            "INSERT INTO effects(work,operation,target,binding,command,fingerprint,grant_id,status)"
            " VALUES (?,?,?,?,?,?,?,'armed')",
            (work_id, operation, binding["target"], encode(binding), command, fingerprint, grant["id"]))
        self._save(work, "running", reason="effect_armed", used=work["data"]["used"] + 1)
        self._event("effect_intent", work_id, command=command, effect=cursor.lastrowid,
                    operation=operation, binding=binding, grant=grant["id"])
        return cursor.lastrowid

    def _publication_plan(self, work):
        self._require(work["state"] == "queued" and work["phase"] == "publish", "publication_gate_required")
        grant = self._grant(work, "publish")
        self._require(grant is not None, "publication_grant_inactive")
        self._require(not self._settings()["publication_frozen"], "publication_frozen")
        self._require(self._review_valid(work), "review_not_current")
        self._require(self._evidence(work) is not None, "evidence_unavailable")
        self._require(not self._exhausted(work), "budget_exhausted")
        binding = self._binding(work, "publish")
        self._require(self._row(
            "SELECT id FROM effects WHERE target=? AND status IN ('armed','unknown','mismatch')",
            (binding["target"],)) is None, "target_quarantined")
        self._require(not self._armed() and self._row(
            "SELECT id FROM attempts WHERE status='active'") is None, "runner_busy")
        self._check_dispatch_lock()
        pushed = self._row("SELECT * FROM effects WHERE work=? AND operation='push'"
                           " AND status != 'not_dispatched' ORDER BY id DESC LIMIT 1", (work["id"],))
        self._require(pushed is None or pushed["status"] == "confirmed", "push_not_confirmed")
        operation = "push" if pushed is None else "draft"
        self._require(self._row("SELECT id FROM effects WHERE work=? AND operation=?"
                                " AND status != 'not_dispatched'",
                                (work["id"], operation)) is None, "effect_already_recorded")
        return operation, binding, grant

    def _cmd_publish(self, work, command, armed, fault="none"):
        self._require(fault in FixtureBroker.FAULTS, "invalid_fault")
        if armed is None:
            # Re-evaluate eligibility for diagnostics. A prior armed or unknown
            # record can only be reconciled, even if a command is retried.
            try:
                self._publication_plan(work)
            except Rejected as exc:
                reason = str(exc)
                if reason == "budget_exhausted":
                    return self._budget_failed(work)
                if reason in ("publication_grant_inactive", "publication_frozen", "evidence_unavailable",
                              "review_not_current", "target_quarantined"):
                    return self._blocked(work, reason)
                raise
            raise Rejected("dispatch_not_armed")
        effect = self._row("SELECT * FROM effects WHERE id=? AND command=? AND work=?",
                           (armed, command, work["id"]))
        self._require(effect is not None and effect["status"] == "armed", "stale_intent")
        binding = self._binding(work, "publish")
        grant = self._grant(work, "publish")
        if (work["state"] != "running" or work["phase"] != "publish"
                or not grant or grant["id"] != effect["grant_id"]
                or encode(binding) != effect["binding"] or self._settings()["publication_frozen"]
                or not self._review_valid(work) or self._evidence(work) is None
                or self._settings()["tick"] >= work["data"]["deadline"]):
            self._stop_unsent(armed)
            return {"ok": False, "reason": "publication_precondition_changed"}
        self._event("effect_dispatch", work["id"], command=command, effect=armed,
                    operation=effect["operation"])
        receipt = self.broker.dispatch(armed, effect["operation"], binding, fault)
        if fault == "commit_gap":
            raise InjectedCrash("after provider commit, before coordinator commit")
        return self._observe_effect(work, effect, receipt)

    def _stop_unsent(self, intent):
        effect = self._row("SELECT * FROM effects WHERE id=? AND status='armed'", (intent,))
        if effect:
            self.db.execute("UPDATE effects SET status='not_dispatched' WHERE id=?", (intent,))
            self._event("effect_not_dispatched", effect["work"], effect=intent,
                        reason="precondition_changed")
            work = self._work(effect["work"])
            if work["state"] == "running":
                self._blocked(work, "publication_precondition_changed")

    def _observe_effect(self, work, effect, receipt):
        binding = json.loads(effect["binding"])
        base, head = self.broker.refs(binding["target"])
        if receipt is None:
            status = "unknown"
        elif receipt["status"] == "ref_drift":
            status = "ref_drift"
        else:
            matches = (receipt["intent"] == effect["id"]
                       and receipt["operation"] == effect["operation"]
                       and receipt["target"] == binding["target"]
                       and receipt["base"] == base == binding["base"]
                       and receipt["head"] == head == binding["candidate"]
                       and (effect["operation"] == "push" or receipt.get("draft") is True))
            status = "confirmed" if matches else "mismatch"
        self.db.execute("UPDATE effects SET status=?,receipt=? WHERE id=?",
                        (status, encode(receipt), effect["id"]))
        self._event("effect_observed", work["id"], effect=effect["id"], operation=effect["operation"],
                    status=status, receipt=receipt)
        if work["state"] in TERMINAL or work["state"] == "cancelling":
            return self._ok(work, effect_status=status)
        if status != "confirmed":
            return self._blocked(work, f"effect_{status}")
        if effect["operation"] == "push":
            self._save(work, "queued" if self._permitted(work) else "blocked",
                       reason="push_confirmed")
        else:
            if self._evidence(work) is None or not self._review_valid(work):
                return self._blocked(work, "published_evidence_unavailable",
                                     resume_handoff="fixture_draft_confirmed")
            self._handoff(work, "fixture_draft_confirmed")
        return self._ok(work, effect_status=status)

    def _cmd_reconcile(self, work):
        effect = self._unresolved(work)
        self._require(effect is not None and effect["status"] != "mismatch", "no_reconcilable_intent")
        result = self._observe_effect(work, effect, self.broker.observe(effect["id"]))
        if work["state"] == "cancelling" and not self._unresolved(work):
            self._save(work, "cancelled", reason="cancellation_reconciled")
            return self._ok(work, effect_status=result.get("effect_status"))
        return result

    def _handoff(self, work, outcome):
        evidence = self._evidence(work)
        if evidence is None:
            return self._blocked(work, "evidence_unavailable", resume_handoff=outcome)
        effects = self.db.execute("SELECT id,status FROM effects WHERE work=? ORDER BY id",
                                  (work["id"],)).fetchall()
        checkpoints = self.db.execute("SELECT phase,attempt,artifact FROM checkpoints WHERE work=?"
                                       " ORDER BY id", (work["id"],)).fetchall()
        decisions = self.db.execute("SELECT seq FROM events WHERE kind='approval_decision' AND work=?"
                                    " ORDER BY seq", (work["id"],)).fetchall()
        handoff = {"work": work["id"], "revision": work["revision"], "outcome": outcome,
                   "candidate": work["data"].get("candidate"), "evidence": list(evidence),
                   "effects": [dict(row) for row in effects],
                   "checkpoints": [dict(row) for row in checkpoints],
                   "approval_events": [row["seq"] for row in decisions]}
        try:
            key = self.artifacts.put(handoff)
        except EvidenceError:
            return self._blocked(work, "handoff_unavailable", resume_handoff=outcome)
        self._save(work, "awaiting_acceptance", reason=outcome, handoff=key, resume_handoff=None)
        self._event("handoff_ready", work["id"], handoff=key, outcome=outcome)
        return self._ok(work, handoff=key)

    def _cmd_accept(self, work, handoff, allow=True):
        self._require(work["state"] == "awaiting_acceptance" and type(allow) is bool
                      and handoff == work["data"].get("handoff"), "handoff_mismatch")
        try:
            snapshot = self.artifacts.get(handoff)
        except EvidenceError:
            return self._blocked(work, "handoff_unavailable", resume_handoff=work["data"]["reason"])
        if self._evidence(work) is None:
            return self._blocked(work, "evidence_unavailable", resume_handoff=snapshot["outcome"])
        state = "completed" if allow else "rejected"
        self._save(work, state, reason=f"handoff_{state}")
        self._event("handoff_decision", work["id"], actor="operator", handoff=handoff,
                    accepted=allow)
        return self._ok(work)

    def _cmd_archive(self, work, reference, revision):
        self._require(self._revision(work)["kind"] != "source_investigation", "source_readonly")
        self._require(isinstance(reference, str) and isinstance(revision, str)
                      and len(reference) <= 128 and len(revision) <= 128, "invalid_reference")
        allowed = (work["phase"] == "investigate" and work["state"] in ("queued", "running")
                   and self._settings()["tick"] < work["data"]["deadline"]
                   and self._permitted(work))
        outcome = archive_lookup(reference, revision, allowed)
        self._event("archive_lookup", work["id"], actor="operator",
                    reference_digest=digest([reference, revision]), outcome=outcome)
        return self._ok(work, outcome=outcome, satisfied=outcome == "resolved")

    def message(self, principal, message):
        self._require(principal is OPERATOR, "operator_required")
        self._require(_valid_id(message), "unknown_message")
        row = self._row("SELECT * FROM messages WHERE id=?", (message,))
        self._require(row is not None, "unknown_message")
        return dict(row)

    def source_evidence(self, principal, work_id, key):
        self._require(principal is OPERATOR, "operator_required")
        with transaction(self.db, write=False):
            work = self._work(work_id)
            self._require(self._revision(work)["kind"] == "source_investigation",
                          "source_investigation_required")
            observations = self.db.execute(
                "SELECT data FROM events WHERE work=? AND kind='source_observed'", (work_id,))
            self._require(any(json.loads(row["data"])["artifact"] == key for row in observations),
                          "source_evidence_not_assigned")
            return self.artifacts.get(key)

    def report(self, principal, work_id):
        """Redacted export. Free-form conversation is available only through message()."""
        self._require(principal is OPERATOR, "operator_required")
        with transaction(self.db, write=False):
            work = self._work(work_id)
            binding = self._revision(work)
            lineage = [work_id]
            if "investigation" in binding:
                lineage.append(binding["investigation"])
            slots = ",".join("?" for _ in lineage)
            events = [dict(row) | {"data": json.loads(row["data"]),
                                   "command": digest(row["command"]) if row["command"] else None}
                      for row in self.db.execute(
                f"SELECT * FROM events WHERE work IN ({slots}) ORDER BY seq", lineage)]
            grants = [dict(row) | {"binding": json.loads(row["binding"])} for row in self.db.execute(
                "SELECT * FROM grants WHERE work=? ORDER BY id", (work_id,))]
            effects = [dict(row) | {"binding": json.loads(row["binding"]),
                                    "command": digest(row["command"]),
                                    "receipt": json.loads(row["receipt"]) if row["receipt"] else None}
                       for row in self.db.execute("SELECT * FROM effects WHERE work=? ORDER BY id", (work_id,))]
            for effect in effects:
                effect.pop("fingerprint")
            attempts = [dict(row) for row in self.db.execute(
                f"SELECT * FROM attempts WHERE work IN ({slots}) ORDER BY id", lineage)]
            checkpoints = [dict(row) for row in self.db.execute(
                f"SELECT * FROM checkpoints WHERE work IN ({slots}) ORDER BY id", lineage)]
            available = self._evidence(work, export=True)
            evidence = [{"digest": row["artifact"], "phase": row["phase"],
                         "available": row["artifact"] in available,
                         "result": available.get(row["artifact"]), "attempt": row["attempt"]}
                        for row in checkpoints]
            partial = []
            if binding["kind"] == "source_investigation":
                for item in evidence:
                    if item["result"] is not None:
                        item["result"].pop("streams", None)
                checkpoint_keys = {item["digest"] for item in evidence}
                for event in events:
                    if event["kind"] != "source_observed":
                        continue
                    key = event["data"]["artifact"]
                    if key in checkpoint_keys:
                        continue
                    try:
                        data = self.artifacts.get(key)
                        data.pop("streams", None)
                    except EvidenceError:
                        data = None
                    partial.append({"digest": key, "result": data, "accepted": False})
            pending = None
            if work["state"] == "awaiting_approval":
                pending = self._binding(work, work["phase"])
            handoff = None
            if work["data"].get("handoff"):
                try:
                    handoff = self.artifacts.get(work["data"]["handoff"])
                except EvidenceError:
                    pass
            result = {"work": work, "request": binding, "pending_approval": pending, "handoff": handoff,
                    "source_message_text": "[redacted]", "grants": grants, "attempts": attempts,
                    "evidence": evidence, "effects": effects, "events": events,
                    "limits": ["fixture-only execution and provider", "no live security validation"]}
            if binding["kind"] == "source_investigation":
                result["partial"] = partial
                result["limits"] = ["Synthetic repository only", "Raw output omitted from report",
                                    "Test text does not establish independent verification"]
            return result


def render_report(report):
    work = report["work"]
    data = work["data"]
    request = report["request"]
    lines = [
        f"Work {work['id']} · {work['state']} / {work['phase']}",
        f"Reason · {data['reason']}",
        f"Original message · {request['message']} (text redacted)",
        f"Resource and base · {request['resource']} @ {request['base']}",
        f"Scope · {encode(request['scope'])}",
        f"Revision · {work['revision']}",
        f"Budget · {data['used']}/{data['budget']} dispatches reserved",
        f"Candidate · {data.get('candidate') or 'none'}",
        f"Handoff · {data.get('handoff') or 'none'}",
    ]
    if report["pending_approval"]:
        lines.append(f"Pending approval · {encode(report['pending_approval'])}")
    for label, records in (
        ("Grant", report["grants"]), ("Attempt", report["attempts"]),
        ("Evidence", report["evidence"]), ("Effect", report["effects"]),
    ):
        lines.extend(f"{label} · {encode(record)}" for record in records)
    for event in report["events"]:
        if event["kind"] in ("approval_decision", "interrupted", "independent_review", "handoff_decision"):
            lines.append(f"Audit · {event['kind']} · {encode(event['data'])}")
    for item in report.get("partial", []):
        lines.append(f"Partial observation · {encode(item)}")
    lines.append(f"Audit events · {len(report['events'])}")
    lines.extend(f"Limit · {limit}" for limit in report["limits"])
    return "\n".join(lines)
