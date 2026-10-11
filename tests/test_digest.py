from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from attention_triage import policy, rules, store
from attention_triage.digest import digest
from attention_triage.normalize import normalize

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        yield conn


def bash(
    conn, project, session, minute, severity="high", bypass=True, hook="PreToolUse", repo=None
):
    """Store a Bash call running `<session>-<minute>`, flagged at `severity` when it bypasses the
    sandbox. `repo` stands in for a worktree's main checkout (see test_normalize)."""
    command = f"{session}-{minute}"
    event = normalize(
        {
            "hook_event_name": hook,
            "session_id": session,
            "cwd": project,
            "tool_name": "Bash",
            "tool_use_id": command,
            "tool_input": {
                "command": command,
                "description": f"run {command}",
                "dangerouslyDisableSandbox": bypass,
            },
        },
        now=T0 + timedelta(minutes=minute),
    )
    store.insert_event(conn, {**event, "repo_root": repo or event["repo_root"]})
    text = policy.DEFAULT_POLICY.replace("severity: high", f"severity: {severity}")
    store.insert_flags(conn, rules.evaluate(event, *policy.parse(text), now=T0))


def shape(result):
    return [
        (
            p["project"],
            [(s["session_id"], [f["target"] for f in s["flags"]]) for s in p["sessions"]],
        )
        for p in result["projects"]
    ]


def test_empty_db(conn):
    assert digest(conn) == {
        "headline": {"actions": 0, "captured": 0, "need_review": 0},
        "projects": [],
    }


def test_open_flags_grouped_by_project_then_session_high_first(conn):
    bash(conn, "/p/a", "s1", 1, severity="review")
    bash(conn, "/p/a", "s1", 2)
    bash(conn, "/p/a", "s1", 3, bypass=False)  # not flagged
    bash(conn, "/p/a", "s1", 3, bypass=False, hook="PostToolUse")  # not an action
    bash(conn, "/p/b", "s2", 0)
    bash(conn, "/p/a", "s3", 4, severity="review")
    bash(conn, "/p/b", "s4", 5)
    conn.execute("UPDATE flags SET status = 'dismissed' WHERE evidence LIKE '%s4-5%'")

    result = digest(conn)

    assert result["headline"] == {"actions": 6, "captured": 6, "need_review": 4}
    assert shape(result) == [
        ("/p/b", [("s2", ["s2-0"])]),
        ("/p/a", [("s1", ["s1-2", "s1-1"]), ("s3", ["s3-4"])]),
    ]


def test_worktrees_group_under_their_main_checkout(conn):
    bash(conn, "/p/other", "s3", 0)
    bash(conn, "/p/main", "s1", 1)
    bash(conn, "/p/main/.claude/worktrees/wt", "s1", 2, repo="/p/main")
    bash(conn, "/elsewhere/wt2", "s2", 3, repo="/p/main")
    # stored before repo_root existed
    conn.execute("UPDATE events SET repo_root = NULL WHERE project_root = '/p/other'")

    result = digest(conn)

    assert shape(result) == [
        ("/p/other", [("s3", ["s3-0"])]),
        ("/p/main", [("s1", ["s1-1", "s1-2"]), ("s2", ["s2-3"])]),
    ]
    [main] = [p for p in result["projects"] if p["project"] == "/p/main"]
    assert [f["project"] for s in main["sessions"] for f in s["flags"]] == [
        "/p/main",
        "/p/main/.claude/worktrees/wt",
        "/elsewhere/wt2",
    ]


def test_each_flag_says_what_ran_why_and_which_rule(conn):
    bash(conn, "/p/a", "s1", 1)
    [flag] = digest(conn)["projects"][0]["sessions"][0]["flags"]
    assert isinstance(flag.pop("id"), int)
    assert flag == {
        "tool": "Bash",
        "target_kind": "command",
        "target": "s1-1",
        "stated_reason": "run s1-1",
        "rule": "sandbox_bypass",
        "severity": "high",
        "label": "preemptive",
        "evidence": {"command": "s1-1"},
        "time": "2026-10-08T12:01:00.000+00:00",
        "project": "/p/a",
        "outcome": {"kind": "unknown", "error": None},
    }


def test_a_flag_shows_its_outcome_once_the_result_arrives(conn):
    bash(conn, "/p/a", "s1", 1)
    bash(conn, "/p/a", "s1", 1, hook="PostToolUse")
    [flag] = digest(conn)["projects"][0]["sessions"][0]["flags"]
    assert flag["outcome"] == {"kind": "ran", "error": None}
