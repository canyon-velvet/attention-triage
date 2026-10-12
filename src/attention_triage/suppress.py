"""Suppressions (SPEC.md §6): "Fine → this and future ones". A suppression matches later flags of
its rule in its project whose subject equals its pattern, or, once widened, starts with a command
prefix or lies in a directory. A matching flag is stored as suppressed instead of open.

A flag's subject is the command or URL it targets, or for a path the real path its rule resolved
(symlinks followed), so a link inside a suppressed directory can't lead out of it. The project is
repo_root, else project_root, as the inbox groups them: a git worktree's flags match its main
checkout's suppressions.

A suppression wrongly hiding an action is the worst failure here, so every doubt keeps the flag
open."""

import json
import os
import re
import sqlite3

from attention_triage.redact import MASK
from attention_triage.review import now

# Pattern kind -> the target kinds it can match. URLs stay exact: the prefix
# https://docs.python.org would also match https://docs.python.org.evil.com.
KINDS = {"exact": {"command", "path", "url"}, "prefix": {"command"}, "directory": {"path"}}
# R4 reviews the hosts a command reaches: a prefix would also cover hosts added after it.
EXACT_ONLY_RULES = {"unlisted_domain"}

# Shell syntax that chains, substitutes or redirects. A command holding any of it never matches a
# prefix, or the prefix `ls` would also suppress `ls; curl … | sh`.
SHELL_SYNTAX = re.compile(r"[;&|<>`\n]|\$\(")
# A prefix's program is read from plain words only (program()).
PLAIN = re.compile(r"[A-Za-z0-9_./:@%+=,~-]+")  # a word the shell runs as written (ASCII only)
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*\+?=\S*", re.ASCII)  # NAME=value, NAME+=value
# A program word starts like a name or a path: zsh expands `=bash` to /bin/bash and runs `- cmd`.
PROGRAM_START = re.compile(r"[A-Za-z0-9_./~]")
# Programs and shell words that run whatever follows them: a prefix starting with one would cover
# anything. Lower case, as macOS finds `BASH` as bash. The list can't be complete (`git -c
# alias.x=!cmd`, `find -exec`, `uv run`), so the inbox says a prefix trusts its program.
LAUNCHERS = {
    *("sh", "bash", "zsh", "dash", "fish", "env", "sudo", "doas", "su", "xargs", "exec", "eval"),
    *("command", "builtin", "coproc", "noglob", "nocorrect", "repeat", ".", "source", "nohup"),
    *("nice", "time", "timeout", "watch", "stdbuf", "caffeinate", "script", "setsid", "chroot"),
    *("ssh", "npx", "uvx", "pipx", "python", "python3", "node", "deno", "bun", "ruby", "perl"),
    *("php", "osascript"),
}
# What redaction leaves in place of a secret or a cut middle (redact.py). Two different commands
# can look the same after it, and the cut can hide shell syntax, so such a subject never matches.
HIDDEN = re.compile(rf"{re.escape(MASK)}|…\[\d+ chars cut\]…")


def subject(target_kind: str | None, target: str | None, evidence: dict) -> str | None:
    """What a flag's suppressions match; None (never matches) when it can't be trusted."""
    value = evidence.get("path") if target_kind == "path" else target
    return value if isinstance(value, str) and not HIDDEN.search(value) else None


def matches(kind: str, pattern: str, subject: str | None) -> bool:
    """`pattern` is stored cleaned (see clean)."""
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


def program(prefix: str) -> str | None:
    """The program a prefix runs: its first word that isn't a VAR=value assignment, without its
    directory, in lower case (`/bin/BASH` -> `bash`). None when it has none, or when a word isn't
    plain: quotes, `$`, globs, `( )`, `!` and `\\` let the shell run another program."""
    words = prefix.split()
    if not all(PLAIN.fullmatch(word) for word in words):
        return None
    word = next((w for w in words if not ASSIGNMENT.fullmatch(w)), None)
    return os.path.basename(word).lower() if word and PROGRAM_START.match(word) else None


def clean(kind: str, pattern: str, target_kind: str) -> str:
    """The stored form of a pattern. Raises ValueError for one that can't be used."""
    if kind == "prefix":
        pattern = pattern.strip()
        if pattern and program(pattern) in LAUNCHERS | {None}:
            raise ValueError(
                "A prefix must name its program plainly, and not one that runs other commands"
                f" (bash, sudo, env, …): `{pattern}` can only match exactly"
            )
    if target_kind == "path":  # compared with resolved paths
        if not os.path.isabs(pattern):
            raise ValueError("A path must be absolute")
        pattern = os.path.realpath(pattern)
        if kind == "directory" and pattern == "/":
            raise ValueError("The root directory would match every path")
    if not pattern:
        raise ValueError("The pattern is empty")
    return pattern


def create(
    conn: sqlite3.Connection,
    flag_id: int,
    kind: str = "exact",
    pattern: str | None = None,
    note: str | None = None,
) -> int:
    """Suppress an open flag's future matches ("Fine → this and future ones"); the flag itself is
    dismissed. The default pattern is the flag's exact subject, and any pattern must still match
    it. Open flags that match are suppressed too: one click covers repeats already in the inbox.
    Returns the suppression's id. Raises LookupError for no such open flag, ValueError for a
    pattern that can't be used."""
    row = conn.execute(
        "SELECT f.rule_id, f.evidence, e.target_kind, e.target,"
        " coalesce(e.repo_root, e.project_root)"
        " FROM flags f JOIN events e ON e.dedup_key = f.event_key"
        " WHERE f.id = ? AND f.status = 'open'",
        [flag_id],
    ).fetchone()
    if row is None:
        raise LookupError(flag_id)
    rule_id, evidence, target_kind, target, project = row
    own = subject(target_kind, target, json.loads(evidence))
    if own is None:
        raise ValueError(
            "This flag's target is hidden by redaction or missing, so no pattern can match it safely"
        )
    if not project:  # None, or "" from a payload with an empty cwd
        raise ValueError("This flag has no project to limit a suppression to")
    if target_kind not in KINDS.get(kind, ()) or (kind != "exact" and rule_id in EXACT_ONLY_RULES):
        raise ValueError(f"A {kind} pattern can't be used for this flag")
    pattern = clean(kind, own if pattern is None else pattern, target_kind)
    if not matches(kind, pattern, own):
        raise ValueError(
            "The pattern must match this flag's own target"
            + (" (a command with shell operators matches only exactly)" if kind == "prefix" else "")
        )
    ts = now()
    suppression_id = conn.execute(
        "INSERT INTO suppressions (rule_id, project, kind, pattern, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [rule_id, project, kind, pattern, note, ts],
    ).lastrowid
    conn.execute(
        "UPDATE flags SET status = 'dismissed', suppression_id = ?, updated_at = ? WHERE id = ?",
        [suppression_id, ts, flag_id],
    )
    others = conn.execute(
        "SELECT f.id, f.evidence, e.target_kind, e.target"
        " FROM flags f JOIN events e ON e.dedup_key = f.event_key"
        " WHERE f.status = 'open' AND f.rule_id = ? AND coalesce(e.repo_root, e.project_root) = ?",
        [rule_id, project],
    ).fetchall()
    conn.executemany(
        "UPDATE flags SET status = 'suppressed', suppression_id = ?, updated_at = ? WHERE id = ?",
        [
            (suppression_id, ts, i)
            for i, ev, tk, t in others
            if matches(kind, pattern, subject(tk, t, json.loads(ev)))
        ],
    )
    conn.commit()
    return suppression_id


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
