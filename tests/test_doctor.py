import json
import os
import shutil
from pathlib import Path

import pytest

from attention_triage import cli, doctor


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


def test_settings_that_cant_be_read_are_reported():
    cli.settings_path().unlink()
    cli.settings_path().mkdir()
    result = doctor.hooks()
    assert not result.ok and "can't be read" in result.what


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
