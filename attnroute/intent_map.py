#!/usr/bin/env python3
"""
Intent -> File mapping for bridging the semantic gap.

Maps common developer intent patterns to file selection strategies.
This acts as a "semantic shortcut" — when a user says "fix the bug",
recently-edited files and test files get boosted without needing
exact keyword matches in file content.
"""
import re
from pathlib import Path

# Intent boost amount (moderate — should complement, not override BM25)
INTENT_BOOST = 0.2

# Intent patterns: compiled regex -> strategy name
INTENT_PATTERNS = [
    # Bug fixes -> recently edited files, test files
    (re.compile(r'\b(?:fix|bug|error|crash|broken|issue|wrong|fail|debug)\b', re.I),
     "recent_edits"),

    # New features -> module/source files
    (re.compile(r'\b(?:add|create|implement|build|new|introduce|feature)\b', re.I),
     "module_files"),

    # Testing -> test files
    (re.compile(r'\b(?:tests?|specs?|assert|coverage|pytest|jest|unittest|mock|run tests)\b', re.I),
     "test_files"),

    # Refactoring -> heavily-edited files
    (re.compile(r'\b(?:refactor|clean|restructure|reorganize|simplify|extract|rename)\b', re.I),
     "refactor_targets"),

    # Configuration -> config files
    (re.compile(r'\b(?:config|setting|environment|env|deploy|docker|yaml|toml)\b', re.I),
     "config_files"),

    # Performance -> benchmark/perf files
    (re.compile(r'\b(?:performance|slow|optimize|speed|latency|profile|benchmark)\b', re.I),
     "perf_files"),

    # Documentation -> doc files
    (re.compile(r'\b(?:document|readme|docs|explain|describe|comment|changelog)\b', re.I),
     "doc_files"),
]

_SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".rb"}


def detect_intent(prompt: str) -> list[str]:
    """Detect developer intent from prompt text.

    Returns list of strategy names (may be empty or multiple).
    """
    strategies = []
    for pattern, strategy in INTENT_PATTERNS:
        if pattern.search(prompt):
            strategies.append(strategy)
    return strategies


def boost_by_intent(
    strategies: list[str],
    scores: dict[str, float],
    state: dict,
) -> dict[str, float]:
    """Apply intent-based score adjustments.

    Args:
        strategies: List of detected intent strategies
        scores: Current attention scores {path: score}
        state: Full attention state (for streak/history access)

    Returns:
        Modified scores dict
    """
    if not strategies:
        return scores

    for strategy in strategies:
        if strategy == "test_files":
            for path in scores:
                p = path.lower()
                if "test" in p or "spec" in p:
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST)

        elif strategy == "recent_edits":
            # Boost files with active streaks (recently used)
            streaks = state.get("consecutive_turns", {})
            for path, streak in streaks.items():
                if path in scores and streak >= 2:
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST * 0.5)

        elif strategy == "module_files":
            # Boost non-test source files
            for path in scores:
                ext = Path(path).suffix.lower()
                p = path.lower()
                if ext in _SOURCE_EXTS and "test" not in p and "config" not in p:
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST * 0.3)

        elif strategy == "refactor_targets":
            # Boost files with high streaks (the ones being actively worked on)
            streaks = state.get("consecutive_turns", {})
            for path, streak in streaks.items():
                if path in scores and streak >= 3:
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST)

        elif strategy == "config_files":
            for path in scores:
                base = Path(path).name.lower()
                if any(kw in base for kw in (
                    "config", "setting", ".env", ".yaml", ".yml", ".toml",
                    "docker", "makefile", "package.json",
                )):
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST)

        elif strategy == "perf_files":
            for path in scores:
                p = path.lower()
                if "benchmark" in p or "perf" in p or "profile" in p:
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST)

        elif strategy == "doc_files":
            for path in scores:
                ext = Path(path).suffix.lower()
                p = path.lower()
                if ext == ".md" or "readme" in p or "docs" in p or "changelog" in p:
                    scores[path] = min(1.0, scores[path] + INTENT_BOOST)

    return scores
