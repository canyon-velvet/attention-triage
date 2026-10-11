"""What happened to a flagged tool call (SPEC.md §5 "Outcome"). Derived from the call's other events
each time the digest is read, never stored on the flag (ADR-0003): hooks run async, so those events
arrive in any order, and one stored late is simply picked up by the next read."""

import json
import sqlite3
from datetime import datetime

from attention_triage.normalize import text


def outcome(conn: sqlite3.Connection, event_key: str) -> dict:
    """{"kind", "error"} for the tool_call event stored as `event_key`. Kinds: ran (without a
    prompt), approved (after a prompt), failed, approved_failed, denied_by_auto_mode,
    denied_by_user, unknown (or still in progress). Only the failed kinds have error text."""
    call = conn.execute(
        "SELECT session_id, tool_use_id, tool_name, ts, raw FROM events WHERE dedup_key = ?",
        [event_key],
    ).fetchone()
    session_id, tool_use_id, tool_name, ts, raw = call
    results = dict(
        conn.execute(
            "SELECT event_type, summary FROM events WHERE session_id = ? AND tool_use_id = ?"
            " AND event_type IN ('tool_result', 'tool_failure', 'permission_denied')",
            [session_id, tool_use_id],
        )
    )
    prompted = was_prompted(conn, event_key, session_id, tool_name, raw)
    if "tool_failure" in results:
        # A drifted payload's error may not be text (check_shape records it); the inbox gets none.
        error = text(json.loads(results["tool_failure"]).get("error"))
        return {"kind": "approved_failed" if prompted else "failed", "error": error}
    if "tool_result" in results:
        return {"kind": "approved" if prompted else "ran", "error": None}
    if "permission_denied" in results:
        return {"kind": "denied_by_auto_mode", "error": None}
    # A user denial fires no hook event: the call just never gets a result, and the turn ends.
    if prompted and turn_ended_after(conn, session_id, ts):
        return {"kind": "denied_by_user", "error": None}
    return {"kind": "unknown", "error": None}


def turn_ended_after(conn, session_id, ts) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND event_type IN ('turn_end', 'session_end')"
            " AND ts > ?",
            [session_id, ts],
        ).fetchone()
    )


def was_prompted(conn, event_key, session_id, tool_name, raw) -> bool:
    """A PermissionRequest has no tool_use_id, so it pairs with the call of the same session, tool
    and identical tool_input nearest to it in time. Nearest, not any: a session can repeat a call,
    unprompted after "don't ask again", and a request can be stored just before its call. `raw` is
    dumped with sorted keys, so identical inputs extract to identical text.

    Known limit: a call repeated in one turn and prompted again sends a byte-identical request,
    which dedup drops as a redelivery, so the repeat reads as unprompted: ran or failed instead of
    approved or approved_failed, and unknown instead of denied_by_user."""

    def same_input(event_type: str) -> list[tuple[str, datetime]]:
        rows = conn.execute(
            "SELECT dedup_key, ts FROM events WHERE session_id = ? AND event_type = ?"
            " AND tool_name = ?"
            " AND json_extract(raw, '$.tool_input') = json_extract(?, '$.tool_input') ORDER BY ts",
            [session_id, event_type, tool_name, raw],
        )
        return [(key, datetime.fromisoformat(ts)) for key, ts in rows]

    # Requests first: a session has far fewer than calls, and most calls have none to pair.
    if not (requests := same_input("permission_request")):
        return False
    calls = same_input("tool_call")
    return any(min(calls, key=lambda call: abs(call[1] - at))[0] == event_key for _, at in requests)
