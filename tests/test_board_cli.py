"""`attnroute board` -- the command agents actually type.

Driven as a subprocess, because what is being checked includes the exit code and which
stream each thing goes to: the row goes to STDOUT so it can be piped, and everything about
the row -- its age, its staleness, what went wrong -- goes to STDERR so it cannot
contaminate the piped content.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Board Test", "GIT_AUTHOR_EMAIL": "board@test",
    "GIT_COMMITTER_NAME": "Board Test", "GIT_COMMITTER_EMAIL": "board@test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def clone(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, timeout=60)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, timeout=60)
    env = {**os.environ, **GIT_ENV}
    (work / "README.md").write_text("# work\n", encoding="utf-8")
    for args in (["add", "README.md"], ["commit", "-qm", "initial"],
                 ["push", "-q", "origin", "HEAD:refs/heads/main"]):
        subprocess.run(["git", *args], cwd=str(work), check=True, env=env, timeout=60)
    return work


def run(clone, *args, stdin=None):
    env = {**os.environ, **GIT_ENV}
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "attnroute.cli", "board", *args],
                          cwd=str(clone), text=True, input=stdin, capture_output=True,
                          env=env, timeout=300)


def test_a_row_is_published_from_stdin_and_read_back(clone):
    written = run(clone, "set", "--team", "T4", "--file", "-",
                  stdin="## T4\nrudder bolt torqued\n")
    assert written.returncode == 0, written.stderr

    got = run(clone, "get", "--team", "T4")
    assert got.returncode == 0, got.stderr
    assert got.stdout == "## T4\nrudder bolt torqued\n", (
        "the row itself must be the whole of stdout, so it can be piped")
    assert "T4" in got.stderr, "the age and flags belong on stderr"


def test_a_missing_row_exits_nonzero_and_prints_nothing_on_stdout(clone):
    got = run(clone, "get", "--team", "T4")
    assert got.returncode == 1
    assert got.stdout == ""
    assert "not present in this clone" in got.stderr


def test_the_json_form_carries_the_age_and_the_staleness(clone):
    run(clone, "set", "--team", "T4", "--file", "-", stdin="row\n")
    got = run(clone, "get", "--team", "T4", "--json")
    assert got.returncode == 0, got.stderr
    row = json.loads(got.stdout)
    assert row["stale"] is False
    assert row["age_hours"] is not None
    assert row["source"] == "origin/board:teams/T4.md"


def test_an_empty_row_is_refused(clone):
    """An empty row would read as "this team has nothing to say", which is not the same as
    a team whose row failed to build."""
    written = run(clone, "set", "--team", "T4", "--file", "-", stdin="   \n\n")
    assert written.returncode == 1
    assert "refusing to write an empty row" in written.stderr


def test_a_row_can_be_published_from_a_file(clone, tmp_path):
    source = tmp_path / "row.md"
    source.write_text("## T7\ninstaller: both forms green\n", encoding="utf-8")
    written = run(clone, "set", "--team", "T7", "--file", str(source))
    assert written.returncode == 0, written.stderr
    assert "installer" in run(clone, "get", "--team", "T7").stdout


def test_the_listing_shows_every_writer(clone):
    for team, text in (("T1", "one\n"), ("T4", "four\n"), ("_lead", "north star\n")):
        run(clone, "set", "--team", team, "--file", "-", stdin=text)
    listed = run(clone, "list")
    assert listed.returncode == 0, listed.stderr
    for expected in ("T1", "T4", "_lead.md"):
        assert expected in listed.stdout, listed.stdout


def test_the_listing_says_so_when_there_is_no_board(clone):
    listed = run(clone, "list")
    assert listed.returncode == 1
    assert "no board rows" in listed.stderr


def test_a_bad_writer_name_is_refused_with_an_explanation(clone):
    written = run(clone, "set", "--team", "../escape", "--file", "-", stdin="x\n")
    assert written.returncode == 1
    assert "not a usable writer name" in written.stderr
