"""Fix-it guidance for an inbox item (SPEC.md §6): the files its call changed, the session's commits
made after it, and copy-ready git commands. Built from what was captured; Triage never runs git
(ADR-0002). How to stop a rule flagging something is left to suppressions and their suggestions.

Commands are for the project the call ran in, and only when that is a git checkout. A file outside
it gets no commands: git can't diff or restore it from there. Every path is shell-quoted."""

import json
import os
import re
import shlex
import sqlite3

from attention_triage.suppress import HIDDEN

# `git commit` prints "[main 1a2b3c4] subject", or "[main (root-commit) 1a2b3c4]", or
# "[detached HEAD 1a2b3c4]". Only the hex id goes into a command.
COMMIT = re.compile(r"^\[[^\]\n]+? ([0-9a-f]{7,40})\] (.*)$", re.MULTILINE)
FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}


def guidance(conn: sqlite3.Connection, event_key: str, evidence: dict) -> dict:
    """{"files", "commits"} for a flag on the call stored as `event_key`, with its `evidence`.
    Commits are those reported by this call or any later one in its session."""
    session_id, tool, tool_use_id, target, root, ts = conn.execute(
        "SELECT session_id, tool_name, tool_use_id, target, project_root, ts FROM events"
        " WHERE dedup_key = ?",
        [event_key],
    ).fetchone()
    results = conn.execute(
        "SELECT tool_use_id, project_root, raw FROM events WHERE session_id = ?"
        " AND event_type = 'tool_result' AND (tool_use_id = ? OR ts > ?) ORDER BY ts, id",
        [session_id, tool_use_id, ts],
    ).fetchall()
    responses = [(t, r, json.loads(raw).get("tool_response")) for t, r, raw in results]
    # This call's result; none if denied, failed or running. A drifted call without an id has no
    # result of its own (None == None would match another call's, as SQL's NULL never does).
    own = [resp for t, _, resp in responses if tool_use_id is not None and t == tool_use_id]
    if tool in FILE_TOOLS:  # the path as the rule resolved it, once the write really ran
        changed = [evidence.get("path") or target] if own else []
    else:  # Claude Code lists the files a Bash command changed
        diff = own[0].get("bashEditDiff") if own and isinstance(own[0], dict) else None
        changed = diff.get("changedFiles", []) if isinstance(diff, dict) else []
    return {
        "files": [file(root, p) for p in changed if isinstance(p, str) and not HIDDEN.search(p)],
        "commits": [
            commit
            for t, result_root, resp in responses
            if isinstance(resp, dict) and isinstance(resp.get("stdout"), str)
            for commit in commits(result_root, resp["stdout"])
        ],
    }


def file(root: str | None, path: str) -> dict:
    """A changed file, with diff and restore commands when it lies in the git checkout `root`."""
    if not checkout(root) or not os.path.isabs(path):
        return {"path": path, "commands": []}
    real_root, real_path = os.path.realpath(root), os.path.realpath(path)
    if os.path.commonpath([real_root, real_path]) != real_root or real_path == real_root:
        return {"path": path, "commands": []}
    git, rel = f"git -C {shlex.quote(root)}", shlex.quote(os.path.relpath(real_path, real_root))
    return {"path": path, "commands": [f"{git} diff -- {rel}", f"{git} restore -- {rel}"]}


def commits(root: str | None, stdout: str) -> list[dict]:
    """The commits a command's output reports, each with a revert command."""
    if not checkout(root):
        return []
    return [
        {"sha": sha, "subject": subject, "command": f"git -C {shlex.quote(root)} revert {sha}"}
        for sha, subject in COMMIT.findall(stdout)
    ]


def checkout(root: str | None) -> bool:
    """Is `root` (still) a git checkout? Read from the disk: the project may be gone by now."""
    return bool(root) and os.path.exists(os.path.join(root, ".git"))
