"""A latency budget for the hook path, gated in CI.

WHY THIS FILE EXISTS. attnroute runs as hooks: `UserPromptSubmit`, `Stop`, and with the read
ledger also `PreToolUse`/`PostToolUse`. Each event is a fresh interpreter. Measured before
the work this file guards:

    import attnroute                   5,670 ms   (eager re-export of compressor -> chromadb)
    attnroute.context_router           1,725 ms   (networkx 840 ms, indexer 540 ms)
    telemetry_record, one Stop event  15,419 ms   (burnrate re-parsed 300 MB from byte zero)

WHY THE BUDGET IS NOT 150 ms PER PROCESS. On the Windows box these were measured on, an
empty `python -c pass` takes 396-444 ms. A process-level budget below half a second would be
measuring interpreter startup, not this package, and it would fail on a loaded CI runner for
reasons no change here could fix. So the gate has two parts:

  * what we import, bounded in milliseconds with generous headroom;
  * what we PARSE, bounded as a proportion -- deterministic, and the property that actually
    made the Stop hook slow.

The proportional test is the real gate. A timing test on CI hardware is a coin flip; "the
scan skipped 90% of the file" is the same answer on every machine.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

#: Modules that cost hundreds of milliseconds and that no hook event needs up front.
FORBIDDEN_AT_IMPORT = ("chromadb", "networkx", "model2vec", "attnroute.indexer",
                       "attnroute.compressor", "opentelemetry")

#: Every module registered as a hook entry point.
HOOK_MODULES = ("attnroute.context_router", "attnroute.session_init",
                "attnroute.telemetry_record", "attnroute.read_ledger",
                "attnroute.session_state", "attnroute.output_cap")

#: Generous: the measured figures after the fix are 390 ms (context_router) and ~230 ms for
#: the others, and an empty interpreter is already ~400 ms of that on Windows.
IMPORT_BUDGET_MS = 1200


def fresh(code: str):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180)


@pytest.mark.parametrize("module", HOOK_MODULES)
def test_a_hook_module_imports_nothing_heavy(module):
    """In a FRESH interpreter: this one has already imported everything, so a check of its
    own `sys.modules` would pass no matter what the hook path does."""
    r = fresh(
        "import sys\n"
        f"import {module}\n"
        f"print('EAGER:' + ','.join(m for m in {FORBIDDEN_AT_IMPORT!r} if m in sys.modules))\n"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    line = [x for x in r.stdout.splitlines() if x.startswith("EAGER:")][0]
    assert line == "EAGER:", f"{module} eagerly imported {line[6:]}"


@pytest.mark.parametrize("module", HOOK_MODULES)
def test_a_hook_module_imports_within_budget(module):
    r = fresh(
        "import time\n"
        "t = time.perf_counter()\n"
        f"import {module}\n"
        "print('MS:%d' % ((time.perf_counter() - t) * 1000))\n"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    ms = int([x for x in r.stdout.splitlines() if x.startswith("MS:")][0][3:])
    assert ms < IMPORT_BUDGET_MS, f"importing {module} took {ms} ms"


# ---- what the Stop hook parses -------------------------------------------------------
def big_transcript(path, hours_span=40.0, lines=24_000, pad=500):
    """A transcript spanning far more time than the burn-rate window.

    The window is 5 hours of 40, so a scan that honours the window must ignore most of the
    file. Before the seek, every byte was parsed on every Stop event.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_span)
    step = timedelta(hours=hours_span) / lines
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(lines):
            fh.write(json.dumps({
                "type": "assistant", "timestamp": (start + step * i).isoformat(),
                "requestId": f"r{i}", "cwd": "/repo",
                "message": {"usage": {"input_tokens": 5, "output_tokens": 1,
                                      "cache_read_input_tokens": 100,
                                      "cache_creation_input_tokens": 1}},
                "pad": "p" * pad,
            }) + "\n")
    return path


def test_the_stop_hook_skips_the_bytes_outside_its_window(tmp_path):
    """THE REAL GATE, and it is proportional rather than timed.

    The burn-rate window is 5 hours. Given a 40-hour transcript, at most about an eighth of
    the file can be relevant, so the scan must start deep into it. This fails the moment
    anyone restores a scan from byte zero, on any hardware, with no flakiness.
    """
    from attnroute.plugins.burnrate import BurnRatePlugin

    plugin = BurnRatePlugin()
    p = big_transcript(tmp_path / "big.jsonl")
    size = p.stat().st_size
    assert size > plugin.SEEK_MIN_FILE_BYTES, (
        f"the fixture is only {size} bytes: raise `lines` or `pad` so the seek engages")

    window_start = datetime.now(timezone.utc) - timedelta(hours=plugin.WINDOW_HOURS)
    offset = plugin._window_first_byte(p, window_start)
    parsed = size - offset

    # The budget is stated from the two things that legitimately have to be read: the
    # window's own share of the file, and the backoff the search deliberately rewinds by.
    # Anything beyond that is waste. (The backoff is 4 MB and dominates a fixture this
    # size, which is why the gate is arithmetic rather than a flat percentage.)
    share = size * (plugin.WINDOW_HOURS / 40.0)
    budget = share + plugin.SEEK_BACKOFF_BYTES + plugin.SEEK_PROBE_BYTES
    assert parsed <= budget, (
        f"the scan would parse {parsed / 1e6:.1f} MB of a {size / 1e6:.1f} MB file for a "
        f"{plugin.WINDOW_HOURS}h window on a 40h transcript; budget is "
        f"{budget / 1e6:.1f} MB ({share / 1e6:.1f} MB window share + "
        f"{plugin.SEEK_BACKOFF_BYTES / 1e6:.1f} MB backoff + "
        f"{plugin.SEEK_PROBE_BYTES / 1e6:.2f} MB search granularity)")
    assert parsed < size * 0.9, "nothing was skipped at all"


def test_the_window_scan_still_finds_every_record_in_the_window(tmp_path):
    """Positive control for the test above: skipping bytes is only good if the records are
    all still there. Without this, `return size` would pass the gate and break the feature."""
    from attnroute.plugins.burnrate import BurnRatePlugin

    plugin = BurnRatePlugin()
    p = big_transcript(tmp_path / "big2.jsonl")
    window_start = datetime.now(timezone.utc) - timedelta(hours=plugin.WINDOW_HOURS)

    got = plugin._extract_usage_records(p, window_start, set())

    # Count what a full parse would have found, independently of the code under test.
    expected = 0
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts >= window_start:
                expected += 1
    assert expected > 100, "the fixture produced too few in-window records to prove anything"
    assert len(got) == expected, f"the seek found {len(got)} of {expected} in-window records"
