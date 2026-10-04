"""`attnroute savings`: savings come from transcript usage, groups come from the stream, and an
interval that cannot exclude zero says so."""

import json

from attnroute import savings, telemetry_stream


def _write_transcript(path, sid, turns, per_turn_cache_read, compact_after=None,
                      repeat_after_compact=False, dup_lines=False):
    lines = []
    for i in range(turns):
        if compact_after is not None and i == compact_after:
            lines.append({"type": "user", "isCompactSummary": True, "sessionId": sid})
        content = []
        if repeat_after_compact or i == 0:
            content.append({"type": "tool_use", "name": "Read",
                            "input": {"file_path": "/repo/a.py"}})
        msg = {"id": f"m{i}", "content": content,
               "usage": {"cache_read_input_tokens": per_turn_cache_read,
                         "cache_creation_input_tokens": 0, "input_tokens": 0,
                         "output_tokens": 0}}
        e = {"type": "assistant", "sessionId": sid, "message": msg}
        lines.append(e)
        if dup_lines:
            lines.append(e)          # streaming writes one message as several lines
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


class TestScan:
    def test_positive_control_units_are_weighted_usage(self, tmp_path):
        p = _write_transcript(tmp_path / "a.jsonl", "A", turns=10, per_turn_cache_read=1000)
        r = savings.scan_transcript(p)
        assert r["turns"] == 10
        assert r["units"] == 10 * 1000 * 0.1

    def test_streamed_duplicate_lines_count_once(self, tmp_path):
        p = _write_transcript(tmp_path / "a.jsonl", "A", turns=10, per_turn_cache_read=1000,
                              dup_lines=True)
        assert savings.scan_transcript(p)["turns"] == 10

    def test_compaction_splits_windows(self, tmp_path):
        p = _write_transcript(tmp_path / "a.jsonl", "A", turns=10, per_turn_cache_read=1,
                              compact_after=5)
        r = savings.scan_transcript(p)
        assert r["windows"] == 2 and r["compactions"] == 1

    def test_repeat_after_compaction_is_counted(self, tmp_path):
        p = _write_transcript(tmp_path / "a.jsonl", "A", turns=10, per_turn_cache_read=1,
                              compact_after=5, repeat_after_compact=True)
        assert savings.scan_transcript(p)["repeats"] == 1     # once per window, not per call

    def test_no_compaction_no_repeats(self, tmp_path):
        p = _write_transcript(tmp_path / "a.jsonl", "A", turns=10, per_turn_cache_read=1,
                              repeat_after_compact=True)
        assert savings.scan_transcript(p)["repeats"] == 0


def _stream(tmp_path, records):
    p = tmp_path / "stream.jsonl"
    p.write_text("\n".join(json.dumps({"v": 1, "component": "read_ledger", **r})
                           for r in records) + "\n", encoding="utf-8")
    return p


class TestAnalyse:
    def test_positive_control_a_cheaper_treated_group_shows_a_saving(self, tmp_path):
        ts = [_write_transcript(tmp_path / f"t{i}.jsonl", f"T{i}", 20, 600 + i) for i in range(4)]
        bs = [_write_transcript(tmp_path / f"b{i}.jsonl", f"B{i}", 20, 1000 + i) for i in range(4)]
        st = _stream(tmp_path, [{"session_id": f"T{i}", "acting": True} for i in range(4)])
        res = savings.analyse(transcripts=ts + bs, stream=st)
        assert res["groups"]["treated"]["sessions"] == 4
        assert 0.35 < res["saving_per_turn"] < 0.45
        lo, hi = res["ci95"]
        assert lo > 0                                    # established

    def test_observing_sessions_are_not_counted_as_treated(self, tmp_path):
        ts = [_write_transcript(tmp_path / "o.jsonl", "O", 5, 100)]
        st = _stream(tmp_path, [{"session_id": "O", "acting": False}])
        res = savings.analyse(transcripts=ts, stream=st)
        assert "treated" not in res["groups"] and res["groups"]["observed"]["sessions"] == 1

    def test_empty_group_is_not_checked_not_zero(self, tmp_path):
        bs = [_write_transcript(tmp_path / "b.jsonl", "B", 5, 100)]
        res = savings.analyse(transcripts=bs, stream=_stream(tmp_path, []))
        assert res["saving_per_turn"] is None
        assert "NOT CHECKED" in savings.render(res)

    def test_noisy_equal_groups_are_not_established(self, tmp_path):
        vals_t, vals_b = [500, 1500, 900, 1100], [1400, 600, 1000, 1000]
        ts = [_write_transcript(tmp_path / f"t{i}.jsonl", f"T{i}", 20, v)
              for i, v in enumerate(vals_t)]
        bs = [_write_transcript(tmp_path / f"b{i}.jsonl", f"B{i}", 20, v)
              for i, v in enumerate(vals_b)]
        st = _stream(tmp_path, [{"session_id": f"T{i}", "acting": True} for i in range(4)])
        out = savings.render(savings.analyse(transcripts=ts + bs, stream=st))
        assert "NOT ESTABLISHED" in out

    def test_estimates_are_labelled_as_estimates(self, tmp_path):
        bs = [_write_transcript(tmp_path / "b.jsonl", "B", 5, 100)]
        st = _stream(tmp_path, [{"session_id": "X", "acting": False, "arm": "ledger",
                                 "saved_tokens_est": 900}])
        out = savings.render(savings.analyse(transcripts=bs, stream=st))
        assert "ESTIMATES" in out and "not evidence" in out


class TestStream:
    def test_emit_round_trips_with_schema_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(telemetry_stream, "stream_path", lambda: tmp_path / "s.jsonl")
        telemetry_stream.emit("output_cap", "result", session_id="S", agent_id=None,
                              arm="cap", acting=False, chars=10)
        recs = list(telemetry_stream.read(tmp_path / "s.jsonl"))
        assert recs[0]["v"] == telemetry_stream.SCHEMA_VERSION
        assert recs[0]["component"] == "output_cap" and recs[0]["chars"] == 10

    def test_torn_line_is_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text('{"component": "x", "v": 1}\n{"torn\n', encoding="utf-8")
        assert len(list(telemetry_stream.read(p))) == 1
