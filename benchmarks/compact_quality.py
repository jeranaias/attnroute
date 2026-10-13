#!/usr/bin/env python3
"""Does compacting cost quality? Derive the baseline from transcript history.

⚠⚠ WHY A COST PROGRAMME NEEDS THIS AT ALL. Every lever in attnroute buys cheaper context, and
the bill is the only half that has ever been measured. "Without quality loss" has been asserted
on every one of them and counted on none. This measures the second half -- badly, but measurably.

⚠⚠ IT USES THE PRODUCTION COUNTER, NOT A COPY OF IT. `call_signature`, `LEDGER_AFTER` and
`LEDGER_SIGS` are imported from `attnroute.session_state`, so the historical baseline and the
live ledger cannot drift apart into two numbers that look comparable and are not. That mattered
here twice over. The first pass of this script used an unbounded signature set while the
implementation is bounded at 400, flattering the implementation by 6%; and it truncated a command
signature at 120 characters where `call_signature` truncates at 200, which conflated long commands
sharing a prefix and reported 10.4 repeats per compaction where the real counter reports 8.4 -- a
24% error, in a number with one decimal place and no way to tell it was wrong. A bound is passed
on the command line only to price that bound, never to measure a baseline the live ledger will
then be compared against.

⚠ AND THE NUMBER IS NOT A WASTE COUNT. Re-reading a file you are editing is a repeat and is
counted. The per-session spread below is nearly eightfold -- 2.4 to 18.7 repeats per compaction
between seats doing the same kind of work -- so a LEVEL says nothing about any of them. The signal
is a RISE, measured the same way on the same sessions, and the comparison that means most is one
session against its own earlier windows.

Streams each file and holds only a bounded window, because these transcripts run to hundreds of
megabytes and the machine they are read on is memory-constrained.

    python -m benchmarks.compact_quality          # at the live ledger's own bound
    python -m benchmarks.compact_quality 0        # unbounded, to price the bound
"""
from __future__ import annotations

import collections
import glob
import json
import os
import sys

from attnroute.session_state import LEDGER_AFTER, LEDGER_SIGS, call_signature

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def scan(path: str, bound: int) -> tuple[int, int, int]:
    """-> (compactions, repeats, calls). `bound` of 0 means unbounded."""
    seen: collections.OrderedDict[str, None] = collections.OrderedDict()
    after_left = 0
    compactions = repeats = calls = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"compact_boundary"' in line:
                compactions += 1
                after_left = LEDGER_AFTER
                continue
            if '"tool_use"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                sig = call_signature(block.get("name", ""), block.get("input"))
                calls += 1
                if after_left > 0:
                    after_left -= 1
                    if sig in seen:
                        repeats += 1
                seen.pop(sig, None)
                seen[sig] = None
                if bound and len(seen) > bound:
                    seen.popitem(last=False)
    return compactions, repeats, calls


def main() -> int:
    bound = int(sys.argv[1]) if len(sys.argv) > 1 else LEDGER_SIGS
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    files = sorted(glob.glob(os.path.join(root, "*", "*.jsonl")),
                   key=os.path.getsize, reverse=True)[:6]
    print("=" * 78)
    print("RE-DERIVATION ACROSS A COMPACT BOUNDARY")
    print(f"first {LEDGER_AFTER} calls after each compaction; "
          f"signature memory {bound or 'UNBOUNDED'}")
    print("=" * 78)
    print(f"{'transcript':<40} {'compac':>6} {'calls':>8} {'repeats':>8} "
          f"{'per compac':>9}")
    total_c = total_r = 0
    for path in files:
        try:
            compactions, repeats, calls = scan(path, bound)
        except OSError:
            continue
        if not compactions:
            continue
        total_c += compactions
        total_r += repeats
        print(f"{os.path.basename(path)[:40]:<40} {compactions:>6} {calls:>8} "
              f"{repeats:>8} {repeats / compactions:>9.1f}")
    print("-" * 78)
    if not total_c:
        print("NO COMPACTIONS FOUND -- a zero here is a broken reader, not a clean fleet.")
        return 2
    print(f"{'ALL':<40} {total_c:>6} {'':>8} {total_r:>8} {total_r / total_c:>9.1f}")
    print(f"\nBASELINE: {total_r / total_c:.1f} repeated call(s) per compaction, "
          f"{total_r / (total_c * LEDGER_AFTER):.3f} per counted call.")
    print("\n⚠ A level means nothing on its own -- re-reading a file you are editing counts.")
    print("  The signal is a RISE, measured the same way on the same sessions, and the")
    print("  comparison that means most is one session against its own earlier windows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
