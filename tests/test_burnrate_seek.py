"""The window seek must give the SAME records as parsing from byte zero.

`_extract_usage_records` used to read every transcript from the first byte on every Stop
event: profiled at 9.9 s of tottime over four files and 65,378 `json.loads`. It now binary
searches for the window's first byte.

A faster scan that returns a different answer is worse than a slow one, so the test that
matters here is EQUIVALENCE, measured against the old behaviour -- which is still reachable
by raising the file-size threshold above the file under test.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from attnroute.plugins.burnrate import BurnRatePlugin

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)

#: Small enough to keep the test quick, large enough to make the search take several hops.
SMALL_MIN = 40_000
SMALL_BACKOFF = 8_000


def write_transcript(path, hours_span=10.0, lines=1200, pad=400):
    """An append-only transcript in time order, with usage on the assistant lines.

    `pad` exists to push the file past the size threshold without needing many lines, and
    mirrors the real shape: most bytes in a transcript are not usage records.
    """
    start = NOW - timedelta(hours=hours_span)
    step = timedelta(hours=hours_span) / max(1, lines)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(lines):
            ts = (start + step * i).isoformat()
            if i % 3 == 1:
                # A line with no timestamp at all: the search must cope with these.
                fh.write(json.dumps({"type": "user", "content": "x" * pad}) + "\n")
                continue
            fh.write(json.dumps({
                "type": "assistant", "timestamp": ts, "requestId": f"req{i}",
                "cwd": "/repo",
                "message": {"usage": {"input_tokens": 10, "output_tokens": 2,
                                      "cache_read_input_tokens": 1000,
                                      "cache_creation_input_tokens": 5}},
                "filler": "y" * pad,
            }) + "\n")
    return path


@pytest.fixture
def transcript(tmp_path):
    return write_transcript(tmp_path / "session.jsonl")


@pytest.fixture
def plugin():
    return BurnRatePlugin()


def scan(plugin, path, window_start):
    return plugin._extract_usage_records(path, window_start, set())


def with_seek(monkeypatch, plugin):
    monkeypatch.setattr(plugin, "SEEK_MIN_FILE_BYTES", SMALL_MIN, raising=False)
    monkeypatch.setattr(plugin, "SEEK_BACKOFF_BYTES", SMALL_BACKOFF, raising=False)


def without_seek(monkeypatch, plugin):
    """The OLD behaviour: a threshold above the file size means no seek at all."""
    monkeypatch.setattr(plugin, "SEEK_MIN_FILE_BYTES", 10 ** 12, raising=False)


@pytest.mark.parametrize("hours", [0.5, 2.0, 5.0, 9.0, 12.0])
def test_the_seek_returns_exactly_what_a_full_scan_returns(
        monkeypatch, plugin, transcript, hours):
    window_start = NOW - timedelta(hours=hours)

    without_seek(monkeypatch, plugin)
    full = scan(plugin, transcript, window_start)

    with_seek(monkeypatch, plugin)
    seeked = scan(plugin, transcript, window_start)

    assert [r["timestamp"] for r in seeked] == [r["timestamp"] for r in full]
    assert seeked == full


def test_the_seek_actually_skips_bytes(monkeypatch, plugin, transcript):
    """Positive control. Without this, the equivalence test above would also pass if the
    seek silently always returned 0 -- which is to say, if the fix did nothing."""
    with_seek(monkeypatch, plugin)
    offset = plugin._window_first_byte(transcript, NOW - timedelta(hours=1))
    assert offset > 0, "the search returned byte zero, so nothing was skipped"
    assert offset < transcript.stat().st_size


def test_a_small_file_is_never_seeked(plugin, tmp_path):
    """Below the threshold there is nothing to gain, and the real constant applies here."""
    p = write_transcript(tmp_path / "tiny.jsonl", lines=20, pad=10)
    assert p.stat().st_size < plugin.SEEK_MIN_FILE_BYTES
    assert plugin._window_first_byte(p, NOW - timedelta(hours=1)) == 0


def test_a_window_that_covers_everything_starts_at_zero(monkeypatch, plugin, transcript):
    with_seek(monkeypatch, plugin)
    assert plugin._window_first_byte(transcript, NOW - timedelta(days=30)) == 0


def test_an_unreadable_file_answers_zero_rather_than_raising(plugin, tmp_path):
    """A measurement must never cost the user their turn."""
    assert plugin._window_first_byte(tmp_path / "nope.jsonl", NOW) == 0


def test_a_file_of_lines_with_no_timestamps_answers_zero(monkeypatch, plugin, tmp_path):
    """The search cannot narrow without timestamps, so it must fall back to the old
    behaviour (parse everything) rather than guess an offset."""
    p = tmp_path / "notime.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for i in range(2000):
            fh.write(json.dumps({"type": "user", "content": "z" * 400}) + "\n")
    with_seek(monkeypatch, plugin)
    assert plugin._window_first_byte(p, NOW - timedelta(hours=1)) == 0


def test_records_before_the_window_are_still_filtered_after_a_seek(
        monkeypatch, plugin, transcript):
    """The seek is approximate by design: it rewinds by a backoff, so lines older than the
    window DO get parsed. They must not get counted."""
    with_seek(monkeypatch, plugin)
    window_start = NOW - timedelta(hours=1)
    for r in scan(plugin, transcript, window_start):
        ts = plugin._parse_timestamp(r["timestamp"])
        assert ts >= window_start, f"a record from {ts} slipped into a 1-hour window"


def write_jittered(path, hours_span=10.0, lines=1200, pad=400, jitter_lines=12):
    """A transcript whose timestamps go slightly BACKWARDS now and then.

    Real transcripts are written by several processes and are only near-monotonic. This is
    what SEEK_BACKOFF_BYTES is for, and without a file like this the backoff is untested:
    removing it leaves every other test in this module passing.
    """
    start = NOW - timedelta(hours=hours_span)
    step = timedelta(hours=hours_span) / max(1, lines)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(lines):
            back = jitter_lines if i % 17 == 0 else 0
            ts = (start + step * max(0, i - back)).isoformat()
            fh.write(json.dumps({
                "type": "assistant", "timestamp": ts, "requestId": f"req{i}",
                "cwd": "/repo",
                "message": {"usage": {"input_tokens": 10, "output_tokens": 2,
                                      "cache_read_input_tokens": 1000,
                                      "cache_creation_input_tokens": 5}},
                "filler": "y" * pad,
            }) + "\n")
    return path


@pytest.mark.parametrize("hours", [1.0, 3.0, 5.0])
def test_out_of_order_timestamps_do_not_lose_records(monkeypatch, plugin, tmp_path, hours):
    p = write_jittered(tmp_path / "jitter.jsonl")
    window_start = NOW - timedelta(hours=hours)

    without_seek(monkeypatch, plugin)
    full = scan(plugin, p, window_start)

    with_seek(monkeypatch, plugin)
    seeked = scan(plugin, p, window_start)

    assert len(seeked) == len(full), (
        f"the seek lost {len(full) - len(seeked)} of {len(full)} records to timestamp jitter")
    assert seeked == full
