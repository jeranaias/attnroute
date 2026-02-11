#!/usr/bin/env python3
"""
attnroute.telemetry_report — Token Efficiency Analysis

On-demand reporting: waste ratios, dollar cost, session efficiency,
trend sparklines, file leaderboards, and optimization history.

Usage: attnroute-report [--days 7] [--project NAME] [--cost] [--sessions] [--trend] [--all]
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    from attnroute import __version__ as VERSION
except ImportError:
    VERSION = "?"

try:
    from attnroute.telemetry_lib import (
        OPTIMIZATION_LOG_FILE,
        load_router_overrides,
        load_stats_cache,
        load_turns,
        windows_utf8_io,
    )
    windows_utf8_io()
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from telemetry_lib import (
            OPTIMIZATION_LOG_FILE,
            load_router_overrides,
            load_stats_cache,
            load_turns,
            windows_utf8_io,
        )
        windows_utf8_io()
    except ImportError:
        print("ERROR: telemetry_lib.py not found", file=sys.stderr)
        sys.exit(1)


# ============================================================================
# FORMATTING HELPERS
# ============================================================================

W = 70  # Report width


def section(title: str):
    """Print a section header with consistent width."""
    pad = W - len(title) - 4
    left = pad // 2
    right = pad - left
    print(f"{'─' * left}[ {title} ]{'─' * right}")


def divider():
    print("─" * W)


# ============================================================================
# PRICING TABLE (per 1M tokens)
# ============================================================================

PRICING = {
    "claude-opus-4-5-20251101":    {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75, "short": "Opus 4.5"},
    "claude-sonnet-4-20250514":    {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75, "short": "Sonnet 4"},
    "claude-3-5-sonnet-20241022":  {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75, "short": "Sonnet 3.5"},
    "claude-3-5-haiku-20241022":   {"input":  0.80, "output":  4.00, "cache_read": 0.08, "cache_write":  1.00, "short": "Haiku 3.5"},
}

DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "short": "unknown"}


def get_model_pricing(model_id: str) -> dict:
    """Get pricing for a model ID, with fuzzy matching."""
    if model_id in PRICING:
        return PRICING[model_id]
    # Fuzzy match
    model_lower = model_id.lower()
    for key, pricing in PRICING.items():
        if key.split("-")[1] in model_lower:
            return pricing
    return DEFAULT_PRICING


# ============================================================================
# REPORTS
# ============================================================================

def report_burn_rate(stats: dict, days: int):
    """Token burn rate from stats-cache.json."""
    section("TOKEN BURN RATE")

    daily = stats.get("dailyModelTokens", [])
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [d for d in daily if d.get("date", "") >= cutoff]

    if not recent:
        print("  No data in range")
        return

    total_by_model = Counter()
    for d in recent:
        for model, tokens in d.get("tokensByModel", {}).items():
            total_by_model[model] += tokens

    total = sum(total_by_model.values())
    n_days = len(recent)
    avg_daily = total / n_days if n_days else 0

    print(f"  Period: last {days} days ({n_days} active days)")
    for model, tok in total_by_model.most_common():
        short = model.split("-")[1] if "-" in model else model
        print(f"  {short}: {tok:>12,} tokens ({tok/1e6:.1f}M)")
    print(f"  Total:  {total:>12,} tokens")
    print(f"  Avg/day: {avg_daily:,.0f} ({avg_daily/1e6:.1f}M)")
    print(f"  30d projection: {avg_daily*30/1e6:.0f}M tokens")
    print()

    # Cache efficiency
    model_usage = stats.get("modelUsage", {})
    for model, usage in model_usage.items():
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_write = usage.get("cacheCreationInputTokens", 0)
        input_tok = usage.get("inputTokens", 0)
        output_tok = usage.get("outputTokens", 0)
        total_input = input_tok + cache_read + cache_write
        if total_input > 0:
            cache_pct = cache_read / total_input * 100
            short = model.split("-")[1] if "-" in model else model
            print(f"  {short} cache hit: {cache_pct:.1f}% ({cache_read/1e9:.1f}B read / {cache_write/1e9:.1f}B written)")
            print(f"  {short} I/O: {input_tok/1e6:.1f}M in / {output_tok/1e6:.1f}M out")
    print()


def report_waste(turns: list):
    """Waste analysis from turns.jsonl."""
    section("WASTE ANALYSIS")

    if not turns:
        print("  No turn data")
        return

    waste_ratios = [t["waste_ratio"] for t in turns if t.get("waste_ratio", -1) >= 0]
    notif_turns = [t for t in turns if t.get("was_notification")]
    normal_turns = [t for t in turns if not t.get("was_notification")]

    print(f"  Total turns recorded: {len(turns)}")
    print(f"  Notifications: {len(notif_turns)} ({len(notif_turns)/len(turns)*100:.0f}%)")
    print(f"  Normal turns: {len(normal_turns)}")

    if waste_ratios:
        avg = sum(waste_ratios) / len(waste_ratios)
        print(f"  Avg waste ratio: {avg:.1%} (target: <40%)")
    print()

    # Injection size distribution (chars and tokens)
    inj_sizes = [t.get("injection_chars", 0) for t in normal_turns]
    tok_sizes = [t.get("injection_tokens_est", 0) for t in normal_turns]
    total_ctx = [t.get("total_context_tokens_est", 0) for t in normal_turns]
    claude_md = [t.get("claude_md_tokens_est", 0) for t in normal_turns]

    if inj_sizes:
        nonzero = [s for s in inj_sizes if s > 0]
        zero = len(inj_sizes) - len(nonzero)
        print(f"  Zero-injection turns: {zero}/{len(inj_sizes)} ({zero/len(inj_sizes)*100:.0f}%)")
        if nonzero:
            avg_tok = sum(t for t in tok_sizes if t > 0) / len([t for t in tok_sizes if t > 0]) if any(t > 0 for t in tok_sizes) else sum(nonzero)/len(nonzero)/3.3
            print(f"  Avg injection: {sum(nonzero)/len(nonzero):,.0f} chars (~{avg_tok:,.0f} tokens)")
            print(f"  Max injection: {max(nonzero):,} chars")

        # CLAUDE.md overhead
        nonzero_md = [t for t in claude_md if t > 0]
        if nonzero_md:
            avg_md = sum(nonzero_md) / len(nonzero_md)
            print(f"  CLAUDE.md overhead: ~{avg_md:,.0f} tokens/turn (always injected by Claude Code)")

        # Total context
        nonzero_ctx = [t for t in total_ctx if t > 0]
        if nonzero_ctx:
            avg_ctx = sum(nonzero_ctx) / len(nonzero_ctx)
            print(f"  Total context per turn: ~{avg_ctx:,.0f} tokens (attnroute + CLAUDE.md + pool)")
    print()


def report_file_leaderboard(turns: list):
    """Files ranked by waste."""
    section("FILE WASTE LEADERBOARD")

    inject_count = Counter()
    use_count = Counter()
    for t in turns:
        for f in t.get("files_injected", []):
            inject_count[f] += 1
        for f in t.get("files_used", []):
            use_count[f] += 1

    if not inject_count:
        print("  No injection data")
        return

    ranked = sorted(
        inject_count.items(),
        key=lambda x: x[1] - use_count.get(x[0], 0),
        reverse=True
    )

    print(f"  {'File':<40} {'Injected':>8} {'Used':>6} {'Waste':>6}")
    print(f"  {'-'*40} {'-'*8} {'-'*6} {'-'*6}")
    for f, inj in ranked[:15]:
        used = use_count.get(f, 0)
        waste = inj - used
        print(f"  {f:<40} {inj:>8} {used:>6} {waste:>6}")
    print()


def report_project_breakdown(turns: list):
    """Per-project stats."""
    section("PROJECT BREAKDOWN")

    project_turns = Counter()
    project_waste = {}
    project_tokens = defaultdict(list)
    for t in turns:
        proj = t.get("project", "unknown")
        short = proj.replace("c:\\", "").replace("c:/", "").split("\\")[-1].split("/")[-1]
        project_turns[short] += 1
        if short not in project_waste:
            project_waste[short] = []
        wr = t.get("waste_ratio", -1)
        if wr >= 0:
            project_waste[short].append(wr)
        ctx_tok = t.get("total_context_tokens_est", 0)
        if ctx_tok > 0:
            project_tokens[short].append(ctx_tok)

    print(f"  {'Project':<20} {'Turns':>6} {'Avg Waste':>10} {'Avg Tok':>10}")
    print(f"  {'-'*20} {'-'*6} {'-'*10} {'-'*10}")
    for proj, count in project_turns.most_common():
        ratios = project_waste.get(proj, [])
        avg = sum(ratios) / len(ratios) if ratios else -1
        waste_str = f"{avg:.1%}" if avg >= 0 else "n/a"
        toks = project_tokens.get(proj, [])
        tok_str = f"{sum(toks)/len(toks):,.0f}" if toks else "n/a"
        print(f"  {proj:<20} {count:>6} {waste_str:>10} {tok_str:>10}")
    print()


def report_optimization_history():
    """Timeline of parameter changes."""
    section("OPTIMIZATION HISTORY")

    if not OPTIMIZATION_LOG_FILE.exists():
        print("  No optimization runs yet")
        print()
        return

    try:
        entries = []
        with open(OPTIMIZATION_LOG_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not entries:
            print("  No optimization entries")
        else:
            for e in entries[-10:]:
                ts = e.get("timestamp", "")[:19]
                metrics = e.get("metrics", {})
                new = e.get("new", {})
                prev = e.get("previous", {})
                changes = []
                for k in new:
                    if new[k] != prev.get(k):
                        changes.append(f"{k}:{prev.get(k,'-')}->{new[k]}")
                print(f"  [{ts}] waste={metrics.get('avg_waste','?')} | {', '.join(changes) or 'no change'}")
    except Exception as e:
        print(f"  Error reading log: {e}")
    print()


def report_current_overrides():
    """Current auto-tuned parameters."""
    section("CURRENT ROUTER OVERRIDES")

    overrides = load_router_overrides()
    params = overrides.get("overrides", {})
    if not params:
        print("  Using defaults (no overrides)")
    else:
        for k, v in sorted(params.items()):
            default = {"MAX_HOT_FILES": 4, "MAX_WARM_FILES": 8, "MAX_TOTAL_CHARS": 25000,
                       "COACTIVATION_BOOST": 0.35, "DECAY_RATES.default": 0.70}.get(k, "?")
            print(f"  {k}: {v} (default: {default})")
    metrics = overrides.get("metrics_snapshot", {})
    if metrics:
        print(f"  Based on: {metrics.get('turns_analyzed', '?')} turns, waste={metrics.get('avg_waste_ratio', '?')}")

    # Show demoted files
    demoted = overrides.get("demoted_files", [])
    if demoted:
        print(f"  Demoted files ({len(demoted)}):")
        for f in demoted:
            print(f"    - {f}")
    print()


# ============================================================================
# COST ESTIMATES
# ============================================================================

def report_cost(stats: dict, days: int):
    """Dollar cost estimates from stats-cache.json model usage."""
    section("COST ESTIMATE")

    model_usage = stats.get("modelUsage", {})
    if not model_usage:
        print("  No model usage data")
        print()
        return

    total_cost = 0.0
    for model_id, usage in model_usage.items():
        pricing = get_model_pricing(model_id)
        short = pricing["short"]

        input_tok = usage.get("inputTokens", 0)
        output_tok = usage.get("outputTokens", 0)
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_write = usage.get("cacheCreationInputTokens", 0)

        input_cost = input_tok / 1e6 * pricing["input"]
        output_cost = output_tok / 1e6 * pricing["output"]
        cache_read_cost = cache_read / 1e6 * pricing["cache_read"]
        cache_write_cost = cache_write / 1e6 * pricing["cache_write"]
        model_total = input_cost + output_cost + cache_read_cost + cache_write_cost

        print(f"  {short}:")
        if input_tok > 0:
            print(f"    Input:       {input_tok/1e6:>8.1f}M x ${pricing['input']:>6.2f}/M = ${input_cost:>10,.2f}")
        if output_tok > 0:
            print(f"    Output:      {output_tok/1e6:>8.1f}M x ${pricing['output']:>6.2f}/M = ${output_cost:>10,.2f}")
        if cache_read > 0:
            print(f"    Cache read:  {cache_read/1e6:>8.1f}M x ${pricing['cache_read']:>6.2f}/M = ${cache_read_cost:>10,.2f}")
        if cache_write > 0:
            print(f"    Cache write: {cache_write/1e6:>8.1f}M x ${pricing['cache_write']:>6.2f}/M = ${cache_write_cost:>10,.2f}")
        print(f"    Subtotal: ${model_total:>10,.2f}")
        total_cost += model_total

    print(f"\n  TOTAL (all-time): ${total_cost:,.2f}")

    # Daily cost estimate
    daily = stats.get("dailyModelTokens", [])
    if daily:
        n_days = len(daily)
        if n_days > 0:
            daily_cost = total_cost / n_days
            print(f"  Avg/day: ${daily_cost:,.2f} ({n_days} active days)")
            print(f"  30d projection: ${daily_cost * 30:,.2f}")

    # attnroute savings estimate
    # Context injection savings = avoided tokens * input price
    # Conservative: if we save 30% of context injection on average
    print()
    print("  Note: attnroute saves tokens by injecting only relevant context.")
    print("  Without attnroute, all .claude/*.md files would be injected every turn.")
    print()


# ============================================================================
# PER-SESSION EFFICIENCY
# ============================================================================

def report_sessions(turns: list):
    """Per-session efficiency breakdown."""
    section("SESSION EFFICIENCY")

    if not turns:
        print("  No turn data")
        print()
        return

    # Group by session
    sessions = defaultdict(list)
    for t in turns:
        sid = t.get("session_id", "unknown")
        sessions[sid].append(t)

    if len(sessions) <= 1 and "unknown" in sessions:
        print("  Session IDs not yet populated (needs more turns)")
        print()
        return

    print(f"  {'Session':<14} {'Turns':>6} {'Duration':>10} {'Waste':>8} {'Avg Tok':>10} {'Project':>15}")
    print(f"  {'-'*14} {'-'*6} {'-'*10} {'-'*8} {'-'*10} {'-'*15}")

    for sid, session_turns in sorted(sessions.items(), key=lambda x: x[1][0].get("timestamp", ""), reverse=True):
        if sid == "unknown":
            continue

        n = len(session_turns)
        # Duration
        timestamps = [t.get("timestamp", "") for t in session_turns if t.get("timestamp")]
        if len(timestamps) >= 2:
            try:
                start = datetime.fromisoformat(timestamps[0])
                end = datetime.fromisoformat(timestamps[-1])
                dur = end - start
                hours = dur.total_seconds() / 3600
                if hours >= 1:
                    dur_str = f"{hours:.1f}h"
                else:
                    dur_str = f"{dur.total_seconds()/60:.0f}m"
            except Exception:
                dur_str = "?"
        else:
            dur_str = "?"

        # Waste
        waste_ratios = [t["waste_ratio"] for t in session_turns if t.get("waste_ratio", -1) >= 0]
        avg_waste = sum(waste_ratios) / len(waste_ratios) if waste_ratios else -1
        waste_str = f"{avg_waste:.1%}" if avg_waste >= 0 else "n/a"

        # Avg tokens
        tok_vals = [t.get("total_context_tokens_est", 0) for t in session_turns if t.get("total_context_tokens_est", 0) > 0]
        avg_tok = sum(tok_vals) / len(tok_vals) if tok_vals else 0
        tok_str = f"{avg_tok:,.0f}" if avg_tok > 0 else "n/a"

        # Project
        projects = Counter(t.get("project", "").split("/")[-1].split("\\")[-1] for t in session_turns)
        proj = projects.most_common(1)[0][0] if projects else "?"

        short_sid = sid[:12]
        print(f"  {short_sid:<14} {n:>6} {dur_str:>10} {waste_str:>8} {tok_str:>10} {proj:>15}")

    print()


# ============================================================================
# TREND ANALYSIS
# ============================================================================

SPARKLINE_CHARS = " _.-:=+*#%@"  # Low to high


def sparkline(values: list, width: int = 20) -> str:
    """Create a text sparkline from values (0-1 range)."""
    if not values:
        return ""
    # Bucket values into width bins
    bucket_size = max(1, len(values) // width)
    buckets = []
    for i in range(0, len(values), bucket_size):
        chunk = values[i:i+bucket_size]
        buckets.append(sum(chunk) / len(chunk))

    # Map to sparkline chars
    chars = []
    for v in buckets[:width]:
        idx = min(len(SPARKLINE_CHARS) - 1, max(0, int(v * (len(SPARKLINE_CHARS) - 1))))
        chars.append(SPARKLINE_CHARS[idx])
    return "".join(chars)


def report_trend(turns: list):
    """Waste ratio trend with sparkline and parameter changes."""
    section("EFFICIENCY TREND")

    if not turns:
        print("  No turn data")
        print()
        return

    # Waste trend
    waste_vals = []
    for t in turns:
        wr = t.get("waste_ratio", -1)
        if wr >= 0:
            waste_vals.append(wr)

    if waste_vals:
        spark = sparkline(waste_vals, width=30)
        first_val = waste_vals[0] if waste_vals else 0
        last_val = waste_vals[-1] if waste_vals else 0
        print(f"  Waste: [{spark}] ({first_val:.0%} -> {last_val:.0%})")
    else:
        print("  Waste: no data yet")

    # Token trend
    tok_vals = [t.get("total_context_tokens_est", 0) for t in turns if t.get("total_context_tokens_est", 0) > 0]
    if tok_vals:
        # Normalize to 0-1 for sparkline
        max_tok = max(tok_vals) if tok_vals else 1
        norm_vals = [v / max_tok for v in tok_vals]
        spark = sparkline(norm_vals, width=30)
        print(f"  Tokens: [{spark}] ({tok_vals[0]:,} -> {tok_vals[-1]:,})")

    # Parameter changes from optimization log
    if OPTIMIZATION_LOG_FILE.exists():
        try:
            opt_entries = []
            with open(OPTIMIZATION_LOG_FILE, encoding="utf-8") as f:
                for line in f:
                    try:
                        opt_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            if opt_entries:
                print(f"\n  Parameter changes ({len(opt_entries)} optimizations):")
                for e in opt_entries[-5:]:
                    ts = e.get("timestamp", "")[:10]
                    new = e.get("new", {})
                    prev = e.get("previous", {})
                    changes = []
                    for k in new:
                        if new[k] != prev.get(k):
                            changes.append(f"{k.split('.')[-1]}:{prev.get(k,'-')}->{new[k]}")
                    if changes:
                        print(f"    [{ts}] {', '.join(changes)}")

                # Demoted files
                last_opt = opt_entries[-1]
                demoted = last_opt.get("demoted_files", [])
                if demoted:
                    print(f"\n  Demoted: {', '.join(Path(f).stem for f in demoted)}")
        except Exception:
            pass

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="attnroute token efficiency report")
    parser.add_argument("--days", type=int, default=7, help="Days to analyze")
    parser.add_argument("--project", type=str, default=None, help="Filter by project")
    parser.add_argument("--cost", action="store_true", help="Show dollar cost estimates")
    parser.add_argument("--sessions", action="store_true", help="Show per-session efficiency")
    parser.add_argument("--trend", action="store_true", help="Show efficiency trend with sparkline")
    parser.add_argument("--all", action="store_true", help="Show all reports")
    args = parser.parse_args()

    # If no specific report requested, show defaults
    show_all = args.all or not (args.cost or args.sessions or args.trend)

    print()
    print(f"  attnroute v{VERSION}  ─  Token Efficiency Report")
    print(f"  Period: last {args.days} day{'s' if args.days != 1 else ''}" +
          (f"  |  Project: {args.project}" if args.project else ""))
    divider()

    stats = load_stats_cache()
    turns = load_turns(n=500, project=args.project)

    if show_all:
        report_burn_rate(stats, args.days)
        report_waste(turns)
        report_file_leaderboard(turns)
        report_project_breakdown(turns)
        report_optimization_history()
        report_current_overrides()

    if args.cost or args.all:
        report_cost(stats, args.days)

    if args.sessions or args.all:
        report_sessions(turns)

    if args.trend or args.all:
        report_trend(turns)


if __name__ == "__main__":
    main()
