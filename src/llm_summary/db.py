"""SQLite access helpers: connection, schema init, transactions, state cursor."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

SCHEMA_RESOURCE = "schema.sql"


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision, 'Z' suffix)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sensible pragmas and Row factory."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_sql() -> str:
    return resources.files("llm_summary").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not exist, then apply in-place migrations."""
    conn.executescript(_schema_sql())
    _migrate(conn)
    conn.commit()


def export_snapshot(conn: sqlite3.Connection, destination: Path) -> None:
    """Atomically publish a standalone snapshot of committed data, including WAL."""
    if conn.in_transaction:
        raise ValueError("Cannot export a database with an active transaction")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Stage on the same filesystem so replace is atomic. A failed backup or
    # replacement leaves the previous download intact and cleans up staging.
    with TemporaryDirectory(prefix=".snapshot-", dir=destination.parent) as staging:
        temporary = Path(staging) / destination.name
        with closing(sqlite3.connect(temporary)) as snapshot:
            conn.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
        temporary.chmod(0o644)
        temporary.replace(destination)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring pre-existing databases up to the current schema."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(objects)")}
    if "snapshot_json" not in columns:
        conn.execute("ALTER TABLE objects ADD COLUMN snapshot_json TEXT")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a unit of work in a transaction (commit on success, rollback on error)."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- state cursor helpers ---------------------------------------------------

def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


STATE_LAST_UNTIL = "github_last_successful_until"
