"""A lever must reach a session that is already running, and a rollback must always work.

⚠⚠ THE FEATURE IS "NO RESTART", so the tests that matter are the ones about WHO DECIDED and
   WHETHER A CHANGE IS SEEN. A lever that resolves correctly but caches its file, or that lets
   the file overrule an explicit environment OFF, is a lever that reads right in a unit test and
   fails the only two jobs it has.
"""

import json

import pytest

from attnroute import levers


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private HOME, so a test never reads or writes the real flag file."""
    monkeypatch.setattr(levers.Path, "home", staticmethod(lambda: tmp_path))
    for env in levers.LEVERS.values():
        monkeypatch.delenv(env, raising=False)
    return tmp_path


def write(home, data):
    p = home / ".claude" / "attn_state"
    p.mkdir(parents=True, exist_ok=True)
    (p / "levers.json").write_text(json.dumps(data), encoding="utf-8")


class TestWhoDecided:

    def test_with_nothing_set_it_is_off_and_says_default(self, home):
        assert levers.resolve("cap_act") == {"on": False, "source": "default", "why": ""}

    def test_the_file_can_turn_it_on(self, home):
        write(home, {"cap_act": True})
        r = levers.resolve("cap_act")
        assert (r["on"], r["source"]) == (True, "file")

    def test_a_file_that_says_false_is_the_file_deciding_not_a_default(self, home):
        """⚠ The distinction is not cosmetic. "default" means nobody has set this lever;
        "file" means somebody set it off. A trial that cannot tell those apart cannot tell an
        un-provisioned machine from a deliberately held-back one."""
        write(home, {"cap_act": False})
        assert levers.resolve("cap_act")["source"] == "file"

    def test_the_environment_overrules_the_file(self, home, monkeypatch):
        write(home, {"cap_act": False})
        monkeypatch.setenv("ATTNROUTE_CAP_ACT", "1")
        r = levers.resolve("cap_act")
        assert (r["on"], r["source"]) == (True, "env")

    @pytest.mark.parametrize("off", ["0", "false", "no", "off", "nonsense"])
    def test_AN_ENVIRONMENT_OFF_BEATS_A_FILE_THAT_SAYS_ON(self, home, monkeypatch, off):
        """⚠⚠ THE ROLLBACK TEST, AND THE REASON THIS MODULE IS SHAPED THE WAY IT IS.

        `ATTNROUTE_CAP_ACT` has never had a false value: unset and `=0` both meant observe,
        because `acting()` only tested membership in a true-list. Carry that straight into a
        file-backed lever and the documented rollback -- "unset it or set it to 0" -- would
        silently do NOTHING whenever the file said on. The operator would be looking at a lever
        they believed they had turned off, on a fleet, with the telemetry agreeing with them.

        So a variable that is SET and not true means OFF, and the file is not consulted.
        """
        write(home, {"cap_act": True})
        monkeypatch.setenv("ATTNROUTE_CAP_ACT", off)
        r = levers.resolve("cap_act")
        assert r["on"] is False
        assert r["source"] == "env", "the file must not get a say once the env has spoken"
        assert "not a true value" in r["why"]

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_empty_variable_counts_as_absent(self, home, monkeypatch, blank):
        """ℹ On Windows, setting a variable to the empty string is how it is REMOVED. Treating
        `VAR=` as "set to something false" would make a removal behave like an override, so an
        operator clearing the variable to fall back to the file would instead pin it off."""
        write(home, {"cap_act": True})
        monkeypatch.setenv("ATTNROUTE_CAP_ACT", blank)
        assert levers.resolve("cap_act")["source"] == "file"

    def test_both_levers_are_independent(self, home):
        write(home, {"cap_act": True, "ledger_act": False})
        assert levers.resolve("cap_act")["on"] is True
        assert levers.resolve("ledger_act")["on"] is False

    def test_an_unknown_lever_is_off_and_names_itself(self, home):
        r = levers.resolve("not_a_lever")
        assert r["on"] is False and "not a known lever" in r["why"]


class TestNothingHereMayRaise:
    """It runs on every hook event. A crash costs the user their turn, so every malformed input
    resolves to the safe answer and explains itself."""

    def test_malformed_json_is_off_and_names_the_file(self, home):
        p = home / ".claude" / "attn_state"
        p.mkdir(parents=True)
        (p / "levers.json").write_text("{not json", encoding="utf-8")
        r = levers.resolve("cap_act")
        assert r["on"] is False
        assert "not valid JSON" in r["why"]
        assert "levers.json" in r["why"], "a complaint about a file must name the file"

    def test_json_that_is_not_an_object_is_off(self, home):
        p = home / ".claude" / "attn_state"
        p.mkdir(parents=True)
        (p / "levers.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert "does not hold an object" in levers.resolve("cap_act")["why"]

    def test_a_value_of_the_wrong_type_is_off_and_says_so(self, home):
        """⚠ `7` is not a typo that becomes True by accident. Python would call it truthy, and a
        lever turned on by a stray number is a lever nobody chose to turn on."""
        write(home, {"cap_act": 7})
        r = levers.resolve("cap_act")
        assert r["on"] is False
        assert "neither true/false" in r["why"]

    def test_a_string_true_is_accepted_so_a_hand_edit_works(self, home):
        """A human editing the file will write "on" as often as true, and the tokens are the
        same ones the environment has always accepted -- one word cannot mean two things."""
        write(home, {"cap_act": "on"})
        assert levers.resolve("cap_act")["on"] is True
        write(home, {"cap_act": "nope"})
        assert levers.resolve("cap_act")["on"] is False

    def test_an_unreadable_path_does_not_raise(self, home):
        """A directory where the file should be: an OSError on the hot path."""
        (home / ".claude" / "attn_state" / "levers.json").mkdir(parents=True)
        assert levers.resolve("cap_act")["on"] is False


class TestTheChangeIsSeenWithoutARestart:

    def test_a_second_call_sees_a_new_value(self, home):
        """⚠⚠ THE NO-CACHE GUARD, AND THE ONE THAT PROTECTS THE WHOLE FEATURE. Caching the file
        is the obvious optimisation and it would kill the only thing this module is for -- a flip
        reaching a session that is already running. Every other test in this file would still
        pass with a cache in place, because each one resolves once.
        """
        write(home, {"cap_act": False})
        assert levers.resolve("cap_act")["on"] is False
        write(home, {"cap_act": True})
        assert levers.resolve("cap_act")["on"] is True, "the file was cached"
        write(home, {"cap_act": False})
        assert levers.resolve("cap_act")["on"] is False, "the file was cached"


class TestWriting:

    def test_it_writes_and_reads_back(self, home):
        levers.set_lever("cap_act", True)
        assert levers.resolve("cap_act")["on"] is True

    def test_it_keeps_the_other_lever(self, home):
        """⚠ Turning one lever on must not quietly turn the other off: these are separate
        experiments and losing one of them would show up as an empty arm, not as an error."""
        levers.set_lever("ledger_act", True)
        levers.set_lever("cap_act", True)
        assert levers.resolve("ledger_act")["on"] is True

    def test_a_rollback_still_works_over_a_corrupt_file_and_reports_the_loss(self, home):
        """⚠⚠ REFUSING WOULD BE DEFENSIBLE FOR THE FILE AND WRONG FOR THE OPERATOR. This command
        is the documented way to turn a lever OFF. A rollback that refuses because the file it is
        rolling back is corrupt is a rollback that does not work -- at the moment someone needs it
        most. So it writes, and `lost` names what went back to default."""
        p = home / ".claude" / "attn_state"
        p.mkdir(parents=True)
        (p / "levers.json").write_text("{corrupt", encoding="utf-8")
        out = levers.set_lever("cap_act", False)
        assert "ledger_act" in out["lost"], "the loss has to be reported, not hidden"
        assert levers.resolve("cap_act") == {"on": False, "source": "file", "why": ""}

    def test_it_leaves_no_temp_file_behind(self, home):
        """The write is temp-and-replace so a hook reading mid-write sees old or new, never
        half. A leftover .tmp would mean the replace did not happen."""
        levers.set_lever("cap_act", True)
        assert list((home / ".claude" / "attn_state").glob("*.tmp")) == []

    def test_an_unknown_lever_refuses_rather_than_inventing_a_key(self, home):
        with pytest.raises(KeyError):
            levers.set_lever("cap_acting", True)


class TestTheCallersActuallyGoThroughIt:
    """⚠ Without these the module could be perfect and unreached. The wiring is the change."""

    def test_output_cap_reads_the_file(self, home):
        from attnroute import output_cap
        write(home, {"cap_act": True})
        assert output_cap.acting() is True
        assert output_cap.acting_source()["source"] == "file"

    def test_read_ledger_reads_the_file(self, home):
        from attnroute import read_ledger
        write(home, {"ledger_act": True})
        assert read_ledger.acting() is True

    def test_the_two_do_not_share_one_switch(self, home):
        """⚠ One flag for both would turn the read ledger on with the cap, and the read ledger
        is the one whose saving is currently zero and under investigation."""
        from attnroute import output_cap, read_ledger
        write(home, {"cap_act": True, "ledger_act": False})
        assert output_cap.acting() is True
        assert read_ledger.acting() is False
