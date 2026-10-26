"""SQLite storage for reminders.

This is the only module that touches SQL. Every write runs in its own short
BEGIN IMMEDIATE ... COMMIT transaction, so a crash leaves each reminder either
fully written or untouched. Status changes are conditional updates
(`... WHERE status = 'pending'`), and SQLite allows one writer at a time, so a
cancel and a dispatcher claim on the same reminder can never both succeed.

All times are integers: UTC microseconds since the Unix epoch.
"""

import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id              TEXT PRIMARY KEY,
  fire_at_us      INTEGER NOT NULL,
  callback_url    TEXT NOT NULL,
  payload         TEXT NOT NULL,
  status          TEXT NOT NULL
                  CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
  created_at_us   INTEGER NOT NULL,
  cancelled_at_us INTEGER,
  attempted_at_us INTEGER,
  lease_until_us  INTEGER,
  response_status INTEGER,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (fire_at_us) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS reminders_lease ON reminders (lease_until_us) WHERE status = 'sending';
"""

LEASE_EXPIRED_ERROR = "server stopped during delivery; outcome unknown"


# Current wall-clock time as UTC microseconds since the epoch.
def now_us() -> int:
    return time.time_ns() // 1000


class ReminderStore:
    """Reads and writes reminders in one SQLite file."""

    # Remembers the database path; connections are opened per operation.
    def __init__(self, db_path: str):
        self.db_path = db_path

    # Opens a connection with durability settings. autocommit=True means Python
    # never opens transactions on its own; we issue BEGIN/COMMIT explicitly.
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, autocommit=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous = FULL")  # a COMMIT survives power loss
        conn.execute("PRAGMA busy_timeout = 5000")  # wait up to 5s for the write lock
        return conn

    # Runs the block inside BEGIN IMMEDIATE ... COMMIT, rolling back on any error.
    # IMMEDIATE takes the write lock up front, so two writers queue instead of
    # failing halfway through.
    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # Creates the table and indexes if missing and switches the file to WAL mode.
    def init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    # Saves a new pending reminder and returns its id. The id is only returned
    # after COMMIT, so a caller holding an id has a durable reminder.
    def create(self, fire_at_us: int, callback_url: str, payload: str, created_at_us: int) -> str:
        reminder_id = str(uuid.uuid4())
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO reminders (id, fire_at_us, callback_url, payload, status, created_at_us)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (reminder_id, fire_at_us, callback_url, payload, created_at_us),
            )
        return reminder_id

    # Returns one reminder as a dict, or None if the id is unknown.
    def get(self, reminder_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None

    # Cancels the reminder if it is still pending, then returns it as it now is
    # (None if unknown). The caller reads the status: 'cancelled' means it will
    # never be sent; anything else means it was too late to cancel.
    def cancel(self, reminder_id: str, cancelled_at_us: int) -> dict | None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE reminders SET status = 'cancelled', cancelled_at_us = ?"
                " WHERE id = ? AND status = 'pending'",
                (cancelled_at_us, reminder_id),
            )
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None

    # Atomically moves up to `limit` due reminders (oldest fire_at first) from
    # pending to sending, stamps when the attempt started and when its lease
    # ends, and returns them. Only rows returned here may be sent.
    def claim_due(self, now: int, limit: int, lease_us: int) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                "UPDATE reminders"
                " SET status = 'sending', attempted_at_us = ?, lease_until_us = ?"
                " WHERE id IN (SELECT id FROM reminders WHERE status = 'pending' AND fire_at_us <= ?"
                "              ORDER BY fire_at_us LIMIT ?)"
                " RETURNING *",
                (now, now + lease_us, now, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # Records a successful delivery. Returns False if the reminder was not in
    # 'sending' (for example, its lease had already expired).
    def mark_sent(self, reminder_id: str, response_status: int) -> bool:
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE reminders SET status = 'sent', response_status = ?"
                " WHERE id = ? AND status = 'sending'",
                (response_status, reminder_id),
            )
        return cur.rowcount == 1

    # Records a failed delivery and why. Returns False if the reminder was not in 'sending'.
    def mark_failed(self, reminder_id: str, response_status: int | None, error: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE reminders SET status = 'failed', response_status = ?, error = ?"
                " WHERE id = ? AND status = 'sending'",
                (response_status, error, reminder_id),
            )
        return cur.rowcount == 1

    # Marks sends whose lease ran out as failed (the process died mid-send, so
    # we cannot know if the receiver got it). They are never resent. Returns how many.
    def fail_expired_leases(self, now: int) -> int:
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE reminders SET status = 'failed', error = ?"
                " WHERE status = 'sending' AND lease_until_us < ?",
                (LEASE_EXPIRED_ERROR, now),
            )
        return cur.rowcount
