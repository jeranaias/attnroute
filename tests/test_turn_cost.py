"""Turn read-cost instrumentation: observations only, and it must never cost a turn record.

Built against the real transcript shape, measured 2026-10-03 on a 311 MB session file:
tool_use blocks sit in `assistant` entries, their `tool_result` arrives in a LATER `user`
entry paired by `tool_use_id`, and the final `assistant` entry of a turn is followed only by
metadata -- so walking forward from it finds nothing. The first version of this module did
exactly that and reported ZERO reads on a session full of them.
"""

import json

from attnroute.turn_cost import (
    FIRST_TURN_TAIL_BYTES,
    MAX_SPAN_BYTES,
    read_span,
    turn_read_cost,
)


def _transcript(tmp_path, entries, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return p


def _use(tool, target, use_id):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": use_id, "name": tool,
                                 "input": {"file_path": target}}]},
    }


def _result(use_id, text):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id,
                                 "content": text}]},
    }


def _meta():
    """The metadata entries that follow a turn's last assistant block in a real transcript."""
    return {"type": "last-prompt"}


class TestReadCostIsCounted:
    def test_results_are_summed_and_paired(self, tmp_path):
        t = _transcript(tmp_path, [
            _use("Read", "nav/plan_gate.py", "u1"),
            _result("u1", "x" * 3300),
            _use("Read", "nav/depth_gate.py", "u2"),
            _result("u2", "y" * 660),
            _meta(),            # ⚠ the turn ends with metadata, not with the tool_result
        ])
        out = turn_read_cost(t, [], relationships={}, start_offset=0)

        assert out["tool_result_count"] == 2
        assert out["tool_read_chars"] == 3960
        assert out["tool_read_tokens_est"] > 0
        assert out["turn_cost_error"] is None

        # ⚠ PER-READ ATTRIBUTION. A per-turn total cannot support a per-file signal, and the
        #   weighted reward needs the cost of THIS read.
        by_target = {r["target"]: r["read_tokens_est"] for r in out["reads"]}
        assert by_target["nav/plan_gate.py"] > by_target["nav/depth_gate.py"] > 0

    def test_trailing_metadata_does_not_hide_the_turn(self, tmp_path):
        """The regression that motivated the rewrite: zeros on a session full of reads."""
        t = _transcript(tmp_path, [
            _use("Read", "a.py", "u1"),
            _result("u1", "z" * 1000),
            _meta(), _meta(), _meta(),
        ])
        out = turn_read_cost(t, [], relationships={}, start_offset=0)
        assert out["tool_read_chars"] == 1000, "the old shape reported 0 here"

    def test_a_result_with_list_content_is_counted(self, tmp_path):
        t = _transcript(tmp_path, [
            _use("Read", "a.py", "u1"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1",
                 "content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "de"}]}]}},
        ])
        out = turn_read_cost(t, [], relationships={}, start_offset=0)
        assert out["tool_read_chars"] == 5


class TestTheTwoKindsOfMiss:
    REL = {"systems/network.md": {"describes": ["nav/net.py"]}}

    def test_a_read_whose_doc_was_not_injected_is_a_ROUTING_MISS(self, tmp_path):
        t = _transcript(tmp_path, [_use("Read", "nav/net.py", "u1"), _result("u1", "q" * 500)])
        out = turn_read_cost(t, files_injected=[], relationships=self.REL, start_offset=0)

        assert len(out["routing_misses"]) == 1
        miss = out["routing_misses"][0]
        assert miss["would_have_routed"] == ["systems/network.md"]
        assert miss["read_tokens_est"] > 0, "the reward is token-weighted; an event is not enough"
        assert out["corpus_gaps"] == []

    def test_a_read_with_no_describing_doc_is_a_CORPUS_GAP(self, tmp_path):
        """Different remedy: write a doc, or nothing. Folding these in would teach the router
        to send documents that do not exist."""
        t = _transcript(tmp_path, [_use("Read", "nav/other.py", "u1"), _result("u1", "q" * 500)])
        out = turn_read_cost(t, files_injected=[], relationships=self.REL, start_offset=0)

        assert [g["read"] for g in out["corpus_gaps"]] == ["nav/other.py"]
        assert out["routing_misses"] == []

    def test_injected_and_read_is_a_DEPTH_signal(self, tmp_path):
        t = _transcript(tmp_path, [_use("Read", "nav/net.py", "u1"), _result("u1", "q" * 500)])
        out = turn_read_cost(t, files_injected=["systems/network.md"],
                             relationships=self.REL, start_offset=0)

        assert len(out["read_after_inject"]) == 1
        assert out["routing_misses"] == []

    def test_read_before_an_EDIT_is_not_a_depth_signal(self, tmp_path):
        """⚠ No outline prevents an edit, so reading in order to edit says nothing about tier."""
        t = _transcript(tmp_path, [
            _use("Read", "nav/net.py", "u1"), _result("u1", "q" * 500),
            _use("Edit", "nav/net.py", "u2"), _result("u2", "ok"),
        ])
        out = turn_read_cost(t, files_injected=["systems/network.md"],
                             relationships=self.REL, start_offset=0)
        assert out["read_after_inject"] == []


class TestTheWindowIsReportedNotHidden:
    def test_no_prior_offset_is_reported_incomplete(self, tmp_path):
        t = _transcript(tmp_path, [_use("Read", "a.py", "u1"), _result("u1", "x" * 10)])
        out = turn_read_cost(t, [], relationships={}, start_offset=None)
        assert out["turn_view_incomplete"] is True, "the first turn after install sees a tail"

    def test_a_prior_offset_is_complete(self, tmp_path):
        t = _transcript(tmp_path, [_use("Read", "a.py", "u1"), _result("u1", "x" * 10)])
        out = turn_read_cost(t, [], relationships={}, start_offset=0)
        assert out["turn_view_incomplete"] is False

    def test_the_new_offset_is_returned_so_the_next_turn_is_exact(self, tmp_path):
        t = _transcript(tmp_path, [_use("Read", "a.py", "u1"), _result("u1", "x" * 10)])
        out = turn_read_cost(t, [], relationships={}, start_offset=0)
        assert out["transcript_offset"] == t.stat().st_size

    def test_an_offset_past_the_end_falls_back_and_says_so(self, tmp_path):
        """A rotated or truncated transcript must not produce a negative span."""
        t = _transcript(tmp_path, [_use("Read", "a.py", "u1"), _result("u1", "x" * 10)])
        out = turn_read_cost(t, [], relationships={}, start_offset=10 ** 9)
        assert out["turn_view_incomplete"] is True
        assert out["turn_cost_error"] is None

    def test_read_span_bounds_a_pathological_span(self, tmp_path):
        p = tmp_path / "big.jsonl"
        p.write_bytes(b'{"type":"x"}\n' * 10)
        _, offset, complete = read_span(p, 0)
        assert complete is True and offset == p.stat().st_size
        assert MAX_SPAN_BYTES > FIRST_TURN_TAIL_BYTES


class TestItNeverCostsTheTurnRecord:
    def test_a_missing_transcript_reports_rather_than_raises(self, tmp_path):
        out = turn_read_cost(tmp_path / "nope.jsonl", [], relationships={})
        assert out["turn_cost_error"] == "transcript not found"
        assert out["tool_read_tokens_est"] == 0

    def test_an_empty_path_reports_rather_than_raises(self):
        out = turn_read_cost("", [], relationships={})
        assert out["turn_cost_error"] == "transcript not found"

    def test_unparseable_lines_are_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "bad.jsonl"
        p.write_text('{"type":"assistant"\nnot json at all\n'
                     + json.dumps(_use("Read", "a.py", "u1")) + "\n"
                     + json.dumps(_result("u1", "abcd")) + "\n", encoding="utf-8")
        out = turn_read_cost(p, [], relationships={}, start_offset=0)
        assert out["turn_cost_error"] is None
        assert out["tool_read_chars"] == 4

    def test_it_always_reports_its_own_cost(self, tmp_path):
        t = _transcript(tmp_path, [_use("Read", "a.py", "u1"), _result("u1", "x")])
        out = turn_read_cost(t, [], relationships={}, start_offset=0)
        assert "instrumentation_ms" in out and out["instrumentation_ms"] >= 0


def test_nothing_here_rewards_anything():
    """⚠ PR A is observations only. If this module ever imports the learner or the advisor,
    the gate that kept the inverted signal out of the reward path has been stepped around."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "attnroute" / "turn_cost.py").read_text(encoding="utf-8")
    for forbidden in ("from attnroute.advisor", "import advisor",
                      "trust_used_signal", "affinit"):
        assert forbidden not in src, f"turn_cost.py references {forbidden!r}"
