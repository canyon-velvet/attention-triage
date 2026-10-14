import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from attention_triage import cli, doctor, policy, review, store
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


def log_errors(*hours_ago: float) -> None:
    """Hook errors as hook.log_error writes them, the given hours ago, oldest first."""
    store.data_dir().mkdir(parents=True, exist_ok=True)
    with open(store.data_dir() / "hook-errors.log", "a") as log:
        for i, hours in enumerate(hours_ago):
            at = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
            log.write(f"{at} Traceback (most recent call last):\n  ...\nOSError: error {i}\n\n")


def test_no_hook_errors_or_only_old_ones_pass():
    assert doctor.hook_errors().ok
    log_errors(30, 25)
    assert doctor.hook_errors().ok


def test_recent_hook_errors_fail_and_name_the_newest():
    log_errors(30, 3, 1)
    result = doctor.hook_errors()
    assert not result.ok
    assert result.what == (
        "The hook logged 2 error(s) in the last 24 hours, the latest: OSError: error 2"
    )
    assert "hook-errors.log" in result.fix


def record_drift(field: str, days_ago: float = 0, last_seen=True) -> None:
    at = review.now(datetime.now(UTC) - timedelta(days=days_ago))
    with closing(store.connect()) as conn:
        store.record_drift(conn, "PreToolUse", [(field, "missing")], at)
        if not last_seen:  # recorded before last_seen existed
            conn.execute("UPDATE drift SET last_seen = NULL WHERE field = ?", [field])
            conn.commit()


def test_recent_drift_is_a_warning_in_plain_language(capsys):
    record_drift("tool_use_id", days_ago=3)
    [result] = doctor.drift()
    assert (result.ok, result.warning) == (True, True)
    since = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
    assert result.what == (
        f"Claude Code's hook format changed: `PreToolUse.tool_use_id` missing since {since}."
        " Re-record payloads with the recorder and update the normalizer."
    )
    cli.main(["doctor"])  # a warning alone doesn't fail doctor
    assert "! Claude Code's hook format changed" in capsys.readouterr().out


def test_drift_not_seen_for_a_week_is_taken_as_fixed():
    record_drift("tool_use_id", days_ago=10)
    record_drift("cwd", days_ago=10, last_seen=False)
    assert [r.warning for r in doctor.drift()] == [False]
    record_drift("tool_use_id")  # seen again today: reported, still since its first day
    [result] = doctor.drift()
    first = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()
    assert result.warning and f"missing since {first}." in result.what


def test_a_db_from_before_last_seen_gets_the_column(home):
    path = store.data_dir() / "triage.db"
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as old:
        old.execute(
            "CREATE TABLE drift (hook_event TEXT NOT NULL, field TEXT NOT NULL,"
            " problem TEXT NOT NULL, first_seen TEXT NOT NULL,"
            " PRIMARY KEY (hook_event, field, problem))"
        )
        old.execute("INSERT INTO drift VALUES ('PreToolUse', 'cwd', 'missing', ?)", [review.now()])
        old.commit()
    [result] = doctor.drift()  # read as first_seen
    assert result.warning and "`PreToolUse.cwd` missing" in result.what
