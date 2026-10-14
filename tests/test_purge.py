import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_hook import BYPASS

from attention_triage import cli, policy, server, store
from attention_triage.normalize import normalize


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


def store_aged(*days_old: int) -> None:
    with closing(store.connect()) as conn:
        for i, days in enumerate(days_old):
            payload = {**json.loads(BYPASS.read_text()), "tool_use_id": f"t{i}"}
            event = normalize(payload, now=datetime.now(UTC) - timedelta(days=days))
            store.insert_event(conn, event)


def count() -> int:
    with closing(store.connect()) as conn:
        return conn.execute("SELECT count(*) FROM events").fetchone()[0]


def set_retention(value) -> None:
    policy.path().parent.mkdir(parents=True, exist_ok=True)
    policy.path().write_text(
        policy.DEFAULT_POLICY.replace("retention_days: 7", f"retention_days: {value}")
    )


def test_purge_deletes_events_older_than_retention_days_and_says_how_many(capsys):
    store_aged(10, 5, 1)
    set_retention(3)
    cli.main(["purge"])
    assert capsys.readouterr().out == "purge: deleted 2 events older than 3 days\n"
    assert count() == 1


def test_purge_with_an_invalid_policy_deletes_nothing():
    store_aged(10)
    set_retention(0)
    with pytest.raises(SystemExit, match="policy.yaml is invalid, so nothing was deleted"):
        cli.main(["purge"])
    assert count() == 1


def test_starting_the_ui_purges(monkeypatch):
    store_aged(10, 1)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)
    server.serve(8765)
    assert count() == 1


def test_starting_the_ui_with_an_invalid_policy_keeps_everything_and_says_so(monkeypatch, capsys):
    store_aged(10, 1)
    set_retention(0)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)
    server.serve(8765)
    assert count() == 2
    assert "old events were not deleted" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [b"retention_days: \xe9\n", None])  # not UTF-8; a directory
def test_an_unreadable_policy_neither_stops_the_ui_nor_crashes_purge(monkeypatch, capsys, bad):
    store_aged(10, 1)
    policy.path().parent.mkdir(parents=True, exist_ok=True)
    if bad is None:
        policy.path().mkdir()
    else:
        policy.path().write_bytes(bad)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: None)
    server.serve(8765)  # starts
    with pytest.raises(SystemExit, match="policy.yaml is invalid, so nothing was deleted"):
        cli.main(["purge"])
    assert count() == 2


def test_a_century_of_retention_purges_nothing_without_error():
    store_aged(10)
    set_retention(36500)
    cli.main(["purge"])
    assert count() == 1
