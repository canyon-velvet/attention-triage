import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from attention_triage import policy, rules, store
from attention_triage.normalize import normalize
from attention_triage.redact import redact

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"
ALL_FIXTURES = sorted(FIXTURES.glob("*/*.json"))
# A PermissionRequest's key includes its capture time (normalize.dedup_key): each firing counts.
REQUESTS = sorted(FIXTURES.glob("*/*-PermissionRequest.json"))
HOOK = Path(sys.executable).parent / "triage-hook"  # the installed console script


def run_hook(home: Path, stdin: bytes) -> int:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([HOOK], input=stdin, env=env, timeout=10).returncode


def rows(home: Path, table: str = "events") -> list[sqlite3.Row]:
    db = home / ".attention-triage" / "triage.db"
    if not db.exists():
        return []
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table}").fetchall()


@pytest.mark.parametrize(
    "path",
    [p for p in ALL_FIXTURES if p not in REQUESTS],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_each_fixture_is_stored_exactly_once(tmp_path, path):
    assert run_hook(tmp_path, path.read_bytes()) == 0
    assert len(rows(tmp_path)) == 1
    assert run_hook(tmp_path, path.read_bytes()) == 0
    assert len(rows(tmp_path)) == 1


def test_a_permission_request_fired_again_is_stored_again(tmp_path):
    """The same call repeated in one turn and prompted again sends a byte-identical request."""
    assert run_hook(tmp_path, REQUESTS[0].read_bytes()) == 0
    assert run_hook(tmp_path, REQUESTS[0].read_bytes()) == 0
    assert len(rows(tmp_path)) == 2


def test_a_spooled_permission_request_is_stored_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    event = normalize(redact(json.loads(REQUESTS[0].read_text())))
    with closing(store.connect()) as conn:
        store.insert_event(conn, event)  # stored, then the run failed and spooled it
    store.spool(event)
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes())
    assert len(rows(tmp_path)) == 2 and spool_lines(tmp_path) == []


def test_all_fixtures_get_distinct_rows(tmp_path):
    for path in ALL_FIXTURES:
        run_hook(tmp_path, path.read_bytes())
    assert len(rows(tmp_path)) == len(ALL_FIXTURES)


def test_stored_row_matches_the_normalized_event(tmp_path):
    path = FIXTURES / "auto-sandbox-block-filesystem" / "02-PostToolUseFailure.json"
    run_hook(tmp_path, path.read_bytes())
    [row] = rows(tmp_path)
    expected = normalize(redact(json.loads(path.read_text())))
    for column in store.COLUMNS:
        if column == "summary":
            assert json.loads(row[column]) == expected[column]
        elif column != "ts":
            assert row[column] == expected[column], column


def test_lone_surrogate_from_a_cut_emoji_is_still_stored(tmp_path):
    path = FIXTURES / "auto-sandbox-block-filesystem" / "01-PreToolUse.json"
    stdin = path.read_text().replace("touch ~/triage-spike-blocked.txt", "echo \\ud83d").encode()
    assert run_hook(tmp_path, stdin) == 0
    [row] = rows(tmp_path)
    assert row["target"] == "echo ?"


def test_db_uses_wal_and_a_two_second_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000


@pytest.mark.parametrize("stdin", [b"not json", b"[1, 2]", b""])
def test_bad_stdin_exits_0_stores_nothing_and_logs(tmp_path, stdin):
    assert run_hook(tmp_path, stdin) == 0
    assert rows(tmp_path) == []
    assert (tmp_path / ".attention-triage" / "hook-errors.log").read_text()


def spool_lines(home: Path) -> list[bytes]:
    """Non-blank spool records (each one starts on a fresh line, so blank lines are expected)."""
    spool = home / ".attention-triage" / "spool.jsonl"
    return [line for line in spool.read_bytes().splitlines() if line] if spool.exists() else []


def make_db_unavailable(home: Path) -> Path:
    """A directory where triage.db should be: sqlite3 can't open it, like a broken DB."""
    db = home / ".attention-triage" / "triage.db"
    db.mkdir(parents=True)
    return db


def test_db_unavailable_spools_the_event_logs_and_exits_0(tmp_path):
    make_db_unavailable(tmp_path)
    path = FIXTURES / "auto-webfetch" / "01-PreToolUse.json"
    assert run_hook(tmp_path, path.read_bytes()) == 0
    [line] = spool_lines(tmp_path)
    expected = normalize(redact(json.loads(path.read_text())))
    assert json.loads(line)["dedup_key"] == expected["dedup_key"]
    assert "OperationalError" in (tmp_path / ".attention-triage" / "hook-errors.log").read_text()


def test_next_successful_run_ingests_the_spool_without_duplicates(tmp_path):
    spooled = (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes()
    db = make_db_unavailable(tmp_path)
    run_hook(tmp_path, spooled)
    run_hook(tmp_path, spooled)  # delivered twice while the DB was down
    db.rmdir()
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "02-PostToolUse.json").read_bytes())
    assert len(rows(tmp_path)) == 2 and spool_lines(tmp_path) == []
    run_hook(tmp_path, spooled)
    assert len(rows(tmp_path)) == 2


