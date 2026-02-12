#!/usr/bin/env python3
"""
attnroute.history — Turn History Viewer

Browse and filter the telemetry turn log with tier transitions,
file patterns, and summary statistics.

Usage:
  attnroute-history                     # Last 20 turns
  attnroute-history --since 2h          # Last 2 hours
  attnroute-history --file ppe          # Filter by file pattern
  attnroute-history --instance A        # Filter by instance
  attnroute-history --transitions       # Show only turns with tier changes
  attnroute-history --stats             # Show summary statistics
  attnroute-history --format json       # Output raw JSON
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

try:
    from attnroute.telemetry_lib import windows_utf8_io
    windows_utf8_io()
except ImportError:
    if sys.platform == "win32":
        for _s in (sys.stdout, sys.stderr):
            if hasattr(_s, "reconfigure"):
                try:
                    _s.reconfigure(encoding="utf-8")
                except Exception:
                    pass

HISTORY_FILE = Path.home() / ".claude" / "attention_history.jsonl"


def parse_duration(s: str) -> timedelta:
    """Parse '2h', '30m', '1d' into timedelta."""
    match = re.match(r'(\d+)([hdm])', s.lower())
    if not match:
        return timedelta(hours=1)
    val, unit = int(match.group(1)), match.group(2)
    if unit == 'h':
        return timedelta(hours=val)
    if unit == 'd':
        return timedelta(days=val)
    if unit == 'm':
        return timedelta(minutes=val)
    return timedelta(hours=1)


def load_history(
    since: timedelta = None,
    instance: str = None,
    file_pattern: str = None,
    transitions_only: bool = False
) -> list:
    """Load and filter history entries (TOCTOU-safe)."""
    cutoff = datetime.now() - since if since else None
    entries = []

    try:
        f = open(HISTORY_FILE, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []

    with f:
        for line in f:
            try:
                entry = json.loads(line.strip())

                # Time filter
                if cutoff:
                    try:
                        entry_time = datetime.fromisoformat(entry["timestamp"])
                        if entry_time < cutoff:
                            continue
                    except (ValueError, KeyError):
                        continue  # Skip entries with malformed timestamps

                # Instance filter
                if instance and entry.get("instance_id") != instance:
                    continue

                # File pattern filter
                if file_pattern:
                    all_files = entry.get("hot", []) + entry.get("warm", []) + entry.get("activated", [])
                    if not any(file_pattern.lower() in f.lower() for f in all_files):
                        continue

                # Transitions filter
                if transitions_only:
                    trans = entry.get("transitions", {})
                    if not any(trans.get(k) for k in ["to_hot", "to_warm", "to_cold"]):
                        continue

                entries.append(entry)
            except json.JSONDecodeError:
                continue

    return entries


def format_stats(entries: list) -> str:
    """Format summary statistics."""
    if not entries:
        return "No entries to analyze."

    W = 62
    lines = []

    def _section(title: str) -> str:
        pad = W - len(title) - 4
        left = pad // 2
        right = pad - left
        return f"{'─' * left}[ {title} ]{'─' * right}"

    lines.append("")
    lines.append(_section("ATTENTION STATISTICS"))
    lines.append("")

    # Basic counts
    lines.append(f"  Total turns: {len(entries)}")
    lines.append(f"  Time range:  {entries[0]['timestamp'][:10]} to {entries[-1]['timestamp'][:10]}")

    # Instance breakdown
    instances = Counter(e.get("instance_id", "default") for e in entries)
    if len(instances) > 1:
        lines.append(f"  Instances:   {dict(instances)}")

    # Most HOT files
    hot_counter = Counter()
    for entry in entries:
        for f in entry.get("hot", []):
            hot_counter[f] += 1

    lines.append("")
    lines.append(_section("MOST FREQUENTLY HOT"))
    for file, count in hot_counter.most_common(5):
        bar = "█" * min(count, 20)
        lines.append(f"  {count:3d} turns  {bar}  {Path(file).name}")

    # Most transitions
    transition_counter = Counter()
    for entry in entries:
        trans = entry.get("transitions", {})
        for f in trans.get("to_hot", []):
            transition_counter[f] += 1

    if transition_counter:
        lines.append("")
        lines.append(_section("MOST PROMOTED TO HOT"))
        for file, count in transition_counter.most_common(5):
            lines.append(f"  {count:3d} times  {Path(file).name}")

    # Daily activity
    daily_counter = Counter()
    for entry in entries:
        day = entry["timestamp"][:10]
        daily_counter[day] += 1

    lines.append("")
    lines.append(_section("DAILY ACTIVITY"))
    for day, count in sorted(daily_counter.items(), key=lambda x: x[1], reverse=True)[:5]:
        bar = "▓" * min(count, 30)
        lines.append(f"  {day}  {count:>3} turns  {bar}")

    # Average context size
    avg_chars = sum(e.get("total_chars", 0) for e in entries) / len(entries)
    lines.append("")
    lines.append(f"  Avg context: {avg_chars:,.0f} chars/turn")

    return "\n".join(lines)


def format_changelog(entries: list) -> str:
    """Format entries as human-readable changelog."""
    lines = []
    current_day = None

    for entry in entries:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
        except (ValueError, KeyError):
            continue  # Skip entries with malformed timestamps
        day = ts.strftime("%Y-%m-%d")

        if day != current_day:
            lines.append(f"\n{'─' * 62}")
            lines.append(f"  {day}")
            lines.append(f"{'─' * 62}")
            current_day = day

        time_str = ts.strftime("%H:%M:%S")
        instance = entry.get("instance_id", "?")
        turn = entry.get("turn", "?")

        lines.append(f"\n[{time_str}] Instance {instance} | Turn {turn}")

        # Keywords
        keywords = entry.get("prompt_keywords", [])
        if keywords:
            lines.append(f"  Query: {' '.join(keywords[:5])}")

        # HOT files
        hot = entry.get("hot", [])
        if hot:
            lines.append(f"  [HOT]  {', '.join(Path(f).name for f in hot)}")

        # WARM files
        warm = entry.get("warm", [])
        if warm:
            lines.append(f"  [WARM] {', '.join(Path(f).name for f in warm[:5])}" +
                        (f" (+{len(warm)-5} more)" if len(warm) > 5 else ""))

        # Transitions
        trans = entry.get("transitions", {})
        if trans.get("to_hot"):
            lines.append(f"    + promoted: {', '.join(Path(f).name for f in trans['to_hot'])}")
        if trans.get("to_cold"):
            lines.append(f"    - decayed:  {', '.join(Path(f).name for f in trans['to_cold'])}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="attnroute — Turn History Viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  attnroute-history                     # Last 20 turns
  attnroute-history --since 2h          # Last 2 hours
  attnroute-history --file ppe          # Filter by file pattern
  attnroute-history --transitions  # Only tier changes
  attnroute-history --stats        # Summary statistics
  attnroute-history --instance A   # Filter by instance
        """
    )
    parser.add_argument("--since", type=str, help="Time window (e.g., 2h, 30m, 1d)")
    parser.add_argument("--last", type=int, default=20, help="Last N entries (default: 20)")
    parser.add_argument("--instance", type=str, help="Filter by instance ID")
    parser.add_argument("--file", type=str, help="Filter by file pattern")
    parser.add_argument("--transitions", action="store_true", help="Show only turns with tier changes")
    parser.add_argument("--stats", action="store_true", help="Show summary statistics")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")

    args = parser.parse_args()

    since = parse_duration(args.since) if args.since else None
    entries = load_history(
        since=since,
        instance=args.instance,
        file_pattern=args.file,
        transitions_only=args.transitions
    )

    # Apply --last limit (unless --since specified)
    if not args.since:
        entries = entries[-args.last:]

    if not entries:
        print("No history entries found.")
        if not HISTORY_FILE.exists():
            print(f"\nHistory file not found: {HISTORY_FILE}")
            print("History tracking starts after first turn with updated router.")
        return

    if args.stats:
        print(format_stats(entries))
    elif args.format == "json":
        print(json.dumps(entries, indent=2))
    else:
        print(format_changelog(entries))
        print(f"\n[{len(entries)} entries]")


if __name__ == "__main__":
    main()
