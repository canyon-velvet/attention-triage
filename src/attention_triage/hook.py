"""triage-hook: store one Claude Code hook event from stdin. Never raises; always exits 0."""

import json
import sys
import traceback
from contextlib import closing
from datetime import UTC, datetime

from attention_triage import policy, rules, store, suppress
from attention_triage.normalize import check_shape, normalize, text
from attention_triage.redact import redact


def main() -> None:
    if sys.argv[1:] == ["--session-notice"]:
        session_notice()
        sys.exit(0)
    try:
        payload = read_payload()
        event = normalize(redact(payload))
        try:
            with closing(store.connect()) as conn:
                # Flag each batch as soon as it is stored, so a later failure can't skip it.
                flag(conn, store.ingest_spool(conn))
                if store.insert_event(conn, event):
                    flag(conn, [event])
                if issues := check_shape(payload):
                    store.record_drift(
                        conn, text(payload.get("hook_event_name")), issues, event["ts"]
                    )
                if event["event_type"] == "session_start":
                    purge_old_events(conn)
        except Exception:
            store.spool(event)  # a delay, not a loss; the error is still logged below
            raise
    except Exception:
        log_error()
    sys.exit(0)


def flag(conn, events: list[dict]) -> None:
    """Run the rules on stored events. They are already stored, so a failure here (an invalid
    policy.yaml, a rule bug) is logged, never spooled."""
    if not events:
        return
    try:
        loaded, version = policy.load()
        store.insert_flags(
            conn,
            [
                f
                for e in events
                for f in suppress.apply(conn, e, rules.evaluate(e, loaded, version, conn))
            ],
        )
    except Exception:
        log_error()


def purge_old_events(conn) -> None:
    """Retention without the UI: runs at each session start, in this async hook so no one waits.
    The event is already stored, so a failed purge is logged, never spooled."""
    try:
        store.purge(conn)
    except Exception:
        log_error()


def session_notice() -> None:
    """Synchronous SessionStart hook (Claude Code ignores async hooks' output): while events are
    buffered in the spool instead of stored, tell the user once per session."""
    try:
        session_id = str(read_payload().get("session_id"))
        notified = store.data_dir() / "notified-sessions"
        if not store.spool_path().exists() or (
            notified.exists() and session_id in notified.read_text().split()
        ):
            return
        log = store.data_dir() / "hook-errors.log"
        lines = log.read_text().splitlines() if log.exists() else []
        reason = next((line for line in reversed(lines) if line.strip()), "unknown error")
        message = (
            f"attention-triage: capture failing ({reason[:200]}); events buffered."
            " Run `triage doctor`."
        )
        print(json.dumps({"systemMessage": message}))
        # Recorded after printing: if this write fails, the user is still told (just again later).
        with open(notified, "a") as f:
            f.write(session_id + "\n")
    except Exception:
        log_error()


def read_payload():
    payload = json.loads(sys.stdin.buffer.read())
    # A lone UTF-16 surrogate (e.g. an emoji cut in half) can't be stored in SQLite; replace it.
    return json.loads(json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace"))


def log_error() -> None:
    try:
        store.data_dir().mkdir(parents=True, exist_ok=True)
        with open(store.data_dir() / "hook-errors.log", "a") as log:
            log.write(f"{datetime.now(UTC).isoformat()} {traceback.format_exc()}\n")
    except OSError:
        pass
