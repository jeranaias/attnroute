"""The per-session state file and the handback that makes frequent compaction safe.

The saving here is not in compacting -- it is in compaction stopping being lossy. So what
these tests are really about is what survives a window boundary and what is allowed to be
wrong about it.

Two of them are the ones I would keep if I could keep only two:

  * a LIVE note is handed back EVERY window, not only the first. "Deltas only" is wrong for
    anything un-promoted, because the handback itself is part of the window that is about to
    be summarised away.
  * nothing is inferred from prose. An assistant paragraph saying "I've decided to use the
    achieved radius" produces NO note, because a guess handed to the next window as a ruling
    is worse than no handback at all.
"""

import json
import os
from pathlib import Path

import pytest

from attnroute import session_state as ss


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Redirect HOME, and refuse to run if the redirect did not take -- a test that writes
    to the real ~/.claude would be worse than a test that fails."""
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


def transcript(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return path


def assistant(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


_CALL = [0]


def tool_use(name, data):
    """Each call gets its OWN id, as the real transcript does -- the facts layer counts by
    id, so reusing one would quietly test the wrong thing."""
    _CALL[0] += 1
    return {"type": "tool_use", "id": f"t{_CALL[0]}", "name": name, "input": data}


def tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


class TestWhatSurvivesAWindowBoundary:

    def test_a_live_note_is_handed_back_every_window(self, repo):
        """⚠ THE CORRECTION THAT MATTERS. A note handed into window 2 is gone again by
        window 3: the handback was part of window 2's context, and window 3 inherits a
        summary of it. So anything un-promoted is repeated, every time."""
        state = ss.empty_state("s")
        note = ss.add_note(state, "depth gate refuses UNKNOWN legs", kind="ruling")

        first = ss.handback(state, repo=repo)
        ss.mark_handed_back(state, first["included"])
        second = ss.handback(state, repo=repo)

        assert note["id"] in first["included"]
        assert note["id"] in second["included"], "an un-promoted ruling was dropped"
        assert "depth gate" in second["text"]

    def test_a_promoted_note_drops_out_once_it_has_been_handed_back(self, repo):
        """Positive control for the test above: if nothing ever dropped out, "repeat the
        live ones" would be indistinguishable from "repeat everything"."""
        (repo / "ADR.md").write_text(
            "The depth gate refuses UNKNOWN legs and is ON by default.\n", encoding="utf-8")
        state = ss.empty_state("s")
        note = ss.add_note(state, "depth gate refuses UNKNOWN legs", kind="ruling",
                           promoted_to="ADR.md")

        first = ss.handback(state, repo=repo)
        ss.mark_handed_back(state, first["included"])
        second = ss.handback(state, repo=repo)

        assert note["id"] in first["included"]
        assert note["id"] not in second["included"]

    def test_a_note_too_old_to_matter_stops_being_carried(self, repo):
        state = ss.empty_state("s")
        ss.add_note(state, "last week's plan", kind="ruling")
        state["notes"][0]["at"] = "2026-01-01T00:00:00+00:00"
        assert ss.handback(state, repo=repo)["included"] == []


class TestTheBudgetIsEnforcedAndTheDropIsStated:

    def test_the_handback_never_exceeds_its_budget(self, repo):
        state = ss.empty_state("s")
        for i in range(400):
            ss.add_note(state, f"ruling number {i} about the depth gate and the run-in "
                               f"length and several other things besides", kind="ruling")
        built = ss.handback(state, repo=repo)
        assert built["tokens"] <= ss.TOKEN_BUDGET, built["tokens"]

    def test_what_did_not_fit_is_counted_in_the_text(self, repo):
        """A handback that truncates silently is how a window loses the one ruling it
        needed while believing it has everything."""
        state = ss.empty_state("s")
        for i in range(400):
            ss.add_note(state, f"ruling number {i} " + "x" * 60, kind="ruling")
        built = ss.handback(state, repo=repo)
        assert built["dropped"] > 0
        assert "did not fit" in built["text"]
        assert str(built["dropped"]) in built["text"]

    def test_rulings_outrank_measurements_when_the_budget_binds(self, repo):
        state = ss.empty_state("s")
        for i in range(200):
            ss.add_note(state, f"measurement {i} " + "m" * 60, kind="measurement")
        ss.add_note(state, "THE RULING that must survive", kind="ruling")
        built = ss.handback(state, repo=repo)
        assert "THE RULING that must survive" in built["text"]
        assert built["dropped"] > 0

    def test_a_small_handback_drops_nothing(self, repo):
        state = ss.empty_state("s")
        ss.add_note(state, "one ruling", kind="ruling")
        built = ss.handback(state, repo=repo)
        assert built["dropped"] == 0
        assert built["tokens"] < ss.TOKEN_BUDGET


