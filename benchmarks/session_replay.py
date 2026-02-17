#!/usr/bin/env python3
"""
Session Replay Benchmark — The real proof.

Replays actual Claude Code transcripts through attnroute's context router
and measures whether it predicts the files Claude actually accessed.

For each user prompt in a real session:
  1. Build a search index over the session's project files
  2. Run attnroute's update_attention() to get predicted files
  3. Look at what files Claude actually Read/Edit/Grep'd afterward
  4. Calculate precision, recall, F1

This answers the question: "Does attnroute surface the right files?"

Usage:
    python benchmarks/session_replay.py                    # All sessions
    python benchmarks/session_replay.py --limit 5          # First 5 sessions
    python benchmarks/session_replay.py --session <uuid>   # Specific session
"""

import json
import os
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from attnroute.predictor import (
    PredictorModelV5,
    load_model,
    predict_files_v5,
    save_model,
    train_model_v5,
)
from attnroute.predictor import normalize_path as predictor_normalize

# Claude Code project dirs
CLAUDE_DIR = Path.home() / ".claude" / "projects"

# Trained predictor model (loaded once, shared across sessions)
_predictor_model: PredictorModelV5 | None = None

# Project dir name → actual directory on disk
# Claude Code encodes project paths as directory names with -- separators
_PROJECT_DIR_CACHE: dict[str, Path | None] = {}


# ============================================================================
# TRANSCRIPT PARSING
# ============================================================================

def find_sessions(limit: int = None, session_id: str = None) -> list[Path]:
    """Find JSONL transcript files."""
    sessions = []

    if not CLAUDE_DIR.exists():
        print(f"  No Claude projects dir found at {CLAUDE_DIR}")
        return sessions

    for project_dir in CLAUDE_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            # Skip very small files (< 1KB, probably empty)
            if jsonl.stat().st_size < 1024:
                continue
            if session_id and session_id not in jsonl.stem:
                continue
            sessions.append(jsonl)

    # Sort by modification time (newest first)
    sessions.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if limit:
        sessions = sessions[:limit]

    return sessions


def parse_transcript(jsonl_path: Path) -> list[dict]:
    """
    Parse a JSONL transcript into a list of turns.

    Each turn is: {prompt: str, cwd: str, tool_calls: [{tool, target, input}]}
    """
    lines = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    # Build turns: pair user prompts with subsequent tool calls
    turns = []
    current_turn = None

    for entry in lines:
        msg_type = entry.get("type")

        if msg_type == "user":
            message = entry.get("message", {})
            content = message.get("content", "")

            # Only process text prompts (not tool results)
            if isinstance(content, str) and content.strip():
                # Save any previous turn
                if current_turn and current_turn.get("prompt"):
                    turns.append(current_turn)

                current_turn = {
                    "prompt": content,
                    "cwd": entry.get("cwd", ""),
                    "timestamp": entry.get("timestamp", ""),
                    "tool_calls": [],
                }

        elif msg_type == "assistant" and current_turn:
            message = entry.get("message", {})
            content_blocks = message.get("content", [])

            if not isinstance(content_blocks, list):
                continue

            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue

                tool_name = block.get("name", "")
                tool_input = block.get("input", {})

                # Extract target file from tool input
                target = _extract_target_from_input(tool_name, tool_input)
                if target:
                    current_turn["tool_calls"].append({
                        "tool": tool_name,
                        "target": target,
                        "input": tool_input,
                    })

    # Don't forget the last turn
    if current_turn and current_turn.get("prompt"):
        turns.append(current_turn)

    return turns


def _extract_target_from_input(tool_name: str, tool_input: dict) -> str | None:
    """Extract the file path target from a tool's input dict."""
    if not tool_input:
        return None

    # Read, Edit, Write — use file_path
    if tool_name in ("Read", "Edit", "Write", "MultiEdit"):
        return tool_input.get("file_path")

    # Grep — use path or infer from pattern
    if tool_name in ("Grep", "grep"):
        return tool_input.get("path")

    # Glob — use path
    if tool_name in ("Glob", "glob"):
        return tool_input.get("path")

    # Bash — try to extract file references from command
    if tool_name in ("Bash", "bash"):
        cmd = tool_input.get("command", "")
        # Look for file paths in common commands
        # git, python, pytest, etc. often reference files
        path_matches = re.findall(
            r'(?:[\w./\\-]+/)?[\w.-]+\.(?:py|js|ts|tsx|jsx|rs|go|java|rb|cpp|c|h|json|yaml|yml|toml|md)',
            cmd
        )
        if path_matches:
            return path_matches[0]  # First file reference

    return None


