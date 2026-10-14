import os
import time
from contextlib import closing
from itertools import count

import pytest

from attention_triage import cli, hook, policy, review, store, suppress
from attention_triage.normalize import normalize
from attention_triage.redact import redact
from attention_triage.reeval import reeval

IDS = count()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        yield conn


def set_policy(old: str, new: str) -> None:
    policy.path().parent.mkdir(parents=True, exist_ok=True)
    assert old in policy.DEFAULT_POLICY
    policy.path().write_text(policy.DEFAULT_POLICY.replace(old, new))


def store_call(conn, tool: str, tool_input: dict, flag=True) -> str:
    """Store a PreToolUse in /p/a, flagged as the hook does unless `flag` is False (an insert that
    failed). Returns its event key."""
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/p/a"}
    payload |= {"tool_name": tool, "tool_use_id": f"t{next(IDS)}", "tool_input": tool_input}
    event = normalize(redact(payload))
    store.insert_event(conn, event)
    if flag:
        hook.flag(conn, [event])
    return event["dedup_key"]


def bypass(conn, command="docker ps", **kwargs) -> str:
    return store_call(
        conn, "Bash", {"command": command, "dangerouslyDisableSandbox": True}, **kwargs
    )


def flags(conn) -> list[tuple]:
    return conn.execute("SELECT rule_id, status, label, severity FROM flags ORDER BY id").fetchall()


def test_after_tightening_a_rule_past_events_get_its_flags(conn):
    store_call(conn, "Write", {"file_path": "/p/a/.zshrc", "content": "x"})  # inside the project
    assert flags(conn) == []
    set_policy("extra_protected_paths: []", "extra_protected_paths: [/p/a/.zshrc]")
    assert reeval(conn)["added"] == 1
    assert flags(conn) == [("config_edit", "open", "", "high")]


def test_a_new_flag_that_a_suppression_matches_is_stored_suppressed(conn):
    flag_id = conn.execute("SELECT id FROM flags WHERE event_key = ?", [bypass(conn)]).fetchone()[0]
    suppress.create(conn, flag_id)
    bypass(conn, flag=False)  # its insert failed
    reeval(conn)
    assert [status for _, status, _, _ in flags(conn)] == ["dismissed", "suppressed"]


def test_a_flag_whose_insert_failed_is_added(conn):
    bypass(conn, flag=False)
    assert reeval(conn)["added"] == 1
    assert flags(conn) == [("sandbox_bypass", "open", "preemptive", "high")]


def test_a_flag_that_differs_is_corrected_and_keeps_its_status(conn):
    bypass(conn)
    [(flag_id,)] = conn.execute("SELECT id FROM flags").fetchall()
    review.set_status(conn, flag_id, "dismissed")
    conn.execute("UPDATE flags SET label = 'stale' WHERE id = ?", [flag_id])
    conn.commit()
    assert reeval(conn)["corrected"] == 1
    assert flags(conn) == [("sandbox_bypass", "dismissed", "preemptive", "high")]


def test_a_new_policy_version_alone_is_stored_but_not_counted_as_a_correction(conn):
    bypass(conn)
    [(old_version,)] = conn.execute("SELECT policy_version FROM flags").fetchall()
    set_policy("allowed_paths: []", "allowed_paths: [/srv/cache]")  # unrelated to this flag
    counts = reeval(conn)
    assert counts["policy_version"] != old_version
    assert (counts["added"], counts["corrected"]) == (0, 0)
    [(stored,)] = conn.execute("SELECT policy_version FROM flags").fetchall()
    assert stored == counts["policy_version"]


def test_a_flag_hidden_by_a_deleted_suppression_reopens(conn):
    first = conn.execute("SELECT id FROM flags WHERE event_key = ?", [bypass(conn)]).fetchone()[0]
    suppression_id = suppress.create(conn, first)
    bypass(conn)
    # An undo that raced a capture: the suppression is gone, its flag still says suppressed.
    conn.execute("DELETE FROM suppressions WHERE id = ?", [suppression_id])
    conn.commit()
    assert reeval(conn)["reopened"] == 1
    assert [status for _, status, _, _ in flags(conn)] == ["dismissed", "open"]


