from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_digest import T0, bash

from attention_triage import server, store
from attention_triage.digest import digest


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


def test_dismissing_future_ones_is_not_built_yet(conn):
    bash(conn, "/p/a", "s1", 1)
    flag_id = open_flags(conn)["s1-1"]
    for body in ({"scope": "future"}, None):
        assert client().post(f"/api/flags/{flag_id}/dismiss", json=body).status_code == 422
    assert statuses(conn)[flag_id] == "open"


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
    assert statuses(conn)[flag_id] == "open"
    assert attacker.get("/api/digest").status_code == 200  # it can't read the response anyway


def test_the_inbox_page_itself_can_write(conn):
    bash(conn, "/p/a", "s1", 1)
    flag_id = open_flags(conn)["s1-1"]
    assert client("http://127.0.0.1:8765").post(f"/api/flags/{flag_id}/ack").status_code == 200
