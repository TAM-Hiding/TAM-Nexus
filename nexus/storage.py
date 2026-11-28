"""Transactional coordinator records and a private content-addressed store."""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from contextlib import contextmanager


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("ascii")).hexdigest()


class EvidenceError(Exception):
    pass


class InjectedCrash(Exception):
    """A deliberate failure at a durable boundary."""


def private_directory(path, *, create=True):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("state and evidence directories must be owner-only directories")
    return path


def open_database(path, schema, migrations=(), *, readonly=False):
    # The enclosing directory is private. Restrict the database itself as well.
    if not readonly:
        fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDONLY, 0o600)
        os.close(fd)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("database must be a private regular file")
    target = path.absolute().as_uri() + "?mode=ro" if readonly else path
    db = sqlite3.connect(target, uri=readonly, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        if readonly:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != len(migrations) + 1:
                raise ValueError("open the coordinator normally to upgrade this state before export")
            return db
        with transaction(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in schema:
                    db.execute(statement)
                version = 1
                db.execute("PRAGMA user_version = 1")
            if not 1 <= version <= len(migrations) + 1:
                raise ValueError(f"unsupported database schema {version}")
            for index in range(version - 1, len(migrations)):
                for statement in migrations[index]:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {index + 2}")
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def transaction(db, *, write=True):
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


SCHEMA = (
    """CREATE TABLE settings (
        id INTEGER PRIMARY KEY CHECK (id = 1), tick INTEGER NOT NULL,
        policy INTEGER NOT NULL, publication_frozen INTEGER NOT NULL
    )""",
    "INSERT INTO settings VALUES (1, 0, 1, 0)",
    """CREATE TABLE messages (
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, tick INTEGER NOT NULL
    )""",
    """CREATE TABLE work (
        id INTEGER PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL,
        phase TEXT NOT NULL, revision TEXT NOT NULL, data TEXT NOT NULL
    )""",
    """CREATE TABLE revisions (
        digest TEXT PRIMARY KEY, work INTEGER NOT NULL REFERENCES work(id),
        body TEXT NOT NULL
    )""",
    """CREATE TABLE commands (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL
    )""",
    """CREATE TABLE grants (
        id INTEGER PRIMARY KEY, work INTEGER NOT NULL REFERENCES work(id),
        action TEXT NOT NULL, binding TEXT NOT NULL, principal TEXT NOT NULL,
        policy INTEGER NOT NULL, expiry INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE attempts (
        id INTEGER PRIMARY KEY, work INTEGER NOT NULL REFERENCES work(id),
        generation INTEGER NOT NULL, phase TEXT NOT NULL, revision TEXT NOT NULL,
        candidate TEXT, worker TEXT NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL,
        UNIQUE(work, generation)
    )""",
    "CREATE UNIQUE INDEX one_live_attempt ON attempts(status) WHERE status = 'active'",
    """CREATE TABLE checkpoints (
        id INTEGER PRIMARY KEY, work INTEGER NOT NULL REFERENCES work(id),
        attempt INTEGER NOT NULL UNIQUE REFERENCES attempts(id), phase TEXT NOT NULL,
        artifact TEXT NOT NULL, candidate TEXT
    )""",
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY, tick INTEGER NOT NULL, kind TEXT NOT NULL,
        work INTEGER REFERENCES work(id), command TEXT, actor TEXT NOT NULL,
        policy INTEGER NOT NULL, data TEXT NOT NULL
    )""",
    """CREATE TABLE effects (
        id INTEGER PRIMARY KEY, work INTEGER NOT NULL REFERENCES work(id),
        operation TEXT NOT NULL, target TEXT NOT NULL, binding TEXT NOT NULL,
        command TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
        grant_id INTEGER NOT NULL REFERENCES grants(id), status TEXT NOT NULL, receipt TEXT
    )""",
    """CREATE UNIQUE INDEX quarantined_target ON effects(target)
       WHERE status IN ('armed', 'unknown', 'mismatch')""",
    """CREATE TABLE workers (
        id TEXT PRIMARY KEY, priority INTEGER NOT NULL, capabilities TEXT NOT NULL,
        allowed INTEGER NOT NULL, version INTEGER NOT NULL
    )""",
)

COORDINATOR_MIGRATIONS = ((
    """CREATE TABLE linux_runs (
        attempt INTEGER PRIMARY KEY REFERENCES attempts(id),
        command TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, status TEXT NOT NULL
    )""",
), (
    """CREATE TABLE source_authorizations (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL, resource TEXT NOT NULL,
        revision TEXT NOT NULL, snapshot TEXT NOT NULL, policy INTEGER NOT NULL,
        UNIQUE(path,resource,revision,snapshot,policy)
    )""",
), (
    """CREATE TABLE pilot_authorizations (
        id INTEGER PRIMARY KEY, checkout TEXT NOT NULL, environment TEXT NOT NULL,
        resource TEXT NOT NULL, manifest TEXT NOT NULL, policy INTEGER NOT NULL
    )""",
))


class Artifacts:
    MAX_BYTES = 32768

    def __init__(self, root, *, create=True):
        self.root = private_directory(root, create=create)

    def put(self, value, *, crash=False):
        contents = encode(value).encode("ascii")
        if len(contents) > self.MAX_BYTES:
            raise EvidenceError("artifact_too_large")
        key = hashlib.sha256(contents).hexdigest()
        fd, staging = tempfile.mkstemp(prefix=".staging-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(staging, self.root / key)
            except FileExistsError:
                self.get(key)
            directory = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if crash:
                raise InjectedCrash("after artifact publication, before checkpoint")
        finally:
            os.unlink(staging)
        return key

    def get(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise EvidenceError("invalid_artifact")
        try:
            fd = os.open(self.root / key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise EvidenceError("unsafe_artifact")
                contents = stream.read(self.MAX_BYTES + 1)
        except OSError as exc:
            raise EvidenceError("missing_or_unsafe_artifact") from exc
        if len(contents) > self.MAX_BYTES or hashlib.sha256(contents).hexdigest() != key:
            raise EvidenceError("corrupt_artifact")
        try:
            return json.loads(contents)
        except (ValueError, UnicodeError) as exc:
            raise EvidenceError("invalid_artifact_encoding") from exc
