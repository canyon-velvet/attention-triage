"""`triage doctor` (SPEC.md §7, item 4): checks that capture is healthy, and says how to fix what
isn't, in plain language. Each check returns a Result; any failure makes the command exit 1."""

import json
import os
import shlex
import shutil
from dataclasses import dataclass

from attention_triage import cli


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


CHECKS = [hooks]


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
