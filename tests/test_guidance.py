import shlex
from contextlib import closing
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from attention_triage import hook, store
from attention_triage.digest import digest
from attention_triage.normalize import normalize
from attention_triage.redact import redact

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SECONDS = count()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git checkout (a .git dir is all Triage looks at; nothing runs git)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "my repo" / ".git").mkdir(parents=True)
    return tmp_path / "my repo"


@pytest.fixture
def conn(repo):
    with closing(store.connect()) as conn:
        yield conn


def call(conn, cwd, tool: str, tool_input: dict, response: dict | None = None, use_id=None):
    """Store a call (and its result, when given) as the hook does."""
    use_id = use_id or f"t{next(SECONDS)}"
    for name, extra in [("PreToolUse", {}), ("PostToolUse", {"tool_response": response})]:
        if name == "PostToolUse" and response is None:
            break
        payload = {"hook_event_name": name, "session_id": "s1", "cwd": str(cwd), "tool_name": tool}
        payload |= {"tool_use_id": use_id, "tool_input": tool_input, **extra}
        event = normalize(redact(payload), now=T0 + timedelta(seconds=next(SECONDS)))
        store.insert_event(conn, event)
        hook.flag(conn, [event])


def guidance(conn) -> list[dict]:
    return [
        f["guidance"] for p in digest(conn)["projects"] for s in p["sessions"] for f in s["flags"]
    ]


def test_a_file_in_the_checkout_gets_quoted_diff_and_restore_commands(conn, repo):
    call(
        conn, repo, "Write", {"file_path": str(repo / ".claude" / "settings.json"), "content": "{}"}
    )
    [found] = guidance(conn)
    git = f"git -C {shlex.quote(str(repo))}"
    assert git.endswith("/my repo'")  # the checkout's path has a space, so it is quoted
    assert found["files"] == [
        {
            "path": str(repo / ".claude" / "settings.json"),
            "commands": [
                f"{git} diff -- .claude/settings.json",
                f"{git} restore -- .claude/settings.json",
            ],
        }
    ]


def test_a_file_outside_the_checkout_gets_no_git_commands(conn, repo):
    call(conn, repo, "Write", {"file_path": "/srv/notes.txt", "content": "x"})  # not a temp dir
    [found] = guidance(conn)
    assert found["files"] == [{"path": "/srv/notes.txt", "commands": []}]


def test_a_bash_calls_changed_files_and_later_commits_get_commands(conn, repo):
    call(conn, repo, "Bash", {"command": "git commit -m old", "dangerouslyDisableSandbox": False},
         {"stdout": "[main 0000000] Before the flag\n"})  # fmt: skip
    changed = str(repo / "src" / "a b'c.py")
    call(conn, repo, "Bash", {"command": "make fix", "dangerouslyDisableSandbox": True},
         {"stdout": "", "bashEditDiff": {"changedFiles": [changed]}})  # fmt: skip
    call(conn, repo, "Bash", {"command": "git commit -am fix"},
         {"stdout": "[detached HEAD 1a2b3c4] Fix it\n 1 file changed\n"})  # fmt: skip
    [found] = guidance(conn)
    git = f"git -C {shlex.quote(str(repo))}"
    assert found["files"] == [
        {
            "path": changed,
            "commands": [f"{git} diff -- {shlex.quote('src/a b' + chr(39) + 'c.py')}",
                         f"{git} restore -- {shlex.quote('src/a b' + chr(39) + 'c.py')}"],
        }
    ]  # fmt: skip
    assert found["commits"] == [
        {"sha": "1a2b3c4", "subject": "Fix it", "command": f"{git} revert 1a2b3c4"}
    ]


def test_without_a_git_checkout_there_are_no_commands(conn, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    call(conn, plain, "Bash", {"command": "make", "dangerouslyDisableSandbox": True},
         {"stdout": "[main 1a2b3c4] Fix\n", "bashEditDiff": {"changedFiles": [str(plain / "x")]}})  # fmt: skip
    [found] = guidance(conn)
    assert found["files"] == [{"path": str(plain / "x"), "commands": []}]
    assert found["commits"] == []


def test_a_checkout_deleted_since_gets_no_commands(conn, repo):
    call(
        conn, repo, "Write", {"file_path": str(repo / ".claude" / "settings.json"), "content": "{}"}
    )
    (repo / ".git").rmdir()
    [found] = guidance(conn)
    assert found["files"][0]["commands"] == []


def test_a_path_that_redaction_cut_is_left_out(conn, repo):
    long = str(repo / ("d" * 3000) / "f")
    call(conn, repo, "Bash", {"command": "make", "dangerouslyDisableSandbox": True},
         {"stdout": "", "bashEditDiff": {"changedFiles": [long]}})  # fmt: skip
    [found] = guidance(conn)
    assert found["files"] == []


def test_a_call_that_changed_nothing_has_no_guidance(conn, repo):
    call(conn, repo, "WebFetch", {"url": "https://unlisted.example/x", "prompt": "read"})
    assert guidance(conn) == [{"files": [], "commits": []}]
