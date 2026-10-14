"""`triage doctor` (SPEC.md §7, item 4): checks that capture is healthy, and says how to fix what
isn't, in plain language. Each check returns a Result; any failure makes the command exit 1."""

import json
import os
import shlex
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from attention_triage import cli, hook, policy, store


@dataclass
class Result:
    ok: bool
    what: str  # what was checked, or what is wrong
    fix: str = ""  # what to run or change; failures only


def hooks() -> Result:
    """Every event install adds still has Triage's handler, plus the synchronous session notice,
    and the program they run still exists: a hook that never runs can't report itself."""
    path = cli.settings_path()
    try:
        settings = json.loads(cli.read_settings() or "{}")
        groups = settings.get("hooks", {})
        ours = {
            event: [h for g in groups.get(event, []) for h in g.get("hooks", []) if cli.is_ours(h)]
            for event in cli.TOOL_EVENTS + cli.OTHER_EVENTS
        }
    except (OSError, ValueError, AttributeError, TypeError):  # missing is fine: read as ""
        return Result(
            False, f"{path} can't be read as settings", "Fix the file, then run `triage install`."
        )
    # The notice is ours too, but it captures nothing: each event needs a capture handler.
    missing = [event for event, handlers in ours.items() if not any(map(capturing, handlers))]
    if all(map(capturing, ours["SessionStart"])):
        missing.append("the capture-failure notice")
    if missing:
        return Result(
            False, f"Hooks missing from {path}: {', '.join(missing)}", "Run `triage install`."
        )
    programs = {shlex.split(h["command"])[0] for handlers in ours.values() for h in handlers}
    # As the shell finds them: a bare name on PATH, `~` expanded; the file must be executable.
    gone = sorted(p for p in programs if not shutil.which(os.path.expanduser(p)))
    if gone:
        return Result(
            False,
            f"The hook program is gone, so nothing is captured: {', '.join(gone)}",
            "Reinstall Triage (`uv tool install`), then run `triage install`.",
        )
    return Result(True, f"Hooks installed in {path}")


def capturing(handler: dict) -> bool:
    return "--session-notice" not in shlex.split(handler["command"])


def database() -> Result:
    """The DB opens and takes the write lock; nothing is written."""
    path = store.data_dir() / "triage.db"
    try:
        with closing(store.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
    except (sqlite3.Error, OSError) as e:  # OSError: the data dir can't be created
        return Result(
            False,
            f"Can't write to {path}: {e}",
            f"Check that {store.data_dir()} is yours and writable; if the database is locked, close"
            " other programs using it and try again.",
        )
    return Result(True, f"Database writable: {path}")


def spool() -> Result:
    """Events the DB couldn't take are stored now, as the next hook run would."""
    try:
        with closing(store.connect()) as conn:
            hook.flag(conn, store.ingest_spool(conn))
    except Exception as e:  # a DB error, or a spooled line that isn't an event
        return Result(
            False,
            f"Buffered events in {store.spool_path()} can't be stored: {e!r}",
            "Fix the database problem above, then run `triage doctor` again.",
        )
    if store.spool_path().exists():  # a hook spooled again just now: storing is still failing
        return Result(
            False,
            f"Buffered events are still waiting in {store.spool_path()}",
            f"See {store.data_dir() / 'hook-errors.log'} for why, then run `triage doctor` again.",
        )
    return Result(True, "No buffered events waiting")


def policy_file() -> Result:
    try:
        policy.load()
    except (ValueError, OSError) as e:  # PolicyError, or a file that isn't UTF-8
        return Result(
            False,
            f"{policy.path()} is invalid: {e}",
            "Correct it, or delete the file to get the default policy back.",
        )
    return Result(True, f"Policy valid: {policy.path()}")


CHECKS = [hooks, database, spool, policy_file]


def run() -> list[Result]:
    """Every check runs, and none ends doctor in a traceback: one that fails unexpectedly is a
    failed result too."""
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as e:
            results.append(
                Result(
                    False,
                    f"The {check.__name__} check couldn't run: {e!r}",
                    "Fix what the error names, then run `triage doctor` again.",
                )
            )
    return results