@pytest.mark.parametrize("cut", [b'{"agent": "clau', '{"target": "中'.encode()[:-2]])
def test_a_record_cut_short_loses_only_itself(tmp_path, cut):
    db = make_db_unavailable(tmp_path)
    (tmp_path / ".attention-triage" / "spool.jsonl").write_bytes(cut)  # a hook killed mid-write
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes())
    db.rmdir()
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "02-PostToolUse.json").read_bytes())
    assert len(rows(tmp_path)) == 2 and spool_lines(tmp_path) == []


def test_a_line_separator_inside_an_event_does_not_split_it(tmp_path):
    db = make_db_unavailable(tmp_path)
    payload = json.loads((FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_text())
    payload["tool_input"]["prompt"] = "first\u2028second"
    run_hook(tmp_path, json.dumps(payload, ensure_ascii=False).encode())
    db.rmdir()
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "02-PostToolUse.json").read_bytes())
    assert len(rows(tmp_path)) == 2


def test_parallel_hooks_spool_and_ingest_every_event(tmp_path):
    db = make_db_unavailable(tmp_path)
    payload = json.loads((FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_text())
    env = {**os.environ, "HOME": str(tmp_path)}
    hooks = []
    for i in range(20):
        hook = subprocess.Popen([HOOK], stdin=subprocess.PIPE, env=env)
        hook.stdin.write(json.dumps({**payload, "tool_use_id": f"toolu_{i}"}).encode())
        hook.stdin.close()
        hooks.append(hook)
    assert all(hook.wait(timeout=10) == 0 for hook in hooks)
    assert len(spool_lines(tmp_path)) == 20
    db.rmdir()
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "02-PostToolUse.json").read_bytes())
    assert len(rows(tmp_path)) == 21 and spool_lines(tmp_path) == []


def run_notice(home: Path, session_id: str) -> subprocess.CompletedProcess:
    payload = json.loads((FIXTURES / "session-lifecycle" / "01-SessionStart.json").read_text())
    stdin = json.dumps({**payload, "session_id": session_id}).encode()
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [HOOK, "--session-notice"], input=stdin, env=env, capture_output=True, timeout=10
    )


def test_session_notice_is_silent_while_capture_works(tmp_path):
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes())
    notice = run_notice(tmp_path, "s1")
    assert notice.returncode == 0 and notice.stdout == b""


def test_session_notice_warns_once_per_session_while_events_are_buffered(tmp_path):
    make_db_unavailable(tmp_path)
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes())
    first = run_notice(tmp_path, "s1")
    assert first.returncode == 0
    message = json.loads(first.stdout)["systemMessage"]
    assert "capture failing" in message and "unable to open database file" in message
    assert message.endswith("Run `triage doctor`.")
    assert run_notice(tmp_path, "s1").stdout == b""  # resume / compact: same session, no repeat
    assert run_notice(tmp_path, "s2").stdout != b""  # a new session is told again


def test_session_notice_is_shown_even_if_it_cannot_be_recorded(tmp_path):
    make_db_unavailable(tmp_path)
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes())
    data = tmp_path / ".attention-triage"
    data.chmod(0o555)  # notified-sessions can't be created
    try:
        notice = run_notice(tmp_path, "s1")
    finally:
        data.chmod(0o755)
    assert (
        notice.returncode == 0 and "capture failing" in json.loads(notice.stdout)["systemMessage"]
    )


def seed_events_aged(home: Path, monkeypatch, days_old: list[int]) -> None:
    """Store one fixture event per age, as if each was captured that many days ago."""
    monkeypatch.setenv("HOME", str(home))
    paths = [
        FIXTURES / "auto-webfetch" / "01-PreToolUse.json",
        FIXTURES / "auto-webfetch" / "02-PostToolUse.json",
    ]
    with closing(store.connect()) as conn:
        for path, days in zip(paths, days_old, strict=False):
            now = datetime.now(UTC) - timedelta(days=days)
            store.insert_event(conn, normalize(json.loads(path.read_text()), now=now))


@pytest.mark.parametrize(
    "trigger", ["session-lifecycle/01-SessionStart.json", "auto-subagent/01-PreToolUse.json"]
)
def test_session_start_deletes_events_older_than_the_retention_period(
    tmp_path, monkeypatch, trigger
):
    seed_events_aged(tmp_path, monkeypatch, [store.RETENTION_DAYS + 1, store.RETENTION_DAYS - 1])
    run_hook(tmp_path, (FIXTURES / trigger).read_bytes())
    kept = [row["event_type"] for row in rows(tmp_path) if row["tool_name"] == "WebFetch"]
    if trigger.endswith("SessionStart.json"):
        assert kept == ["tool_result"]  # the older tool_call is gone, the newer result stays
    else:
        assert kept == ["tool_call", "tool_result"]  # other events never purge


BYPASS = FIXTURES / "auto-bypass-preemptive" / "01-PreToolUse.json"


