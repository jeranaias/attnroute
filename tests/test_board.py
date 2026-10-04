"""The team board, exercised against real git repositories.

Two clones of one bare remote stand in for the two machines the teams actually sit on,
because every interesting property of this design is about what happens when two writers
act at once, and a mocked git cannot tell you that.

What is asserted here, in order of how much it would hurt to get wrong:

  * the read path NEVER touches the network -- it is a `SessionStart` hook;
  * a write does not touch the working tree or the index of the repository it runs in;
  * two writers never conflict, and a rejected push rebuilds rather than clobbers;
  * a stale row says so, instead of looking current.
"""

import os
import subprocess
from pathlib import Path

import pytest

from attnroute import board

#: These drive REAL git in real repositories, which is the point of them and also ~83 s of
#: wall clock. CI runs them on one Linux job and one Windows job -- Windows because paths
#: and `os.replace` behave differently there -- and skips them elsewhere. They are never
#: mocked: every property worth having here is about two writers racing, and a mocked git
#: cannot show you that.
pytestmark = pytest.mark.realgit

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Board Test", "GIT_AUTHOR_EMAIL": "board@test",
    "GIT_COMMITTER_NAME": "Board Test", "GIT_COMMITTER_EMAIL": "board@test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo, *args, stdin=None, env_extra=None):
    env = {**os.environ, **GIT_ENV, **(env_extra or {})}
    proc = subprocess.run(["git", *args], cwd=str(repo), text=True, input=stdin,
                          capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """A test must not depend on, or write to, the developer's git config."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, timeout=60)
    return bare


@pytest.fixture
def clones(tmp_path, remote):
    """Two working clones: the two machines."""
    made = []
    for name in ("machine-a", "machine-b"):
        path = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True,
                       timeout=60)
        (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        git(path, "add", "README.md")
        git(path, "commit", "-qm", "initial")
        made.append(path)
    # One clone pushes main so both have a normal history to be an orphan branch beside.
    git(made[0], "push", "-q", "origin", "HEAD:refs/heads/main")
    git(made[1], "fetch", "-q", "origin")
    return made


class TestWritingTouchesNothingItShouldNot:

    def test_the_working_tree_and_index_are_untouched(self, clones):
        a, _ = clones
        (a / "work-in-progress.py").write_text("x = 1\n", encoding="utf-8")
        git(a, "add", "work-in-progress.py")
        before = git(a, "status", "--porcelain")

        board.write("T4", "## T4\nrudder bolt torqued\n", repo=a)

        assert git(a, "status", "--porcelain") == before, (
            "a board write must be invisible to whatever else is happening in that repo")

    def test_the_branch_is_never_checked_out(self, clones):
        a, _ = clones
        board.write("T4", "row\n", repo=a)
        assert git(a, "rev-parse", "--abbrev-ref", "HEAD").strip() != board.BRANCH
        assert not (a / "teams").exists(), "the board must not appear in the working tree"

    def test_the_branch_is_an_orphan(self, clones):
        """It must share no history with main, so `git log main` never shows board rows."""
        a, _ = clones
        board.write("T4", "row\n", repo=a)
        main = git(a, "rev-parse", "origin/main").strip()
        history = git(a, "rev-list", "origin/board").split()
        assert main not in history, "the board is in main's history"
        assert len(history) == 1, history

    def test_no_local_branch_is_created(self, clones):
        """`refs/heads/board` in a developer's clone is one `git checkout` from trouble."""
        a, _ = clones
        board.write("T4", "row\n", repo=a)
        heads = git(a, "for-each-ref", "--format=%(refname)", "refs/heads/")
        assert "refs/heads/board" not in heads, heads


class TestReadingNeedsNoNetwork:

    def test_a_row_is_read_from_the_local_ref_with_the_remote_gone(self, clones, tmp_path):
        """THE PROPERTY THAT MATTERS: this runs in a SessionStart hook. Pointing the remote
        at a path that does not exist is a stand-in for having no network at all."""
        a, _ = clones
        board.write("T4", "## T4\ndepth gate ON\n", repo=a)
        git(a, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

        row = board.read("T4", repo=a)

        assert "depth gate ON" in row["text"]
        assert row["stale"] is False
        assert row["source"] == "origin/board:teams/T4.md"

    def test_a_missing_row_is_distinguished_from_a_missing_branch(self, clones):
        """Different problems, different fixes: nobody has written one, versus this clone
        has never fetched the branch."""
        a, _ = clones
        absent_branch = board.read("T4", repo=a)
        assert "not present in this clone" in absent_branch["why"]
        assert absent_branch["text"] == ""

        board.write("T1", "row\n", repo=a)
        absent_row = board.read("T4", repo=a)
        assert absent_row["why"] == "no row for T4 on origin/board"

    def test_reading_reports_the_age(self, clones):
        a, _ = clones
        board.write("T4", "row\n", repo=a)
        row = board.read("T4", repo=a)
        # The whole row goes in the message: when this failed on one CI job only, the
        # reason was in `why` and the log did not show it.
        assert row["written_at"], row
        assert row["age_hours"] is not None and row["age_hours"] < 1, row

    def test_an_old_row_is_reported_stale_rather_than_dropped(self, clones, monkeypatch):
        """A row that is quietly dropped is indistinguishable from a team that never
        wrote one, so staleness is reported with the row, not instead of it."""
        a, _ = clones
        old = "2026-01-01T00:00:00+00:00"
        monkeypatch.setenv("GIT_COMMITTER_DATE", old)
        monkeypatch.setenv("GIT_AUTHOR_DATE", old)
        board.write("T4", "ancient\n", repo=a)

        row = board.read("T4", repo=a)

        assert row["stale"] is True, row
        assert "ancient" in row["text"], "the text must still be served"
        assert "older than" in row["why"], row


class TestOneWriterPerFileMeansNoConflicts:

    def test_two_machines_each_keep_their_own_row(self, clones):
        a, b = clones
        board.write("T1", "## T1\nfrom machine a\n", repo=a)
        git(b, "fetch", "-q", "origin", "board:refs/remotes/origin/board")
        board.write("T4", "## T4\nfrom machine b\n", repo=b)

        git(a, "fetch", "-q", "origin", "board:refs/remotes/origin/board")
        assert "from machine a" in board.read("T1", repo=a)["text"]
        assert "from machine b" in board.read("T4", repo=a)["text"]

    def test_a_rejected_push_rebuilds_instead_of_clobbering(self, clones):
        """⚠ THE REAL RACE. Machine A writes without ever having seen B's write, so its
        push is a non-fast-forward. It must rebuild its own file on B's tip -- and B's row
        must survive, which is exactly what a force-push or a clobber would destroy."""
        a, b = clones
        board.write("T1", "## T1\nfirst\n", repo=a)
        git(b, "fetch", "-q", "origin", "board:refs/remotes/origin/board")
        board.write("T4", "## T4\nfrom b\n", repo=b)
        # A still believes the tip is its own commit: it has not fetched B's.

        result = board.write("T1", "## T1\nsecond\n", repo=a)

        assert result["attempts"] >= 2, "the push should have been rejected once"
        git(a, "fetch", "-q", "origin", "board:refs/remotes/origin/board")
        assert "second" in board.read("T1", repo=a)["text"]
        assert "from b" in board.read("T4", repo=a)["text"], "B's row was lost"

    def test_the_lead_file_is_its_own_writer(self, clones):
        a, _ = clones
        board.write("_lead", "# North star\nDelivery first.\n", repo=a)
        row = board.read("_lead", repo=a)
        assert "North star" in row["text"]
        assert row["source"] == "origin/board:_lead.md"

    def test_writers_lists_the_teams_with_the_lead_last(self, clones):
        a, _ = clones
        for name in ("T4", "T1", "_lead"):
            board.write(name, f"row {name}\n", repo=a)
        assert board.writers(repo=a) == ["T1", "T4", "_lead.md"]

    def test_writers_is_empty_rather_than_an_error_without_a_branch(self, clones):
        a, _ = clones
        assert board.writers(repo=a) == []


class TestTheWriterNameIsValidated:
    """It becomes a path in a git tree, so it is checked rather than trusted."""

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", "   ", "T4;rm -rf /", "T4.md"])
    def test_a_name_that_is_not_a_plain_identifier_is_refused(self, bad):
        with pytest.raises(board.BoardError):
            board.team_file(bad)

    @pytest.mark.parametrize("good", ["T1", "T7", "architect", "jesse-dd", "team_5"])
    def test_an_ordinary_name_is_accepted(self, good):
        assert board.team_file(good) == f"teams/{good}.md"


def test_a_write_signs_only_when_the_repository_asks(clones):
    """A board write must not bypass a signing policy, and must not invent one either."""
    a, _ = clones
    assert board._signing_args(a) == []
    git(a, "config", "commit.gpgsign", "true")
    assert board._signing_args(a) == ["-S"]


def test_a_row_always_ends_with_a_newline(clones):
    a, _ = clones
    board.write("T4", "no trailing newline", repo=a)
    assert board.read("T4", repo=a)["text"].endswith("\n")
