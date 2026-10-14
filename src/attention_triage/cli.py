"""triage: install and uninstall the capture hook in Claude Code's user settings (ADR-0004), serve
the review UI, and re-check stored events against the policy."""

import argparse
import copy
import difflib
import json
import shlex
import shutil
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from attention_triage import policy, review, server, store
from attention_triage.reeval import reeval

HOOK = "triage-hook"
TOOL_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
]
OTHER_EVENTS = ["SessionStart", "SessionEnd", "Stop", "SubagentStart", "SubagentStop"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="triage")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="add the capture hook to Claude Code")
    install_parser.add_argument("--dry-run", action="store_true", help="show the diff only")
    commands.add_parser("uninstall", help="remove Triage's hook entries")
    ui_parser = commands.add_parser("ui", help="serve the review API on 127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    reeval_parser = commands.add_parser("reeval", help="re-check stored events against the policy")
    reeval_parser.add_argument(
        "--since",
        type=local_time,
        help="only events after this local date or time, e.g. 2026-10-12",
    )
    args = parser.parse_args(argv)
    if args.command == "install":
        install(args.dry_run)
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "reeval":
        recheck(args.since)
    else:
        server.serve(args.port)


def local_time(text: str) -> str:
    """A date or date-time in local time, as the UTC timestamp events are stored with."""
    try:
        return review.now(datetime.fromisoformat(text))  # a naive time is read as local
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date or time: {text!r}") from None


def recheck(since: str | None) -> None:
    try:
        with closing(store.connect()) as conn:
            counts = reeval(conn, since)
    except policy.PolicyError as e:
        sys.exit(f"policy.yaml is invalid, so nothing was changed: {e}")
    print(
        f"reeval: {counts['added']} added, {counts['corrected']} corrected,"
        f" {counts['reopened']} reopened (policy {counts['policy_version'][:7]})"
    )


def install(dry_run: bool) -> None:
    hook = Path(sys.executable).parent / HOOK  # the console script installed next to this Python
    if not hook.exists():
        sys.exit(f"{hook} not found; install Triage with `uv tool install` first.")
    old_text = read_settings()
    old = json.loads(old_text or "{}")
    new = without_ours(old)
    handler = {"type": "command", "command": shlex.quote(str(hook)), "async": True}
    for event in TOOL_EVENTS + OTHER_EVENTS:
        group = (
            {"matcher": "*", "hooks": [handler]} if event in TOOL_EVENTS else {"hooks": [handler]}
        )
        new.setdefault("hooks", {}).setdefault(event, []).append(group)
    # Synchronous, because Claude Code ignores async hooks' output (SPEC §7, in-session notice).
    notice = {"type": "command", "command": f"{shlex.quote(str(hook))} --session-notice"}
    new["hooks"]["SessionStart"].append({"hooks": [notice]})
    if new == old:
        print("Triage's hooks are already installed.")
    elif save(old_text, new, dry_run):
        store.data_dir().mkdir(parents=True, exist_ok=True)
        print("Installed. Restart running Claude Code sessions to start capturing.")


def uninstall() -> None:
    old_text = read_settings()
    old = json.loads(old_text or "{}")
    new = without_ours(old)
    if new == old:
        print("Triage's hooks are not installed.")
    elif save(old_text, new, dry_run=False):
        print("Uninstalled.")


def is_ours(handler: dict) -> bool:
    """Ours = the command runs a program named triage-hook, wherever it was installed from."""
    try:
        words = shlex.split(handler.get("command", ""))
    except ValueError:  # unbalanced quotes: a user's command, not ours
        return False
    return bool(words) and Path(words[0]).name == HOOK


def without_ours(settings: dict) -> dict:
    """Copy of settings without Triage's handlers. A group, event or `hooks` key is dropped only
    when removing ours left it empty, so install then uninstall gives back the same settings
    (byte for byte for a file in Claude Code's own 2-space format). One exception: an event list
    or `hooks` object that was already empty before install is dropped too."""
    settings = copy.deepcopy(settings)
    hooks = settings.get("hooks", {})
    had_hooks = bool(hooks)
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            handlers = group.get("hooks", [])
            rest = [handler for handler in handlers if not is_ours(handler)]
            if len(rest) == len(handlers):
                kept.append(group)
            elif rest:
                kept.append({**group, "hooks": rest})
        if groups and not kept:
            del hooks[event]
        else:
            hooks[event] = kept
    if had_hooks and not hooks:
        del settings["hooks"]
    return settings


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def read_settings() -> str:
    path = settings_path()
    return path.read_text() if path.exists() else ""


def save(old_text: str, new: dict, dry_run: bool) -> bool:
    """Show the diff from old_text, then back up and write unless it's a dry run or the user
    declines. Stops if the file changed while the user was deciding, so no edit is lost."""
    path = settings_path()
    new_text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), str(path), str(path)
    )
    sys.stdout.writelines(diff)
    if dry_run:
        print("Dry run: nothing written.")
        return False
    try:
        reply = input(f"Write {path}? [y/N] ")
    except EOFError:  # no one to answer, e.g. stdin closed in a script: same as "no"
        reply = ""
    if reply.strip().lower() != "y":
        print("Nothing written.")
        return False
    if read_settings() != old_text:
        sys.exit(f"{path} changed while waiting; nothing written. Run the command again.")
    if path.exists():
        backup = path.with_name(f"settings.json.triage-backup-{datetime.now():%Y%m%d-%H%M%S-%f}")
        shutil.copy2(path, backup)
        print(f"Backed up to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)  # in place, so a symlinked settings.json stays a symlink
    return True