BYPASS_RETRY = "auto-bypass-retry-after-block/01-PreToolUse.json"


def test_the_hook_labels_a_retry_of_a_sandbox_blocked_command(tmp_path):
    for name in ["auto-sandbox-block-filesystem/02-PostToolUseFailure.json", BYPASS_RETRY]:
        assert run_hook(tmp_path, (FIXTURES / name).read_bytes()) == 0
    [flag] = rows(tmp_path, "flags")
    assert flag["label"] == "retry-after-block"


def test_a_bypass_is_flagged_under_the_default_policy(tmp_path):
    assert run_hook(tmp_path, BYPASS.read_bytes()) == 0
    [event] = rows(tmp_path)
    [flag] = rows(tmp_path, "flags")
    policy_file = tmp_path / ".attention-triage" / "policy.yaml"
    assert policy_file.read_text() == policy.DEFAULT_POLICY
    assert dict(flag) | {"id": None, "created_at": None, "updated_at": None} == {
        "id": None,
        "event_key": event["dedup_key"],
        "rule_id": "sandbox_bypass",
        "severity": "high",
        "reason": "The agent asked to run a command with the sandbox disabled.",
        "label": "preemptive",
        "evidence": '{"command": "ls ~/Desktop"}',
        "policy_version": policy.parse(policy.DEFAULT_POLICY)[1],
        "status": "open",
        "suppression_id": None,
        "created_at": None,
        "updated_at": None,
    }
    assert flag["created_at"] == flag["updated_at"] >= event["ts"]
    run_hook(tmp_path, BYPASS.read_bytes())  # a duplicate event adds no flag
    assert len(rows(tmp_path, "flags")) == 1


def test_a_rule_disabled_in_the_policy_file_flags_nothing(tmp_path):
    (tmp_path / ".attention-triage").mkdir()
    (tmp_path / ".attention-triage" / "policy.yaml").write_text(
        "version: 1\nrules:\n  sandbox_bypass:\n    enabled: false\n"
    )
    assert run_hook(tmp_path, BYPASS.read_bytes()) == 0
    assert len(rows(tmp_path)) == 1 and rows(tmp_path, "flags") == []


def test_an_invalid_policy_still_stores_the_event_and_exits_0(tmp_path):
    (tmp_path / ".attention-triage").mkdir()
    (tmp_path / ".attention-triage" / "policy.yaml").write_text("version: 2\n")
    assert run_hook(tmp_path, BYPASS.read_bytes()) == 0
    assert len(rows(tmp_path)) == 1 and rows(tmp_path, "flags") == []
    assert spool_lines(tmp_path) == []  # stored, so not buffered for a retry
    assert "PolicyError" in (tmp_path / ".attention-triage" / "hook-errors.log").read_text()


def test_a_spooled_bypass_is_flagged_when_ingested(tmp_path):
    db = make_db_unavailable(tmp_path)
    run_hook(tmp_path, BYPASS.read_bytes())
    db.rmdir()
    run_hook(tmp_path, (FIXTURES / "auto-write-in-project" / "01-PreToolUse.json").read_bytes())
    [flag] = rows(tmp_path, "flags")
    assert flag["event_key"] == normalize(redact(json.loads(BYPASS.read_text())))["dedup_key"]


def test_a_spooled_event_stored_by_a_failed_run_still_gets_its_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    event = normalize(redact(json.loads(BYPASS.read_text())))
    with closing(store.connect()) as conn:
        store.insert_event(conn, event)  # stored, then the run failed and spooled it
    store.spool(event)
    run_hook(tmp_path, (FIXTURES / "auto-write-in-project" / "01-PreToolUse.json").read_bytes())
    [flag] = rows(tmp_path, "flags")
    assert flag["event_key"] == event["dedup_key"]


def test_purging_an_event_deletes_its_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    old = datetime.now(UTC) - timedelta(days=store.RETENTION_DAYS + 1)
    event = normalize(json.loads(BYPASS.read_text()), now=old)
    with closing(store.connect()) as conn:
        store.insert_event(conn, event)
        store.insert_flags(conn, rules.evaluate(event, *policy.parse(policy.DEFAULT_POLICY)))
        store.purge(conn)
    assert rows(tmp_path) == [] and rows(tmp_path, "flags") == []


def test_a_db_from_before_repo_root_gains_the_column(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / ".attention-triage" / "triage.db"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(store.SCHEMA.replace("    repo_root TEXT,\n", ""))
    for _ in range(2):  # and connecting again doesn't try to add it twice
        with closing(store.connect()) as conn:
            assert "repo_root" in [c[1] for c in conn.execute("PRAGMA table_info(events)")]


def test_an_event_spooled_before_repo_root_existed_is_still_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    old = normalize(redact(json.loads(BYPASS.read_text())))
    del old["repo_root"]
    store.spool(old)
    run_hook(tmp_path, (FIXTURES / "auto-webfetch" / "01-PreToolUse.json").read_bytes())
    assert len(rows(tmp_path)) == 2 and spool_lines(tmp_path) == []
