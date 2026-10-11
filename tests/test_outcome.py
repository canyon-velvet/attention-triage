import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from attention_triage import store
from attention_triage.normalize import normalize
from attention_triage.outcome import outcome
from attention_triage.redact import redact

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        yield conn


def payload(name: str, **changes) -> dict:
    return {**json.loads((FIXTURES / name).read_text()), **changes}


def put(conn, payload: dict, second: float) -> str:
    """Store a payload as if it arrived `second`s after T0; returns its dedup_key."""
    event = normalize(redact(payload), now=T0 + timedelta(seconds=second))
    store.insert_event(conn, event)
    return event["dedup_key"]


def put_scenario(conn, folder: str, reverse: bool) -> str:
    """Store a recorded scenario, in arrival order or reversed; returns its PreToolUse's key."""
    names = sorted(p.name for p in (FIXTURES / folder).glob("*.json"))
    keys = {
        name: put(conn, payload(f"{folder}/{name}"), second)
        for second, name in enumerate(reversed(names) if reverse else names)
    }
    [call] = [key for name, key in keys.items() if name.endswith("PreToolUse.json")]
    return call


def stop(session_id: str) -> dict:
    return {"hook_event_name": "Stop", "session_id": session_id, "cwd": "/Users/alice/Dev"}


@pytest.mark.parametrize("reverse", [False, True], ids=["arrival-order", "reversed"])
@pytest.mark.parametrize(
    ("folder", "kind"),
    [
        ("auto-bypass-retry-after-block", "ran"),
        ("default-prompt-approved-write", "approved"),
        ("default-prompt-approved-bypass-retry", "approved"),  # request stored before its call
        ("auto-sandbox-block-filesystem", "failed"),
        ("default-prompt-approved-then-sandbox-block", "approved_failed"),
    ],
)
def test_recorded_outcomes_in_any_arrival_order(conn, folder, kind, reverse):
    result = outcome(conn, put_scenario(conn, folder, reverse))
    assert result["kind"] == kind
    if "failed" in kind:
        assert result["error"].startswith("Exit code 1\ntouch: ")
        assert result["error"].endswith("Operation not permitted")
    else:
        assert result["error"] is None


def test_a_failure_whose_error_is_not_text_has_no_error(conn):
    folder = "auto-sandbox-block-filesystem"
    call = put(conn, payload(f"{folder}/01-PreToolUse.json"), 0)
    put(conn, payload(f"{folder}/02-PostToolUseFailure.json", error={"message": "x"}), 1)
    assert outcome(conn, call) == {"kind": "failed", "error": None}


def test_a_call_with_no_result_yet_is_unknown(conn):
    call = put(conn, payload("auto-webfetch/01-PreToolUse.json"), 0)
    assert outcome(conn, call) == {"kind": "unknown", "error": None}


def test_auto_mode_denial(conn):
    # Not recorded in the spike; the docs give PermissionDenied the call's tool_use_id.
    call = put(conn, payload("auto-webfetch/01-PreToolUse.json"), 0)
    put(conn, payload("auto-webfetch/01-PreToolUse.json", hook_event_name="PermissionDenied"), 1)
    assert outcome(conn, call) == {"kind": "denied_by_auto_mode", "error": None}


@pytest.mark.parametrize("reverse", [False, True], ids=["arrival-order", "reversed"])
def test_a_prompt_with_no_result_is_a_user_denial_once_the_turn_ends(conn, reverse):
    folder = "default-prompt-denied-by-user"
    call = put_scenario(conn, folder, reverse)
    session_id = payload(f"{folder}/01-PreToolUse.json")["session_id"]
    put(conn, stop(session_id), -1)  # an earlier turn's end says nothing about this call
    assert outcome(conn, call)["kind"] == "unknown"  # the prompt may still be open

    put(conn, stop(session_id) | {"stop_hook_active": False}, 5)  # another payload, not a repeat
    assert outcome(conn, call) == {"kind": "denied_by_user", "error": None}


def test_a_call_ending_its_turn_without_a_prompt_stays_unknown(conn):
    call = put(conn, payload("default-prompt-denied-by-user/01-PreToolUse.json"), 0)
    # a prompt for another call in the same session
    put(conn, payload("default-prompt-approved-write/02-PermissionRequest.json"), 1)
    put(conn, stop(payload("default-prompt-approved-write/01-PreToolUse.json")["session_id"]), 2)
    assert outcome(conn, call)["kind"] == "unknown"


def test_a_prompt_pairs_with_the_nearest_identical_call(conn):
    """After "don't ask again" the same call runs again without a prompt."""
    folder = "default-prompt-approved-write"
    first = put_scenario(conn, folder, reverse=False)
    again = {"tool_use_id": "toolu_again"}
    second = put(conn, payload(f"{folder}/01-PreToolUse.json", **again), 60)
    put(conn, payload(f"{folder}/03-PostToolUse.json", **again), 61)
    assert outcome(conn, first)["kind"] == "approved"
    assert outcome(conn, second)["kind"] == "ran"
