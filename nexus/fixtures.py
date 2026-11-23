"""Closed, local fixtures. No repository, model, or provider connections."""

import json

from .storage import digest, encode, open_database, transaction

RESOURCE = "fixture:hero-cad"
BASE = digest({"hero-cad": "synthetic base v1"})
SCOPE = "fixture/model-steps.json"
PHASES = ("investigate", "implement", "verify", "review")
DEFAULT_WORKERS = (
    ("investigator", 10, ("investigate",), True, 1),
    ("implementer", 10, ("implement",), True, 1),
    ("verifier", 10, ("verify",), True, 1),
    ("reviewer", 10, ("review",), True, 1),
)
ARCHIVE_FIXTURES = {"example": "r1", "restricted": "r2", "offline": "r1"}
PROVIDER_SCHEMA = (
    "CREATE TABLE settings (id INTEGER PRIMARY KEY, base TEXT NOT NULL)",
    f"INSERT INTO settings VALUES (1, '{BASE}')",
    "CREATE TABLE topic (target TEXT PRIMARY KEY, head TEXT NOT NULL)",
    "CREATE TABLE receipts (intent INTEGER PRIMARY KEY, receipt TEXT NOT NULL)",
)


def archive_lookup(reference, revision, allowed):
    if not allowed or reference == "restricted":
        return "denied"
    if reference == "offline":
        return "unavailable"
    if reference not in ARCHIVE_FIXTURES:
        return "missing"
    return "resolved" if ARCHIVE_FIXTURES[reference] == revision else "stale"


def run_fixture(phase, *, mode, candidate, iteration):
    if phase == "investigate":
        observations = [
            {"classification": "observed", "sequence": ["X", "Y"],
             "result": "fixture_failure"},
        ]
        if mode == "inconclusive":
            observations.append({"classification": "inconclusive", "sequence": ["X", "Y", "Z"],
                                 "result": "control_unavailable"})
            return {"observations": observations, "reproduced": False,
                    "limitation": "control result unavailable"}
        observations.append({"classification": "observed", "sequence": ["X", "Y", "Z"],
                             "result": "fixture_pass"})
        return {"observations": observations, "reproduced": True,
                "hypothesis": {"classification": "inferred", "claim": "fixture state is stale"},
                "limitation": "synthetic observations only"}
    if phase == "implement":
        return {"path": SCOPE, "base": BASE, "iteration": iteration,
                "change": "refresh simulated state after Y"}
    if phase == "verify":
        return {"candidate": candidate, "command": "fixture-check-X-Y-and-control",
                "passed": 2, "failed": 0, "skipped": 0}
    if phase == "review":
        return {"candidate": candidate, "verdict": "passed",
                "checks": ["fixture-scope", "verification-identity"]}
    raise ValueError("unsupported fixture phase")


class FixtureBroker:
    """Independent local SQLite ledger so a provider effect outlives a lost reply."""

    FAULTS = ("none", "lost_before", "lost_after", "mismatch", "commit_gap")

    def __init__(self, root):
        self.db = open_database(root / "fixture-provider.sqlite3", PROVIDER_SCHEMA)

    def close(self):
        self.db.close()

    def refs(self, target):
        base = self.db.execute("SELECT base FROM settings").fetchone()[0]
        head = self.db.execute("SELECT head FROM topic WHERE target = ?", (target,)).fetchone()
        return base, head[0] if head else None

    def observe(self, intent):
        row = self.db.execute("SELECT receipt FROM receipts WHERE intent = ?", (intent,)).fetchone()
        return json.loads(row[0]) if row else None

    def dispatch(self, intent, operation, binding, fault):
        with transaction(self.db):
            if self.observe(intent) is not None:
                raise RuntimeError("duplicate provider call")
            if fault == "lost_before":
                return None
            target = binding["target"]
            base, head = self.refs(target)
            expected_head = None if operation == "push" else binding["candidate"]
            if base != binding["base"] or head != expected_head:
                return {"status": "ref_drift", "base": base, "head": head}
            if operation == "push":
                self.db.execute("INSERT INTO topic VALUES (?, ?)", (target, binding["candidate"]))
                head = binding["candidate"]
            receipt = {"intent": intent, "status": "applied", "operation": operation,
                       "target": target, "base": base, "head": head}
            if operation == "draft":
                receipt.update(draft=fault != "mismatch", number=intent)
            self.db.execute("INSERT INTO receipts VALUES (?, ?)", (intent, encode(receipt)))
            return None if fault == "lost_after" else receipt

    def drift(self, target, *, base=False):
        """Test control outside the operator command surface."""
        with transaction(self.db):
            if base:
                self.db.execute("UPDATE settings SET base = ?", (digest("fixture base drift"),))
            else:
                self.db.execute("INSERT OR REPLACE INTO topic VALUES (?, ?)",
                                (target, digest("fixture head drift")))
