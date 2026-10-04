"""`attnroute note` and `attnroute state` -- the commands the nudge names.

The nudge tells the model to run `attnroute note add`. A nudge that names a command which
does not exist is the same mistake the read ledger's notice made before review caught it:
advertising an escape hatch that does not work.

Run as a subprocess, because the exit code is part of the contract here -- an unsupported
promotion claim is a FAILURE of the command, not a remark in passing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path):
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


def run(home, project, *args, stdin=None, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # Both names are cleared, so a test that means to exercise the environment has to say
    # so -- and so this suite cannot pass by inheriting the id of the session running it.
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "attnroute.cli", *args],
                          cwd=str(project), text=True, input=stdin, capture_output=True,
                          env=env, timeout=300)


def test_the_command_the_nudge_names_exists_and_records(home, project):
    """If this fails, the nudge is pointing at nothing."""
    from attnroute.session_state import NUDGE_TEXT, note_command

    # The nudge used to advise a bare `attnroute`, and on the shared machine that resolved
    # to an older install with no `note` subcommand. It now names this install's own
    # executable; tests/test_nudge_command.py runs the command it names.
    assert "note add --kind ruling" in NUDGE_TEXT
    assert note_command()["command"] in NUDGE_TEXT.format(
        edits=1, command=note_command()["command"])
    done = run(home, project, "note", "add", "depth gate refuses unknown legs",
               "--kind", "ruling", "--session", "s")
    assert done.returncode == 0, done.stderr
    assert "recorded ruling" in done.stderr

    shown = run(home, project, "state", "show", "--session", "s")
    assert shown.returncode == 0, shown.stderr
    assert "depth gate refuses unknown legs" in shown.stdout
    assert "UNPROMOTED" in shown.stdout


def test_an_unsupported_promotion_claim_fails_the_command(home, project):
    """⚠ Exit 1, not a note in passing. `--promoted-to docs/x.md` when that file says
    nothing about this ruling is worse than no claim, because every listing afterwards
    reads it as filed."""
    (project / "ADR.md").write_text("Nothing to do with it.\n", encoding="utf-8")
    done = run(home, project, "note", "add", "depth gate refuses unknown legs",
               "--kind", "ruling", "--promoted-to", "ADR.md", "--session", "s")
    assert done.returncode == 1
    assert "CLAIMED" in done.stderr


def test_a_supported_promotion_claim_succeeds(home, project):
    """Positive control for the test above: the check must be capable of passing."""
    (project / "ADR.md").write_text(
        "The depth gate refuses unknown legs outright.\n", encoding="utf-8")
    done = run(home, project, "note", "add", "depth gate refuses unknown legs",
               "--kind", "ruling", "--promoted-to", "ADR.md", "--session", "s")
    assert done.returncode == 0, done.stderr
    assert "PROMOTED" in done.stderr


def test_an_empty_note_is_refused(home, project):
    done = run(home, project, "note", "add", "   ", "--session", "s")
    assert done.returncode == 1
    assert "refusing to record an empty note" in done.stderr


def test_an_unknown_kind_is_refused_with_the_list(home, project):
    done = run(home, project, "note", "add", "x", "--kind", "opinion", "--session", "s")
    assert done.returncode == 1
    assert "ruling" in done.stderr and "measurement" in done.stderr


def test_state_show_says_so_when_there_is_nothing(home, project):
    done = run(home, project, "state", "show", "--session", "nobody")
    assert done.returncode == 1
    assert "no notes recorded" in done.stderr


def test_state_handback_prints_what_the_next_window_would_get(home, project):
    run(home, project, "note", "add", "the ruling that must survive", "--kind", "ruling",
        "--session", "s")
    done = run(home, project, "state", "handback", "--session", "s")
    assert done.returncode == 0, done.stderr
    assert "the ruling that must survive" in done.stdout
    assert "of 1500 tokens" in done.stderr


def test_the_handback_json_carries_the_budget_and_the_drops(home, project):
    run(home, project, "note", "add", "a ruling", "--kind", "ruling", "--session", "s")
    done = run(home, project, "state", "handback", "--session", "s", "--json")
    assert done.returncode == 0, done.stderr
    built = json.loads(done.stdout)
    assert built["budget"] == 1500
    assert built["dropped"] == 0
    assert built["tokens"] <= built["budget"]


def test_the_claimed_rows_are_explained_in_the_listing(home, project):
    (project / "ADR.md").write_text("Nothing relevant.\n", encoding="utf-8")
    run(home, project, "note", "add", "depth gate refuses unknown legs", "--kind", "ruling",
        "--promoted-to", "ADR.md", "--session", "s")
    shown = run(home, project, "state", "show", "--session", "s")
    assert "CLAIMED" in shown.stdout
    assert "mentions only" in shown.stdout


def test_a_note_lands_on_the_session_the_CLI_actually_names(home, project):
    """⚠ END TO END, with the environment set exactly as 2.1.280 sets it. This is the test
    that would have caught the blocker: before the fix, `note add` filed under "local" and
    `state show --session <real id>` found nothing."""
    env_id = "4b298fa8-0cd4-4405-9453-4035018bcd25"
    added = run(home, project, "note", "add", "the ruling that must survive",
                "--kind", "ruling", env_extra={"CLAUDE_CODE_SESSION_ID": env_id})
    assert added.returncode == 0, added.stderr

    shown = run(home, project, "state", "show", "--session", env_id)
    assert shown.returncode == 0, shown.stderr
    assert "the ruling that must survive" in shown.stdout


def test_the_note_is_not_filed_under_a_made_up_session(home, project):
    """Positive control for the test above: with NO session in the environment the command
    must fail, not quietly invent one."""
    added = run(home, project, "note", "add", "a ruling", "--kind", "ruling")
    assert added.returncode == 1
    assert "no session id in the environment" in added.stderr


def test_the_handback_reads_the_same_session(home, project):
    env_id = "sess-42"
    run(home, project, "note", "add", "depth gate refuses unknown legs", "--kind", "ruling",
        env_extra={"CLAUDE_CODE_SESSION_ID": env_id})
    built = run(home, project, "state", "handback",
                env_extra={"CLAUDE_CODE_SESSION_ID": env_id})
    assert built.returncode == 0, built.stderr
    assert "depth gate refuses unknown legs" in built.stdout