def test_nothing_is_removed_when_a_rule_no_longer_fires(conn):
    bypass(conn)
    bypass(conn)
    [(acked,), _] = conn.execute("SELECT id FROM flags ORDER BY id").fetchall()
    review.set_status(conn, acked, "acknowledged")
    set_policy("  sandbox_bypass:\n    enabled: true", "  sandbox_bypass:\n    enabled: false")
    reeval(conn)
    assert [status for _, status, _, _ in flags(conn)] == ["acknowledged", "open"]


def test_running_it_twice_changes_nothing_the_second_time(conn):
    bypass(conn)
    bypass(conn, flag=False)
    reeval(conn)
    before = list(conn.iterdump())
    counts = reeval(conn)
    assert (counts["added"], counts["corrected"], counts["reopened"]) == (0, 0, 0)
    assert list(conn.iterdump()) == before


def test_an_invalid_policy_changes_nothing(conn, capsys):
    bypass(conn, flag=False)
    set_policy("severity: review", "severity: urgent")
    with pytest.raises(policy.PolicyError):
        reeval(conn)
    with pytest.raises(SystemExit, match="policy.yaml is invalid, so nothing was changed"):
        cli.main(["reeval"])
    assert flags(conn) == []


def test_since_limits_the_events_rechecked(conn):
    bypass(conn, flag=False)
    later = review.now()
    assert reeval(conn, since=later)["added"] == 0
    assert reeval(conn, since=None)["added"] == 1


def test_a_recorded_path_is_kept_when_its_link_now_leads_elsewhere(conn, tmp_path):
    """Path rules resolve symlinks when they run; the flag records the file the write reached."""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    (project / ".claude").mkdir()
    for target in ("first", "second"):
        (tmp_path / target).mkdir()
    link = project / ".claude" / "settings.json"
    link.symlink_to(tmp_path / "first" / "settings.json")
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(project)}
    payload |= {"tool_name": "Write", "tool_use_id": "w1"}
    event = normalize(redact({**payload, "tool_input": {"file_path": str(link), "content": "{}"}}))
    store.insert_event(conn, event)
    hook.flag(conn, [event])
    [(recorded,)] = conn.execute("SELECT evidence FROM flags").fetchall()
    link.unlink()
    link.symlink_to(tmp_path / "second" / "settings.json")
    assert reeval(conn)["corrected"] == 0
    assert conn.execute("SELECT evidence FROM flags").fetchall() == [(recorded,)]


def test_a_suppression_undone_while_it_runs_hides_no_new_flag(conn, monkeypatch):
    first = conn.execute("SELECT id FROM flags WHERE event_key = ?", [bypass(conn)]).fetchone()[0]
    suppression_id = suppress.create(conn, first)
    bypass(conn, flag=False)  # its insert failed; reeval adds it, matched by the suppression
    apply = suppress.apply

    def undo_meanwhile(*args):
        found = apply(*args)
        if any(f.get("suppression_id") for f in found):  # just after the missing flag matched
            with closing(store.connect()) as other:  # the inbox's undo, from another connection
                suppress.delete(other, suppression_id)
        return found

    monkeypatch.setattr(suppress, "apply", undo_meanwhile)
    assert reeval(conn)["added"] == 1
    assert [status for _, status, _, _ in flags(conn)] == ["dismissed", "open"]


@pytest.fixture
def tokyo():
    """Local time UTC+9 (no daylight saving) for the test, then back to what it was."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_since_is_read_as_a_local_date_or_time(tokyo):
    assert cli.local_time("2026-10-12") == "2026-10-11T15:00:00.000+00:00"
    assert cli.local_time("2026-10-12 09:30") == "2026-10-12T00:30:00.000+00:00"
    with pytest.raises(SystemExit):
        cli.main(["reeval", "--since", "last week"])


def test_the_command_prints_the_counts(conn, capsys):
    bypass(conn, flag=False)
    cli.main(["reeval"])
    assert capsys.readouterr().out.startswith("reeval: 1 added, 0 corrected, 0 reopened (policy ")
