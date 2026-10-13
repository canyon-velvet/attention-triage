"""Suppressions (SPEC.md §6): "Fine → this and future ones". A suppression matches later flags of
its rule in its project whose subject equals its pattern, or, once widened, starts with a command
prefix or lies in a directory. A matching flag is stored as suppressed instead of open.

A flag's subject is the command or URL it targets, or for a path the real path its rule resolved
(symlinks followed), so a link inside a suppressed directory can't lead out of it. The project is
repo_root, else project_root, as the inbox groups them: a git worktree's flags match its main
checkout's suppressions.

A suppression wrongly hiding an action is the worst failure here, so every doubt keeps the flag
open."""

import re
import sqlite3

from attention_triage.redact import MASK

# Shell syntax that chains, substitutes or redirects. A command holding any of it never matches a
# prefix, or the prefix `ls` would also suppress `ls; curl … | sh`.
SHELL_SYNTAX = re.compile(r"[;&|<>`\n]|\$\(")
# What redaction leaves in place of a secret or a cut middle (redact.py). Two different commands
# can look the same after it, and the cut can hide shell syntax, so such a subject never matches.
HIDDEN = re.compile(rf"{re.escape(MASK)}|…\[\d+ chars cut\]…")


def subject(target_kind: str | None, target: str | None, evidence: dict) -> str | None:
    """What a flag's suppressions match; None (never matches) when it can't be trusted."""
    value = evidence.get("path") if target_kind == "path" else target
    return value if isinstance(value, str) and not HIDDEN.search(value) else None


def matches(kind: str, pattern: str, subject: str | None) -> bool:
    """`pattern` is stored cleaned: a directory resolved, a prefix stripped."""
    if subject is None:
        return False
    if kind == "exact":
        return subject == pattern
    if kind == "prefix":  # whole words only: `git` doesn't match `gitk`
        rest = subject.removeprefix(pattern)
        return (
            rest != subject
            and (rest == "" or rest[0] in " \t")  # what bash splits words on
            and not SHELL_SYNTAX.search(subject)
        )
    if kind == "directory":
        return subject == pattern or subject.startswith(pattern + "/")
    return False


def apply(conn: sqlite3.Connection, event: dict, flags: list[dict]) -> list[dict]:
    """Mark each new flag of `event` that a suppression matches as suppressed (capture time)."""
    # Older spool lines lack repo_root. A flag with no project never matches (`= NULL`).
    project = event.get("repo_root") or event.get("project_root")
    for flag in flags:
        found = subject(event["target_kind"], event["target"], flag["evidence"])
        for suppression_id, kind, pattern in conn.execute(
            "SELECT id, kind, pattern FROM suppressions WHERE rule_id = ? AND project = ?"
            " ORDER BY id",
            [flag["rule_id"], project],
        ):
            if matches(kind, pattern, found):
                flag.update(status="suppressed", suppression_id=suppression_id)
                break
    return flags
