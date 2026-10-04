"""Correcting and deleting notes, which I needed within a minute of using the tool myself.

I mistyped `--repo` on two notes, they were permanently CLAIMED, and the only remedy was to
record a corrected duplicate -- so the state file carried three versions of one ruling and
the handback budget paid for all three.

The design question is what happens to the original. `amend` SUPERSEDES: the original stays
with a pointer to its replacement, because a state file that silently rewrites what a session
decided is one nobody can audit -- the record would read as though the ruling had always said
this. `rm` is a real delete, because a note recorded by mistake is noise, and keeping noise
for the sake of history just spends the budget on it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from attnroute import session_state as ss

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake))
    assert str(ss.state_path("probe")).startswith(str(fake)), "HOME redirect failed"
    return fake


@pytest.fixture
def filled():
    state = ss.empty_state("s")
    a = ss.add_note(state, "depth gate refuses unknwon legs", kind="ruling")
    b = ss.add_note(state, "a duplicate worth deleting", kind="note")
    return state, a, b


class TestFindingANote:
    """The ids carry a unix timestamp -- `n4-1791113536` is not something anyone retypes."""

    def test_an_exact_id_is_found(self, filled):
        state, a, _ = filled
        assert ss.find_note(state, a["id"])["note"] is a

    def test_an_unambiguous_prefix_is_found(self, filled):
        state, a, _ = filled
        assert ss.find_note(state, a["id"][:3])["note"] is a

    def test_an_ambiguous_prefix_is_REFUSED_not_resolved(self, filled):
        """⚠ Deleting or rewriting the wrong ruling is worse than being asked for another
        character. "n" matches both notes here."""
        state, _, _ = filled
        found = ss.find_note(state, "n")
        assert found["note"] is None
        assert "matches 2 notes" in found["why"]

    def test_an_unknown_id_says_so(self, filled):
        state, _, _ = filled
        assert "no note with id" in ss.find_note(state, "nope")["why"]

    def test_an_empty_id_is_refused(self, filled):
        state, _, _ = filled
        assert ss.find_note(state, "  ")["note"] is None


class TestRemoveIsARealDelete:

    def test_the_note_is_gone(self, filled):
        state, a, b = filled
        assert ss.remove_note(state, b["id"])["removed"] is b
        assert [n["id"] for n in state["notes"]] == [a["id"]]

    def test_a_prefix_works(self, filled):
        state, _, b = filled
        assert ss.remove_note(state, b["id"][:3])["removed"] is b

    def test_an_unknown_id_removes_nothing(self, filled):
        state, _, _ = filled
        before = len(state["notes"])
        done = ss.remove_note(state, "nope")
        assert done["removed"] is None and done["why"]
        assert len(state["notes"]) == before

    def test_the_deleted_id_leaves_handed_back(self, filled):
        """Otherwise a deleted note's id sits in the file for the rest of the session."""
        state, a, b = filled
        ss.mark_handed_back(state, [a["id"], b["id"]])
        ss.remove_note(state, b["id"])
        assert state["handed_back"] == [a["id"]]

    def test_a_deleted_note_stops_being_handed_back(self, filled):
        state, _, b = filled
        ss.remove_note(state, b["id"])
        assert "duplicate worth deleting" not in ss.handback(state)["text"]


class TestAmendSupersedes:

    def test_the_original_is_kept_and_points_at_its_replacement(self, filled):
        state, a, _ = filled
        done = ss.amend_note(state, a["id"], "depth gate refuses unknown legs")
        assert done["note"]["supersedes"] == a["id"]
        assert a["superseded_by"] == done["note"]["id"]
        assert any(n["id"] == a["id"] for n in state["notes"]), "the original was dropped"

    def test_only_the_replacement_is_handed_back(self, filled):
        state, a, _ = filled
        ss.amend_note(state, a["id"], "depth gate refuses unknown legs")
        text = ss.handback(state)["text"]
        rulings = [line for line in text.splitlines() if line.startswith("- RULING")]
        assert len(rulings) == 1, rulings
        assert "unknown legs" in rulings[0]
        assert "unknwon" not in text, "the superseded draft is still being carried"

    def test_the_kind_and_the_promotion_are_inherited(self, filled, tmp_path):
        state, a, _ = filled
        a["promoted_to"] = "ADR.md"
        done = ss.amend_note(state, a["id"], "corrected")
        assert done["note"]["kind"] == "ruling"
        assert done["note"]["promoted_to"] == "ADR.md"

    def test_both_can_be_overridden(self, filled):
        state, a, _ = filled
        done = ss.amend_note(state, a["id"], "corrected", kind="decision",
                             promoted_to="docs/x.md")
        assert done["note"]["kind"] == "decision"
        assert done["note"]["promoted_to"] == "docs/x.md"

    def test_amending_an_already_superseded_note_is_refused_with_a_pointer(self, filled):
        """Otherwise a chain of amendments forks, and which one is current depends on
        whichever the operator happened to remember."""
        state, a, _ = filled
        first = ss.amend_note(state, a["id"], "second version")
        again = ss.amend_note(state, a["id"], "third version")
        assert again["note"] is None
        assert first["note"]["id"] in again["why"]
        assert "amend that one instead" in again["why"]

    def test_the_replacement_can_itself_be_amended(self, filled):
        """Positive control for the test above: a chain is allowed, through its tip."""
        state, a, _ = filled
        second = ss.amend_note(state, a["id"], "second version")["note"]
        third = ss.amend_note(state, second["id"], "third version")
        assert third["note"]["supersedes"] == second["id"]
        assert "third version" in ss.handback(state)["text"]
        assert "second version" not in ss.handback(state)["text"]

    def test_an_unknown_id_amends_nothing(self, filled):
        state, _, _ = filled
        before = len(state["notes"])
        assert ss.amend_note(state, "nope", "x")["note"] is None
        assert len(state["notes"]) == before


