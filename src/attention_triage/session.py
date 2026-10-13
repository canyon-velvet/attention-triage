"""One session's actions (SPEC.md §6 "Session detail"): every event in order, the main agent's
first, then each subagent's in a section of its own. A tool call is one row: its result, failure
or denial folds into its outcome (outcome.py), and so does a permission prompt. A result whose call
wasn't stored stays a row of its own, so nothing captured goes missing from the page."""

import json
import sqlite3

from attention_triage.outcome import outcome

# Events that only say what happened to a call (by tool_use_id). A PermissionRequest has no
# tool_use_id: outcome.py pairs it with its call by tool and input, and it is never a row.
RESULTS = {"tool_result", "tool_failure", "permission_denied"}
FLAG_FIELDS = ["id", "rule", "severity", "label", "reason", "status"]


def detail(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """{"session_id", "events", "subagents"}, or None for a session with no stored events. Each
    flagged row lists its flags whatever their status, so a dismissed or suppressed one shows too."""
    rows = conn.execute(
        "SELECT dedup_key, agent_id, event_type, tool_name, tool_use_id, target_kind, target,"
        " stated_reason, project_root, ts, summary FROM events WHERE session_id = ? ORDER BY ts, id",
        [session_id],
    ).fetchall()
    if not rows:
        return None
    flags: dict[str, list[dict]] = {}
    for key, *flag in conn.execute(
        "SELECT f.event_key, f.id, f.rule_id, f.severity, f.label, f.reason, f.status"
        " FROM flags f JOIN events e ON e.dedup_key = f.event_key"
        " WHERE e.session_id = ? ORDER BY f.id",
        [session_id],
    ):
        flags.setdefault(key, []).append(dict(zip(FLAG_FIELDS, flag, strict=True)))
    calls = {row[4] for row in rows if row[2] == "tool_call" and row[4]}
    main: list[dict] = []
    subagents: dict[str, dict] = {}
    for key, agent_id, event_type, tool, tool_use_id, target_kind, target, *rest in rows:
        stated_reason, project, ts, summary = rest
        agent_type = json.loads(summary).get("agent_type")
        if event_type == "permission_request" or (event_type in RESULTS and tool_use_id in calls):
            continue
        if event_type == "subagent_stop" and not agent_type:
            continue  # every turn ends with an internal one, with no subagent behind it (SPEC §10)
        row = {
            "event_type": event_type,
            "tool": tool,
            "target_kind": target_kind,
            "target": target,
            "stated_reason": stated_reason,
            "project": project,
            "time": ts,
            "outcome": outcome(conn, key) if event_type == "tool_call" else None,
            "flags": flags.get(key, []),
        }
        if agent_id is None:
            main.append(row)
            continue
        section = subagents.setdefault(
            agent_id, {"agent_id": agent_id, "agent_type": None, "events": []}
        )
        section["agent_type"] = section["agent_type"] or agent_type or None
        section["events"].append(row)
    return {"session_id": session_id, "events": main, "subagents": list(subagents.values())}
