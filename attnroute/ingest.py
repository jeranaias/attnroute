#!/usr/bin/env python3
"""
attnroute.ingest — Bootstrap learner from Claude Code conversation history.

Parses JSONL transcripts in ~/.claude/projects/ to seed the learner with
co-activation patterns, prompt-file affinity, and file usage statistics.

This eliminates the cold-start problem on established projects by leveraging
existing Claude Code conversation history.

Usage:
    attnroute ingest                    # Ingest all projects
    attnroute ingest --project myapp    # Ingest specific project only
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Reuse normalize_path from predictor (dual import for dev/installed mode)
try:
    from attnroute.predictor import normalize_path
except ImportError:
    try:
        from predictor import normalize_path
    except ImportError:
        # Inline fallback if predictor not available
        import sys
        def normalize_path(p: str) -> str:
            """Normalize path for consistent comparison."""
            normalized = p.replace("\\", "/")
            if sys.platform == "win32":
                normalized = normalized.lower()
            return normalized

PROJECTS_DIR = Path.home() / ".claude" / "projects"
INGEST_WEIGHT_SCALE = 0.5  # Ingested associations weighted at 50% of live-observed


def extract_prompt_text(entry: dict) -> str | None:
    """Extract text from a user message entry (handles string and list formats)."""
    message = entry.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if c.get("type") == "text"
        )
    return None


def extract_tool_files(entry: dict) -> tuple[set[str], set[str]]:
    """Extract (all_files, edited_files) from assistant tool_use blocks."""
    all_files = set()
    edited_files = set()
    content = entry.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return all_files, edited_files
    for block in content:
        if block.get("type") == "tool_use":
            name = block.get("name", "")
            fp = block.get("input", {}).get("file_path", "")
            if fp and name in ("Read", "Edit", "Write"):
                norm = normalize_path(fp)
                all_files.add(norm)
                if name in ("Edit", "Write"):
                    edited_files.add(norm)
    return all_files, edited_files


def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from prompt text."""
    words = re.findall(r'[a-zA-Z][a-zA-Z0-9_-]{2,}', text.lower())
    # Filter common stop words
    stop = {'the', 'and', 'for', 'that', 'this', 'with', 'from', 'are', 'was',
            'were', 'been', 'have', 'has', 'had', 'but', 'not', 'you', 'all',
            'can', 'her', 'his', 'they', 'will', 'would', 'could', 'should',
            'may', 'might', 'shall', 'into', 'also', 'than', 'then', 'them',
            'these', 'those', 'what', 'which', 'who', 'whom', 'how', 'when',
            'where', 'why', 'please', 'thanks', 'help', 'need', 'want', 'like',
            'make', 'just', 'let', 'use', 'try', 'get', 'take', 'see', 'look'}
    return [w for w in words if w not in stop and len(w) >= 3]


