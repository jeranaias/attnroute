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

    def test_no_payload_while_observing(self, ledger, big):
        ledger.record(str(big), tokens=2000)
        payload, log = rl.hook_decision(ledger, "s1", "Read", {"file_path": str(big)}, turn=4)
        assert payload is None
        assert log["action"] == rl.NOTICE and log["acting"] is False
        assert log["saved_tokens_est"] > 0, "the log must say what it WOULD have saved"

    def test_a_payload_appears_only_when_acting(self, ledger, big, monkeypatch):
        """Positive control: the gate is what suppresses it, not a broken fixture."""
        monkeypatch.setenv(rl.ACT_ENV, "1")
        ledger.record(str(big), tokens=2000)
        payload, _ = rl.hook_decision(ledger, "s1", "Read", {"file_path": str(big)}, turn=4)
        assert payload is not None
        assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "attnroute:full" in payload["hookSpecificOutput"]["additionalContext"]

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
