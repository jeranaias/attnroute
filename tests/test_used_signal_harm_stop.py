"""The injected-vs-used ratio must not drive a reward, a penalty or a recommendation.

Why these exist, measured 2026-10-03 against `telemetry_record.compute_files_used`:

    "A file is 'used' if any tool call targeted a file that the .md doc describes."

`used` therefore needs a tool call on the subject AFTER the document was injected -- and
injection exists to make that tool call unnecessary. The two are substitutive, so a working
router drives the ratio toward 100% "waste". Issue #10's field report (1,706 injections, 100
accesses, zero intersection) is the expected output of that definition, not a mis-routing.

Two behaviours acted on it: the advisor recommended removing the files whose injection had
worked, and the learner penalised them. Both are gated OFF by default.

⚠ EVERY TEST HERE REDIRECTS `LEARNED_STATE_FILE`. Both classes bind the user's real
  ~/.claude/telemetry/learned_state.json at module scope with no injection point, so an
  un-redirected test would read and WRITE the developer's own learned state.
"""

import pytest

from attnroute import advisor as advisor_mod
from attnroute import learner as learner_mod
from attnroute.used_signal import ENV_VAR, WHY_NOT_TRUSTED, trust_used_signal


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No inherited flag, and no access to the real learned state."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    state = tmp_path / "learned_state.json"
    monkeypatch.setattr(advisor_mod, "LEARNED_STATE_FILE", state, raising=False)
    monkeypatch.setattr(learner_mod, "LEARNED_STATE_FILE", state, raising=False)
    return state


def _wasteful_turns(n=12):
    """Turns where a file was injected every time and never 'used' -- the SUCCESS case."""
    return [{"files_injected": ["systems/network.md"], "files_used": []} for _ in range(n)]


class TestTheFlagItself:
    def test_default_is_not_trusted(self):
        assert trust_used_signal() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_opt_in_values(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert trust_used_signal() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_everything_else_is_not_trusted(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert trust_used_signal() is False

    def test_it_is_read_per_call_not_cached_at_import(self, monkeypatch):
        """A hook process is long-lived; a cached flag could not be changed in place."""
        assert trust_used_signal() is False
        monkeypatch.setenv(ENV_VAR, "1")
        assert trust_used_signal() is True
        monkeypatch.delenv(ENV_VAR)
        assert trust_used_signal() is False

    def test_the_reason_is_available_for_a_surface_to_print(self):
        assert WHY_NOT_TRUSTED and "substitutive" in WHY_NOT_TRUSTED


class TestAdvisorDoesNotRecommendRemoval:
    def test_no_high_waste_suggestion_by_default(self):
        """⚠ THE ROW THAT FAILS IF THE GATE IS REMOVED.

        Twelve injections with nothing 'used' is a waste ratio of 1.0 -- above the 0.9
        threshold and past the 10-injection minimum -- so the ungated code emitted
        "Consider removing systems/network.md from keywords.json".
        """
        assert advisor_mod.ClaudeMdAdvisor()._find_high_waste(_wasteful_turns()) == []

    def test_it_does_fire_when_explicitly_trusted(self, monkeypatch):
        """Positive control: the GATE suppresses it, not a broken fixture.

        Without this, a fixture that produced no suggestions for an unrelated reason would
        make the test above pass while proving nothing.
        """
        monkeypatch.setenv(ENV_VAR, "1")
        out = advisor_mod.ClaudeMdAdvisor()._find_high_waste(_wasteful_turns())
        assert out, "the fixture must be capable of producing a suggestion"
        assert out[0]["type"] == "high_waste"
        assert "systems/network.md" in out[0]["action"]

    def test_below_the_threshold_nothing_fires_either_way(self, monkeypatch):
        """Boundary: fewer than 10 injections was never enough data."""
        monkeypatch.setenv(ENV_VAR, "1")
        assert advisor_mod.ClaudeMdAdvisor()._find_high_waste(_wasteful_turns(9)) == []


class TestLearnerDoesNotPenaliseSuccess:
    def _turns(self):
        return [
            {
                # ⚠ `prompt_keywords`, a LIST, is what the method reads -- not `prompt`.
                # A fixture with "prompt": "..." made the method `continue` immediately, so
                # BOTH arms of this class passed while nothing ran. The positive control
                # below is what caught it.
                "prompt_keywords": ["depth", "gate"],
                "files_injected": ["systems/network.md"],
                "files_used": [],
                "turn_id": f"t{i}",
            }
            for i in range(5)
        ]

    def _affinity_after(self):
        learner = learner_mod.Learner()
        learner.state["prompt_file_affinity"] = {"depth": {"systems/network.md": 0.5}}
        learner._learn_prompt_associations(self._turns())
        return learner.state["prompt_file_affinity"].get("depth", {}).get("systems/network.md")

    def test_affinity_is_not_reduced_by_default(self):
        """⚠ An injected-but-not-'used' file is the SUCCESS case. Its affinity must hold."""
        after = self._affinity_after()
        assert after == pytest.approx(0.5), f"affinity moved with the penalty gated off: {after!r}"

    def test_the_penalty_does_apply_when_explicitly_trusted(self, monkeypatch):
        """Positive control: the gate is doing the work, and the fixture can move the number."""
        monkeypatch.setenv(ENV_VAR, "1")
        after = self._affinity_after()
        assert after is None or after < 0.5, (
            f"the fixture cannot drive the penalty, so the test above proves nothing: {after!r}"
        )


def test_nothing_in_the_package_sets_the_flag_for_the_user():
    """A flag the package turns on for itself is not a flag."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "attnroute"
    offenders = []
    for py in root.rglob("*.py"):
        for line in py.read_text(encoding="utf-8", errors="replace").split("\n"):
            if ENV_VAR in line and ("setenv" in line or "environ[" in line):
                offenders.append(f"{py.name}: {line.strip()}")
    assert not offenders, offenders


def test_the_isolation_fixture_really_redirects(_isolated):
    """Guard on the guard: if this ever points at a real home directory, stop."""
    assert ".claude" not in str(_isolated), (
        f"tests would touch the user's real learned state: {_isolated}"
    )
    assert advisor_mod.LEARNED_STATE_FILE == _isolated
    assert learner_mod.LEARNED_STATE_FILE == _isolated