def ingest_transcripts(
    project_filter: str | None = None,
    max_sessions: int = 200,
) -> dict:
    """
    Parse Claude Code JSONL transcripts and produce learner-compatible state.

    Args:
        project_filter: Only ingest projects whose path contains this string
        max_sessions: Maximum number of session files to process

    Returns:
        Dict compatible with Learner state format
    """
    if not PROJECTS_DIR.exists():
        return _empty_state()

    # Accumulators
    prompt_file_pairs = []
    coactivation_counts = defaultdict(Counter)  # file -> {file -> count}
    file_access_times = defaultdict(list)
    file_access_counts = Counter()
    file_edit_counts = Counter()
    total_turns = 0
    sessions_processed = 0

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter.lower() not in str(project_dir).lower():
            continue

        for session_file in sorted(project_dir.glob("*.jsonl"))[-max_sessions:]:
            if sessions_processed >= max_sessions:
                break
            if session_file.stat().st_size < 1000:
                continue

            current_prompt = None
            current_files = set()
            current_edits = set()
            turn_idx = 0

            try:
                with open(session_file, encoding='utf-8', errors='replace') as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                        except (json.JSONDecodeError, ValueError):
                            continue

                        if entry.get("type") == "user":
                            # Flush previous turn
                            if current_prompt and current_files:
                                words = extract_keywords(current_prompt)
                                if words:
                                    prompt_file_pairs.append((words, list(current_files)))

                                for f_path in current_files:
                                    file_access_counts[f_path] += 1
                                    file_access_times[f_path].append(turn_idx)
                                for f_path in current_edits:
                                    file_edit_counts[f_path] += 1

                                # Co-activation: files accessed in same turn
                                file_list = list(current_files)
                                for i, f1 in enumerate(file_list):
                                    for f2 in file_list[i + 1:]:
                                        coactivation_counts[f1][f2] += 1
                                        coactivation_counts[f2][f1] += 1

                                total_turns += 1

                            # Parse new prompt
                            current_prompt = extract_prompt_text(entry)
                            current_files = set()
                            current_edits = set()
                            turn_idx += 1

                        elif entry.get("type") == "assistant":
                            files, edits = extract_tool_files(entry)
                            current_files.update(files)
                            current_edits.update(edits)

            except Exception:
                continue

            sessions_processed += 1

        if sessions_processed >= max_sessions:
            break

    # Build learner-compatible state
    return _build_learner_state(
        prompt_file_pairs,
        coactivation_counts,
        file_access_times,
        file_access_counts,
        file_edit_counts,
        total_turns,
    )


def _empty_state() -> dict:
    """Return empty learner state."""
    return {
        "version": 2,
        "meta": {"turns_learned": 0, "last_learned": None,
                 "created": datetime.now().isoformat()},
        "prompt_file_affinity": {},
        "word_frequency": {},
        "coactivation_learned": {},
        "file_rhythm": {},
        "session_memory": {},
        "discoveries": [],
        "usefulness": {},
    }


def _build_learner_state(
    prompt_file_pairs: list,
    coactivation_counts: dict,
    file_access_times: dict,
    file_access_counts: Counter,
    file_edit_counts: Counter,
    total_turns: int,
) -> dict:
    """Convert raw ingestion data to learner state format."""
    state = _empty_state()

    # 1. Build prompt-file affinity (scaled by INGEST_WEIGHT_SCALE)
    affinities = defaultdict(dict)
    word_counts = Counter()
    for words, files in prompt_file_pairs:
        for word in words:
            word_counts[word] += 1
            for f in files:
                if f not in affinities[word]:
                    affinities[word][f] = 0.0
                affinities[word][f] += INGEST_WEIGHT_SCALE * 0.08  # base learning rate

    # Cap affinities at 1.0
    state["prompt_file_affinity"] = {
        word: {f: round(min(1.0, score), 4) for f, score in files.items()}
        for word, files in affinities.items()
        if files  # skip empty
    }

    # 2. Build word frequency (fraction of turns containing each word)
    if total_turns > 0:
        state["word_frequency"] = {
            word: round(count / total_turns, 4)
            for word, count in word_counts.items()
        }

    # 3. Build co-activation (top co-occurring files per file)
    for f, related in coactivation_counts.items():
        top_related = [r for r, _ in related.most_common(10)]
        if top_related:
            state["coactivation_learned"][f] = top_related

    # 4. Build file rhythms from access timing
    for f, times in file_access_times.items():
        if len(times) >= 2:
            gaps = [times[i] - times[i-1] for i in range(1, len(times))]
            avg_gap = sum(gaps) / len(gaps)
            # Convert gap to decay rate (shorter gap = slower decay)
            # Clamp between 0.5 (fast decay) and 0.98 (slow decay)
            decay = min(0.98, max(0.5, 1.0 - (1.0 / (avg_gap + 1))))
            state["file_rhythm"][f] = round(decay, 4)

    # 5. Build usefulness scores
    for f in file_access_counts:
        state["usefulness"][f] = {
            "injected": file_access_counts[f],  # approximate: accessed ~ injected
            "accessed": file_access_counts[f],
            "edited": file_edit_counts.get(f, 0),
        }

    # 6. Metadata
    state["meta"]["turns_learned"] = total_turns
    state["meta"]["last_learned"] = datetime.now().isoformat()

    return state