class TestPromotionHasThreeStatesNotTwo:
    """CLAIMED is the dangerous one: it looks like PROMOTED to anything that only checks
    whether the field is set."""

    def test_no_claim_is_UNPROMOTED(self):
        assert ss.check_promotion({"text": "a ruling"})["state"] == "UNPROMOTED"

    def test_a_claim_to_a_missing_file_is_CLAIMED(self, repo):
        note = {"text": "depth gate ruling", "promoted_to": "docs/nope.md"}
        answer = ss.check_promotion(note, repo)
        assert answer["state"] == "CLAIMED"
        assert "does not exist" in answer["why"]

    def test_a_claim_to_a_file_that_does_not_mention_it_is_CLAIMED(self, repo):
        (repo / "ADR.md").write_text("Something else entirely.\n", encoding="utf-8")
        note = {"text": "depth gate refuses unknown legs", "promoted_to": "ADR.md"}
        answer = ss.check_promotion(note, repo)
        assert answer["state"] == "CLAIMED"
        assert "mentions only" in answer["why"]

    def test_a_claim_the_file_supports_is_PROMOTED(self, repo):
        (repo / "ADR.md").write_text(
            "The depth gate refuses unknown legs outright.\n", encoding="utf-8")
        note = {"text": "depth gate refuses unknown legs", "promoted_to": "ADR.md"}
        assert ss.check_promotion(note, repo)["state"] == "PROMOTED"

    def test_an_unsupported_claim_is_flagged_in_the_handback(self, repo):
        (repo / "ADR.md").write_text("Nothing to do with it.\n", encoding="utf-8")
        state = ss.empty_state("s")
        ss.add_note(state, "depth gate refuses unknown legs", kind="ruling",
                    promoted_to="ADR.md")
        text = ss.handback(state, repo=repo)["text"]
        assert "[claimed:" in text, "an unsupported claim must not read as written down"


class TestTheDerivedLayerOnlyCarriesWhatIsWrittenDown:

    def test_edits_are_counted(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [
            assistant([tool_use("Edit", {"file_path": "a.py"})]),
            assistant([tool_use("Edit", {"file_path": "a.py"})]),
            assistant([tool_use("Write", {"file_path": "b.py"})]),
        ])
        state = ss.empty_state("s")
        facts = ss.derive_facts(state, path)
        assert facts["edited"] == {"a.py": 2, "b.py": 1}

    def test_a_second_pass_over_the_same_span_does_not_double_count(self, tmp_path):
        """⚠ Caught reporting 4 edits for 2. Spans overlap -- a rewound offset, a rotated
        transcript, two hooks racing -- and an accumulating counter turns that into work
        that never happened."""
        path = transcript(tmp_path / "t.jsonl", [
            assistant([tool_use("Edit", {"file_path": "a.py"})]),
            assistant([tool_use("Edit", {"file_path": "a.py"})]),
        ])
        state = ss.empty_state("s")
        ss.derive_facts(state, path)
        state["transcript_offset"] = 0        # as if the span were replayed
        facts = ss.derive_facts(state, path)
        assert facts["edited"] == {"a.py": 2}

    def test_the_last_test_and_lint_results_are_captured(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [
            tool_result("565 passed, 1 warning in 65.90s"),
            tool_result("All checks passed!"),
        ])
        state = ss.empty_state("s")
        state["transcript_offset"] = 0
        facts = ss.derive_facts(state, path)
        assert "565 passed" in facts["last_test"]
        assert facts["last_lint"] == "All checks passed!"

    def test_commits_and_pushes_are_captured_and_plain_commands_are_not(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [
            assistant([tool_use("Bash", {"command": "git commit -m 'fix the gate'"})]),
            assistant([tool_use("Bash", {"command": "ls -la"})]),
            assistant([tool_use("Bash", {"command": "git push origin main"})]),
        ])
        state = ss.empty_state("s")
        state["transcript_offset"] = 0
        facts = ss.derive_facts(state, path)
        assert len(facts["commands"]) == 2, facts["commands"]
        assert not any("ls -la" in c for c in facts["commands"])

    def test_prose_about_a_decision_produces_NO_note(self, tmp_path, repo):
        """⚠ THE LINE THIS MODULE WILL NOT CROSS. The transcript is full of sentences that
        look like rulings. Harvesting them would hand the next window a guess wearing the
        clothes of a decision."""
        path = transcript(tmp_path / "t.jsonl", [
            assistant([{"type": "text", "text":
                        "I have decided: the depth gate must refuse UNKNOWN legs, and I am "
                        "ruling that the turn radius figure is withdrawn."}]),
        ])
        state = ss.empty_state("s")
        state["transcript_offset"] = 0
        ss.derive_facts(state, path)
        assert state["notes"] == []
        assert "depth gate" not in ss.handback(state, repo=repo)["text"]

    def test_an_incomplete_view_is_declared_in_the_handback(self, tmp_path, repo):
        """A derived fact built from part of the window must say so, or the next window
        treats a partial list of edits as the whole of the work."""
        path = transcript(tmp_path / "t.jsonl",
                          [assistant([tool_use("Edit", {"file_path": "a.py"})])])
        state = ss.empty_state("s")
        ss.derive_facts(state, path)          # no prior offset -> incomplete by definition
        assert state["facts"]["view_complete"] is False
        state["facts"]["edited"] = {"a.py": 1}
        assert "INCOMPLETE" in ss.handback(state, repo=repo)["text"]

    def test_a_missing_transcript_is_not_an_error(self, tmp_path):
        state = ss.empty_state("s")
        assert ss.derive_facts(state, tmp_path / "nope.jsonl") is not None


