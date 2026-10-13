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


def usage_line(total, *, cached=0, created=0):
    """One assistant entry carrying a `usage` block, as the transcript writes it."""
    return {"type": "assistant", "message": {"role": "assistant", "content": [],
            "usage": {"input_tokens": total, "cache_read_input_tokens": cached,
                      "cache_creation_input_tokens": created, "output_tokens": 99}}}


def settings(root, **keys):
    import json as _json
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "settings.json").write_text(_json.dumps(keys), encoding="utf-8")
    return root


class TestTheApproachingBoundaryAlert:
    """⚠⚠ WHAT THIS IS FOR. Auto-compact lands wherever the token count reaches the window,
    which is as likely to be the middle of a half-applied refactor as a natural break. The
    handback is 1,500 tokens against a window of hundreds of thousands, so most of what the
    session knows goes either way -- but a human who knows the boundary is close can take it at a
    break, for free.

    The two tests worth keeping if only two could be kept:

      * with no `autoCompactWindow` configured and no compaction observed, it stays SILENT no
        matter how large the context is. "80% of what" is the whole question, and 80% of an
        invented window reads exactly like 80% of a measured one;
      * cache reads count toward the context. BurnRate shipped with them excluded and hid up to
        95% of real usage; the same slip here would silently move the threshold by the same
        factor, and every test about percentages would still pass.
    """

    def test_the_window_is_read_from_the_project_over_the_user(self, tmp_path, home):
        settings(home, autoCompactWindow=1000000)
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        found = ss.auto_compact_window(ss.empty_state("s"), project)
        assert found["tokens"] == 400000
        assert "400,000" in found["why"]

    def test_the_user_setting_is_used_when_the_project_says_nothing(self, tmp_path, home):
        settings(home, autoCompactWindow=250000)
        (tmp_path / "p").mkdir()
        found = ss.auto_compact_window(ss.empty_state("s"), tmp_path / "p")
        assert found["tokens"] == 250000

    @pytest.mark.parametrize("unusable", ["lots", 0, -1, None, ""])
    def test_an_unusable_setting_is_passed_over_rather_than_coerced(
            self, tmp_path, home, unusable):
        """⚠ ZERO IS THE CASE THAT NEEDS THE GUARD, and it is why this is parametrised. A
        window of "lots" is refused by anything that calls `int()` on it, so a test using only
        that string passes under every plausible mis-implementation. Zero is perfectly
        coercible -- and a window of 0 puts every context over the threshold, which is an alert
        on every turn of every session: the noise this package exists to remove, arriving in the
        name of preventing it."""
        settings(home, autoCompactWindow=400000)
        project = settings(tmp_path / "p", autoCompactWindow=unusable)
        assert ss.auto_compact_window(ss.empty_state("s"), project)["tokens"] == 400000

    def test_with_nothing_configured_it_falls_back_to_where_compaction_HAPPENED(
            self, tmp_path, home):
        """⚠ Observed, not assumed -- and the SMALLEST observation, because the alert's job is
        to arrive before the boundary and only the earliest one guarantees that."""
        (tmp_path / "p").mkdir()
        state = ss.empty_state("s")
        state["compacted_at"] = [402000, 371000, 396000]
        found = ss.auto_compact_window(state, tmp_path / "p")
        assert found["tokens"] == 371000
        assert found["source"] == "observed"

    def test_with_no_denominator_it_says_so_and_names_the_setting(self, tmp_path, home):
        (tmp_path / "p").mkdir()
        found = ss.auto_compact_window(ss.empty_state("s"), tmp_path / "p")
        assert found["tokens"] is None
        assert "autoCompactWindow" in found["why"]

    def test_it_stays_silent_with_no_denominator_however_large_the_context(
            self, tmp_path, home):
        """⚠⚠ THE REFUSAL THIS MODULE IS BUILT AROUND. A million tokens of context and no
        configured window produces NO alert, because "80% of the window" with no window is a
        percentage of nothing -- and it would print to the same decimal place as a real one.
        Falling back to a plausible default is the version of this that looks like it works."""
        (tmp_path / "p").mkdir()
        path = transcript(tmp_path / "t.jsonl", [usage_line(1_000_000)])
        out = ss.compact_alert(ss.empty_state("s"), path, tmp_path / "p")
        assert out["due"] is False
        assert "no denominator" in out["why"] or "autoCompactWindow" in out["why"]

    # ---- reading the context size ---------------------------------------------------------
    def test_cache_reads_count_toward_the_context(self, tmp_path):
        """⚠⚠ THEY ARE CONTEXT. A request carrying 300,000 cached tokens is 300,000 tokens
        into the window. BurnRate shipped with cache reads excluded and hid up to 95% of real
        usage (CHANGELOG 1.0.2); here the same slip would move the threshold by the same factor
        with every percentage test still passing."""
        path = transcript(tmp_path / "t.jsonl",
                          [usage_line(1_000, cached=300_000, created=5_000)])
        assert ss.context_tokens(path)["tokens"] == 306_000

    def test_the_most_recent_usage_block_is_the_one_used(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl",
                          [usage_line(10_000), usage_line(90_000)])
        assert ss.context_tokens(path)["tokens"] == 90_000

    def test_no_usage_block_reports_why_rather_than_zero(self, tmp_path):
        """A zero would be indistinguishable from an empty context, and an empty context is the
        safest-looking reading there is."""
        path = transcript(tmp_path / "t.jsonl", [{"type": "user", "message": {}}])
        found = ss.context_tokens(path)
        assert found["tokens"] is None
        assert "no usage block" in found["why"]

    def test_an_absent_transcript_reports_why_rather_than_zero(self, tmp_path):
        found = ss.context_tokens(tmp_path / "gone.jsonl")
        assert found["tokens"] is None
        assert "unreadable" in found["why"]

    def _padded(self, path, head, tail):
        """A transcript bigger than the tail: `head` usage at the front, `tail` at the back."""
        with open(path, "w", encoding="utf-8") as fh:
            if head is not None:
                fh.write(json.dumps(usage_line(head)) + "\n")
            pad = {"type": "user", "message": {"role": "user", "content": "x" * 4000}}
            for _ in range(200):                 # ~800 KB, over the 256 KB tail
                fh.write(json.dumps(pad) + "\n")
            if tail is not None:
                fh.write(json.dumps(usage_line(tail)) + "\n")
        assert path.stat().st_size > ss.USAGE_TAIL_BYTES
        return path

    def test_the_last_usage_block_is_found_in_a_file_bigger_than_the_tail(self, tmp_path):
        path = self._padded(tmp_path / "t.jsonl", head=123, tail=77_000)
        assert ss.context_tokens(path)["tokens"] == 77_000

    def test_the_read_is_bounded_in_bytes(self, tmp_path, monkeypatch):
        """⚠⚠ MEASURED AS A RESOURCE PROPERTY, BECAUSE THE ANSWER CANNOT SHOW IT. Unbounding
        the read -- seeking to 0 instead of to the tail -- returns the SAME number from the same
        files, so every behavioural test stayed green through it. It was found by mutation and it
        is pinned here by watching the read itself.

        That is not a stylistic preference. This runs on a hook, and the Stop path once
        re-parsed 300 MB from byte zero and cost 15 s; `tests/test_hook_latency_budget.py` gates
        the same class of mistake as a proportion for the same reason. A timing assertion would
        be a coin flip on a loaded runner. "It read no more than the tail" is the same answer on
        every machine.
        """
        import builtins
        path = self._padded(tmp_path / "t.jsonl", head=55_000, tail=77_000)
        real_open = builtins.open
        sizes = []

        class Probe:
            """Just enough file to serve `context_tokens`, counting what it hands over."""

            def __init__(self, fh):
                self.fh = fh

            def seek(self, *a):
                return self.fh.seek(*a)

            def read(self, *a):
                data = self.fh.read(*a)
                sizes.append(len(data))
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fh.close()

        def counting_open(file, mode="r", *a, **k):
            fh = real_open(file, mode, *a, **k)
            return Probe(fh) if "b" in mode and str(file) == str(path) else fh

        monkeypatch.setattr(builtins, "open", counting_open)
        found = ss.context_tokens(path)
        monkeypatch.undo()

        assert found["tokens"] == 77_000, "and it still answers correctly"
        assert sizes, "the probe saw nothing, so this test proves nothing"
        assert max(sizes) <= ss.USAGE_TAIL_BYTES, (
            f"read {max(sizes):,} bytes of a {path.stat().st_size:,}-byte transcript")

    def test_a_usage_block_older_than_the_tail_is_not_found(self, tmp_path):
        """⚠⚠ THE LIMITATION, ASSERTED RATHER THAN IMPLIED -- and the only observable
        difference between a tail read and a whole-file read. The first version of this test
        checked that the LAST block is found, which a `seek(0)` does equally well: it answered a
        narrower question than its own name asked, and the mutation that unbounded the read left
        it green. The read is bounded because this runs in a hook -- the Stop path once re-parsed
        300 MB from byte zero and cost 15 s -- and the price of that bound is this: a session
        whose last 256 KB contain no usage block is reported as unknown, not as empty."""
        path = self._padded(tmp_path / "t.jsonl", head=55_000, tail=None)
        found = ss.context_tokens(path)
        assert found["tokens"] is None
        assert "no usage block" in found["why"]

    # ---- when it speaks -------------------------------------------------------------------
    def test_it_speaks_at_the_threshold_and_not_below_it(self, tmp_path, home):
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        under = transcript(tmp_path / "under.jsonl", [usage_line(319_000)])
        over = transcript(tmp_path / "over.jsonl", [usage_line(321_000)])
        assert ss.compact_alert(ss.empty_state("s"), under, project)["due"] is False
        assert ss.compact_alert(ss.empty_state("s"), over, project)["due"] is True

    def test_the_threshold_leaves_room_for_the_turn_that_crosses_it(self):
        """⚠ Not 0.95. A hook reads the context the LAST request carried, so the turn that
        actually crosses the window is the turn that never gets a warning. The margin is that
        one turn of slack, and it is the reason for the number."""
        assert ss.ALERT_AT <= 0.85

    def test_it_names_the_numbers_and_where_the_limit_came_from(self, tmp_path, home):
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        path = transcript(tmp_path / "t.jsonl", [usage_line(340_000)])
        out = ss.compact_alert(ss.empty_state("s"), path, project)
        assert "340,000" in out["text"] and "400,000" in out["text"]
        assert "settings.json" in out["text"], "a percentage without its denominator's source"

    def test_it_asks_for_a_sentence_and_not_for_an_action(self, tmp_path, home):
        """⚠ Whether a break is a good place to stop is the human's call. A hook that acted on
        its own judgement here would be taking it."""
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        path = transcript(tmp_path / "t.jsonl", [usage_line(340_000)])
        text = ss.compact_alert(ss.empty_state("s"), path, project)["text"]
        assert "Tell the user" in text
        assert "Do not compact anything yourself" in text

    def test_it_speaks_once_per_window(self, tmp_path, home):
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        path = transcript(tmp_path / "t.jsonl", [usage_line(340_000)])
        state = ss.empty_state("s")
        assert ss.compact_alert(state, path, project)["due"] is True
        state["alerted_window"] = state["window"]        # as the Stop handler records it
        again = ss.compact_alert(state, path, project)
        assert again["due"] is False
        state["window"] += 1                             # a compaction happened
        assert ss.compact_alert(state, path, project)["due"] is True

    # ---- the hook path --------------------------------------------------------------------
    def test_the_stop_hook_carries_the_alert(self, tmp_path, home):
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        path = transcript(tmp_path / "t.jsonl", [usage_line(340_000)])
        out = ss.hook({"hook_event_name": "Stop", "session_id": "s",
                       "transcript_path": str(path), "cwd": str(project)}, repo=project)
        assert "autoCompactWindow" in out["hookSpecificOutput"]["additionalContext"]

    def test_the_alert_outranks_the_note_nudge_but_does_not_cancel_it(self, tmp_path, home):
        """⚠⚠ `additionalContext` IS ONE STRING AND ONLY ONE OF THEM CAN SPEAK. Two
        instructions in it compete, and the nudge would win on being easier to act on. A note
        that waits another 25 turns costs a note; a boundary landing mid-task costs the window.
        So the nudge is DEFERRED -- its turn counter keeps running -- and not skipped."""
        project = settings(tmp_path / "p", autoCompactWindow=400000)
        path = tmp_path / "t.jsonl"
        entries = [assistant([tool_use("Edit", {"file_path": f"f{i}.py"})])
                   for i in range(ss.NUDGE_AFTER_EDITS + 2)]
        transcript(path, [*entries, usage_line(340_000)])
        state = ss.empty_state("s")
        state["turns_since_nudge"] = ss.NUDGE_EVERY_TURNS + 1
        ss.save("s", state)

        out = ss.hook({"hook_event_name": "Stop", "session_id": "s",
                       "transcript_path": str(path), "cwd": str(project)}, repo=project)
        said = out["hookSpecificOutput"]["additionalContext"]
        assert "autoCompactWindow" in said, "the boundary warning is the one that speaks"
        assert "note add" not in said
        after = ss.load("s")
        assert after["turns_since_nudge"] > ss.NUDGE_EVERY_TURNS, "the nudge is still owed"

    def test_precompact_records_where_the_boundary_actually_was(self, tmp_path, home):
        path = transcript(tmp_path / "t.jsonl", [usage_line(398_000)])
        ss.hook({"hook_event_name": "PreCompact", "session_id": "s",
                 "transcript_path": str(path)}, repo=tmp_path)
        assert ss.load("s")["compacted_at"] == [398_000]

    def test_the_observed_boundaries_are_bounded(self, tmp_path, home):
        state = ss.empty_state("s")
        state["compacted_at"] = list(range(1, 30))
        ss.save("s", state)
        path = transcript(tmp_path / "t.jsonl", [usage_line(400_000)])
        ss.hook({"hook_event_name": "PreCompact", "session_id": "s",
                 "transcript_path": str(path)}, repo=tmp_path)
        assert len(ss.load("s")["compacted_at"]) == 8