class TestTheCommands:
    """Driven as a subprocess: the exit code and the discoverability of the id are the
    contract here."""

    def run(self, home, project, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["CLAUDE_CODE_SESSION_ID"] = "cli-demo"
        return subprocess.run([sys.executable, "-m", "attnroute.cli", "note", *args]
                              if args and args[0] in ("add", "rm", "amend")
                              else [sys.executable, "-m", "attnroute.cli", *args],
                              cwd=str(project), text=True, capture_output=True,
                              env=env, timeout=300)

    @pytest.fixture
    def project(self, tmp_path):
        d = tmp_path / "project"
        d.mkdir()
        return d

    def test_state_show_prints_the_id(self, home, project):
        """⚠ IT DID NOT, AND THAT MADE BOTH NEW COMMANDS UNUSABLE. The listing showed
        everything about a note except the one field rm and amend need."""
        self.run(home, project, "add", "a ruling", "--kind", "ruling")
        shown = self.run(home, project, "state", "show")
        assert shown.returncode == 0, shown.stderr
        ids = [w for line in shown.stdout.splitlines() for w in line.split()[:1]
               if w.startswith("n") and "-" in w]
        assert ids, f"no note id in the listing:\n{shown.stdout}"

    def test_rm_deletes_and_exits_zero(self, home, project):
        self.run(home, project, "add", "a duplicate", "--kind", "note")
        shown = self.run(home, project, "state", "show")
        note_id = shown.stdout.split()[0]
        done = self.run(home, project, "rm", note_id)
        assert done.returncode == 0, done.stderr
        assert "deleted" in done.stderr
        assert "no notes recorded" in self.run(home, project, "state", "show").stderr

    def test_rm_of_an_unknown_id_exits_one(self, home, project):
        self.run(home, project, "add", "a ruling", "--kind", "ruling")
        done = self.run(home, project, "rm", "nope")
        assert done.returncode == 1
        assert "no note with id" in done.stderr

    def test_amend_supersedes_through_the_command(self, home, project):
        self.run(home, project, "add", "origianl wording", "--kind", "ruling")
        note_id = self.run(home, project, "state", "show").stdout.split()[0]
        done = self.run(home, project, "amend", note_id, "original wording")
        assert done.returncode == 0, done.stderr
        assert "supersedes" in done.stderr

        handback = self.run(home, project, "state", "handback")
        assert "original wording" in handback.stdout
        assert "origianl" not in handback.stdout
        # and the history is still listed
        assert "SUPERSEDED" in self.run(home, project, "state", "show").stdout

    def test_amend_without_the_new_text_is_refused(self, home, project):
        self.run(home, project, "add", "a ruling", "--kind", "ruling")
        note_id = self.run(home, project, "state", "show").stdout.split()[0]
        done = self.run(home, project, "amend", note_id)
        assert done.returncode == 1
        assert "corrected text" in done.stderr

    def test_rm_without_an_id_is_refused_and_says_where_to_find_one(self, home, project):
        done = self.run(home, project, "rm")
        assert done.returncode == 1
        assert "need a note id" in done.stderr
        assert "state show" in done.stderr, "say where the ids are, not just that one is needed"


def test_the_state_file_survives_a_round_trip_with_both(filled):
    state, a, b = filled
    ss.amend_note(state, a["id"], "corrected")
    ss.remove_note(state, b["id"])
    ss.save("s", state)
    back = ss.load("s")
    assert len(back["notes"]) == 2, "the original and its replacement"
    assert json.dumps(back)          # must stay serialisable
    assert "corrected" in ss.handback(back)["text"]