def _normalize_path(path: str, cwd: str = "") -> str:
    """Normalize a file path for comparison."""
    if not path:
        return ""

    # Convert backslashes
    path = path.replace("\\", "/")

    # Make relative to cwd if absolute
    if cwd:
        cwd = cwd.replace("\\", "/")
        if path.startswith(cwd):
            path = path[len(cwd):].lstrip("/")

    # Remove drive letter on Windows
    if len(path) > 2 and path[1] == ":":
        path = path[2:]

    # Lowercase on Windows for comparison
    if sys.platform == "win32":
        path = path.lower()

    return path.strip("/")


def _extract_basename(path: str) -> str:
    """Get just the filename from a path."""
    return Path(path.replace("\\", "/")).name.lower()


# ============================================================================
# PROJECT INITIALIZATION
# ============================================================================

def _resolve_project_dir(cwd: str, tool_calls: list[dict] = None) -> Path | None:
    """
    Resolve the actual project directory from a session's CWD and tool call targets.

    The CWD from Claude Code sessions is often the user's home directory, not
    the project root. We use tool call file paths to find the actual project
    (identified by a .git directory).
    """
    if not cwd:
        return None

    cwd_path = Path(cwd)

    # If CWD itself is a git project, use it
    if cwd_path.is_dir() and (cwd_path / ".git").exists():
        return cwd_path

    # Try to find the project from tool call file paths
    if tool_calls:
        # Collect candidate project directories from file paths
        candidates: dict[str, int] = {}
        for tc in tool_calls:
            target = tc.get("target", "")
            if not target:
                continue
            target_path = Path(target.replace("\\", "/"))
            # Walk up from the file to find a .git directory
            for parent in target_path.parents:
                if parent.is_dir() and (parent / ".git").exists():
                    key = str(parent)
                    candidates[key] = candidates.get(key, 0) + 1
                    break

        if candidates:
            # Return the most common project root
            best = max(candidates, key=candidates.get)
            return Path(best)

    # If CWD is a real directory but not a git repo, check for .git in children
    if cwd_path.is_dir():
        # Don't use home directory or root as project dir — too broad
        home = Path.home()
        if cwd_path == home or len(str(cwd_path)) <= 4:
            return None
        return cwd_path

    return None


def _reinitialize_for_project(project_dir: Path) -> int:
    """
    Reinitialize attnroute's context router for a specific project directory.

    This resets the search index, keywords, and docs root cache so the
    context router can find files in the target project.

    Returns: number of indexed documents
    """
    import attnroute.context_router as cr

    # 1. Reset docs root cache so resolve_docs_root() re-evaluates for new CWD
    cr._docs_root_cache = None
    cr._docs_root_checked = False

    # 2. Reload keywords from the project (may find .claude/keywords.json)
    cr.KEYWORDS, cr.CO_ACTIVATION, loaded_pinned = cr.load_keyword_config()
    if loaded_pinned:
        cr.PINNED_FILES = loaded_pinned
    cr._COMPILED_KEYWORDS = cr._build_compiled_keywords(cr.KEYWORDS)

    # 3. Rebuild co-activation
    cr.CO_ACTIVATION = cr.build_bidirectional_coactivation(cr.CO_ACTIVATION)
    if hasattr(cr, 'build_coactivation_graph') and cr.NETWORKX_AVAILABLE:
        cr._coactivation_graph = cr.build_coactivation_graph(cr.CO_ACTIVATION)

    # 4. Build a fresh search index for this project
    indexed_count = 0
    if cr.SEARCH_AVAILABLE and cr._search_loader is not None:
        from attnroute.indexer import SearchIndex

        # Use a temp database so we don't pollute the real index
        temp_db = Path(tempfile.mktemp(suffix=".db", prefix="attnroute_replay_"))
        idx = SearchIndex(db_path=temp_db)

        # Use the project's own docs directory (not global ~/.claude)
        # Check for project-local docs in priority order
        docs_root = None
        for candidate in [
            project_dir / ".claude",
            project_dir / "docs",
            project_dir / "doc",
        ]:
            if candidate.is_dir():
                docs_root = candidate
                break

        if docs_root is None:
            # Create a temp docs root (no markdown docs to index)
            docs_root = Path(tempfile.mkdtemp(prefix="attnroute_docs_"))

        code_roots = [project_dir]

        try:
            idx.build(docs_root, code_roots=code_roots)
            status = idx.status()
            indexed_count = status.get("indexed_documents", 0)
        except Exception as e:
            print(f"[replay] Index build failed: {e}", file=sys.stderr)

        # Inject into the context router's lazy loader
        cr._search_loader._instance = idx
        cr._search_loader._initialized = True

    return indexed_count


