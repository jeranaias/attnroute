"""The nudge has to name a command that works.

From the field: it said `attnroute note add …`, and on the shared machine `attnroute` on PATH
was an older install with no `note` subcommand. A session followed the advice, got an error,
and concluded the feature did not exist. That is the same shape as every other bug this
project keeps turning up -- authoritative text naming something that does not do what it
says -- and the fix is the same: derive the answer from what is actually there, and check it.

The test with teeth is the last one: it RUNS the command the nudge names.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from attnroute import session_state as ss

REPO = Path(__file__).resolve().parents[1]


class TestTheCommandIsDerivedAndChecked:

    def test_a_console_script_beside_the_interpreter_is_used(self, tmp_path, monkeypatch):
        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        name = "attnroute.exe" if os.name == "nt" else "attnroute"
        (scripts / name).write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("sysconfig.get_path", lambda what: str(scripts))

        found = ss.note_command()

        assert name in found["command"]
        assert found["why"] == "console script"

    def test_the_module_form_is_used_when_there_is_no_console_script(
            self, tmp_path, monkeypatch):
        """⚠ Naming a path that does not exist would be the same bug in a new costume, so
        the console script is used ONLY when the file is really there."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr("sysconfig.get_path", lambda what: str(empty))
        monkeypatch.setattr(ss, "Path", Path)        # keep Path real
        monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))

        found = ss.note_command()

        assert "-m attnroute.cli" in found["command"]
        assert "no console script" in found["why"]

    def test_a_path_with_a_space_is_quoted(self, tmp_path, monkeypatch):
        scripts = tmp_path / "Program Files" / "Scripts"
        scripts.mkdir(parents=True)
        name = "attnroute.exe" if os.name == "nt" else "attnroute"
        (scripts / name).write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("sysconfig.get_path", lambda what: str(scripts))

        command = ss.note_command()["command"]

        assert command.startswith('"') and command.endswith('"'), command

    def test_a_path_without_a_space_is_not_quoted(self, tmp_path, monkeypatch):
        scripts = tmp_path / "plain"
        scripts.mkdir()
        name = "attnroute.exe" if os.name == "nt" else "attnroute"
        (scripts / name).write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("sysconfig.get_path", lambda what: str(scripts))
        assert '"' not in ss.note_command()["command"]

    def test_a_broken_sysconfig_does_not_raise(self, monkeypatch):
        monkeypatch.setattr("sysconfig.get_path",
                            lambda what: (_ for _ in ()).throw(KeyError("scripts")))
        assert ss.note_command()["command"]


class TestTheNudgeNamesIt:

    def test_the_text_carries_the_derived_command(self):
        command = ss.note_command()["command"]
        text = ss.NUDGE_TEXT.format(edits=7, command=command)
        assert command in text

    def test_the_text_no_longer_advises_a_bare_attnroute(self):
        """⚠ THE FIELD BUG. `attnroute note add` on its own is advice that depends on PATH,
        and PATH pointed at an install without the subcommand."""
        text = ss.NUDGE_TEXT.format(edits=7, command=ss.note_command()["command"])
        assert "\n  attnroute note add" not in text
        assert "on PATH may be a different one" in text

    def test_the_hook_fills_the_command_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv(ss.ACT_ENV if hasattr(ss, "ACT_ENV") else "X", raising=False)
        state = ss.empty_state("s")
        state["facts"] = {"edited": {f"f{i}.py": 1 for i in range(10)}, "commands": []}
        state["turns_since_nudge"] = ss.NUDGE_EVERY_TURNS
        ss.save("s", state)

        out = ss.hook({"hook_event_name": "Stop", "session_id": "s", "transcript_path": ""})

        assert out, "the nudge was due and did not fire"
        text = out["hookSpecificOutput"]["additionalContext"]
        assert ss.note_command()["command"] in text
        assert "{command}" not in text, "the placeholder was left unfilled"


def test_the_command_the_nudge_names_actually_runs():
    """THE ONE WITH TEETH. Everything above checks the string; this checks the string is a
    command that exists and answers to `note`. It is what would have caught the field bug."""
    command = ss.note_command()["command"]
    # Rebuild the argv rather than going through a shell, so quoting is not under test here.
    if " -m attnroute.cli" in command:
        argv = [command.split(" -m ")[0].strip('"'), "-m", "attnroute.cli"]
    else:
        argv = [command.strip('"')]

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([*argv, "note", "--help"], capture_output=True, text=True,
                          timeout=300, env=env)

    assert done.returncode == 0, f"{argv} note --help failed:\n{done.stderr[-2000:]}"
    assert "amend" in done.stdout, "the command this names has no amend subcommand"


def test_status_reports_both_installs():
    """The answer to "should it warn?": yes, in `status`, whose job is to say what you have
    -- not on the hook path, where a PATH scan every turn buys a message nobody asked for."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([sys.executable, "-m", "attnroute.cli", "status"],
                          capture_output=True, text=True, timeout=300, env=env)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "Executables:" in done.stdout
    assert "this install:" in done.stdout
    assert "on PATH:" in done.stdout