class TestTheNudgeFiresOnlyWhenOneIsOwed:

    def _worked(self, edits=10):
        state = ss.empty_state("s")
        state["facts"] = {"edited": {f"f{i}.py": 1 for i in range(edits)}, "commands": []}
        state["turns_since_nudge"] = ss.NUDGE_EVERY_TURNS
        return state

    def test_it_is_due_after_real_work_with_nothing_recorded(self):
        assert ss.nudge_due(self._worked())["due"] is True

    def test_it_is_not_due_once_a_note_exists_in_this_window(self):
        state = self._worked()
        ss.add_note(state, "the ruling", kind="ruling")
        answer = ss.nudge_due(state)
        assert answer["due"] is False
        assert "already been written" in answer["why"]

    def test_it_is_not_due_before_any_real_work(self):
        answer = ss.nudge_due(self._worked(edits=1))
        assert answer["due"] is False
        assert "edit(s)" in answer["why"]

    def test_it_is_rate_limited(self):
        state = self._worked()
        state["turns_since_nudge"] = 1
        assert ss.nudge_due(state)["due"] is False

    def test_a_commit_counts_as_real_work_on_its_own(self):
        state = ss.empty_state("s")
        state["facts"] = {"edited": {}, "commands": ["git commit -m x"]}
        state["turns_since_nudge"] = ss.NUDGE_EVERY_TURNS
        assert ss.nudge_due(state)["due"] is True


class TestTheHookWiring:

    def test_SessionStart_compact_hands_back_as_additionalContext(self, repo, tmp_path):
        state = ss.empty_state("s")
        ss.add_note(state, "depth gate refuses UNKNOWN legs", kind="ruling")
        ss.save("s", state)

        out = ss.hook({"hook_event_name": "SessionStart", "source": "compact",
                       "session_id": "s", "cwd": str(repo)}, repo=repo)

        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "depth gate" in out["hookSpecificOutput"]["additionalContext"]

    def test_SessionStart_for_any_other_reason_hands_back_nothing(self, repo):
        state = ss.empty_state("s")
        ss.add_note(state, "a ruling", kind="ruling")
        ss.save("s", state)
        assert ss.hook({"hook_event_name": "SessionStart", "source": "startup",
                        "session_id": "s"}, repo=repo) is None

    def test_PreCompact_closes_the_window(self, repo, tmp_path):
        path = transcript(tmp_path / "t.jsonl",
                          [assistant([tool_use("Edit", {"file_path": "a.py"})])])
        ss.hook({"hook_event_name": "PreCompact", "session_id": "s",
                 "transcript_path": str(path)}, repo=repo)
        assert ss.load("s")["window"] == 1

    def test_Stop_derives_the_facts(self, repo, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [
            assistant([tool_use("Edit", {"file_path": "a.py"})]),
            tool_result("565 passed in 60s"),
        ])
        ss.hook({"hook_event_name": "Stop", "session_id": "s",
                 "transcript_path": str(path)}, repo=repo)
        facts = ss.load("s")["facts"]
        assert facts.get("derived_at")

    def test_a_subagent_keeps_its_own_state(self, repo):
        state = ss.empty_state("s")
        ss.add_note(state, "the main loop's ruling", kind="ruling")
        ss.save("s", state)

        out = ss.hook({"hook_event_name": "SessionStart", "source": "compact",
                       "session_id": "s", "agent_id": "sub-7", "cwd": str(repo)},
                      repo=repo)

        assert "the main loop's ruling" not in out["hookSpecificOutput"]["additionalContext"]

    def test_an_unknown_event_does_nothing(self, repo):
        assert ss.hook({"hook_event_name": "Nonsense", "session_id": "s"}, repo=repo) is None

    def test_main_exits_zero_on_rubbish_stdin(self, monkeypatch):
        import io as _io
        monkeypatch.setattr("sys.stdin", _io.StringIO("not json"))
        assert ss.main() == 0

    def test_the_handback_does_not_reach_the_network(self, repo, monkeypatch):
        """The board read is local-ref only, which is what lets this run in a hook."""
        import socket
        from attnroute import no_egress
        no_egress.lock_down()
        try:
            state = ss.empty_state("s")
            ss.add_note(state, "a ruling", kind="ruling")
            ss.save("s", state)
            ss.hook({"hook_event_name": "SessionStart", "source": "compact",
                     "session_id": "s", "cwd": str(repo)}, repo=repo)
            assert no_egress.refused() == [], no_egress.refused()
        finally:
            no_egress.release()
            no_egress.REFUSED.clear()
            assert socket.getaddrinfo is no_egress._ORIGINAL_GETADDRINFO


