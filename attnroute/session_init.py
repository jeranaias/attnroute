#!/usr/bin/env python3
"""
attnroute.session_init — SessionStart Hook

Detects project switches, resets attention state to prevent cross-project
bleed, and outputs a compact efficiency dashboard.

Hook: SessionStart
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from attnroute.compat import try_import

# Import telemetry lib
_telem_imports, TELEMETRY_LIB_AVAILABLE = try_import(
    "attnroute.telemetry_lib", "telemetry_lib",
    ["ATTN_STATE_PROJECT", "TELEMETRY_DIR", "ensure_telemetry_dir", "get_project",
     "load_router_overrides", "load_session_state", "load_stats_cache",
     "load_turns", "save_session_state", "windows_utf8_io"]
)
if TELEMETRY_LIB_AVAILABLE:
    ATTN_STATE_PROJECT = _telem_imports["ATTN_STATE_PROJECT"]
    TELEMETRY_DIR = _telem_imports["TELEMETRY_DIR"]
    ensure_telemetry_dir = _telem_imports["ensure_telemetry_dir"]
    get_project = _telem_imports["get_project"]
    load_router_overrides = _telem_imports["load_router_overrides"]
    load_session_state = _telem_imports["load_session_state"]
    load_stats_cache = _telem_imports["load_stats_cache"]
    load_turns = _telem_imports["load_turns"]
    save_session_state = _telem_imports["save_session_state"]
    windows_utf8_io = _telem_imports["windows_utf8_io"]
    windows_utf8_io()
else:
    sys.exit(0)

# Import learner
_learner_imports, LEARNER_AVAILABLE = try_import(
    "attnroute.learner", "learner", ["Learner"]
)
if LEARNER_AVAILABLE:
    Learner = _learner_imports["Learner"]

# Import plugin system
_plugin_imports, PLUGINS_AVAILABLE = try_import(
    "attnroute.plugins", "plugins", ["get_plugins"]
)
if PLUGINS_AVAILABLE:
    get_plugins = _plugin_imports["get_plugins"]


def detect_project_switch():
    """If CWD changed since last session, reset attention scores."""
    current = get_project()
    state = load_session_state()
    prev = state.get("current_project", "")

    if prev and prev != current:
        # Project switched — reset attention state to prevent cross-project bleed
        attn_file = ATTN_STATE_PROJECT
        if attn_file.exists():
            try:
                attn = json.loads(attn_file.read_text(encoding='utf-8'))
                for key in attn.get("scores", {}):
                    attn["scores"][key] = 0.0
                attn["turn_count"] = 0
                attn_file.write_text(json.dumps(attn, indent=2), encoding='utf-8')
                print(f"[telemetry] Project switch: {prev} -> {current}, attention reset", file=sys.stderr)
            except Exception:
                pass

    state["current_project"] = current
    state["session_start"] = datetime.now().isoformat()
    save_session_state(state)

    # Warm-start: pre-load attention from previous session's focus
    if LEARNER_AVAILABLE:
        try:
            learner = Learner()
            warmup = learner.get_warmup_scores()
            if warmup and ATTN_STATE_PROJECT.parent.exists():
                attn_file = ATTN_STATE_PROJECT
                try:
                    attn = json.loads(attn_file.read_text(encoding='utf-8')) if attn_file.exists() else {"scores": {}, "consecutive_turns": {}, "turn_count": 0}
                    applied = 0
                    for f, warmup_score in warmup.items():
                        if f in attn.get("scores", {}):
                            current = attn["scores"][f]
                            if warmup_score > current:
                                attn["scores"][f] = warmup_score
                                applied += 1
                    if applied:
                        attn_file.write_text(json.dumps(attn, indent=2), encoding='utf-8')
                        print(f"[attnroute] Session warm-start: {applied} files pre-warmed from previous session", file=sys.stderr)
                except Exception:
                    pass
        except Exception:
            pass


def build_dashboard() -> str:
    """Build compact efficiency dashboard from telemetry data."""
    lines = []

    # --- Token burn rate from stats-cache.json ---
    stats = load_stats_cache()
    daily_tokens = stats.get("dailyModelTokens", [])
    if daily_tokens:
        # Last 7 days
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        recent = [d for d in daily_tokens if d.get("date", "") >= cutoff]
        if recent:
            total_tok = sum(
                sum(d.get("tokensByModel", {}).values())
                for d in recent
            )
            days = len(recent)
            avg_daily = total_tok / days if days else 0
            projected_monthly = avg_daily * 30
            lines.append(f"7d avg: {avg_daily/1e6:.1f}M tok/day | 30d proj: {projected_monthly/1e6:.0f}M")

    # --- Waste metrics from turns.jsonl ---
    turns = load_turns(n=100)
    if turns:
        waste_ratios = [t["waste_ratio"] for t in turns if t.get("waste_ratio", -1) >= 0]
        notif_count = sum(1 for t in turns if t.get("was_notification"))

        avg_waste = sum(waste_ratios) / len(waste_ratios) if waste_ratios else -1
        notif_pct = (notif_count / len(turns) * 100) if turns else 0

        if avg_waste >= 0:
            lines.append(f"Waste: {avg_waste:.0%} | Notifs filtered: {notif_count}/{len(turns)} ({notif_pct:.0f}%)")

        # Top wasted files
        file_waste = {}
        file_used = {}
        for t in turns:
            for f in t.get("files_injected", []):
                file_waste[f] = file_waste.get(f, 0) + 1
            for f in t.get("files_used", []):
                file_used[f] = file_used.get(f, 0) + 1

        waste_sorted = sorted(
            file_waste.items(),
            key=lambda x: x[1] - file_used.get(x[0], 0),
            reverse=True
        )[:3]
        if waste_sorted:
            waste_strs = []
            for f, inj in waste_sorted:
                used = file_used.get(f, 0)
                name = Path(f).stem
                waste_strs.append(f"{name}({inj}i/{used}u)")
            lines.append(f"Top waste: {', '.join(waste_strs)}")

    # --- Current overrides ---
    overrides = load_router_overrides()
    params = overrides.get("overrides", {})
    if params:
        parts = []
        if "MAX_HOT_FILES" in params:
            parts.append(f"HOT={params['MAX_HOT_FILES']}")
        if "MAX_WARM_FILES" in params:
            parts.append(f"WARM={params['MAX_WARM_FILES']}")
        if "DECAY_RATES.default" in params:
            parts.append(f"decay={params['DECAY_RATES.default']}")
        if "COACTIVATION_BOOST" in params:
            parts.append(f"co-act={params['COACTIVATION_BOOST']}")
        if parts:
            lines.append(f"Tuned: {' '.join(parts)}")

    # --- Learner intelligence status ---
    if LEARNER_AVAILABLE:
        try:
            learner = Learner()
            lines.append(learner.summary())
            disc = learner.discoveries_summary()
            if disc:
                lines.append(disc)
        except Exception:
            pass

    if not lines:
        return ""  # No data yet

    header = "## attnroute"
    return header + "\n" + "\n".join(lines)


def cleanup_failed_events():
    """Delete 1p_failed_events files that clutter the telemetry directory."""
    for f in TELEMETRY_DIR.glob("1p_failed_events*"):
        try:
            f.unlink()
        except Exception:
            pass


def main():
    ensure_telemetry_dir()
    cleanup_failed_events()
    detect_project_switch()
    dashboard = build_dashboard()
    if dashboard:
        print(dashboard)

    # === PLUGIN: on_session_start ===
    if PLUGINS_AVAILABLE:
        import os
        session_state = {"session_id": os.environ.get("CLAUDE_SESSION_ID", "unknown")}
        for plugin in get_plugins():
            try:
                output = plugin.on_session_start(session_state)
                if output:
                    print(output)
            except Exception:
                pass  # Never fail the hook due to plugins


if __name__ == "__main__":
    main()
