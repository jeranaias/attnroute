"""UNPROMOTED means "this session only", and the places an operator reads must say so.

From the field: a ruling recorded in one session is never handed to a new one. Two halves,
and only one was reported.

  (a) A NEW session id has its own empty state file, so prior notes are invisible to it.
      That is not a bug -- a per-session file is per session -- but nothing said it, so a
      team assumed a recorded ruling was safe.
  (b) Even the SAME session got a handback only at `SessionStart:compact`. The source enum
      in the installed CLI is {startup, resume, clear, compact, fork}, so a `resume` or a
      `clear` -- which keep the session and its id -- handed back NOTHING. That one is a
      mechanical miss.
"""

import os
from pathlib import Path

import pytest

from attnroute import session_state as ss


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake))
    assert str(ss.state_path("probe")).startswith(str(fake)), "HOME redirect failed"
    return fake


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    return d


class TestTheWordingSaysWhatItMeans:

    def test_UNPROMOTED_says_this_session_only(self):
        why = ss.check_promotion({"text": "a ruling"})["why"]
        assert "THIS SESSION ONLY" in why
        assert "another team" in why
        assert "after a restart" in why

    def test_it_still_says_the_note_IS_carried_within_the_session(self):
        """⚠ The earlier field bug was the opposite reading: a team concluded bare notes
        did not survive compaction and re-recorded everything by hand. Both facts have to
        be in one sentence -- carried here, invisible elsewhere."""
        why = ss.check_promotion({"text": "a ruling"})["why"]
        assert "handed back every window of this session" in why

    def test_the_nudge_names_both_ways_out(self):
        text = ss.NUDGE_TEXT.format(edits=7, command=ss.note_command()["command"])
        assert "THIS SESSION ONLY" in text
        assert "--promoted-to" in text
        assert "team board" in text

    def test_the_nudge_stays_within_a_reasonable_size(self):
        """It fires on a measured condition and at most once every 25 turns, but it is still
        tokens in someone's context. 181 measured; the bound is a regression guard, not a
        target."""
        from attnroute.session_state import estimate_tokens

        text = ss.NUDGE_TEXT.format(edits=7, command=ss.note_command()["command"])
        assert estimate_tokens(text) < 260, estimate_tokens(text)


class TestResumeAndClearGetAHandback:

    def _with_a_ruling(self):
        state = ss.empty_state("s")
        ss.add_note(state, "depth gate refuses UNKNOWN legs", kind="ruling")
        ss.save("s", state)

    @pytest.mark.parametrize("source", ["compact", "resume", "clear"])
    def test_a_continuing_session_is_handed_its_rulings(self, source, repo):
        """⚠ `resume` and `clear` keep the session id and lose the context, which is every
        reason a compaction needs a handback."""
        self._with_a_ruling()
        out = ss.hook({"hook_event_name": "SessionStart", "source": source,
                       "session_id": "s", "cwd": str(repo)}, repo=repo)
        assert out, f"source={source} handed back nothing"
        assert "depth gate" in out["hookSpecificOutput"]["additionalContext"]

    @pytest.mark.parametrize("source", ["startup", "fork"])
    def test_a_new_identity_is_not_handed_someone_elses_rulings(self, source, repo):
        """The other half, and it is deliberate. `startup` has an empty state file of its
        own, and a `fork` gets a new id -- reaching into the parent's notes is a question
        about whose rulings those are, to be decided on purpose rather than by being in a
        tuple."""
        self._with_a_ruling()
        assert ss.hook({"hook_event_name": "SessionStart", "source": source,
                        "session_id": "s", "cwd": str(repo)}, repo=repo) is None

    def test_the_sources_are_listed_rather_than_inferred(self):
        assert ss.HANDBACK_SOURCES == ("compact", "resume", "clear")

    def test_a_resumed_session_with_nothing_recorded_hands_back_no_rulings(self, repo):
        """Positive control: the handback fires on the source, but it has nothing to say
        when there is nothing recorded."""
        out = ss.hook({"hook_event_name": "SessionStart", "source": "resume",
                       "session_id": "empty", "cwd": str(repo)}, repo=repo)
        text = out["hookSpecificOutput"]["additionalContext"] if out else ""
        assert "EXPLICIT -- decided by this session:" not in text


def test_note_add_points_at_both_ways_out(tmp_path):
    """The CLI is where an operator sees the state of the note they just recorded."""
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(tmp_path / "home")
    env["USERPROFILE"] = env["HOME"]
    env["CLAUDE_CODE_SESSION_ID"] = "cli"
    Path(env["HOME"]).mkdir(exist_ok=True)

    done = subprocess.run([sys.executable, "-m", "attnroute.cli", "note", "add",
                           "a ruling that should outlive the session", "--kind", "ruling"],
                          cwd=str(project), capture_output=True, text=True, timeout=300,
                          env=env)

    assert done.returncode == 0, done.stderr
    assert "THIS SESSION ONLY" in done.stderr
    assert "--promoted-to" in done.stderr
    assert "board set" in done.stderr
