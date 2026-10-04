"""Holdout percentages come from the environment, and travel with every decision.

Why this exists: 10% is the right steady-state holdout and the wrong trial holdout. The
measured decision rates are 1.9 repeat Reads and 2.9 over-cap Bash results per window, so a
10% control arm needs ~257 and ~171 windows respectively to reach 50 control decisions. A
trial at 50% reaches them five times sooner, at the cost of forgoing half the saving while it
runs -- which is nothing against being able to see harm at all.

The test that matters most here is not that the percentage works. It is that a percentage the
environment got WRONG is reported rather than silently replaced by the default: a trial that
ran at 10% while everyone believed it ran at 50% would have a control arm five times smaller
than the analysis assumes, and nothing would look broken.
"""

import pytest

from attnroute import output_cap as oc
from attnroute import read_ledger as rl

#: Enough keys that a share is a share and not a coincidence.
N = 4000


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (rl.FILE_HOLDOUT_ENV, rl.TURN_HOLDOUT_ENV, oc.HOLDOUT_ENV):
        monkeypatch.delenv(name, raising=False)


def ledger_share(arm):
    arms = [rl.holdout("s", f"f{i}.py", i)["arm"] for i in range(N)]
    return 100.0 * arms.count(arm) / len(arms)


def cap_share():
    arms = [oc.arm_for("s", f"cmd {i}")["arm"] for i in range(N)]
    return 100.0 * arms.count("held-out") / len(arms)


class TestTheDefaultsAreUnchanged:

    def test_the_ledger_defaults_to_ten_and_three(self):
        out = rl.holdout("s", "k", 1)
        assert out["file_holdout_pct"] == 10
        assert out["turn_holdout_pct"] == 3

    def test_the_cap_defaults_to_ten(self):
        assert oc.arm_for("s", "c")["holdout_pct"] == 10

    def test_the_measured_shares_match_the_defaults(self):
        assert 7.0 < ledger_share("held-out-file") < 13.0
        assert 1.5 < ledger_share("held-out-turn") < 4.5
        assert 7.0 < cap_share() < 13.0


class TestTheEnvironmentCanSetThemForATrial:

    def test_the_ledger_file_holdout_follows_the_variable(self, monkeypatch):
        monkeypatch.setenv(rl.FILE_HOLDOUT_ENV, "50")
        # Slightly under 50: the TURN holdout is checked first and takes its 3% from the
        # same population. Documented behaviour, not drift.
        assert 44.0 < ledger_share("held-out-file") < 50.0

    def test_the_cap_holdout_follows_the_variable(self, monkeypatch):
        monkeypatch.setenv(oc.HOLDOUT_ENV, "50")
        assert 46.0 < cap_share() < 54.0

    def test_a_trailing_percent_sign_is_accepted(self, monkeypatch):
        monkeypatch.setenv(oc.HOLDOUT_ENV, "50%")
        assert oc.arm_for("s", "c")["holdout_pct"] == 50

    def test_zero_turns_the_holdout_off_entirely(self, monkeypatch):
        monkeypatch.setenv(oc.HOLDOUT_ENV, "0")
        assert cap_share() == 0.0

    def test_one_hundred_holds_everything_out(self, monkeypatch):
        """Useful on its own: the lever observes and never acts, which measures the decision
        rate without changing anything the model sees."""
        monkeypatch.setenv(oc.HOLDOUT_ENV, "100")
        assert cap_share() == 100.0

    def test_the_split_is_still_deterministic_at_a_new_percentage(self, monkeypatch):
        monkeypatch.setenv(oc.HOLDOUT_ENV, "50")
        first = [oc.arm_for("s", f"cmd {i}")["arm"] for i in range(200)]
        again = [oc.arm_for("s", f"cmd {i}")["arm"] for i in range(200)]
        assert first == again


class TestABadValueIsReportedAndNotSwallowed:
    """⚠ The important class. A typo must not look like a successful trial."""

    @pytest.mark.parametrize("bad", ["fifty", "", "   ", "50.5", "-10", "101", "1e2"])
    def test_an_unusable_value_keeps_the_default(self, monkeypatch, bad):
        monkeypatch.setenv(oc.HOLDOUT_ENV, bad)
        assert oc.arm_for("s", "c")["holdout_pct"] == 10

    @pytest.mark.parametrize("bad", ["fifty", "50.5", "-10", "101"])
    def test_and_says_why_in_the_record(self, monkeypatch, bad):
        monkeypatch.setenv(oc.HOLDOUT_ENV, bad)
        out = oc.arm_for("s", "c")
        assert bad in out["holdout_env_ignored"]
        assert "using 10" in out["holdout_env_ignored"]

    def test_an_absent_variable_is_not_an_error(self):
        assert "holdout_env_ignored" not in oc.arm_for("s", "c")

    def test_a_blank_variable_is_treated_as_absent_not_as_an_error(self, monkeypatch):
        """Blank is how a shell writes "unset" by accident; it is not a wrong number."""
        monkeypatch.setenv(oc.HOLDOUT_ENV, "")
        assert "holdout_env_ignored" not in oc.arm_for("s", "c")

    def test_the_ledger_reports_both_variables(self, monkeypatch):
        monkeypatch.setenv(rl.FILE_HOLDOUT_ENV, "x")
        monkeypatch.setenv(rl.TURN_HOLDOUT_ENV, "y")
        why = rl.holdout("s", "k", 1)["holdout_env_ignored"]
        assert rl.FILE_HOLDOUT_ENV in why and rl.TURN_HOLDOUT_ENV in why


class TestTheAllocationTravelsWithTheDecision:
    """An arm without the percentage it was drawn at is not a measurement: an analysis
    spanning a 10% period and a 50% period has to be able to tell them apart."""

    def test_the_ledger_log_record_carries_the_percentages(self, tmp_path, monkeypatch):
        monkeypatch.setenv(rl.FILE_HOLDOUT_ENV, "50")
        f = tmp_path / "mod.py"
        f.write_text("x" * 8000, encoding="utf-8")
        led = rl.ReadLedger()
        led.record(str(f), tokens=2000)
        _, log = rl.hook_decision(led, "s", "Read", {"file_path": str(f)}, turn=3)
        assert log["file_holdout_pct"] == 50
        assert log["turn_holdout_pct"] == 3

    def test_the_cap_log_record_carries_the_percentage(self, monkeypatch):
        monkeypatch.setenv(oc.HOLDOUT_ENV, "50")
        assert oc.arm_for("s", "c")["holdout_pct"] == 50

    def test_a_mixed_analysis_can_separate_the_periods(self, monkeypatch):
        """The point of logging it: two records drawn at different allocations are
        distinguishable afterwards without anyone having to remember."""
        monkeypatch.setenv(oc.HOLDOUT_ENV, "10")
        early = oc.arm_for("s", "c")
        monkeypatch.setenv(oc.HOLDOUT_ENV, "50")
        late = oc.arm_for("s", "c")
        assert (early["holdout_pct"], late["holdout_pct"]) == (10, 50)
