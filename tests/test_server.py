import re
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_digest import bash, shape

from attention_triage import cli, server, store
from attention_triage.digest import digest
from attention_triage.normalize import normalize


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def client(host: str = "127.0.0.1") -> TestClient:
    return TestClient(server.app, base_url=f"http://{host}:8765")


def test_digest_endpoint_returns_the_digest():
    with closing(store.connect()) as conn:
        bash(conn, "/p/a", "s1", 1)
        expected = digest(conn)
    response = client().get("/api/digest")
    assert response.status_code == 200
    served = response.json()
    assert served.pop("as_of") >= expected.pop("as_of")  # when each digest was built
    assert served == expected and expected["headline"]["need_review"] == 1


def test_health():
    assert client().get("/api/health").json() == {"ok": True}


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_hosts_are_served(host):
    assert client(host).get("/api/health").status_code == 200


def test_other_hosts_are_refused():
    """DNS rebinding: a web page can point its own name at 127.0.0.1 and read the API as
    same-origin. Its requests still carry that name in the Host header."""
    assert client("attacker.example").get("/api/digest").status_code == 400
    assert client("attacker.example").get("/").status_code == 400


def test_the_inbox_page_and_its_assets_are_served():
    page = client().get("/")
    assert page.status_code == 200 and '<div id="root">' in page.text
    [script] = re.findall(r'src="(/assets/[^"]+\.js)"', page.text)
    assert client().get(script).status_code == 200


def test_ui_ingests_and_flags_the_spool_then_serves_on_loopback(monkeypatch):
    store.spool(
        normalize(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "cwd": "/p/a",
                "tool_name": "Bash",
                "tool_use_id": "t1",
                "tool_input": {"command": "spooled", "dangerouslyDisableSandbox": True},
            }
        )
    )
    served = {}

    def run(app, host, port):
        with closing(store.connect()) as conn:
            served.update(host=host, port=port, shape=shape(digest(conn)))

    monkeypatch.setattr(server.uvicorn, "run", run)
    cli.main(["ui", "--port", "9999"])
    assert served == {"host": "127.0.0.1", "port": 9999, "shape": [("/p/a", [("s1", ["spooled"])])]}
    assert not store.spool_path().exists()
