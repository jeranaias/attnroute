"""The read ledger: four rules, each from a way it could go wrong.

Sized from three long sessions: 27.6% of read tokens re-read a file already read in the same
compaction window with no intervening edit. The two LEGITIMATE repeat categories -- across a
compaction (8.6%) and after an edit (4.0%) -- are what these tests protect, because suppressing
either would strand the model on content it cannot see.
"""

import tempfile
from pathlib import Path

import pytest

from attnroute import read_ledger as rl


@pytest.fixture
def big(tmp_path):
    """A file whose earlier read was worth more than a notice."""
    f = tmp_path / "mod.py"
    f.write_text("x" * 8000, encoding="utf-8")
    return f


@pytest.fixture
def ledger():
    led = rl.ReadLedger()
    led.state["turn"] = 11
    return led


@pytest.fixture
def ledger_arm(monkeypatch):
    """Pin the holdout arm to "ledger".

    WARNING: WITHOUT THIS, EVERY TEST THAT ASSERTS A PAYLOAD IS A 1-IN-8 COIN FLIP.
      The arm is bucketed from the resolved file path, and pytest's `tmp_path` contains a
      run counter (`pytest-123/...`), so the key -- and therefore the arm -- is DIFFERENT
      on every run. 13% of runs put the file or the turn in a holdout, no payload is
      produced, and the test fails for a reason that has nothing to do with the code. It
      passed 469 times before it failed twice in one suite run.

      The holdout tests below deliberately do NOT use this fixture: they call
      `rl.holdout` to check the split itself.
    """
    real = rl.holdout
    monkeypatch.setattr(rl, "holdout",
                        lambda *a, **k: {**real(*a, **k), "arm": "ledger"})


