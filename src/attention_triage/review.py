"""Review state (SPEC.md §6): the only writes the UI makes, all to Triage's own DB (ADR-0002)."""

import sqlite3
from datetime import UTC, datetime


def set_status(conn: sqlite3.Connection, flag_id: int, status: str) -> bool | None:
    """Move an open flag to `status` (dismissed or acknowledged). None if there is no such flag,
    False if it is no longer open."""
    cursor = conn.execute(
        "UPDATE flags SET status = ?, updated_at = ? WHERE id = ? AND status = 'open'",
        [status, now(), flag_id],
    )
    conn.commit()
    if cursor.rowcount:
        return True
    return False if conn.execute("SELECT 1 FROM flags WHERE id = ?", [flag_id]).fetchone() else None


def mark_all(conn: sqlite3.Connection, flag_ids: list[int], at: datetime | None = None) -> int:
    """Dismiss the listed flags that are still open, and start a new review window. Only the
    flags the page showed: one that arrived after the page loaded stays open, unseen ones are
    never cleared. Returns how many were dismissed."""
    ts = now(at)
    cursor = conn.executemany(
        "UPDATE flags SET status = 'dismissed', updated_at = ? WHERE id = ? AND status = 'open'",
        [(ts, flag_id) for flag_id in flag_ids],
    )
    conn.execute("INSERT INTO reviews (reviewed_at) VALUES (?)", [ts])
    conn.commit()
    return cursor.rowcount


def last_review(conn: sqlite3.Connection) -> str | None:
    """When "mark all reviewed" was last used; None before the first time."""
    [ts] = conn.execute("SELECT max(reviewed_at) FROM reviews").fetchone()
    return ts


def now(at: datetime | None = None) -> str:
    """Same layout as events.ts (UTC, milliseconds), so the two compare as strings."""
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="milliseconds")