# ============================================================================
# ATTNROUTE PREDICTION
# ============================================================================

def predict_files(state: dict, prompt: str, cwd: str) -> tuple[dict, set[str], dict]:
    """
    Run attnroute's context router on a prompt and return predictions.

    Returns:
        (updated_state, predicted_files, tier_map)
    """
    from attnroute.context_router import get_tier, select_diverse_files, update_attention

    old_cwd = os.getcwd()
    try:
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)

        state, activated = update_attention(state, prompt)
    finally:
        os.chdir(old_cwd)

    # Extract predicted files (HOT + WARM), limited to production injection caps
    # Production injects: HOT = full file content, WARM = compressed TOC header
    # WARM is cheap (~50 tokens each), so allow more of them
    MAX_HOT = 3
    MAX_WARM = 8

    # Confidence floor: only inject WARM files above this score
    # Reduces false positives by being more selective
    WARM_CONFIDENCE_FLOOR = 0.35

    MAX_HINT = 20

    # Adaptive injection thresholds (match production)
    CONFIDENT_HOT_THRESHOLD = 0.90
    CONFIDENT_HOT_MIN_STREAK = 2

    hot_files = []
    warm_files = []
    hint_files = []
    for path, score in state.get("scores", {}).items():
        tier = get_tier(score)
        if tier == "HOT":
            # Adaptive: only HOT if truly confident, else demote to WARM
            streak = state.get("consecutive_turns", {}).get(path, 0)
            is_confident = (
                score >= CONFIDENT_HOT_THRESHOLD
                or streak >= CONFIDENT_HOT_MIN_STREAK
            )
            if is_confident:
                hot_files.append((path, score))
            else:
                warm_files.append((path, score))  # Demote: save ~450 tokens
        elif tier == "WARM" and score >= WARM_CONFIDENCE_FLOOR:
            warm_files.append((path, score))
        elif tier == "HINT":
            hint_files.append((path, score))

    # Diversity-aware selection: spread across directories
    all_candidates = hot_files + warm_files
    all_candidates.sort(key=lambda x: -x[1])
    diverse = select_diverse_files(all_candidates, MAX_HOT + MAX_WARM, state)
    hot_files = [(p, s) for p, s in diverse if get_tier(s) == "HOT"][:MAX_HOT]
    warm_files = [(p, s) for p, s in diverse if get_tier(s) != "HOT"][:MAX_WARM]

    # HINT files: path-only, cheap but expand coverage
    # Include overflow WARM files that didn't make the selection cut
    selected = {p for p, _ in hot_files + warm_files}
    overflow_warm = [(p, s) for p, s in all_candidates if p not in selected]
    all_hints = overflow_warm + hint_files
    all_hints.sort(key=lambda x: -x[1])
    hint_files = [(p, s) for p, s in all_hints if p not in selected][:MAX_HINT]

    predicted = set()
    tier_map = {}
    # Core predictions: HOT + WARM (scored for precision/recall)
    # Track estimated token cost per tier
    token_cost = 0
    for path, score in hot_files:
        predicted.add(path)
        tier_map[path] = {"tier": "HOT", "score": score}
        token_cost += 500  # Full file content
    for path, score in warm_files:
        predicted.add(path)
        tier_map[path] = {"tier": "WARM", "score": score}
        token_cost += 50  # Compressed TOC header
    # HINT files tracked separately (cheap path hints, not counted as predictions)
    hint_set = set()
    for path, score in hint_files:
        hint_set.add(path)
        tier_map[path] = {"tier": "HINT", "score": score}
        token_cost += 5  # Path only
    tier_map["_token_cost"] = {"cost": token_cost, "hot": len(hot_files),
                                "warm": len(warm_files), "hint": len(hint_files)}

    return state, predicted, tier_map


