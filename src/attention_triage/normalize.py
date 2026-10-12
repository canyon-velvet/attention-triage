"""Claude Code adapter: hook payload -> agent-neutral event.

This module knows Claude Code's payload format (hook.py also reads `hook_event_name` to label drift
rows); storage, rules and the UI read the neutral event fields. Missing or mistyped fields become
None. Drift is recorded only for the fields Triage relies on: EXPECTED_FIELDS and
EXPECTED_TOOL_INPUT (check_shape).

Supporting another agent (Codex, Cursor, ...) means adding a sibling adapter, not changing this one:
1. move this module to adapters/claude_code.py;
2. add adapters/<agent>.py with that agent's recorded fixtures and expected fields;
3. have the installed hook name its agent (`triage-hook --agent <agent>`) to pick the adapter.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

AGENT = "claude-code"
INSTALL_SCOPE = "user"

EVENT_TYPES = {
    "PreToolUse": "tool_call",
    "PostToolUse": "tool_result",
    "PostToolUseFailure": "tool_failure",
    "PermissionRequest": "permission_request",
    "PermissionDenied": "permission_denied",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "Stop": "turn_end",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
}

# tool_input key -> target_kind; the first key present wins.
TARGET_KEYS = [
    ("command", "command"),
    ("file_path", "path"),
    ("notebook_path", "path"),
    ("url", "url"),
]

# Small top-level payload fields worth keeping in the summary.
SUMMARY_KEYS = ["permission_mode", "agent_type", "source", "reason", "error", "is_interrupt"]

# Fields Triage relies on, with their expected type. A missing or mistyped one is payload drift.
COMMON_FIELDS = {"session_id": str, "cwd": str, "transcript_path": str}
TOOL_FIELDS = {"tool_name": str, "tool_input": dict}
EXPECTED_FIELDS = {
    "PreToolUse": {**COMMON_FIELDS, **TOOL_FIELDS, "tool_use_id": str},
    "PostToolUse": {**COMMON_FIELDS, **TOOL_FIELDS, "tool_use_id": str},
    "PostToolUseFailure": {**COMMON_FIELDS, **TOOL_FIELDS, "tool_use_id": str, "error": str},
    "PermissionRequest": {**COMMON_FIELDS, **TOOL_FIELDS},
    "PermissionDenied": {**COMMON_FIELDS, **TOOL_FIELDS, "tool_use_id": str},
    "SessionStart": COMMON_FIELDS,
    "SessionEnd": COMMON_FIELDS,
    "Stop": COMMON_FIELDS,
    "SubagentStart": {**COMMON_FIELDS, "agent_id": str},
    "SubagentStop": {**COMMON_FIELDS, "agent_id": str},
}
EXPECTED_TOOL_INPUT = {
    "Bash": "command",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
    "WebFetch": "url",
}


def normalize(payload: dict, now: datetime | None = None) -> dict:
    """Map one hook payload to the neutral event shape. Unknown event names pass through."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    target_kind, target = next(
        ((kind, tool_input[key]) for key, kind in TARGET_KEYS if key in tool_input), (None, None)
    )
    hook_event = text(payload.get("hook_event_name"))
    event_type = EVENT_TYPES.get(hook_event, hook_event)
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    session_id, tool_use_id, cwd = (
        text(payload.get("session_id")),
        text(payload.get("tool_use_id")),
        text(payload.get("cwd")),
    )
    root = project_root(cwd)
    ts = (now or datetime.now(UTC)).isoformat(timespec="milliseconds")
    summary = {key: payload[key] for key in SUMMARY_KEYS if key in payload}
    if tool_input.get("dangerouslyDisableSandbox") is True:  # Bash only; absent when not set
        summary["sandbox_disabled"] = True
    return {
        "agent": AGENT,
        "session_id": session_id,
        "agent_id": text(payload.get("agent_id")),
        "event_type": event_type,
        "tool_name": text(payload.get("tool_name")),
        "tool_use_id": tool_use_id,
        "target_kind": target_kind,
        "target": text(target),
        "stated_reason": text(tool_input.get("description")),
        "project_root": root,
        "repo_root": repo_root(root),
        "cwd": cwd,
        "ts": ts,
        "summary": summary,
        "raw": raw,
        "dedup_key": dedup_key(session_id, event_type, tool_use_id, raw, ts),
        "install_scope": INSTALL_SCOPE,
    }


def check_shape(payload: dict) -> list[tuple[str, str]]:
    """(field, "missing" | "type") for each expected field that drifted. Unknown events have no expectations."""
    expected = {
        "hook_event_name": str,
        **EXPECTED_FIELDS.get(text(payload.get("hook_event_name")), {}),
    }
    found = dict(payload)
    tool_input = payload.get("tool_input")
    if (
        "tool_input" in expected
        and isinstance(tool_input, dict)
        and text(payload.get("tool_name")) in EXPECTED_TOOL_INPUT
    ):
        key = EXPECTED_TOOL_INPUT[payload["tool_name"]]
        expected[f"tool_input.{key}"] = str
        if key in tool_input:
            found[f"tool_input.{key}"] = tool_input[key]
    return [
        (field, "missing" if field not in found else "type")
        for field, kind in expected.items()
        if not isinstance(found.get(field), kind)
    ]


def text(value) -> str | None:
    """Neutral text fields hold a string or nothing; drift is reported by check_shape."""
    return value if isinstance(value, str) else None


def dedup_key(session_id, event_type, tool_use_id, raw: str, ts: str) -> str:
    """Same tool call and event -> same key. Events without a tool_use_id, or whose event type is
    unknown (it would merge a call's Pre and Post events), fall back to the full payload.

    A PermissionRequest also adds its capture time: prompts for a call repeated in one turn are
    byte-identical, and each one marks its own call as prompted (outcome.py). Replaying the spool
    still stores it once, since a spooled event keeps the key it was given here."""
    parts = [AGENT, session_id, event_type, (tool_use_id if event_type else None) or raw]
    if event_type == "permission_request":
        parts.append(ts)
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def project_root(cwd: str | None) -> str | None:
    """Git root of cwd (nearest ancestor containing .git), else cwd itself."""
    if not cwd:
        return cwd
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return cwd


def repo_root(project_root: str | None) -> str | None:
    """The main checkout when project_root is a git worktree, so the inbox groups a worktree's
    flags with its repo; else project_root itself. A worktree's .git is a file naming its gitdir
    (<main>/.git/worktrees/<name>), whose `commondir` leads back to <main>/.git. Read at capture:
    worktrees are often deleted before their events are reviewed."""
    if not project_root:
        return project_root
    try:
        line = Path(project_root, ".git").read_text().strip()
        gitdir = Path(project_root, line.removeprefix("gitdir:").strip())
        common = os.path.normpath(gitdir / (gitdir / "commondir").read_text().strip())
    except (OSError, ValueError):  # a .git dir, no .git, a submodule (no commondir), or garbage
        return project_root
    # A bare repo (foo.git) or a --separate-git-dir has no checkout above it: the git dir itself
    # names the repo. Its parent would merge every repo kept in the same folder.
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common
