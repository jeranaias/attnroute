#!/usr/bin/env python3
"""
attnroute.telemetry_record — Stop Hook

Records what Claude actually used vs. what was injected, computing waste
metrics. Activates the learner usage tracking and triggers the
auto-optimizer at adaptive intervals.

Includes Context Confidence Self-Evaluation:
  - Relevance: fraction of injected files referenced by tool calls
  - Sufficiency: did Claude need to Read additional files not provided?
  - Precision: 1.0 - waste_ratio
  - Composite score triggers optimizer on 5 consecutive low scores (<0.4)

Hook: Stop
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from attnroute.telemetry_lib import (
        TELEMETRY_DIR,
        TURNS_FILE,
        ensure_telemetry_dir,
        load_session_state,
        rotate_jsonl,
        save_session_state,
        windows_utf8_io,
    )
    windows_utf8_io()
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from telemetry_lib import (
            TELEMETRY_DIR,
            TURNS_FILE,  # noqa: F401
            ensure_telemetry_dir,
            load_session_state,
            rotate_jsonl,
            save_session_state,
            windows_utf8_io,
        )
        windows_utf8_io()
    except ImportError:
        sys.exit(0)

try:
    from attnroute.learner import Learner
    LEARNER_AVAILABLE = True
except ImportError:
    try:
        from learner import Learner
        LEARNER_AVAILABLE = True
    except ImportError:
        LEARNER_AVAILABLE = False

# Try to import plugin system
try:
    from attnroute.plugins import get_plugins
    PLUGINS_AVAILABLE = True
except ImportError:
    try:
        from plugins import get_plugins
        PLUGINS_AVAILABLE = True
    except ImportError:
        PLUGINS_AVAILABLE = False


def parse_stdin():
    """Parse Stop hook stdin JSON."""
    try:
        data = json.loads(sys.stdin.read())
        return data
    except Exception:
        return {}


def debug_hook_input(hook_input: dict):
    """Dump Stop hook stdin schema for first 5 runs (diagnostic)."""
    debug_file = TELEMETRY_DIR / "stop_hook_debug.json"
    try:
        debug_count = 0
        if debug_file.exists():
            existing = json.loads(debug_file.read_text(encoding="utf-8"))
            debug_count = existing.get("count", 0)

        if debug_count < 5:
            debug_data = {
                "count": debug_count + 1,
                "timestamp": datetime.now().isoformat(),
                "keys": list(hook_input.keys()),
                "sample": {k: str(v)[:300] for k, v in hook_input.items()},
            }
            debug_file.write_text(json.dumps(debug_data, indent=2), encoding="utf-8")
    except Exception:
        pass


def resolve_transcript_path(raw_path: str) -> Path:
    """Resolve transcript path, handling ~ expansion on Windows."""
    if not raw_path:
        return Path("")
    # Expand ~ to home directory (Windows Path doesn't do this)
    if raw_path.startswith("~"):
        raw_path = str(Path.home()) + raw_path[1:]
    return Path(raw_path)


def extract_tool_calls_from_transcript(transcript_path: str) -> list:
    """
    Parse transcript JSONL for tool calls in the LAST assistant turn.
    Returns list of {tool, target} dicts.
    """
    resolved = resolve_transcript_path(transcript_path)
    if not resolved.name or not resolved.exists():
        return []

    tool_calls = []
    try:
        # Read only last 200KB to avoid loading huge transcripts
        file_size = resolved.stat().st_size
        with open(resolved, encoding='utf-8', errors='replace') as f:
            if file_size > 200_000:
                f.seek(max(0, file_size - 200_000))
                f.readline()  # Skip partial line
            lines = f.readlines()

        # Find last assistant message with tool_use blocks
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                role = entry.get("role") or entry.get("type", "")
                if role != "assistant":
                    continue

                content = entry.get("message", {}).get("content", [])
                if not content:
                    content = entry.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue

                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})

                    target = (
                        tool_input.get("file_path") or
                        tool_input.get("path") or
                        tool_input.get("pattern") or
                        tool_input.get("command", "")[:100] or
                        ""
                    )

                    tool_calls.append({
                        "tool": tool_name,
                        "target": str(target),
                    })

                if tool_calls:
                    break
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    return tool_calls


def compute_files_used(tool_calls: list, files_injected: list, project_path: str) -> list:
    """
    Determine which injected .claude/*.md files were actually useful.
    A file is 'used' if any tool call targeted a file that the .md doc describes.
    """
    if not tool_calls or not files_injected:
        return []

    # Build set of all tool target paths (lowercase for matching)
    targets = set()
    for tc in tool_calls:
        t = tc.get("target", "").lower().replace("\\", "/")
        targets.add(t)
        if "/" in t:
            targets.add(t.split("/")[-1])

    used = []
    for doc_file in files_injected:
        doc_lower = doc_file.lower()
        doc_stem = Path(doc_file).stem.lower()
        doc_parts = set(doc_stem.replace("-", " ").replace("_", " ").split())

        for target in targets:
            if doc_lower in target:
                used.append(doc_file)
                break
            if any(part in target for part in doc_parts if len(part) > 3):
                used.append(doc_file)
                break

    return used


def compute_context_confidence(
    tool_calls: list,
    files_injected: list,
    files_used: list,
    waste_ratio: float
) -> dict:
    """
    Compute context confidence score on 3 axes.

    Args:
        tool_calls: List of tool call dicts with 'tool' and 'target'
        files_injected: List of files that were injected into context
        files_used: List of files determined to be actually useful
        waste_ratio: Waste ratio from turn (0.0-1.0)

    Returns:
        {
            "relevance": float,    # fraction of injected files used
            "sufficiency": float,  # 1.0 if no extra files needed, reduced otherwise
            "precision": float,    # 1.0 - waste_ratio
            "composite": float,    # weighted average
        }
    """
    # Relevance: fraction of injected files that were used
    if files_injected:
        relevance = len(files_used) / len(files_injected)
    else:
        relevance = 1.0  # No injection = perfect relevance (nothing wasted)

    # Sufficiency: did Claude need to Read files we didn't provide?
    # Check if any Read tool calls targeted files NOT in our injected set
    extra_reads = 0
    total_reads = 0
    injected_lower = set(f.lower().replace("\\", "/") for f in files_injected)

    for tc in tool_calls:
        tool = tc.get("tool", "").lower()
        if tool == "read":
            total_reads += 1
            target = tc.get("target", "").lower().replace("\\", "/")
            # Check if this read was for a file we provided context about
            target_name = target.split("/")[-1] if "/" in target else target

            # Consider it "extra" if target doesn't match any injected file
            matched = False
            for inj in injected_lower:
                inj_name = inj.split("/")[-1] if "/" in inj else inj
                if target_name in inj_name or inj_name in target_name:
                    matched = True
                    break
                if any(part in target for part in inj.replace("-", " ").replace("_", " ").split() if len(part) > 3):
                    matched = True
                    break
            if not matched:
                extra_reads += 1

    if total_reads > 0:
        # More extra reads = lower sufficiency
        sufficiency = max(0.0, 1.0 - (extra_reads / total_reads))
    else:
        sufficiency = 1.0  # No reads needed = sufficient

    # Precision: inverse of waste
    precision = 1.0 - waste_ratio if waste_ratio >= 0 else 1.0

    # Composite: weighted average
    # 0.4 * relevance + 0.3 * sufficiency + 0.3 * precision
    composite = 0.4 * relevance + 0.3 * sufficiency + 0.3 * precision

    return {
        "relevance": round(relevance, 3),
        "sufficiency": round(sufficiency, 3),
        "precision": round(precision, 3),
        "composite": round(composite, 3),
    }


def update_confidence_trend(state: dict, confidence_score: float) -> bool:
    """
    Update rolling confidence trend in session state.

    Args:
        state: Session state dict (modified in place)
        confidence_score: New composite confidence score

    Returns:
        True if 5 consecutive low scores detected (triggers optimizer)
    """
    # Keep last 10 confidence scores
    trend = state.get("confidence_trend", [])
    trend.append(round(confidence_score, 3))
    trend = trend[-10:]  # Keep last 10
    state["confidence_trend"] = trend

    # Calculate rolling average
    if len(trend) >= 3:
        state["confidence_avg"] = round(sum(trend) / len(trend), 3)

    # Check for 5 consecutive low scores
    if len(trend) >= 5:
        last_5 = trend[-5:]
        if all(s < 0.4 for s in last_5):
            return True  # Trigger optimizer

    return False


def get_confidence_summary() -> str:
    """Get one-line summary of context confidence for dashboard.

    Returns:
        String like "Context quality: 78% (up from 65%)" or empty string
    """
    state = load_session_state()
    trend = state.get("confidence_trend", [])
    avg = state.get("confidence_avg", 0)

    if len(trend) < 3:
        return ""

    # Calculate previous average (before last 3 entries)
    if len(trend) > 3:
        prev_avg = sum(trend[:-3]) / len(trend[:-3])
        delta = avg - prev_avg
        if delta > 0.05:
            direction = f"up from {prev_avg:.0%}"
        elif delta < -0.05:
            direction = f"down from {prev_avg:.0%}"
        else:
            direction = "stable"
        return f"Context quality: {avg:.0%} ({direction})"
    else:
        return f"Context quality: {avg:.0%}"


def read_last_turn():
    """Read turns.jsonl once and return (lines, last_entry) or (None, None)."""
    if not TURNS_FILE.exists():
        return None, None

    try:
        with open(TURNS_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if not lines:
            return None, None

        last = json.loads(lines[-1])
        return lines, last
    except Exception:
        return None, None


def update_last_turn(
    tool_calls: list,
    files_used: list,
    lines: list = None,
    last_entry: dict = None,
    confidence: dict = None
) -> float:
    """Update the last turn record in turns.jsonl with usage data.

    If lines/last_entry provided, reuses pre-read data to avoid disk I/O.

    Returns:
        waste_ratio (float) for use by caller
    """
    if lines is None or last_entry is None:
        lines, last_entry = read_last_turn()
        if lines is None:
            return 0.0

    last = last_entry
    last["files_used"] = files_used
    last["tool_calls"] = len(tool_calls)

    injection_chars = last.get("injection_chars", 0)
    if injection_chars > 0 and files_used:
        total_injected = len(last.get("files_injected", []))
        if total_injected > 0:
            useful_fraction = len(files_used) / total_injected
            last["waste_ratio"] = round(1.0 - useful_fraction, 3)
        else:
            last["waste_ratio"] = 1.0
    elif injection_chars > 0:
        last["waste_ratio"] = 1.0
    else:
        last["waste_ratio"] = 0.0

    # Add confidence data if provided
    if confidence:
        last["confidence"] = confidence

    waste_ratio = last["waste_ratio"]

    try:
        lines[-1] = json.dumps(last) + "\n"
        with open(TURNS_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass

    return waste_ratio


def get_optimize_interval(state: dict) -> int:
    """Dynamic optimization interval based on recent waste."""
    last_waste = state.get("last_waste_ratio", -1)
    if last_waste > 0.7:
        return 10
    elif last_waste > 0.4:
        return 25
    else:
        return 50


def maybe_run_optimizer(force: bool = False):
    """Check if it's time to run the auto-optimizer.

    Args:
        force: If True, trigger optimizer regardless of interval (e.g., low confidence)
    """
    state = load_session_state()
    total_turns = state.get("total_turns", 0)
    last_opt = state.get("last_optimization_turn", 0)
    interval = get_optimize_interval(state)

    if force or total_turns - last_opt >= interval:
        try:
            # Try entry point first (pip install), then script fallback
            import shutil
            if shutil.which("attnroute-optimize"):
                subprocess.Popen(
                    ["attnroute-optimize"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(Path.cwd())
                )
            else:
                # Fallback: find script in parent dir or same dir
                for candidate in [
                    Path(__file__).parent.parent / "telemetry-optimizer.py",
                    Path(__file__).parent / "telemetry_optimizer.py",
                ]:
                    if candidate.exists():
                        subprocess.Popen(
                            [sys.executable, str(candidate)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            cwd=str(Path.cwd())
                        )
                        break
                state["last_optimization_turn"] = total_turns
                save_session_state(state)
        except Exception:
            pass


def main():
    ensure_telemetry_dir()
    hook_input = parse_stdin()

    # Diagnostic: dump hook input schema for first 5 runs
    debug_hook_input(hook_input)

    transcript_path = hook_input.get("transcript_path", "")

    # Check stop_hook_active to prevent infinite loops
    if hook_input.get("stop_hook_active"):
        return

    # Extract tool calls from transcript
    tool_calls = extract_tool_calls_from_transcript(transcript_path)

    # === PLUGIN: on_stop ===
    if PLUGINS_AVAILABLE:
        session_state = load_session_state()
        for plugin in get_plugins():
            try:
                warning = plugin.on_stop(tool_calls, session_state)
                if warning:
                    print(warning, file=sys.stderr)
            except Exception:
                pass  # Never fail the hook due to plugins

    # Read turns.jsonl ONCE (avoid triple-read)
    turn_lines, last_entry = read_last_turn()
    files_injected = last_entry.get("files_injected", []) if last_entry else []

    # Compute which injected files were actually used
    project = str(Path.cwd())
    files_used = compute_files_used(tool_calls, files_injected, project)

    # Compute preliminary waste ratio for confidence calculation
    if files_injected:
        prelim_waste = 1.0 - (len(files_used) / len(files_injected)) if files_used else 1.0
    else:
        prelim_waste = 0.0

    # Compute context confidence score
    confidence = compute_context_confidence(
        tool_calls, files_injected, files_used, prelim_waste
    )

    # Update last turn record with usage data + confidence (reuses pre-read data)
    waste_ratio = update_last_turn(tool_calls, files_used, turn_lines, last_entry, confidence)

    # Update session state counters
    state = load_session_state()
    state["total_turns"] = state.get("total_turns", 0) + 1
    state["current_project"] = project.lower()

    # Get metrics from the now-updated last_entry
    inj_chars = last_entry.get("injection_chars", 0) if last_entry else 0
    useful_chars = 0
    if waste_ratio >= 0 and waste_ratio < 1:
        useful_chars = int(inj_chars * (1 - waste_ratio))

    state["cumulative_injection_chars"] = state.get("cumulative_injection_chars", 0) + inj_chars
    state["cumulative_useful_chars"] = state.get("cumulative_useful_chars", 0) + useful_chars
    state["last_waste_ratio"] = waste_ratio
    state["last_confidence"] = confidence.get("composite", 0.0)
    if files_injected and not files_used:
        state["notifications_wasted"] = state.get("notifications_wasted", 0) + 1

    # Update confidence trend (triggers optimizer on 5 consecutive low scores)
    trigger_optimizer = update_confidence_trend(state, confidence.get("composite", 0.0))

    save_session_state(state)

    # Activate learner usage tracking (merged from usage_tracker)
    if LEARNER_AVAILABLE:
        try:
            learner = Learner()
            if files_injected:
                # Log which files were injected
                injection_dicts = [
                    {"file": f, "tier": "unknown", "score": 0.0, "chars": 0}
                    for f in files_injected
                ]
                learner.log_injection(injection_dicts, "")
            if tool_calls:
                learner.track_turn_usage(tool_calls)
        except Exception:
            pass

    # Save session memory for warm-start continuity
    if LEARNER_AVAILABLE:
        try:
            from attnroute.telemetry_lib import ATTN_STATE_PROJECT
        except ImportError:
            from telemetry_lib import ATTN_STATE_PROJECT
        try:
            learner = Learner()
            # Read current attention scores from project state
            attn_file = ATTN_STATE_PROJECT
            if attn_file.exists():
                attn = json.loads(attn_file.read_text(encoding="utf-8"))
                scores = attn.get("scores", {})
                if scores:
                    learner.save_session(scores)
        except Exception:
            pass

    # Rotate turns.jsonl to prevent unbounded growth
    rotate_jsonl(TURNS_FILE, 500)

    # Maybe trigger optimizer (force if 5 consecutive low-confidence turns)
    maybe_run_optimizer(force=trigger_optimizer)


if __name__ == "__main__":
    main()