# ============================================================================
# METRICS
# ============================================================================

def compute_metrics(predicted: set[str], actual: set[str], cwd: str = "") -> dict:
    """
    Compute precision, recall, F1 between predicted and actual file sets.

    Uses multi-level matching:
    1. Exact match after normalization
    2. Suffix match (predicted relative path is a suffix of actual absolute path)
    3. Basename match (last resort fuzzy match)
    """
    if not actual:
        return {
            "precision": 1.0 if not predicted else 0.0,
            "recall": 1.0,
            "f1": 1.0 if not predicted else 0.0,
            "true_positives": 0,
            "false_positives": len(predicted),
            "false_negatives": 0,
            "predicted_count": len(predicted),
            "actual_count": 0,
        }

    if not predicted:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": len(actual),
            "predicted_count": 0,
            "actual_count": len(actual),
        }

    # Normalize all paths
    pred_normalized = {_normalize_path(p, cwd) for p in predicted}
    actual_normalized = {_normalize_path(p, cwd) for p in actual}

    # Build lookup structures for matching
    actual_basenames: dict[str, set[str]] = {}
    for p in actual_normalized:
        base = _extract_basename(p)
        actual_basenames.setdefault(base, set()).add(p)

    # Count true positives using multiple matching strategies
    tp = 0
    matched_actual = set()

    for pred_path in pred_normalized:
        pred_base = _extract_basename(pred_path)

        # Level 1: Exact match
        if pred_path in actual_normalized:
            tp += 1
            matched_actual.add(pred_path)
            continue

        # Level 2: Suffix match (predicted "attnroute/foo.py" matches actual "/users/x/attnroute/foo.py")
        matched = False
        for actual_path in actual_normalized:
            if actual_path not in matched_actual:
                if actual_path.endswith("/" + pred_path) or actual_path.endswith(pred_path):
                    tp += 1
                    matched_actual.add(actual_path)
                    matched = True
                    break
                # Also try reverse: actual is relative, predicted is longer
                if pred_path.endswith("/" + actual_path) or pred_path.endswith(actual_path):
                    tp += 1
                    matched_actual.add(actual_path)
                    matched = True
                    break
        if matched:
            continue

        # Level 3: Basename match (fuzzy — last resort)
        if pred_base in actual_basenames:
            for candidate in actual_basenames[pred_base]:
                if candidate not in matched_actual:
                    tp += 1
                    matched_actual.add(candidate)
                    break
            continue

    fp = len(pred_normalized) - tp
    fn = len(actual_normalized) - len(matched_actual)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "predicted_count": len(pred_normalized),
        "actual_count": len(actual_normalized),
    }


# ============================================================================
# SESSION REPLAY
# ============================================================================

