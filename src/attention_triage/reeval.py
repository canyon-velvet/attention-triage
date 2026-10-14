"""`triage reeval [--since]` (SPEC.md §5, ADR-0003): re-runs the rules over stored tool calls with
the current policy.yaml, now that their session's events are stored. It adds the flags they lack
(a tightened policy, an insert that failed at capture) and corrects the ones that differ, keeping
their status. A flag left suppressed by a suppression that no longer exists reopens. It never
removes a flag: loosening the policy is handled in the inbox (#19).

Path rules resolve paths and read settings files when they run, so a re-check sees the disk as it
is now. A flag's recorded path is kept, though: it is the file the write really reached."""

import json
import sqlite3

from attention_triage import policy, rules, store, suppress
from attention_triage.review import now

FIELDS = ["severity", "label", "reason", "evidence", "policy_version"]


def reeval(conn: sqlite3.Connection, since: str | None = None) -> dict:
    """Counts of flags added, corrected and reopened, for tool calls after `since` (UTC, the
    events.ts layout), and the policy_version used. Raises PolicyError, changing nothing, for an
    invalid policy.yaml. The rules run first and the changes are written at the end, in one
    transaction, so the capture hook isn't kept waiting on the write lock meanwhile."""
    loaded, version = policy.load()
    rows = conn.execute(
        f"SELECT {', '.join(store.COLUMNS)} FROM events"
        " WHERE event_type = 'tool_call' AND ts > ? ORDER BY ts, id",
        [since or ""],
    ).fetchall()
    events = [dict(zip(store.COLUMNS, row, strict=True)) for row in rows]
    existing = {
        (key, rule_id): (flag_id, dict(zip(FIELDS, values, strict=True)))
        for flag_id, key, rule_id, *values in conn.execute(
            f"SELECT f.id, f.event_key, f.rule_id, {', '.join('f.' + c for c in FIELDS)}"
            " FROM flags f JOIN events e ON e.dedup_key = f.event_key WHERE e.ts > ?",
            [since or ""],
        )
    }
    new, updates = [], []
    for event in events:
        event["summary"] = json.loads(event["summary"])
        missing = []
        for flag in rules.evaluate(event, loaded, version, conn):
            found = existing.get((flag["event_key"], flag["rule_id"]))
            if found is None:
                missing.append(flag)
                continue
            flag_id, old = found
            old = {**old, "evidence": json.loads(old["evidence"])}
            if old["evidence"].get("path") != flag["evidence"].get("path"):
                continue  # resolved when the rule runs: keep the file the write really reached
            changed = [c for c in FIELDS if old[c] != flag[c]]
            if changed:  # a new policy_version alone isn't counted as a correction
                updates.append((flag_id, flag, changed != ["policy_version"]))
        new += suppress.apply(conn, event, missing)
    ts = now()
    # The first write: from here the transaction holds the write lock until insert_flags commits.
    conn.executemany(
        f"UPDATE flags SET {', '.join(c + ' = ?' for c in FIELDS)}, updated_at = ? WHERE id = ?",
        [[json.dumps(flag[c], ensure_ascii=False) if c == "evidence" else flag[c] for c in FIELDS]
         + [ts, flag_id] for flag_id, flag, _ in updates],
    )  # fmt: skip
    reopened = conn.execute(
        "UPDATE flags SET status = 'open', suppression_id = NULL, updated_at = ?"
        " WHERE status = 'suppressed' AND suppression_id NOT IN (SELECT id FROM suppressions)"
        " AND event_key IN (SELECT dedup_key FROM events WHERE ts > ?)",
        [ts, since or ""],
    ).rowcount
    # The UPDATE above holds the write lock: a suppression undone since apply() ran is gone now,
    # and a new flag it matched must not be stored hidden behind it.
    live = {i for (i,) in conn.execute("SELECT id FROM suppressions")}
    for flag in new:
        if flag.get("suppression_id") not in live | {None}:
            flag.update(status="open", suppression_id=None)
    before = conn.total_changes
    store.insert_flags(conn, new)  # commits everything above with it
    return {
        "added": conn.total_changes - before,  # a flag the hook stored meanwhile is skipped
        "corrected": sum(corrected for _, _, corrected in updates),
        "reopened": reopened,
        "policy_version": version,
    }
