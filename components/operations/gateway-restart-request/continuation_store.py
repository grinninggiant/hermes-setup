"""Metadata-only restart continuation ledger; not a scheduler or effect executor."""
from contextlib import contextmanager, closing
from pathlib import Path
import os
import re
import sqlite3
import uuid


class ContinuationStore:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS intents (
                operation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                route_digest TEXT NOT NULL,
                checkpoint_digest TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                owner_id TEXT
            )""")

    @contextmanager
    def _connection(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=0.25)) as db:
            db.row_factory = sqlite3.Row
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db

    def record(self, operation_id, session_id, route_digest, checkpoint_digest):
        for value, pattern in (
            (operation_id, r"[A-Za-z0-9_.:-]{1,128}"),
            (session_id, r"[A-Za-z0-9_.:-]{1,128}"),
            (route_digest, r"[0-9a-f]{64}"),
            (checkpoint_digest, r"[0-9a-f]{64}"),
        ):
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError("invalid_binding")
        with self._connection(write=True) as db:
            existing = db.execute(
                "SELECT session_id, route_digest, checkpoint_digest FROM intents WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (session_id, route_digest, checkpoint_digest):
                    raise ValueError("binding_conflict")
                return
            db.execute("""INSERT INTO intents
                (operation_id, session_id, route_digest, checkpoint_digest)
                VALUES (?, ?, ?, ?)""", (operation_id, session_id, route_digest, checkpoint_digest))

    def claim(self, operation_id, session_id, route_digest, checkpoint_digest, *, owner_id=None):
        """Atomically reserve an exact binding; a claim is not dispatch acceptance.

        A crash after claiming leaves the operation claimed. Never automatically
        recycle it: recovery needs authoritative admission/turn evidence first.
        """
        owner_id = owner_id if owner_id is not None else uuid.uuid4().hex
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("invalid_owner")
        with self._connection(write=True) as db:
            result = db.execute("""UPDATE intents SET state = 'claimed', owner_id = ?
                WHERE operation_id = ? AND session_id = ? AND route_digest = ?
                AND checkpoint_digest = ? AND state = 'pending'""",
                (owner_id, operation_id, session_id, route_digest, checkpoint_digest))
            return result.rowcount == 1

    def submit(self, operation_id, owner_id, inject):
        """Persist uncertainty before scheduling; serialize scheduling against fences.

        The callback must be the bounded native scheduling call, not a model run.
        Even a process death or post-ACK commit failure leaves a non-replayable
        dispatching record. A scheduling ACK is only submitted, never completed.
        """
        with self._connection(write=True) as db:
            changed = db.execute("""UPDATE intents SET state = 'dispatching'
                WHERE operation_id = ? AND owner_id = ? AND state = 'claimed'""",
                (operation_id, owner_id)).rowcount
            if changed != 1:
                return False
        with self._connection(write=True) as db:
            row = db.execute("""SELECT state FROM intents
                WHERE operation_id = ? AND owner_id = ?""", (operation_id, owner_id)).fetchone()
            if row is None or row["state"] != "dispatching":
                return False
            try:
                accepted = inject() is True
            except Exception:
                accepted = False
            db.execute("UPDATE intents SET state = ? WHERE operation_id = ? AND owner_id = ?",
                       ("submitted" if accepted else "operator_required", operation_id, owner_id))
            return accepted

    def fence(self, session_id, *, authorized: object = False):
        """Caller must establish current native source authorization before fencing."""
        if authorized is not True:
            raise PermissionError("unverified_source")
        with self._connection(write=True) as db:
            db.execute("UPDATE intents SET state = 'cancelled' WHERE session_id = ? AND state IN ('pending', 'claimed', 'dispatching', 'submitted')", (session_id,))

    def get(self, operation_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM intents WHERE operation_id = ?", (operation_id,)).fetchone()
            return dict(row) if row else None
