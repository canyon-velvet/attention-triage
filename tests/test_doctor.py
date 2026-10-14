import json
import os
import shutil
from pathlib import Path

import pytest

from attention_triage import cli, doctor, policy, store
from attention_triage.normalize import normalize


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A HOME of its own, with the hook installed from a copy of triage-hook that a test can
    remove; the real ~/.claude and ~/.attention-triage are never touched."""
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / "bin" / cli.HOOK
    program.parent.mkdir()
    shutil.copy(Path(cli.sys.executable).parent / cli.HOOK, program)
    monkeypatch.setattr(cli.sys, "executable", str(program.parent / "python"))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cli.main(["install"])
    return tmp_path


def settings() -> dict:
    return json.loads(cli.settings_path().read_text())


def save(value: dict) -> None:
    cli.settings_path().write_text(json.dumps(value))


def test_a_healthy_install_passes_every_check(capsys):
    assert all(result.ok for result in doctor.run())
    cli.main(["doctor"])  # exits 0: no SystemExit
    out = capsys.readouterr().out
    assert out.count("✓") == len(doctor.CHECKS) and "✗" not in out


def test_any_failure_prints_its_fix_and_exits_1(capsys):
    cli.main(["uninstall"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit:
        cli.main(["doctor"])
    assert exit.value.code == 1
    assert "✗ Hooks missing from" in capsys.readouterr().out


def test_hooks_missing_from_settings_say_to_run_install():
    value = settings()
    del value["hooks"]["PostToolUse"]
    save(value)
    result = doctor.hooks()
    assert (result.ok, result.fix) == (False, "Run `triage install`.")
    assert result.what.endswith(": PostToolUse")


def test_a_missing_capture_failure_notice_is_reported():
    value = settings()
    value["hooks"]["SessionStart"] = value["hooks"]["SessionStart"][:1]  # the capture handler only
    save(value)
    assert doctor.hooks().what.endswith(": the capture-failure notice")


def test_the_notice_alone_doesnt_count_as_capturing_session_start():
    value = settings()
    value["hooks"]["SessionStart"] = value["hooks"]["SessionStart"][1:]  # the notice only
    save(value)
    assert doctor.hooks().what.endswith(": SessionStart")


def test_unreadable_settings_are_reported():
    cli.settings_path().write_text("{not json")
    result = doctor.hooks()
    assert not result.ok and "can't be read" in result.what


def test_a_hook_program_that_is_gone_is_reported(home):
    (home / "bin" / cli.HOOK).unlink()
    result = doctor.hooks()
    assert not result.ok and "nothing is captured" in result.what
    assert "triage install" in result.fix


def test_a_database_that_cant_be_written_is_reported():
    """Another program holds the write lock past the 2 s busy timeout (as root, file permissions
    can't make it fail)."""
    with store.connect() as holder:
        holder.execute("BEGIN IMMEDIATE")
        result = doctor.database()
        holder.rollback()
    assert not result.ok and "database is locked" in result.what and result.fix


def test_buffered_events_are_stored_then_pass(home):
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "/p/a",
        "tool_name": "Bash",
    }
    event |= {
        "tool_use_id": "t1",
        "tool_input": {"command": "ls", "dangerouslyDisableSandbox": True},
    }
    store.spool(normalize(event))
    assert doctor.spool().ok
    assert not store.spool_path().exists()
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM flags").fetchone() == (1,)  # flagged too


def test_buffered_events_that_cant_be_stored_are_reported(monkeypatch):
    store.spool({"not": "an event"})
    result = doctor.spool()
    assert not result.ok and str(store.spool_path()) in result.what and result.fix
    assert store.spool_path().exists()  # kept for the next run


def test_an_invalid_policy_is_reported():
    policy.path().write_text(policy.DEFAULT_POLICY.replace("sandbox_bypass:", "sandbox_bypas:"))
    result = doctor.policy_file()
    assert not result.ok and "sandbox_bypas" in result.what
    assert "delete the file" in result.fix
    os.remove(policy.path())
    assert doctor.policy_file().ok  # the default comes back


def test_a_data_dir_that_isnt_a_folder_is_reported_and_the_other_checks_still_run(home):
    shutil.rmtree(home / ".attention-triage")
    (home / ".attention-triage").write_text("")
    results = doctor.run()
    assert len(results) == len(doctor.CHECKS)
    database = results[doctor.CHECKS.index(doctor.database)]
    assert not database.ok and database.what.startswith("Can't write to") and database.fix


def test_settings_that_cant_be_read_are_reported():
    cli.settings_path().unlink()
    cli.settings_path().mkdir()
    result = doctor.hooks()
    assert not result.ok and "can't be read" in result.what


def test_a_policy_that_isnt_utf8_is_reported():
    policy.path().write_bytes(b"\xff\xfe")
    result = doctor.policy_file()
    assert not result.ok and "is invalid" in result.what and result.fix


def test_a_hook_named_by_path_or_home_is_found(home, monkeypatch):
    monkeypatch.setenv("PATH", str(home / "bin") + os.pathsep + os.environ["PATH"])
    value = settings()
    for groups in value["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                if cli.is_ours(handler):
                    words = handler["command"].split(" ")
                    handler["command"] = " ".join(["triage-hook", *words[1:]])
    save(value)
    assert doctor.hooks().ok
    for groups in value["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = handler["command"].replace(
                    "triage-hook", "~/bin/triage-hook", 1
                )
    save(value)
    assert doctor.hooks().ok


def test_a_check_that_fails_unexpectedly_is_a_failed_result(monkeypatch):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "CHECKS", [broken, *doctor.CHECKS[1:]])
    [first, *_] = doctor.run()
    assert not first.ok and "boom" in first.what and first.fix
