import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from attention_triage import hook, review, server, store
from attention_triage.normalize import normalize
from attention_triage.redact import redact
from attention_triage.session import detail

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SESSION = "323b0a23-0680-4f81-9ba9-cb94f07d9e22"  # every recorded fixture's session


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        yield conn


def put(conn, *names: str) -> None:
    """Store recorded payloads one second apart, in the order given, and flag them as the hook does."""
    second = count()
    for name in names:
        event = normalize(
            redact(json.loads((FIXTURES / name).read_text())),
            now=T0 + timedelta(seconds=next(second)),
        )
        store.insert_event(conn, event)
        hook.flag(conn, [event])


def scenario(folder: str) -> list[str]:
    return [f"{folder}/{p.name}" for p in sorted((FIXTURES / folder).glob("*.json"))]


def shape(rows: list[dict]) -> list[tuple]:
    return [(r["event_type"], r["tool"], r["outcome"] and r["outcome"]["kind"]) for r in rows]


def test_a_subagents_events_get_a_section_of_their_own(conn):
    lifecycle = scenario("session-lifecycle")
    put(conn, lifecycle[0], *scenario("auto-subagent"), *lifecycle[1:])
    found = detail(conn, SESSION)

    # one row per call; the turn's internal SubagentStop isn't shown
    assert shape(found["events"]) == [
        ("session_start", None, None),
        ("tool_call", "Agent", "ran"),
        ("turn_end", None, None),
        ("session_end", None, None),
    ]
    [subagent] = found["subagents"]
    assert (subagent["agent_id"], subagent["agent_type"]) == ("aaa1ae0a35159e3f1", "Explore")
    assert shape(subagent["events"]) == [
        ("subagent_start", None, None),
        ("tool_call", "Bash", "ran"),
        ("tool_call", "Bash", "ran"),
        ("tool_call", "SubagentHandback", "ran"),
        ("subagent_stop", None, None),
    ]


def test_flagged_calls_list_their_flags_whatever_their_status(conn):
    put(conn, *scenario("auto-bypass-preemptive"))
    [flag_id] = [i for (i,) in conn.execute("SELECT id FROM flags")]
    review.set_status(conn, flag_id, "dismissed")

    [row] = detail(conn, SESSION)["events"]
    assert row["outcome"]["kind"] == "ran"
    assert [(f["id"], f["rule"], f["status"]) for f in row["flags"]] == [
        (flag_id, "sandbox_bypass", "dismissed")
    ]


def test_a_permission_prompt_folds_into_its_calls_outcome(conn):
    """Recorded with the prompt stored before its call."""
    put(conn, *scenario("default-prompt-approved-bypass-retry"))
    found = detail(conn, "9e427e7c-0d3a-4502-afa8-b45912778096")
    assert shape(found["events"]) == [("tool_call", "Bash", "approved")]


def test_a_result_whose_call_was_never_stored_is_still_shown(conn):
    put(conn, "auto-bypass-preemptive/02-PostToolUse.json")
    assert shape(detail(conn, SESSION)["events"]) == [("tool_result", "Bash", None)]


def test_the_api_returns_a_session_or_404(conn):
    put(conn, *scenario("auto-bypass-preemptive"))
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.get(f"/api/sessions/{SESSION}")
    assert response.status_code == 200
    assert response.json() == detail(conn, SESSION)
    assert client.get("/api/sessions/unknown").status_code == 404
