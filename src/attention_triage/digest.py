"""The review digest (SPEC.md §6): headline counts, and open flags grouped by project → session."""

import json
import sqlite3

from attention_triage.guidance import guidance
from attention_triage.outcome import outcome
from attention_triage.review import last_review, now


def digest(conn: sqlite3.Connection) -> dict:
    """Flags sort high severity first, then oldest first. Projects and sessions keep the order of
    their first flag, so a group holding a high flag comes before one that doesn't. Each flag
    carries its call's outcome (outcome.py) and fix-it guidance (guidance.py), worked out now from
    the events stored so far.

    A git worktree's flags group under its main checkout (repo_root); each flag's `project` still
    names the worktree it ran in. Events stored before repo_root existed group by project_root."""
    # Taken first: "mark all reviewed" sends it back to start the next window, so actions stored
    # while this page was open are counted next time, not skipped.
    as_of = now()
    since = last_review(conn)  # None before the first "mark all reviewed": every kept action
    [actions] = conn.execute(
        "SELECT count(*) FROM events WHERE event_type = 'tool_call' AND ts > ?", [since or ""]
    ).fetchone()
    [suppressed] = conn.execute(  # same window: flags hidden by a "this and future ones"
        "SELECT count(*) FROM flags f JOIN events e ON e.dedup_key = f.event_key"
        " WHERE f.status = 'suppressed' AND e.ts > ?",
        [since or ""],
    ).fetchone()
    cursor = conn.execute(
        """
        SELECT f.id, e.tool_name AS tool, e.target_kind, e.target, e.stated_reason,
               f.rule_id AS rule, f.severity, f.label, f.evidence, e.ts AS time,
               e.project_root AS project, e.session_id,
               coalesce(e.repo_root, e.project_root) AS repo, f.event_key
        FROM flags f JOIN events e ON e.dedup_key = f.event_key
        WHERE f.status = 'open'
        ORDER BY f.severity != 'high', e.ts, f.id
        """
    )
    cursor.row_factory = sqlite3.Row
    projects: dict[str | None, dict[str | None, list[dict]]] = {}
    for row in cursor:
        flag = {**dict(row), "evidence": json.loads(row["evidence"])}
        session_id, repo = flag.pop("session_id"), flag.pop("repo")
        key = flag.pop("event_key")
        flag["outcome"] = outcome(conn, key)
        flag["guidance"] = guidance(conn, key, flag["evidence"])
        projects.setdefault(repo, {}).setdefault(session_id, []).append(flag)
    return {
        "as_of": as_of,
        "headline": {
            "actions": actions,
            "need_review": sum(len(f) for s in projects.values() for f in s.values()),
            "suppressed": suppressed,
            "since": since,
        },
        "projects": [
            {
                "project": project,
                "sessions": [{"session_id": s, "flags": flags} for s, flags in sessions.items()],
            }
            for project, sessions in projects.items()
        ],
    }
