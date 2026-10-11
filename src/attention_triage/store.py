"""SQLite event store at ~/.attention-triage/triage.db, and the spool that buffers events while
the DB can't be written."""

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Temporary: SPEC §4 reads this from retention_days in policy.yaml (default 7). Deferred until
# the review UI exists (#22).
RETENTION_DAYS = 7

COLUMNS = [
    "agent",
    "session_id",
    "agent_id",
    "event_type",
    "tool_name",
    "tool_use_id",
    "target_kind",
    "target",
    "stated_reason",
    "project_root",
    "repo_root",
    "cwd",
    "ts",
    "summary",
    "raw",
    "dedup_key",
    "install_scope",
]

FLAG_COLUMNS = [
    "event_key",
    "rule_id",
    "severity",
    "reason",
    "label",
    "evidence",
    "policy_version",
    "status",
    "created_at",
    "updated_at",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    session_id TEXT,
    agent_id TEXT,
    event_type TEXT,
    tool_name TEXT,
    tool_use_id TEXT,
    target_kind TEXT,
    target TEXT,
    stated_reason TEXT,
    project_root TEXT,
    repo_root TEXT,
    cwd TEXT,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    raw TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    install_scope TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
-- earlier_events (rules) and outcome.py look up one session's events of a type.
CREATE INDEX IF NOT EXISTS events_session ON events (session_id, event_type);
-- outcome.py finds a call's result by its tool_use_id.
CREATE INDEX IF NOT EXISTS events_tool_use ON events (session_id, tool_use_id);
CREATE TABLE IF NOT EXISTS drift (
    hook_event TEXT NOT NULL,
    field TEXT NOT NULL,
    problem TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (hook_event, field, problem)
);
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES events (dedup_key) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    label TEXT NOT NULL,
    evidence TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    suppression_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (event_key, rule_id)
);
"""


def data_dir() -> Path:
    return Path.home() / ".attention-triage"


def connect() -> sqlite3.Connection:
    """Open the DB in WAL mode with a 2 s busy timeout, creating it if needed. Foreign keys are on,
    so purging an event deletes its flags."""
    path = data_dir() / "triage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    if "repo_root" not in [c[1] for c in conn.execute("PRAGMA table_info(events)")]:
        try:  # a DB created before repo_root existed
            conn.execute("ALTER TABLE events ADD COLUMN repo_root TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):  # else a parallel hook added it first
                raise
    return conn


def insert_event(conn: sqlite3.Connection, event: dict) -> bool:
    """Store a normalized event once. Returns False if its dedup_key is already stored. A missing
    field is stored as NULL: an event spooled before repo_root existed has none."""
    row = {**event, "summary": json.dumps(event["summary"], ensure_ascii=False)}
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
        [row.get(column) for column in COLUMNS],
    )
    conn.commit()
    return cursor.rowcount == 1


def earlier_events(conn: sqlite3.Connection, event: dict, event_type: str) -> list[dict]:
    """Events of one type from the same session, captured no later than `event`, newest first."""
    rows = conn.execute(
        f"SELECT {', '.join(COLUMNS)} FROM events"
        " WHERE session_id = ? AND event_type = ? AND ts <= ? AND dedup_key != ?"
        " ORDER BY ts DESC",
        [event["session_id"], event_type, event["ts"], event["dedup_key"]],
    )
    events = [dict(zip(COLUMNS, row, strict=True)) for row in rows]
    return [{**e, "summary": json.loads(e["summary"])} for e in events]


def insert_flags(conn: sqlite3.Connection, flags: list[dict]) -> None:
    """Store flags; one per event and rule, so a repeat is ignored."""
    conn.executemany(
        f"INSERT OR IGNORE INTO flags ({', '.join(FLAG_COLUMNS)}) VALUES ({', '.join('?' * len(FLAG_COLUMNS))})",
        [
            [
                json.dumps(f[c], ensure_ascii=False) if c == "evidence" else f[c]
                for c in FLAG_COLUMNS
            ]
            for f in flags
        ],
    )
    conn.commit()


def record_drift(
    conn: sqlite3.Connection, hook_event: str | None, issues: list[tuple[str, str]], ts: str
) -> None:
    """Remember each (event, field, problem) the first time it is seen, for `triage doctor`."""
    conn.executemany(
        "INSERT OR IGNORE INTO drift (hook_event, field, problem, first_seen) VALUES (?, ?, ?, ?)",
        [(hook_event or "?", field, problem, ts) for field, problem in issues],
    )
    conn.commit()


def purge(conn: sqlite3.Connection, days: int = RETENTION_DAYS) -> None:
    """Delete events captured more than `days` ago. `ts` is always UTC in one ISO layout, so
    comparing the strings compares the times."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="milliseconds")
    conn.execute("DELETE FROM events WHERE ts < ?", [cutoff])
    conn.commit()


def spool_path() -> Path:
    return data_dir() / "spool.jsonl"


@contextmanager
def spool_lock():
    """Hooks run in parallel: without the lock, an event appended while another hook ingests
    could land in a spool that is about to be deleted."""
    data_dir().mkdir(parents=True, exist_ok=True)
    with open(data_dir() / "spool.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def spool(event: dict) -> None:
    """Buffer an event the DB couldn't take; the next successful hook run stores it."""
    with spool_lock(), open(spool_path(), "a", encoding="utf-8") as f:
        # The leading newline ends a record a killed hook left without one, so this event
        # doesn't join that broken line and get skipped with it.
        f.write("\n" + json.dumps(event, ensure_ascii=False) + "\n")


def ingest_spool(conn: sqlite3.Connection) -> list[dict]:
    """Store spooled events (dedup makes repeats harmless), then delete the spool. If storing
    fails, the spool stays for the next run. Returns every spooled event, repeats included: one
    stored by a run that failed before flagging it still needs its flags."""
    events = []
    if not spool_path().exists():
        return events
    with spool_lock():
        if not spool_path().exists():  # another hook ingested it while we waited
            return events
        # Bytes, split only on \n and \r: str.splitlines() would also split an event on U+2028,
        # which json.dumps(ensure_ascii=False) leaves unescaped.
        for line in spool_path().read_bytes().splitlines():
            try:
                event = json.loads(line)
            except ValueError:  # blank, or cut short (even mid-character) by a killed hook:
                continue  # failing on it would wedge every ingest
            insert_event(conn, event)
            events.append(event)
        spool_path().unlink()
    return events