class TestPersistence:

    def test_a_round_trip_keeps_the_notes(self):
        state = ss.empty_state("s")
        ss.add_note(state, "a ruling", kind="ruling")
        ss.save("s", state)
        assert len(ss.load("s")["notes"]) == 1

    def test_a_state_file_from_another_version_is_discarded_not_guessed_at(self):
        state = ss.empty_state("s")
        ss.add_note(state, "a ruling", kind="ruling")
        state["version"] = 999
        ss.save("s", state)
        assert ss.load("s")["notes"] == []

    def test_an_absent_file_is_an_empty_state(self):
        assert ss.load("never-seen")["notes"] == []

    def test_nothing_is_written_into_the_working_tree(self, repo, tmp_path):
        state = ss.empty_state("s")
        ss.add_note(state, "a ruling", kind="ruling")
        ss.save("s", state)
        ss.hook({"hook_event_name": "SessionStart", "source": "compact",
                 "session_id": "s", "cwd": str(repo)}, repo=repo)
        assert sorted(os.listdir(repo)) == [], sorted(os.listdir(repo))

    def test_the_save_is_atomic(self):
        src = Path(ss.__file__).read_text(encoding="utf-8")
        assert "os.replace(tmp, path)" in src


class TestTheSessionIdComesFromTheRightVariable:
    """⚠ THE BUG THIS CLASS EXISTS FOR. The code read `CLAUDE_SESSION_ID`. In a Bash tool
    call under 2.1.280 that variable is UNSET; the id is in `CLAUDE_CODE_SESSION_ID`
    (verified by printing the environment inside the tool, not by reading the binary). So
    every note was filed under a session called "local", the handback read the real id, and
    the two never met. Nothing raised. The lever simply did nothing -- the worst way for a
    measurement tool to be broken.
    """

    def test_the_variable_the_CLI_actually_sets_is_preferred(self):
        found = ss.session_from_env({"CLAUDE_CODE_SESSION_ID": "real-id",
                                     "CLAUDE_SESSION_ID": "stale-id"})
        assert found["session"] == "real-id"
        assert found["var"] == "CLAUDE_CODE_SESSION_ID"

    def test_the_older_name_still_works_as_a_fallback(self):
        found = ss.session_from_env({"CLAUDE_SESSION_ID": "legacy-id"})
        assert found["session"] == "legacy-id"
        assert found["var"] == "CLAUDE_SESSION_ID"

    def test_an_absent_id_is_REFUSED_rather_than_defaulted(self):
        """A wrong default is invisible; a refusal is not. There is no "local" session."""
        found = ss.session_from_env({})
        assert found["session"] is None
        assert "CLAUDE_CODE_SESSION_ID" in found["why"]
        assert "--session" in found["why"]

    def test_a_blank_value_counts_as_absent(self):
        assert ss.session_from_env({"CLAUDE_CODE_SESSION_ID": "   "})["session"] is None

    def test_no_module_defaults_a_session_to_local(self):
        """Blunt, and deliberately so: the string that caused this is not to come back."""
        src = Path(ss.__file__).read_text(encoding="utf-8")
        from attnroute import cli
        src += Path(cli.__file__).read_text(encoding="utf-8")
        assert 'or "local"' not in src


class TestTelemetry:
    def test_positive_control_a_handback_writes_one_stream_record(self, repo, home):
        from attnroute import telemetry_stream
        state = ss.empty_state("s")
        ss.add_note(state, "depth gate refuses UNKNOWN legs", kind="ruling")
        ss.save("s", state)
        ss.hook({"hook_event_name": "SessionStart", "source": "compact",
                 "session_id": "s", "cwd": str(repo)}, repo=repo)
        recs = [r for r in telemetry_stream.read() if r["component"] == "session_state"]
        assert len(recs) == 1 and recs[0]["event"] == "handback"
        assert recs[0]["session_id"] == "s" and recs[0]["tokens"] > 0
        assert recs[0]["promotion"]["UNPROMOTED"] == 1

    def test_a_plain_startup_writes_nothing(self, repo, home):
        from attnroute import telemetry_stream
        ss.hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s"},
                repo=repo)
        assert not [r for r in telemetry_stream.read() if r["component"] == "session_state"]