class TestTheCompactionQualityLedger:
    """⚠⚠ THE HALF OF THE COST PROGRAMME THAT HAS NEVER BEEN COUNTED. Every lever in this
    package is justified by tokens saved "without quality loss", and the second clause has been
    asserted every time and measured none. These tests are about the one objective thing that can
    be counted -- a call issued after a compaction that this session had already issued before it
    -- and, as much as anything, about the properties that keep that count honest when somebody
    tidies the code later.

    The three worth keeping if only three could be kept:

      * signatures accumulate BEFORE the boundary, because they are what the window after it is
        compared against. Move that one line inside the measuring guard and the first row of every
        session reports a perfect score, for ever, with no test failing;
      * a window cut short is RECORDED, not discarded, because the sessions that die mid-window
        are the ones most likely to have gone badly;
      * `rising` refuses to speak until three windows exist, and compares a session to ITSELF.
        The fleet spread was 2.4 to 18.7 repeats per compaction, so a fleet comparison would
        report a seat as healthy for no better reason than which seat it was.
    """

    def _derive(self, state, path, blocks):
        """Append these tool-use blocks to the transcript and derive. Returns the state."""
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant(blocks)) + "\n")
        ss.derive_facts(state, path)
        return state

    def test_nothing_is_counted_before_the_first_compaction(self, tmp_path):
        """⚠ There is no "after a boundary" before a boundary. A session that has never
        compacted has no row, and a row with no window behind it would be a comparison against
        nothing."""
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        self._derive(state, path, [tool_use("Read", {"file_path": "a.py"}),
                                   tool_use("Read", {"file_path": "a.py"})])
        assert state["ledger"] == []
        assert state["ledger_of"] == 0

    def test_a_call_repeated_after_a_compaction_is_counted(self, repo, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        self._derive(state, path, [tool_use("Read", {"file_path": "nav/heading.py"})])
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Read", {"file_path": "nav/heading.py"}),
                                   tool_use("Read", {"file_path": "nav/new.py"})])
        assert state["ledger_repeats"] == 1
        assert state["ledger_of"] == 2

    def test_signatures_from_before_the_boundary_are_what_make_a_repeat_visible(
            self, tmp_path):
        """⚠⚠ THE LINE A TIDY-UP WOULD MOVE, AND WHY IT MUST NOT. The signature list is
        appended to OUTSIDE the "are we measuring" guard. Move it inside -- which reads like an
        optimisation, since nothing else uses the list -- and the window after a compaction has
        nothing to compare itself against: every row reads zero repeats, the ledger reports
        quality improving, and no other test in this file fails. That is the reassuring failure
        this whole module exists to refuse, so it is pinned here on its own."""
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        # One call, in the window BEFORE the compaction. The measuring window is not open.
        self._derive(state, path, [tool_use("Grep", {"pattern": "FENCE_ACTION"})])
        assert state["ledger_of"] == 0, "nothing should be counted yet"
        assert state["facts"]["sigs"], "but the signature must have been remembered"
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Grep", {"pattern": "FENCE_ACTION"})])
        assert state["ledger_repeats"] == 1

    def test_a_call_first_made_after_the_boundary_is_not_a_repeat(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Read", {"file_path": "a.py"})])
        assert state["ledger_of"] == 1
        assert state["ledger_repeats"] == 0

    def test_the_row_closes_after_the_window_and_records_what_it_counted(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Read", {"file_path": f"f{i}.py"})
                                   for i in range(ss.LEDGER_AFTER + 5)])
        assert len(state["ledger"]) == 1
        row = state["ledger"][0]
        assert row["of"] == ss.LEDGER_AFTER, "the window has a fixed width, not a fixed turn"
        assert state["ledger_left"] == 0, "and it stops counting once it is full"

    def test_a_window_cut_short_is_recorded_rather_than_discarded(self, tmp_path):
        """⚠⚠ A SHORT ROW IS HONEST ABOUT BEING SHORT; A DROPPED ONE IS NOT THERE TO BE
        HONEST ABOUT ANYTHING. A session that compacts again -- or is killed -- five calls into a
        forty-call window is exactly the session whose window went badly, and discarding partial
        rows would quietly delete the evidence most worth having. So the row is written with its
        real denominator and the reader can weight it."""
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Read", {"file_path": f"a{i}.py"})
                                   for i in range(5)])
        ss.mark_handed_back(state, [])          # compacted again, early
        assert len(state["ledger"]) == 1
        assert state["ledger"][0]["of"] == 5

    def test_one_call_seen_in_two_overlapping_spans_is_counted_once(self, tmp_path):
        """A rewound offset, a rotated transcript or two hooks racing can show the same call
        twice. The id guard above this counter is what stops that becoming a repeat the session
        never made -- and a repeat invented by the reader is worse than one missed, because it
        reports rework that did not happen."""
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Read", {"file_path": "a.py"})])
        state["transcript_offset"] = 0          # the span is read again
        ss.derive_facts(state, path)
        assert state["ledger_of"] == 1
        assert state["ledger_repeats"] == 0

    def test_two_different_files_are_two_different_calls(self, tmp_path):
        a = ss.call_signature("Read", {"file_path": "a.py"})
        b = ss.call_signature("Read", {"file_path": "b.py"})
        assert a != b
        assert a == ss.call_signature("Read", {"file_path": "a.py"})

    def test_an_unlisted_tool_signs_on_its_name_alone_and_over_counts(self):
        """⚠ DELIBERATE, AND THE DIRECTION MATTERS. Two different WebSearch queries read as
        the same call, so the number is too high rather than too low. For a measure whose job is
        to reveal a RISE, over-counting costs a false alarm somebody investigates;
        under-counting costs the alarm itself."""
        assert (ss.call_signature("WebSearch", {"query": "one"})
                == ss.call_signature("WebSearch", {"query": "two"}))

    def test_the_committed_baseline_names_the_parameters_it_was_measured_with(self):
        """⚠⚠ THE CHECK THAT STOPS A STALE NUMBER BEING COMPARED TO A LIVE ONE. 8.4 repeats
        per compaction was measured over 40 calls with a signature memory of 400. Change either
        constant and the figure is no longer comparable to anything this ledger counts -- but the
        comparison would still print, to the same decimal place, with nothing to show it had
        stopped meaning something. Re-derive it with `benchmarks/compact_quality.py`."""
        assert ss.LEDGER_BASELINE_MEASURED_AT["after"] == ss.LEDGER_AFTER
        assert ss.LEDGER_BASELINE_MEASURED_AT["sigs"] == ss.LEDGER_SIGS

    def test_the_benchmark_uses_this_counter_rather_than_a_copy_of_it(self):
        """⚠⚠ AND THIS IS WHY THAT BASELINE IS 8.4. A standalone version of the benchmark
        measured 10.4 on the same five transcripts -- a 24% difference -- because it truncated a
        command signature at 120 characters where `call_signature` truncates at 200, so long
        commands sharing a prefix were counted as the same call. A re-implemented measure returns
        a number of the right magnitude that cannot be compared to anything the product does."""
        from benchmarks import compact_quality
        assert compact_quality.call_signature is ss.call_signature
        assert compact_quality.LEDGER_AFTER is ss.LEDGER_AFTER

    # ---- what the number is allowed to SAY ------------------------------------------------
    def _with_rows(self, rows):
        state = ss.empty_state("s")
        state["ledger"] = [{"window": i, "repeats": r, "of": n, "at": ""}
                           for i, (r, n) in enumerate(rows)]
        return state

    def test_rising_says_nothing_until_three_windows_exist(self):
        """One comparison on a measure this noisy is a coin flip with a verdict attached."""
        out = ss.ledger_summary(self._with_rows([(2, 40), (30, 40)]))
        assert out["rising"] is False
        assert "needed before a rise" in out["why"]

    def test_a_session_getting_worse_against_itself_is_what_rising_means(self):
        out = ss.ledger_summary(self._with_rows([(4, 40), (4, 40), (28, 40)]))
        assert out["rising"] is True
        assert out["rate"] == 0.7
        assert out["baseline"] == 0.1

    def test_it_compares_a_session_to_itself_and_not_to_the_fleet(self):
        """⚠⚠ THE SPREAD IS WHY. Per-session rates at a 1,000,000 window ran 2.4 to 18.7
        repeats per compaction -- nearly eightfold, between seats doing the same kind of work. A
        session sitting at twice its own best rate is degrading even while it is still well under
        the fleet average, and a fleet comparison would call it healthy. The fleet figure is
        returned too, under a name that says what it is."""
        # Well below the fleet baseline in absolute terms, and twice its own history.
        state = self._with_rows([(1, 40), (1, 40), (4, 40)])
        out = ss.ledger_summary(state)
        assert out["rate"] < out["fleet_baseline"], "a fleet check would pass this"
        assert out["rising"] is True, "and it is still getting worse"
        assert out["baseline"] == 0.025

    def test_an_empty_ledger_says_so_instead_of_returning_a_number(self):
        out = ss.ledger_summary(ss.empty_state("s"))
        assert out["rows"] == 0
        assert out["rate"] is None
        assert out["rising"] is False

    def test_a_partial_row_is_weighted_by_its_own_denominator(self):
        """Five calls with two repeats is 40%, not two-fortieths, and the earlier windows are
        weighted by how much they actually counted."""
        out = ss.ledger_summary(self._with_rows([(4, 40), (0, 10), (2, 5)]))
        assert out["rate"] == 0.4
        assert out["baseline"] == 4 / 50

    # ---- bounded, because every hook reads this file every turn ---------------------------
    def test_the_rows_are_bounded(self, tmp_path):
        state = self._with_rows([(1, 40)] * (ss.LEDGER_ROWS + 4))
        state["ledger_of"] = 1
        ss._close_ledger_row(state)
        assert len(state["ledger"]) == ss.LEDGER_ROWS

    def test_the_signature_memory_is_bounded(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        self._derive(state, path, [tool_use("Read", {"file_path": f"f{i}.py"})
                                   for i in range(ss.LEDGER_SIGS + 60)])
        assert len(state["facts"]["sigs"]) == ss.LEDGER_SIGS

    def test_a_repeated_call_is_moved_to_the_end_rather_than_duplicated(self, tmp_path):
        """The bound drops the LEAST recently used signature, so a call made throughout a long
        window must not age out of its own memory."""
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        self._derive(state, path, [tool_use("Read", {"file_path": "hot.py"}),
                                   tool_use("Read", {"file_path": "cold.py"}),
                                   tool_use("Read", {"file_path": "hot.py"})])
        sigs = state["facts"]["sigs"]
        assert len(sigs) == 2
        assert sigs[-1] == ss.call_signature("Read", {"file_path": "hot.py"})

    def test_the_ledger_does_not_reach_the_handback(self, repo, tmp_path):
        """⚠ The handback has 1,500 tokens for things the next window must ACT on, and a
        measurement is not one of them. Four hundred signatures reaching it would be the most
        expensive thing in the budget and the least useful."""
        path = transcript(tmp_path / "t.jsonl", [])
        state = ss.empty_state("s")
        ss.mark_handed_back(state, [])
        self._derive(state, path, [tool_use("Read", {"file_path": "a.py"})])
        text = ss.handback(state, repo=repo)["text"]
        assert state["facts"]["sigs"][0] not in text


class TestThePullRequestsThisWindowTouched:
    """⚠ WHY THIS EXISTS. With `autoCompactWindow` at 400,000 instead of 1,000,000, a window
    boundary arrives roughly 2.8x as often, so whatever the handback drops is paid for 2.8x as
    often. "What did I leave open" was costing a `gh pr list` after every compaction -- and once
    cost worse than that: the GitHub account is shared between seats, so `--author @me` returned
    four other teams' pull requests and they were briefly reported as this seat's.
    """

    def test_a_pr_url_in_tool_output_is_carried_over(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [
            tool_result("https://github.com/Lanchester-Industries/meridian/pull/1872"),
        ])
        state = ss.empty_state("s")
        facts = ss.derive_facts(state, path)
        assert facts["prs"] == ["1872"]

    def test_the_handback_says_the_state_was_not_checked(self, repo, tmp_path):
        """⚠⚠ THE LINE THAT KEEPS IT HONEST. These numbers were SEEN; one may have merged
        since. A handback that said "open PRs" would assert a live fact from a stale
        observation, and a merged PR reported as open is exactly how a session pushes to a dead
        branch -- which happened twice on the Meridian programme in one week."""
        path = transcript(tmp_path / "t.jsonl", [
            tool_result("https://github.com/x/y/pull/1855"),
        ])
        state = ss.empty_state("s")
        ss.derive_facts(state, path)
        text = ss.handback(state, repo=repo)["text"]
        assert "#1855" in text
        assert "NOT checked" in text

    def test_prose_about_a_pr_is_not_a_pr_this_window_touched(self, tmp_path):
        """⚠ Nothing is inferred from prose -- the module's own rule. A number this session
        merely TALKED about is not a pull request it touched, and handing one over as though it
        were is the guess this module refuses to make."""
        path = transcript(tmp_path / "t.jsonl", [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text",
                 "text": "I think https://github.com/x/y/pull/999 is probably the right one"}]}},
        ])
        state = ss.empty_state("s")
        facts = ss.derive_facts(state, path)
        assert facts.get("prs") == []

    def test_the_same_pr_seen_twice_is_listed_once(self, tmp_path):
        path = transcript(tmp_path / "t.jsonl", [
            tool_result("https://github.com/x/y/pull/42"),
            tool_result("https://github.com/x/y/pull/42 again, "
                        "and https://github.com/x/y/pull/43"),
        ])
        state = ss.empty_state("s")
        facts = ss.derive_facts(state, path)
        assert facts["prs"] == ["42", "43"]

    def test_a_bare_pull_path_without_a_scheme_is_not_a_url(self, tmp_path):
        """⚠ My first draft of the test above wrote `/pull/43` and expected it to
        count. It does not, and should not: the pattern requires a scheme so that a
        path fragment in prose cannot become a pull request this window touched. The
        test failed, which is the pattern being stricter than the test author."""
        path = transcript(tmp_path / "t.jsonl", [
            tool_result("see /pull/99 for the rest"),
        ])
        state = ss.empty_state("s")
        assert ss.derive_facts(state, path).get("prs") == []

    def test_it_is_bounded_so_it_cannot_eat_the_budget(self, tmp_path):
        """A long window can touch many PRs; the handback has 1,500 tokens for everything."""
        path = transcript(tmp_path / "t.jsonl", [
            tool_result(" ".join(f"https://github.com/x/y/pull/{i}" for i in range(40))),
        ])
        state = ss.empty_state("s")
        facts = ss.derive_facts(state, path)
        assert len(facts["prs"]) == 6


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

    def test_the_UNPROMOTED_message_says_the_note_IS_carried(self):
        """⚠ FROM THE FIELD. It used to say "exists only in a context window", and a team
        read that as "this will not survive compaction", so they re-recorded every note by
        hand -- the exact work the handback exists to remove. UNPROMOTED is the SAFE state:
        kept in the state file, handed back every window. The message has to say so."""
        why = ss.check_promotion({"text": "a ruling"})["why"]
        assert "handed back every window" in why
        assert "only in a context window" not in why, "the misreading must not come back"

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