def replay_session(jsonl_path: Path) -> dict | None:
    """
    Replay a single session through attnroute and measure prediction accuracy.
    """
    turns = parse_transcript(jsonl_path)

    if not turns:
        return None

    # Filter to turns that have tool calls (something to predict)
    turns_with_tools = [t for t in turns if t["tool_calls"]]

    if len(turns_with_tools) < 2:
        return None

    # Get the cwd from the first turn
    session_cwd = turns_with_tools[0].get("cwd", "")

    # Collect all tool calls for project detection
    all_tool_calls = []
    for t in turns_with_tools:
        all_tool_calls.extend(t["tool_calls"])

    # Resolve the actual project directory from CWD + tool call paths
    project_dir = _resolve_project_dir(session_cwd, all_tool_calls)
    if not project_dir:
        return None

    old_cwd = os.getcwd()
    temp_db_path = None
    try:
        os.chdir(project_dir)

        # Build search index for this project's files
        indexed_count = _reinitialize_for_project(project_dir)

        # Track the temp DB for cleanup
        import attnroute.context_router as cr
        if cr.SEARCH_AVAILABLE and cr._search_loader._instance is not None:
            temp_db_path = cr._search_loader._instance.db_path

        # Hard warmup: pre-populate state from git/mtime/import analysis
        from attnroute.warmup import build_warmup_state
        try:
            state = build_warmup_state(project_dir, verbose=False)
        except Exception:
            state = {"scores": {}, "consecutive_turns": {}, "turn_count": 0}

        # Track recently accessed files for FilePredictor sequence predictions
        recent_files: list[str] = []

        # Initialize learner for within-session learning
        # This lets the learner build prompt→file associations as the session progresses
        # Save/restore learner state to avoid polluting real learned data
        learner = None
        learner_turns = []
        learner_backup = None
        if cr.LEARNER_AVAILABLE:
            try:
                learner = cr.get_learner()
                if learner is not None:
                    import copy
                    learner_backup = copy.deepcopy(learner.state)
            except Exception:
                pass

        turn_metrics = []

        for i, turn in enumerate(turns_with_tools):
            prompt = turn["prompt"]
            cwd = turn.get("cwd", session_cwd)

            # What files did Claude actually access?
            actual_files = set()
            tools_used = defaultdict(int)
            for tc in turn["tool_calls"]:
                target = tc["target"]
                if target:
                    actual_files.add(target)
                    tools_used[tc["tool"]] += 1

            if not actual_files:
                continue

            # Phase 0: FilePredictor — inject high-confidence predictions
            if _predictor_model is not None:
                predictions = predict_files_v5(prompt, _predictor_model, recent_files)
                idx = cr._search_loader._instance if cr.SEARCH_AVAILABLE and cr._search_loader._instance else None
                all_indexed = set(idx.get_all_paths()) if idx and hasattr(idx, 'get_all_paths') else set()

                for pred_path, score in predictions:
                    if score < 5.0:  # Only use high-confidence predictions
                        continue
                    # Require path suffix match (not just basename) for precision
                    pred_parts = pred_path.replace("\\", "/").lower().split("/")
                    for indexed_path in all_indexed:
                        idx_parts = indexed_path.lower().split("/")
                        # Match on last 2+ path components (e.g., "attnroute/context_router.py")
                        match_len = 0
                        for pp, ip in zip(reversed(pred_parts), reversed(idx_parts)):
                            if pp == ip:
                                match_len += 1
                            else:
                                break
                        if match_len >= 2:
                            # Strong path match — moderate boost
                            boost = min(0.45, 0.30 + score * 0.005)
                            state["scores"][indexed_path] = max(
                                state["scores"].get(indexed_path, 0), boost
                            )
                        elif match_len == 1 and score >= 10.0:
                            # Basename-only match — only for very high confidence
                            boost = min(0.35, 0.25 + score * 0.003)
                            state["scores"][indexed_path] = max(
                                state["scores"].get(indexed_path, 0), boost
                            )

            # What does attnroute predict?
            t0 = time.perf_counter()
            try:
                state, predicted, tier_map = predict_files(state, prompt, cwd)
            except Exception:
                continue
            predict_ms = (time.perf_counter() - t0) * 1000

            # Compute metrics (core HOT+WARM predictions only)
            metrics = compute_metrics(predicted, actual_files, cwd)
            # Also compute expanded metrics including HINT files
            hint_set = {p for p, t in tier_map.items() if t.get("tier") == "HINT"}
            expanded = predicted | hint_set
            expanded_metrics = compute_metrics(expanded, actual_files, cwd)
            metrics["expanded_recall"] = expanded_metrics["recall"]
            metrics["hint_count"] = len(hint_set)
            metrics["turn"] = i
            metrics["prompt_preview"] = prompt[:80] + "..." if len(prompt) > 80 else prompt
            metrics["predict_ms"] = predict_ms
            metrics["tools_used"] = dict(tools_used)
            # Token cost tracking
            cost_info = tier_map.get("_token_cost", {})
            metrics["token_cost"] = cost_info.get("cost", 0)
            metrics["hot_count"] = cost_info.get("hot", 0)
            metrics["warm_count"] = cost_info.get("warm", 0)

            turn_metrics.append(metrics)

            # Feed actual file accesses back into state via observe_tool_calls
            # This simulates the on_stop hook observing which files Claude used
            from attnroute.context_router import observe_tool_calls
            synthetic_calls = [
                {"name": tc["tool"], "input": tc["input"]}
                for tc in turn["tool_calls"]
            ]
            state = observe_tool_calls(state, synthetic_calls,
                                       project_dir=str(project_dir))

            # Feed learner with turn data so it builds prompt→file associations
            if learner is not None:
                from attnroute.learner import extract_prompt_words
                learner_turns.append({
                    "prompt_keywords": extract_prompt_words(prompt),
                    "files_used": [_normalize_path(f, cwd) for f in actual_files],
                    "files_injected": list(predicted),
                })
                # Trigger learning every 10 turns (matches production optimizer)
                if len(learner_turns) % 10 == 0 and len(learner_turns) >= 10:
                    try:
                        learner.learn_from_turns(learner_turns[-20:])
                    except Exception:
                        pass

            # Update recent_files for FilePredictor (predictor uses normalized paths)
            for f in actual_files:
                norm = predictor_normalize(f)
                if norm not in recent_files[-20:]:
                    recent_files.append(norm)
            recent_files = recent_files[-20:]  # Keep last 20

    finally:
        os.chdir(old_cwd)
        # Restore learner state (don't pollute real learned data)
        if learner is not None and learner_backup is not None:
            learner.state = learner_backup
            learner.maturity = learner._compute_maturity()
        # Clean up temp database
        if temp_db_path and Path(temp_db_path).exists():
            try:
                Path(temp_db_path).unlink()
            except OSError:
                pass

    if not turn_metrics:
        return None

    # Aggregate session metrics
    precisions = [m["precision"] for m in turn_metrics]
    recalls = [m["recall"] for m in turn_metrics]
    f1s = [m["f1"] for m in turn_metrics]

    return {
        "session_file": jsonl_path.name,
        "project": jsonl_path.parent.name,
        "project_dir": str(project_dir),
        "total_turns": len(turns),
        "evaluated_turns": len(turn_metrics),
        "indexed_files": indexed_count,
        "precision_mean": statistics.mean(precisions),
        "recall_mean": statistics.mean(recalls),
        "f1_mean": statistics.mean(f1s),
        "precision_median": statistics.median(precisions),
        "recall_median": statistics.median(recalls),
        "f1_median": statistics.median(f1s),
        "turns_with_hits": sum(1 for m in turn_metrics if m["recall"] > 0),
        "turns_with_perfect_recall": sum(1 for m in turn_metrics if m["recall"] >= 1.0),
        "expanded_recall_mean": statistics.mean([m.get("expanded_recall", m["recall"]) for m in turn_metrics]),
        "turns_with_expanded_hits": sum(1 for m in turn_metrics if m.get("expanded_recall", 0) > 0),
        "avg_token_cost": statistics.mean([m.get("token_cost", 0) for m in turn_metrics]) if turn_metrics else 0,
        "avg_hot_count": statistics.mean([m.get("hot_count", 0) for m in turn_metrics]) if turn_metrics else 0,
        "avg_warm_count": statistics.mean([m.get("warm_count", 0) for m in turn_metrics]) if turn_metrics else 0,
        "total_token_cost": sum(m.get("token_cost", 0) for m in turn_metrics),
        "turn_metrics": turn_metrics,
    }


