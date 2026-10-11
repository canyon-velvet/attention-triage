import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from attention_triage.normalize import EVENT_TYPES, normalize, project_root, repo_root

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"
ALL_FIXTURES = sorted(FIXTURES.glob("*/*.json"))
FIELDS = {
    "agent",
    "session_id",
    "agent_id",
    "event_type",
    "tool_name",
    "tool_use_id",
    "target_kind",
    "target",
    "stated_reason",
    "project_root",
    "repo_root",
    "cwd",
    "ts",
    "summary",
    "raw",
    "dedup_key",
    "install_scope",
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_fixture_maps_to_the_neutral_shape(path):
    payload = json.loads(path.read_text())
    event = normalize(payload)
    assert set(event) == FIELDS
    assert event["agent"] == "claude-code" and event["install_scope"] == "user"
    assert event["event_type"] == EVENT_TYPES[payload["hook_event_name"]]
    assert event["session_id"] == payload["session_id"] and event["cwd"] == payload["cwd"]
    assert json.loads(event["raw"]) == payload
    assert len(event["dedup_key"]) == 64


def test_bash_call_targets_its_command_and_keeps_the_description():
    event = normalize(load("auto-sandbox-block-filesystem/01-PreToolUse.json"))
    assert (event["target_kind"], event["target"]) == (
        "command",
        "touch ~/triage-spike-blocked.txt",
    )
    assert event["stated_reason"] == "Create empty file in home directory"
    assert event["tool_use_id"].startswith("toolu_")
    assert event["summary"] == {"permission_mode": "auto"}


def test_file_and_url_targets():
    write = normalize(load("auto-write-outside-project/01-PreToolUse.json"))
    assert (write["target_kind"], write["target"]) == (
        "path",
        "/Users/alice/Dev/triage-spike-outside/test.txt",
    )
    fetch = normalize(load("auto-webfetch/01-PreToolUse.json"))
    assert (fetch["target_kind"], fetch["target"]) == ("url", "https://example.com")


def test_events_without_a_tool_have_no_target():
    event = normalize(load("session-lifecycle/01-SessionStart.json"))
    assert event["tool_name"] is None and event["target_kind"] is None and event["target"] is None


def test_failure_summary_keeps_the_error():
    event = normalize(load("auto-sandbox-block-filesystem/02-PostToolUseFailure.json"))
    assert "Operation not permitted" in event["summary"]["error"]


def test_subagent_tool_calls_carry_agent_id():
    assert normalize(load("auto-subagent/04-PreToolUse.json"))["agent_id"]
    assert normalize(load("auto-subagent/01-PreToolUse.json"))["agent_id"] is None


def test_dedup_key_separates_events_of_one_tool_call():
    pre = normalize(load("default-prompt-approved-write/01-PreToolUse.json"))
    request = normalize(load("default-prompt-approved-write/02-PermissionRequest.json"))
    post = normalize(load("default-prompt-approved-write/03-PostToolUse.json"))
    assert len({pre["dedup_key"], request["dedup_key"], post["dedup_key"]}) == 3


def test_dedup_key_is_stable_for_a_redelivered_payload():
    payload = load("session-lifecycle/02-Stop.json")  # has no tool_use_id
    assert (
        normalize(payload)["dedup_key"] == normalize(dict(reversed(payload.items())))["dedup_key"]
    )


def test_each_permission_request_firing_gets_its_own_key():
    """Prompts for a call repeated in one turn are byte-identical; only the capture time differs."""
    payload = load("default-prompt-approved-write/02-PermissionRequest.json")
    first, again = (datetime(2026, 10, 8, 12, 0, s, tzinfo=UTC) for s in (0, 30))
    key = normalize(payload, now=first)["dedup_key"]
    assert key != normalize(payload, now=again)["dedup_key"]
    assert key == normalize(dict(reversed(payload.items())), now=first)["dedup_key"]


def test_unknown_event_and_bad_tool_input_pass_through():
    event = normalize({"hook_event_name": "Notification", "session_id": "s", "tool_input": "oops"})
    assert event["event_type"] == "Notification" and event["target"] is None


def test_project_root_is_the_git_root_else_cwd(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    nested = tmp_path / "repo" / "src" / "pkg"
    nested.mkdir(parents=True)
    assert project_root(str(nested)) == str(tmp_path / "repo")
    assert project_root(str(tmp_path)) == str(tmp_path)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True)


def test_repo_root_of_a_worktree_is_its_main_checkout(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=main)
    git("worktree", "add", "-q", str(main / "inside"), cwd=main)
    git("worktree", "add", "-q", str(tmp_path / "outside"), cwd=main)
    for worktree in [main / "inside", tmp_path / "outside"]:
        (worktree / "src").mkdir()
        event = normalize({"hook_event_name": "Stop", "cwd": str(worktree / "src")})
        assert event["project_root"] == str(worktree)  # R1's boundary stays the worktree
        assert event["repo_root"] == str(main)
    assert repo_root(str(main)) == str(main)


def test_worktrees_of_bare_repos_in_one_folder_stay_apart(tmp_path):
    """A bare repo has no checkout above its git dir, so the git dir itself names the repo."""
    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=main)
    for name in ["foo", "bar"]:
        git("clone", "-q", "--bare", str(main), str(tmp_path / f"{name}.git"), cwd=tmp_path)
        git("worktree", "add", "-q", str(tmp_path / f"{name}-wt"), cwd=tmp_path / f"{name}.git")
        assert repo_root(str(tmp_path / f"{name}-wt")) == str(tmp_path / f"{name}.git")


@pytest.mark.parametrize("dot_git", [b"gitdir: ../.git/modules/sub", b"garbage", b"\xff\xfe"])
def test_repo_root_is_the_project_root_unless_dot_git_leads_to_a_main_checkout(tmp_path, dot_git):
    """A submodule's .git file points to a gitdir without a commondir; a broken one, nowhere."""
    (tmp_path / ".git").write_bytes(dot_git)
    assert repo_root(str(tmp_path)) == str(tmp_path)
    assert repo_root(str(tmp_path / "no-git")) == str(tmp_path / "no-git")
    assert repo_root(None) is None