class TestANoticeNeverFiresWhenTheRepeatIsLegitimate:
    """Acceptance (a)."""

    def test_a_notice_fires_on_an_ordinary_repeat(self, ledger, big):
        """Positive control FIRST: without it, every test below passes vacuously."""
        ledger.record(str(big), tokens=2000)
        assert ledger.decide(str(big))["action"] == rl.NOTICE

    def test_never_after_an_in_session_edit(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        ledger.on_edit(str(big))
        d = ledger.decide(str(big))
        assert d["action"] == rl.ALLOW
        assert "not read before" in d["reason"]

    def test_an_edit_invalidates_a_RANGED_entry_too(self, ledger, big):
        """⚠ The bug this caught: a Windows path begins "c:\\", and the key separator used to
        be ":" -- so `on_edit` matched on "c" and invalidated NOTHING on Windows."""
        ledger.record(str(big), offset=10, limit=20, tokens=2000)
        ledger.on_edit(str(big))
        assert ledger.decide(str(big), offset=10, limit=20)["action"] == rl.ALLOW

    def test_never_when_the_file_changed_on_disk(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        big.write_text("y" * 9000, encoding="utf-8")
        d = ledger.decide(str(big))
        assert d["action"] == rl.ALLOW
        assert "changed" in d["reason"]

    def test_never_across_a_compaction(self, ledger, big):
        """8.6% of read tokens are this case. The content is gone from context."""
        ledger.record(str(big), tokens=2000)
        ledger.on_compaction()
        assert ledger.decide(str(big))["action"] == rl.ALLOW

    def test_never_when_an_old_window_survives_in_a_state_file(self, big):
        """Belt and braces: a ledger reloaded from disk could carry an older window."""
        led = rl.ReadLedger({"window": 0, "turn": 1, "entries": {}})
        led.record(str(big), tokens=2000)
        led.state["window"] = 5          # as if compaction happened elsewhere
        d = led.decide(str(big))
        assert d["action"] == rl.ALLOW
        assert "earlier compaction window" in d["reason"]

    def test_never_when_the_earlier_read_was_small(self, ledger, tmp_path):
        """A notice costs ~40 tokens; below the floor it would not pay for itself."""
        f = tmp_path / "tiny.py"
        f.write_text("x" * 100, encoding="utf-8")
        ledger.record(str(f), tokens=50)
        assert ledger.decide(str(f))["action"] == rl.ALLOW

    def test_a_deny_is_not_reachable(self, ledger, big):
        """⚠ The only two actions are ALLOW and NOTICE. A hook that strands the model is worse
        than a re-read, so there is no third value to reach."""
        ledger.record(str(big), tokens=2000)
        for _ in range(4):
            assert ledger.decide(str(big))["action"] in (rl.ALLOW, rl.NOTICE)


class TestTheForceFullEscapeHatch:
    """Acceptance (b). Two routes, because a hatch that must be remembered is not a hatch."""

    def test_the_explicit_marker_forces_a_full_read(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        assert ledger.decide(str(big), forced=True)["action"] == rl.ALLOW

    def test_is_forced_recognises_the_marker_in_any_field(self):
        assert rl.is_forced({"command": "sed -n 1,50p x.py  # attnroute:full"}) is True
        assert rl.is_forced({"file_path": "x.py"}) is False
        assert rl.is_forced(None) is False

    def test_the_SECOND_ask_is_always_served(self, ledger, big):
        """⚠ THE HALF I ALMOST SHIPPED MISSING. `format_notice` promises "re-issue the same
        read", and nothing implemented it until the decide() branch existed. A notice that
        advertises a hatch that does not work is worse than no hatch."""
        ledger.record(str(big), tokens=2000)
        assert ledger.decide(str(big))["action"] == rl.NOTICE
        assert ledger.decide(str(big))["action"] == rl.ALLOW

    def test_the_notice_names_the_hatch(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        d = ledger.decide(str(big))
        text = rl.format_notice(str(big), d)
        assert "attnroute:full" in text
        assert "re-issue" in text.lower()

    def test_a_fresh_read_resets_the_allowance(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        assert ledger.decide(str(big))["action"] == rl.NOTICE
        ledger.record(str(big), tokens=2000)      # read in full again
        assert ledger.decide(str(big))["action"] == rl.NOTICE


class TestTheHoldoutIsDeterministicAndLogged:
    """Acceptance (c)."""

    def test_the_same_inputs_always_give_the_same_arm(self):
        a = rl.holdout("s1", "k1", 7)
        for _ in range(5):
            assert rl.holdout("s1", "k1", 7) == a

    def test_it_does_not_use_pythons_randomised_hash(self):
        """⚠ `hash()` is salted per process by PYTHONHASHSEED, so an arm assigned with it
        could not be re-derived from the logs. sha256 is stable across runs."""
        src = Path(rl.__file__).read_text(encoding="utf-8")
        assert "hashlib.sha256" in src
        assert "= hash(" not in src

    def test_the_arms_land_near_their_targets(self):
        arms = [rl.holdout("s", f"f{i}.py", i)["arm"] for i in range(4000)]
        turn_pct = 100.0 * arms.count("held-out-turn") / len(arms)
        file_pct = 100.0 * arms.count("held-out-file") / len(arms)
        assert 1.5 < turn_pct < 4.5, turn_pct
        assert 7.0 < file_pct < 13.0, file_pct

    def test_the_log_carries_the_seed_and_both_buckets(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        _, log = rl.hook_decision(ledger, "s1", "Read", {"file_path": str(big)}, turn=4)
        for field in ("seed", "file_bucket", "turn_bucket", "arm", "action", "key",
                      "saved_tokens_est", "acting"):
            assert field in log, field

    def test_a_held_out_read_is_left_alone(self, ledger, big, monkeypatch):
        monkeypatch.setenv(rl.ACT_ENV, "1")
        ledger.record(str(big), tokens=2000)
        monkeypatch.setattr(rl, "holdout",
                            lambda *a, **k: {"arm": "held-out-file", "file_bucket": 1,
                                             "turn_bucket": 50, "seed": "t"})
        payload, log = rl.hook_decision(ledger, "s1", "Read", {"file_path": str(big)}, turn=4)
        assert payload is None, "a held-out read must not be modified"
        assert log["action"] == rl.NOTICE, "but the log must still say what would have happened"


class TestObserveOnlyEmitsNothing:
    """Acceptance (d)."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv(rl.ACT_ENV, raising=False)

    def test_acting_is_off_by_default(self):
        assert rl.acting() is False

    def test_no_payload_while_observing(self, ledger, big, ledger_arm):
        ledger.record(str(big), tokens=2000)
        payload, log = rl.hook_decision(ledger, "s1", "Read", {"file_path": str(big)}, turn=4)
        assert payload is None
        assert log["action"] == rl.NOTICE and log["acting"] is False
        assert log["saved_tokens_est"] > 0, "the log must say what it WOULD have saved"

    def test_a_payload_appears_only_when_acting(self, ledger, big, monkeypatch,
                                               ledger_arm):
        """Positive control: the gate is what suppresses it, not a broken fixture."""
        monkeypatch.setenv(rl.ACT_ENV, "1")
        ledger.record(str(big), tokens=2000)
        payload, _ = rl.hook_decision(ledger, "s1", "Read", {"file_path": str(big)}, turn=4)
        assert payload is not None
        out = payload["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        # The mechanism is a DENY, not an annotation: see
        # TestActingActuallyPreventsTheRead. `additionalContext` would let the read run.
        assert out["permissionDecision"] == "deny"
        assert "attnroute:full" in out["permissionDecisionReason"]

    def test_nothing_in_the_package_sets_the_act_flag(self):
        root = Path(rl.__file__).resolve().parent
        offenders = [
            f"{py.name}: {line.strip()}"
            for py in root.rglob("*.py")
            for line in py.read_text(encoding="utf-8", errors="replace").split("\n")
            if rl.ACT_ENV in line and ("setenv" in line or "environ[" in line)
        ]
        assert not offenders, offenders

    def test_a_non_read_tool_is_ignored(self, ledger):
        payload, log = rl.hook_decision(ledger, "s1", "Bash", {"command": "ls"}, turn=1)
        assert payload is None and log is None


def test_persistence_round_trips(tmp_path, monkeypatch, big):
    monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path))
    led = rl.ReadLedger()
    led.record(str(big), tokens=2000)
    rl.save("sess", led)
    back = rl.load("sess")
    assert back.decide(str(big))["action"] == rl.NOTICE


def test_a_missing_ledger_file_is_an_empty_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path))
    assert rl.load("nope").state["entries"] == {}


def test_save_never_raises_on_an_unwritable_path(monkeypatch, big):
    """A measurement must never cost the user their turn."""
    monkeypatch.setattr(rl, "ledger_path",
                        lambda s: Path(tempfile.gettempdir()) / "no" / "such" / "dir" / "x"
                        / "y.json")
    monkeypatch.setattr(rl.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    rl.save("s", rl.ReadLedger())          # must not raise


# ═══ THE REVIEW ITEMS: MECHANISM, WIRING, SUBAGENTS, SECOND WITNESS ════════════════════

class TestActingActuallyPreventsTheRead:
    """The first version used `additionalContext`, WHICH DOES NOT PREVENT ANYTHING.

    PreToolUse `additionalContext` appends text and the tool still runs, so the file was read
    anyway and the notice cost 40 tokens on top: acting would have been strictly worse than
    doing nothing, and every `saved_tokens_est` in the observe log would have been a fiction.
    """

    @pytest.fixture(autouse=True)
    def _acting(self, monkeypatch, ledger_arm):
        monkeypatch.setenv(rl.ACT_ENV, "1")

    def test_the_payload_denies_the_tool_call(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        payload, _ = rl.hook_decision(ledger, "s", "Read", {"file_path": str(big)}, turn=3)
        out = payload["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "attnroute:full" in out["permissionDecisionReason"]

    def test_the_payload_does_not_merely_annotate_the_read(self, ledger, big):
        """A payload carrying only `additionalContext` would let the read through. If this
        ever passes again, the feature has silently become a 40-token tax."""
        ledger.record(str(big), tokens=2000)
        payload, _ = rl.hook_decision(ledger, "s", "Read", {"file_path": str(big)}, turn=3)
        out = payload["hookSpecificOutput"]
        assert "additionalContext" not in out, "this does not prevent the read"

    def test_the_log_names_the_mechanism(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        _, log = rl.hook_decision(ledger, "s", "Read", {"file_path": str(big)}, turn=3)
        assert log["mechanism"] == "deny"

    def test_the_notice_does_not_tell_a_Read_to_change_its_command(self, ledger, big):
        """Read has no `command` field, and the notice used to say "add it to the command"."""
        ledger.record(str(big), tokens=2000)
        text = rl.format_notice(str(big), ledger.decide(str(big)))
        assert "to the command" not in text
        assert "RE-ISSUE THE SAME READ" in text


class TestObservingFollowsTheSameTrajectoryAsActing:
    """WITHOUT THE SHADOW LEDGER, OBSERVE MODE OVER-REPORTS BY UP TO 2x.

    Acting denies the repeat, so the read never happens and the one-notice allowance stays
    spent. Observing lets the read through, PostToolUse records the key afresh, the allowance
    resets, and the NEXT repeat logs another notice -- a saving acting could never deliver.
    """

    @pytest.fixture(autouse=True)
    def _pin(self, ledger_arm):
        """Six reads must all be in the ledger arm, or the counts mean nothing."""

    def _six_reads(self, monkeypatch, tmp_path, acting: bool):
        monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path / "home"))
        monkeypatch.delenv(rl.ACT_ENV, raising=False)
        if acting:
            monkeypatch.setenv(rl.ACT_ENV, "1")
        tmp_path.mkdir(parents=True, exist_ok=True)
        f = tmp_path / "mod.py"
        f.write_text("x" * 20000, encoding="utf-8")
        notices = saved = 0
        for _ in range(6):
            led = rl.load("s")
            out, log = rl.hook_decision(led, "s", "Read", {"file_path": str(f)}, turn=1)
            if log["action"] == rl.NOTICE and log["arm"] == "ledger":
                notices += 1
                saved += log["saved_tokens_est"]
                if not rl.acting():
                    led.note_shadow_deny(log["key"])
            rl.save("s", led)
            if not out:
                rl.handle({"hook_event_name": "PostToolUse", "session_id": "s",
                           "tool_name": "Read", "tool_input": {"file_path": str(f)},
                           "tool_response": {"file": "y" * 20000}})
        return notices, saved

    def test_the_two_arms_report_the_same_saving(self, monkeypatch, tmp_path):
        acting = self._six_reads(monkeypatch, tmp_path / "a", acting=True)
        observing = self._six_reads(monkeypatch, tmp_path / "b", acting=False)
        assert observing == acting, (
            f"observe reported {observing} where acting delivers {acting}")

    def test_a_notice_is_not_served_on_every_single_repeat(self, monkeypatch, tmp_path):
        """Positive control for the test above: if both arms simply noticed everything, the
        equality would hold and mean nothing. Six reads alternate, so three notices."""
        notices, _ = self._six_reads(monkeypatch, tmp_path / "c", acting=True)
        assert notices == 3, notices


class TestASubagentIsADifferentConversation:
    """A subagent's hook events carry the PARENT's session_id plus their own agent_id."""

    def test_the_ledger_id_separates_them(self):
        main = {"session_id": "abc"}
        sub = {"session_id": "abc", "agent_id": "sub-7"}
        assert rl.ledger_id(main) == "abc"
        assert rl.ledger_id(sub) != rl.ledger_id(main)
        assert "sub-7" in rl.ledger_id(sub)

    def test_a_subagents_read_cannot_notice_the_main_loop(self, monkeypatch, tmp_path, big,
                                                          ledger_arm):
        """THE QUALITY BUG THIS PREVENTS: a notice sent to the main loop about content that
        only ever existed in a subagent's context -- a read the recipient never saw."""
        monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv(rl.ACT_ENV, "1")
        rl.handle({"hook_event_name": "PostToolUse", "session_id": "abc",
                   "agent_id": "sub-7", "tool_name": "Read",
                   "tool_input": {"file_path": str(big)},
                   "tool_response": {"file": "z" * 20000}})

        out = rl.handle({"hook_event_name": "PreToolUse", "session_id": "abc",
                         "tool_name": "Read", "tool_input": {"file_path": str(big)}})
        assert out is None, "the main loop was denied a read only the subagent had seen"

    def test_the_subagents_own_repeat_is_still_noticed(self, monkeypatch, tmp_path, big,
                                                       ledger_arm):
        """Positive control: separating them must not disable the ledger inside a subagent."""
        monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv(rl.ACT_ENV, "1")
        sub = {"session_id": "abc", "agent_id": "sub-7", "tool_name": "Read",
               "tool_input": {"file_path": str(big)}}
        rl.handle({**sub, "hook_event_name": "PostToolUse",
                   "tool_response": {"file": "z" * 20000}})
        out = rl.handle({**sub, "hook_event_name": "PreToolUse"})
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestTheWiring:
    """Unwired, this module is inert and its observe log is empty -- which reads exactly like
    a lever that does not pay."""

    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv(rl.ACT_ENV, raising=False)

    def _read(self, big):
        rl.handle({"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Read",
                   "tool_input": {"file_path": str(big)},
                   "tool_response": {"file": "q" * 20000}})

    def test_PostToolUse_on_a_Read_records_it(self, big):
        self._read(big)
        assert rl.load("s").decide(str(big))["action"] == rl.NOTICE

    def test_PostToolUse_on_an_Edit_invalidates_it(self, big):
        self._read(big)
        rl.handle({"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Edit",
                   "tool_input": {"file_path": str(big)}, "tool_response": {}})
        assert rl.load("s").decide(str(big))["action"] == rl.ALLOW

    def test_SessionStart_compact_clears_the_window(self, big):
        self._read(big)
        rl.handle({"hook_event_name": "SessionStart", "session_id": "s", "source": "compact"})
        assert rl.load("s").decide(str(big))["action"] == rl.ALLOW

    def test_SessionStart_for_any_other_reason_does_not(self, big):
        self._read(big)
        rl.handle({"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"})
        assert rl.load("s").decide(str(big))["action"] == rl.NOTICE

    def test_Stop_advances_the_turn(self):
        for _ in range(3):
            rl.handle({"hook_event_name": "Stop", "session_id": "s"})
        assert rl.load("s").state["turn"] == 3

    def test_the_turn_is_what_the_notice_quotes(self, big):
        rl.handle({"hook_event_name": "Stop", "session_id": "s"})
        rl.handle({"hook_event_name": "Stop", "session_id": "s"})
        self._read(big)
        assert rl.load("s").decide(str(big))["prior_turn"] == 2

    def test_a_malformed_event_never_raises(self):
        assert rl.handle({"hook_event_name": "PostToolUse", "session_id": "s",
                          "tool_name": "Read", "tool_input": None}) is None
        assert rl.handle({}) is None

    def test_main_exits_zero_on_rubbish_stdin(self, monkeypatch):
        """A measurement must never cost the user their turn."""
        import io as _io
        monkeypatch.setattr("sys.stdin", _io.StringIO("not json at all"))
        assert rl.main() == 0


class TestTheSecondWitnessForCompaction:
    """If the compact hook does not fire, entries survive into a window that no longer holds
    the content -- and the ledger then suppresses reads of material the model cannot see.
    That is the worst failure this module has, so it does not rest on one signal."""

    def test_a_compaction_marker_in_the_transcript_is_caught(self, tmp_path, big):
        t = tmp_path / "t.jsonl"
        t.write_text('{"type":"assistant"}\n' * 50, encoding="utf-8")
        led = rl.ReadLedger()
        rl._witness(led, {"transcript_path": str(t)})      # first look: establish the offset
        led.record(str(big), tokens=2000)
        assert led.decide(str(big))["action"] == rl.NOTICE

        with open(t, "a", encoding="utf-8") as fh:
            fh.write('{"isCompactSummary":true,"message":{"content":"summary"}}\n')
        caught = rl._witness(led, {"transcript_path": str(t)})

        assert caught == 1
        assert led.decide(str(big))["action"] == rl.ALLOW
        assert led.state["missed_compactions"] == 1

    def test_the_first_look_claims_nothing(self, tmp_path):
        """There is no stored offset on the first call, so a scan would see markers from
        earlier in the session. It records the offset and reports none."""
        t = tmp_path / "t.jsonl"
        t.write_text('{"isCompactSummary":true}\n' * 10, encoding="utf-8")
        led = rl.ReadLedger()
        assert rl._witness(led, {"transcript_path": str(t)}) == 0
        assert led.state["transcript_offset"] == t.stat().st_size

    def test_an_absent_transcript_is_not_a_compaction(self, tmp_path):
        led = rl.ReadLedger()
        assert rl._witness(led, {"transcript_path": str(tmp_path / "nope.jsonl")}) == 0


def test_the_save_is_atomic(tmp_path, monkeypatch, big):
    """Parallel tool calls run this concurrently. A half-written file makes `load` fall back
    to an EMPTY ledger, which looks exactly like a ledger that never fires."""
    monkeypatch.setattr(rl.Path, "home", staticmethod(lambda: tmp_path))
    src = Path(rl.__file__).read_text(encoding="utf-8")
    assert "os.replace(tmp, p)" in src, "a plain write_text can be observed half-written"

    led = rl.ReadLedger()
    led.record(str(big), tokens=2000)
    rl.save("s", led)
    d = tmp_path / ".claude" / "telemetry"
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")], "temp file left behind"
    assert rl.load("s").decide(str(big))["action"] == rl.NOTICE
