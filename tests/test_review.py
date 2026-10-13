from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_digest import T0, bash
from test_suppress import bypass, status

from attention_triage import review, server, store
from attention_triage.digest import digest
from attention_triage.normalize import normalize


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        yield conn


def client(origin: str | None = None) -> TestClient:
    headers = {"Origin": origin} if origin else {}
    return TestClient(server.app, base_url="http://127.0.0.1:8765", headers=headers)


def open_flags(conn) -> dict[str, int]:
    """Target -> flag id of every flag in the inbox."""
    result = digest(conn)
    return {
        f["target"]: f["id"] for p in result["projects"] for s in p["sessions"] for f in s["flags"]
    }


def statuses(conn) -> dict[int, str]:
    return dict(conn.execute("SELECT id, status FROM flags"))


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [("dismiss", {"scope": "once"}, "dismissed"), ("ack", None, "acknowledged")],
)
def test_a_dismissed_or_acknowledged_flag_leaves_the_inbox_and_keeps_its_status(
    conn, path, body, status
):
    bash(conn, "/p/a", "s1", 1)
    bash(conn, "/p/a", "s1", 2)
    flag_id = open_flags(conn)["s1-1"]

    response = client().post(f"/api/flags/{flag_id}/{path}", json=body)

    assert response.status_code == 200
    assert response.json() == {"id": flag_id, "status": status}
    assert list(open_flags(conn)) == ["s1-2"]
    assert statuses(conn)[flag_id] == status
    [updated] = conn.execute("SELECT updated_at FROM flags WHERE id = ?", [flag_id]).fetchone()
    assert updated > T0.isoformat()


def test_only_an_open_flag_changes(conn):
    bash(conn, "/p/a", "s1", 1)
    flag_id = open_flags(conn)["s1-1"]
    assert client().post(f"/api/flags/{flag_id}/ack").status_code == 200
    assert client().post(f"/api/flags/{flag_id}/dismiss", json={"scope": "once"}).status_code == 409
    assert statuses(conn)[flag_id] == "acknowledged"
    assert client().post("/api/flags/999/ack").status_code == 404


def test_fine_for_future_ones_suppresses_later_matches_and_counts_them(conn):
    first = bypass(conn, "docker ps")
    response = client().post(f"/api/flags/{first}/dismiss", json={"scope": "future"})
    assert response.status_code == 200
    suppression_id = response.json()["suppression_id"]
    assert response.json() == {"id": first, "status": "dismissed", "suppression_id": suppression_id}

    later = bypass(conn, "docker ps")
    assert status(conn, later) == ("suppressed", suppression_id)
    headline = digest(conn)["headline"]
    assert (headline["need_review"], headline["suppressed"]) == (0, 1)


def test_fine_for_future_ones_takes_a_widened_pattern_and_a_note(conn):
    first = bypass(conn, "docker ps")
    body = {"scope": "future", "pattern": {"kind": "prefix", "value": "docker"}, "note": "local"}
    assert client().post(f"/api/flags/{first}/dismiss", json=body).status_code == 200
    assert conn.execute("SELECT kind, pattern, note FROM suppressions").fetchall() == [
        ("prefix", "docker", "local")
    ]
    assert status(conn, bypass(conn, "docker compose up"))[0] == "suppressed"


def test_a_pattern_that_cant_be_used_is_refused_with_the_reason(conn):
    flag_id = bypass(conn, "bash -c 'make'")
    body = {"scope": "future", "pattern": {"kind": "prefix", "value": "bash"}}
    response = client().post(f"/api/flags/{flag_id}/dismiss", json=body)
    assert response.status_code == 422
    assert "can only match exactly" in response.json()["detail"]
    assert status(conn, flag_id) == ("open", None)


def test_the_suppressions_page_lists_them_and_undo_flags_matches_again(conn):
    first = bypass(conn, "docker ps")
    client().post(f"/api/flags/{first}/dismiss", json={"scope": "future", "note": "local"})
    hidden = bypass(conn, "docker ps")
    [listed] = client().get("/api/suppressions").json()
    assert (listed["pattern"], listed["note"], listed["hits"]) == ("docker ps", "local", 1)

    assert client().delete(f"/api/suppressions/{listed['id']}").json() == {
        "id": listed["id"],
        "deleted": True,
    }
    assert client().get("/api/suppressions").json() == []
    assert status(conn, hidden) == ("open", None)
    assert status(conn, bypass(conn, "docker ps")) == ("open", None)
    assert client().delete(f"/api/suppressions/{listed['id']}").status_code == 404


def test_another_sites_page_cannot_undo_a_suppression(conn):
    suppression_id = (
        client()
        .post(f"/api/flags/{bypass(conn, 'docker ps')}/dismiss", json={"scope": "future"})
        .json()["suppression_id"]
    )
    assert (
        client("https://evil.example").delete(f"/api/suppressions/{suppression_id}").status_code
        == 403
    )
    assert len(client().get("/api/suppressions").json()) == 1


