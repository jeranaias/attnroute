"""OBSERVE-ONLY computes and logs the injection and emits nothing.

⚠ WHY THIS MODE EXISTS. "Recording-only" was described as "changes no behaviour; it just starts
collecting numbers", and that was not true of the package's default state -- the router still
injected. On eight long-running sessions that is new context in every prompt, unmeasured.

A session in this mode is an honest 100%-held-out baseline arm: the record states what WOULD
have been sent, so it is comparable with an injecting session, and `injection_emitted` is what
distinguishes them.
"""

import io
import json

import pytest

from attnroute import context_router as cr
from attnroute.used_signal import OBSERVE_ONLY_ENV, observe_only


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(OBSERVE_ONLY_ENV, raising=False)


class TestTheFlag:
    def test_default_is_off(self):
        assert observe_only() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_opt_in_values(self, monkeypatch, value):
        monkeypatch.setenv(OBSERVE_ONLY_ENV, value)
        assert observe_only() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "later"])
    def test_everything_else_is_off(self, monkeypatch, value):
        monkeypatch.setenv(OBSERVE_ONLY_ENV, value)
        assert observe_only() is False

    def test_read_per_call(self, monkeypatch):
        assert observe_only() is False
        monkeypatch.setenv(OBSERVE_ONLY_ENV, "1")
        assert observe_only() is True
        monkeypatch.delenv(OBSERVE_ONLY_ENV)
        assert observe_only() is False

    def test_nothing_in_the_package_sets_it(self):
        """A mode the package turns on for itself is not a mode the user chose."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "attnroute"
        offenders = []
        for py in root.rglob("*.py"):
            for line in py.read_text(encoding="utf-8", errors="replace").split("\n"):
                if OBSERVE_ONLY_ENV in line and ("setenv" in line or "environ[" in line):
                    offenders.append(f"{py.name}: {line.strip()}")
        assert not offenders, offenders


class TestTheRecordDistinguishesTheArms:
    """`record_turn_telemetry` must carry whether the injection LANDED.

    The fields describing what was computed stay identical across arms -- that is what makes
    the comparison possible -- so a flag is the only thing that can tell them apart.
    """

    def _record(self, tmp_path, monkeypatch, **kwargs):
        # ⚠ `record_turn_telemetry` derives its path INSIDE the function from `Path.home()`,
        #   with no seam -- the same shape that leaves 43% of this package untested (#8). So the
        #   redirect patches `Path.home`, and the GUARD below refuses to run if it did not take:
        #   an unredirected call would write into the developer's real telemetry.
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: tmp_path))
        assert cr.Path.home() == tmp_path, "redirect failed; refusing to touch real telemetry"
        turns = tmp_path / ".claude" / "telemetry" / "turns.jsonl"
        cr.record_turn_telemetry(
            prompt="depth gate",
            was_notification=False,
            stats={"hot": 1, "warm": 0, "cold": 0},
            activated=set(),
            # `record_turn_telemetry` reads state["scores"] to split hot/warm, so an
            # incomplete state makes it swallow the write in its own try/except.
            state={"turn_count": 1, "scores": {"systems/network.md": 0.9}},
            injection_chars=400,
            **kwargs,
        )
        assert turns.exists(), f"no turn record written to {turns}"
        return json.loads(turns.read_text(encoding="utf-8").strip().split("\n")[-1])

    def test_default_records_an_emitted_injection(self, tmp_path, monkeypatch):
        rec = self._record(tmp_path, monkeypatch)
        assert rec["injection_emitted"] is True
        assert rec["observe_only"] is False

    def test_observe_only_records_a_suppressed_injection(self, tmp_path, monkeypatch):
        rec = self._record(tmp_path, monkeypatch,
                           injection_emitted=False, observe_only_mode=True)
        assert rec["injection_emitted"] is False
        assert rec["observe_only"] is True
        # ⚠ AND THE COMPUTED FIGURES SURVIVE: a suppressed turn must still say what it would
        #   have cost, or there is no baseline to compare against.
        assert rec["injection_chars"] == 400
        assert rec["hot_count"] == 1


def test_the_notice_goes_to_stderr_not_stdout():
    """⚠ stdout IS the injection channel.

    A notice printed there would itself become context -- the exact thing this mode exists to
    prevent -- so the source must write the observe-only line to stderr.
    """
    src = open(cr.__file__, encoding="utf-8").read()
    idx = src.index("OBSERVE-ONLY:")
    window = src[idx:idx + 400]
    assert "file=sys.stderr" in window, "the observe-only notice must not go to stdout"


def test_suppression_is_the_only_thing_observe_only_changes():
    """Everything before the emission must still run, or the record is not a baseline.

    Asserted structurally: the mode is read at the OUTPUT site, after scoring and selection,
    and not at the top of main() where it could short-circuit the computation.
    """
    src = open(cr.__file__, encoding="utf-8").read()
    at_output = src.index("_observe_only = observe_only()")
    at_record = src.index("record_turn_telemetry(prompt, was_notification")
    assert at_output < at_record, "the mode must be resolved before the record is written"
    # and the scoring phases precede it
    assert src.index("Phase 4: Pinned file floor") < at_output


class TestItReallySuppressesStdout:
    """⚠ THE TESTS THAT PROVE THE MODE WORKS.

    Everything above checks flags, fields and source structure, and none of it would fail if
    the suppression were removed. These call the emit decision directly and compare stdout.
    """

    OUT = "<!-- attnroute -->" + chr(10) + "some injected context" + chr(10)
    STATS = {"hot": 1, "warm": 2, "cold": 0}

    def test_default_emits_to_stdout(self, capsys):
        emitted = cr.emit_injection(self.OUT, self.STATS, observe_only_mode=False)
        captured = capsys.readouterr()
        assert emitted is True
        assert self.OUT.strip() in captured.out, "the control arm emitted nothing"

    def test_observe_only_emits_nothing_to_stdout(self, capsys):
        emitted = cr.emit_injection(self.OUT, self.STATS, observe_only_mode=True)
        captured = capsys.readouterr()
        assert emitted is False
        assert captured.out == "", f"stdout was not suppressed: {captured.out[:200]!r}"
        # ⚠ and it announced itself on stderr, which is NOT the injection channel
        assert "OBSERVE-ONLY" in captured.err
        assert "1 hot + 2 warm" in captured.err, "the notice must state what was withheld"

    def test_nothing_selected_emits_nothing_either_way(self, capsys):
        """Boundary: an empty selection was never emitted, so the mode changes nothing here."""
        for mode in (False, True):
            assert cr.emit_injection("", {"hot": 0, "warm": 0}, observe_only_mode=mode) is False
            assert capsys.readouterr().out == ""