# ============================================================================
# MAIN
# ============================================================================

def run_session_replay(limit: int = None, session_id: str = None) -> list[dict]:
    """Run session replay benchmark across all available transcripts."""
    # Suppress noisy BM25S progress bars
    os.environ["BM25S_VERBOSITY"] = "0"
    try:
        import bm25s
        bm25s.hf.disable_progress_bars()
    except (ImportError, AttributeError):
        pass

    global _predictor_model

    print()
    print("=" * 75)
    print("SESSION REPLAY BENCHMARK — Does attnroute predict the right files?")
    print("=" * 75)
    print()

    # Load or train the FilePredictor model
    _predictor_model = load_model()
    if _predictor_model is None:
        print("  Training FilePredictor model from session transcripts...")
        _predictor_model = train_model_v5(max_sessions=50)
        save_model(_predictor_model)
    print(f"  FilePredictor loaded: {len(_predictor_model.strong_keywords)} keywords, "
          f"{len(_predictor_model.file_popularity)} files, "
          f"{len(_predictor_model.project_popularity)} projects")
    print()

    sessions = find_sessions(limit=limit, session_id=session_id)
    print(f"  Found {len(sessions)} transcript files")
    print()

    if not sessions:
        print("  No transcripts found. This benchmark requires real Claude Code sessions.")
        print(f"  Expected location: {CLAUDE_DIR}")
        return []

    results = []
    skipped = 0

    for i, session_path in enumerate(sessions):
        project = session_path.parent.name
        name = session_path.stem[:12]
        print(f"  [{i + 1}/{len(sessions)}] {project}/{name}...", end=" ", flush=True)

        try:
            result = replay_session(session_path)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        if result is None:
            print("skipped (too few turns)")
            skipped += 1
            continue

        r = result
        proj = Path(r.get("project_dir", "")).name or "?"
        print(
            f"F1={r['f1_mean']:.2f} "
            f"P={r['precision_mean']:.2f} "
            f"R={r['recall_mean']:.2f} "
            f"({r['evaluated_turns']} turns, {r.get('indexed_files', 0)} indexed in {proj})"
        )
        results.append(result)

    # Summary
    print()
    print("=" * 75)
    print("RESULTS")
    print("=" * 75)
    print()

    if not results:
        print("  No sessions had enough data to evaluate.")
        return results

    all_precisions = [r["precision_mean"] for r in results]
    all_recalls = [r["recall_mean"] for r in results]
    all_f1s = [r["f1_mean"] for r in results]

    print(f"  Sessions evaluated:  {len(results)}")
    print(f"  Sessions skipped:    {skipped}")
    print()
    print("  AGGREGATE METRICS (mean across sessions):")
    print(f"    Precision:  {statistics.mean(all_precisions):.2f} (of predicted files, how many were used)")
    print(f"    Recall:     {statistics.mean(all_recalls):.2f} (of used files, how many were predicted)")
    print(f"    F1:         {statistics.mean(all_f1s):.2f}")
    print()
    print("  AGGREGATE METRICS (median across sessions):")
    print(f"    Precision:  {statistics.median(all_precisions):.2f}")
    print(f"    Recall:     {statistics.median(all_recalls):.2f}")
    print(f"    F1:         {statistics.median(all_f1s):.2f}")
    print()

    # Per-session breakdown
    print(f"  {'Session':<40} {'Turns':>6} {'Idx':>6} {'P':>6} {'R':>6} {'F1':>6}")
    print(f"  {'-' * 70}")
    for r in results:
        session_label = f"{r['project'][:20]}/{r['session_file'][:16]}"
        print(
            f"  {session_label:<40} "
            f"{r['evaluated_turns']:>6} "
            f"{r.get('indexed_files', 0):>6} "
            f"{r['precision_mean']:>6.2f} "
            f"{r['recall_mean']:>6.2f} "
            f"{r['f1_mean']:>6.2f}"
        )

    print()

    # Hits analysis
    total_turns = sum(r["evaluated_turns"] for r in results)
    total_hits = sum(r["turns_with_hits"] for r in results)
    total_perfect = sum(r["turns_with_perfect_recall"] for r in results)
    total_expanded_hits = sum(r.get("turns_with_expanded_hits", r["turns_with_hits"]) for r in results)
    mean_expanded_recall = statistics.mean([r.get("expanded_recall_mean", r["recall_mean"]) for r in results])
    print("  TURN-LEVEL ANALYSIS:")
    print(f"    Total turns evaluated:  {total_turns}")
    print(f"    Turns with any hit:     {total_hits} ({total_hits / total_turns * 100:.0f}%)")
    print(f"    Turns with perfect R:   {total_perfect} ({total_perfect / total_turns * 100:.0f}%)")
    print(f"    Expanded hits (+HINT):  {total_expanded_hits} ({total_expanded_hits / total_turns * 100:.0f}%)")
    print(f"    Expanded recall (+HINT): {mean_expanded_recall:.0%}")
    print()

    # Token cost analysis
    avg_cost = statistics.mean([r.get("avg_token_cost", 0) for r in results])
    avg_hot = statistics.mean([r.get("avg_hot_count", 0) for r in results])
    avg_warm = statistics.mean([r.get("avg_warm_count", 0) for r in results])
    total_cost = sum(r.get("total_token_cost", 0) for r in results)
    baseline_cost = (3 * 500) + (8 * 50)  # 3 HOT + 8 WARM without adaptive
    print("  TOKEN COST (estimated per turn):")
    print(f"    Avg tokens injected:    {avg_cost:.0f} tokens/turn")
    print(f"    Avg HOT files/turn:     {avg_hot:.1f}")
    print(f"    Avg WARM files/turn:    {avg_warm:.1f}")
    print(f"    Baseline (no adaptive): {baseline_cost} tokens/turn (3 HOT + 8 WARM)")
    if avg_cost > 0 and baseline_cost > 0:
        savings = (1 - avg_cost / baseline_cost) * 100
        print(f"    Savings vs baseline:    {savings:.0f}%")
    print(f"    Total across all turns: {total_cost:,} tokens")
    print()

    # Interpretation
    mean_recall = statistics.mean(all_recalls)
    mean_precision = statistics.mean(all_precisions)

    print("  INTERPRETATION:")
    if mean_recall >= 0.5:
        print(f"    Recall {mean_recall:.0%}: attnroute surfaces MOST files Claude needs.")
    elif mean_recall >= 0.3:
        print(f"    Recall {mean_recall:.0%}: attnroute surfaces SOME files Claude needs.")
    elif mean_recall >= 0.1:
        print(f"    Recall {mean_recall:.0%}: attnroute provides useful but incomplete predictions.")
    else:
        print(f"    Recall {mean_recall:.0%}: attnroute's predictions don't match well (cold start expected).")
        print("    Note: attnroute needs learned associations from usage patterns to improve.")

    if mean_precision >= 0.5:
        print(f"    Precision {mean_precision:.0%}: most surfaced files are actually relevant.")
    elif mean_precision >= 0.2:
        print(f"    Precision {mean_precision:.0%}: some noise in predictions, but signal present.")
    else:
        print(f"    Precision {mean_precision:.0%}: many surfaced files weren't used (high noise).")

    print()
    print("  NOTE: First turns in each session will have low recall (cold start).")
    print("  Scores improve as attnroute learns co-activation patterns within the session.")
    print()

    # Save results
    output_file = Path(__file__).parent / "session_replay_results.json"
    # Strip turn_metrics for file size
    slim_results = []
    for r in results:
        slim = {k: v for k, v in r.items() if k != "turn_metrics"}
        slim["sample_turns"] = r["turn_metrics"][:5]  # Keep first 5 for inspection
        slim_results.append(slim)

    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "sessions_evaluated": len(results),
            "aggregate": {
                "precision_mean": statistics.mean(all_precisions),
                "recall_mean": statistics.mean(all_recalls),
                "f1_mean": statistics.mean(all_f1s),
                "precision_median": statistics.median(all_precisions),
                "recall_median": statistics.median(all_recalls),
                "f1_median": statistics.median(all_f1s),
            },
            "results": slim_results,
        }, f, indent=2, default=str)
    print(f"  Results saved to: {output_file}")
    print()

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Session replay benchmark")
    parser.add_argument("--limit", type=int, help="Max sessions to replay")
    parser.add_argument("--session", type=str, help="Specific session UUID to replay")
    args = parser.parse_args()

    run_session_replay(limit=args.limit, session_id=args.session)