def test_dismiss_bodies_that_dont_fit_are_refused(conn):
    flag_id = bypass(conn, "docker ps")
    pattern = {"kind": "exact", "value": "docker ps"}
    for body in (None, {"scope": "once", "pattern": pattern}, {"scope": "once", "note": "x"}):
        assert client().post(f"/api/flags/{flag_id}/dismiss", json=body).status_code == 422
    future = {"scope": "future"}
    assert client().post("/api/flags/999/dismiss", json=future).status_code == 404
    client().post(f"/api/flags/{flag_id}/ack")
    assert client().post(f"/api/flags/{flag_id}/dismiss", json=future).status_code == 409


def test_mark_all_clears_only_the_flags_the_page_showed(conn):
    bash(conn, "/p/a", "s1", 1)
    bash(conn, "/p/b", "s2", 2)
    shown, as_of = open_flags(conn), digest(conn)["as_of"]
    client().post(f"/api/flags/{shown['s1-1']}/ack")
    bash(conn, "/p/a", "s1", 3)  # flagged after the page loaded

    body = {"flag_ids": list(shown.values()), "as_of": as_of}
    response = client().post("/api/review/mark-all", json=body)

    assert response.json() == {"dismissed": 1}  # s2-2; s1-1 was already acknowledged
    assert list(open_flags(conn)) == ["s1-3"]
    assert statuses(conn)[shown["s1-1"]] == "acknowledged"


def test_mark_all_starts_a_new_review_window(conn):
    bash(conn, "/p/a", "s1", 10)
    bash(conn, "/p/a", "s1", 20, bypass=False)
    assert digest(conn)["headline"] == {
        "actions": 2,
        "captured": 2,
        "need_review": 1,
        "suppressed": 0,
        "since": None,
    }

    review.mark_all(conn, list(open_flags(conn).values()), at=T0 + timedelta(minutes=30))
    assert digest(conn)["headline"] == {
        "actions": 0,
        "captured": 0,
        "need_review": 0,
        "suppressed": 0,
        "since": "2026-10-08T12:30:00.000+00:00",
    }

    bash(conn, "/p/a", "s1", 40)
    assert digest(conn)["headline"]["actions"] == 1
    assert digest(conn)["headline"]["need_review"] == 1


def action_now(conn, command: str) -> None:
    """Store an unflagged Bash call captured at the real current time."""
    event = normalize(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "cwd": "/p/a",
            "tool_name": "Bash",
            "tool_use_id": command,
            "tool_input": {"command": command},
        }
    )
    store.insert_event(conn, event)


def test_the_next_window_starts_when_the_page_loaded_not_at_the_click(conn):
    page = client().get("/api/digest").json()
    action_now(conn, "while-the-page-was-open")
    body = {"flag_ids": [], "as_of": page["as_of"]}
    assert client().post("/api/review/mark-all", json=body).status_code == 200
    headline = digest(conn)["headline"]
    assert headline["since"] == page["as_of"] and headline["actions"] == 1


def test_the_window_start_is_stored_in_utc_and_never_in_the_future(conn):
    local = datetime(2026, 10, 8, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    client().post("/api/review/mark-all", json={"flag_ids": [], "as_of": local.isoformat()})
    assert review.last_review(conn) == "2026-10-08T12:30:00.000+00:00"

    client().post("/api/review/mark-all", json={"flag_ids": [], "as_of": "2999-01-01T00:00:00Z"})
    assert review.last_review(conn) <= review.now()
    for as_of in (None, "2026-10-08T12:30:00"):  # missing, or no timezone
        body = {"flag_ids": [], "as_of": as_of}
        assert client().post("/api/review/mark-all", json=body).status_code == 422


def test_loading_the_inbox_changes_nothing(conn):
    bash(conn, "/p/a", "s1", 1)
    before = list(conn.iterdump())
    assert client().get("/api/digest").status_code == 200
    assert client().get("/").status_code == 200
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:3000", "null"])
def test_another_sites_page_cannot_write(conn, origin):
    """CSRF: a page elsewhere can make the browser POST here; its Origin gives it away."""
    bash(conn, "/p/a", "s1", 1)
    flag_id = open_flags(conn)["s1-1"]
    attacker = client(origin)
    assert attacker.post(f"/api/flags/{flag_id}/ack").status_code == 403
    body = {"scope": "once"}
    assert attacker.post(f"/api/flags/{flag_id}/dismiss", json=body).status_code == 403
    body = {"flag_ids": [flag_id], "as_of": digest(conn)["as_of"]}
    assert attacker.post("/api/review/mark-all", json=body).status_code == 403
    assert statuses(conn)[flag_id] == "open"
    assert review.last_review(conn) is None
    assert attacker.get("/api/digest").status_code == 200  # it can't read the response anyway


def test_the_inbox_page_itself_can_write(conn):
    bash(conn, "/p/a", "s1", 1)
    flag_id = open_flags(conn)["s1-1"]
    assert client("http://127.0.0.1:8765").post(f"/api/flags/{flag_id}/ack").status_code == 200
