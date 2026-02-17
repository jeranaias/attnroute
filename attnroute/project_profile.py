#!/usr/bin/env python3
"""
Cross-session memory: per-project file importance profiles.

Persists learned file usage patterns across sessions so turn-0 of a new
session starts warm instead of cold. Each project gets its own profile
stored in ~/.claude/telemetry/project_profiles.json.

Profiles track:
  - Top files by usage count and average attention score
  - File clusters (groups of files frequently accessed together)
  - Staleness decay (projects not touched in 14+ days fade)
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Thresholds
WARM_THRESHOLD = 0.25
MAX_PROFILE_AGE_DAYS = 14
MAX_TOP_FILES = 30
MAX_CLUSTERS = 10
PROFILE_WARM_CAP = 0.6  # Cap warm-start scores below HOT

PROFILE_FILE = Path.home() / ".claude" / "telemetry" / "project_profiles.json"


def _normalize_key(project_path: str) -> str:
    """Normalize project path to a consistent key."""
    return project_path.lower().replace("\\", "/").rstrip("/")


def load_profiles() -> dict:
    """Load all project profiles, purging stale ones (>14 days)."""
    try:
        if not PROFILE_FILE.exists():
            return {}
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
    except (json.JSONDecodeError, OSError):
        return {}

    # Purge stale profiles
    cutoff = datetime.now() - timedelta(days=MAX_PROFILE_AGE_DAYS)
    pruned = {}
    for key, profile in data.items():
        try:
            last = datetime.fromisoformat(profile.get("last_active", "2000-01-01"))
            if last >= cutoff:
                pruned[key] = profile
        except (ValueError, TypeError):
            continue
    return pruned


def save_profiles(profiles: dict) -> None:
    """Save all profiles atomically."""
    try:
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            from attnroute.compat import safe_atomic_write
            safe_atomic_write(PROFILE_FILE, json.dumps(profiles, indent=2))
        except ImportError:
            PROFILE_FILE.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[attnroute] Failed to save profiles: {e}", file=sys.stderr)


def update_profile_from_session(
    project_path: str,
    state: dict,
    tool_calls: list[dict] | None = None,
) -> None:
    """Update a project's profile from the current session's final state.

    Args:
        project_path: Project root path
        state: Attention state dict with scores
        tool_calls: Optional list of tool call dicts from the session
    """
    key = _normalize_key(project_path)
    profiles = load_profiles()
    profile = profiles.get(key, {
        "total_sessions": 0,
        "top_files": {},
        "file_clusters": [],
    })

    profile["last_active"] = datetime.now().isoformat()
    profile["total_sessions"] = profile.get("total_sessions", 0) + 1

    # Update top files from attention scores
    scores = state.get("scores", {})
    top_files = profile.get("top_files", {})

    for path, score in scores.items():
        if score < WARM_THRESHOLD:
            continue
        entry = top_files.get(path, {"usage_count": 0, "avg_score": 0.0})
        count = entry["usage_count"]
        avg = entry["avg_score"]
        # Running average
        new_count = count + 1
        new_avg = (avg * count + score) / new_count
        top_files[path] = {
            "usage_count": new_count,
            "avg_score": round(new_avg, 3),
        }

    # Also count files from tool calls
    if tool_calls:
        accessed = set()
        for tc in tool_calls:
            target = tc.get("target", "")
            if target and not target.startswith("git "):
                # Normalize
                rel = target.replace("\\", "/")
                project_norm = project_path.replace("\\", "/").rstrip("/")
                if rel.lower().startswith(project_norm.lower()):
                    rel = rel[len(project_norm):].lstrip("/")
                if len(rel) > 2 and rel[1] == ":":
                    rel = rel[2:].lstrip("/")
                if rel and len(rel) < 200:
                    accessed.add(rel)

        for path in accessed:
            entry = top_files.get(path, {"usage_count": 0, "avg_score": 0.3})
            entry["usage_count"] = entry["usage_count"] + 1
            top_files[path] = entry

    # Trim to MAX_TOP_FILES by usage_count
    if len(top_files) > MAX_TOP_FILES:
        sorted_files = sorted(
            top_files.items(),
            key=lambda x: x[1]["usage_count"],
            reverse=True,
        )
        top_files = dict(sorted_files[:MAX_TOP_FILES])

    profile["top_files"] = top_files

    # Update file clusters from co-accessed files in this session
    if tool_calls:
        accessed_list = list(accessed) if tool_calls else []
        if len(accessed_list) >= 2:
            # Take top 5 most accessed as a cluster
            cluster = sorted(accessed_list)[:5]
            clusters = profile.get("file_clusters", [])
            if cluster not in clusters:
                clusters.append(cluster)
                if len(clusters) > MAX_CLUSTERS:
                    clusters = clusters[-MAX_CLUSTERS:]
            profile["file_clusters"] = clusters

    profiles[key] = profile
    save_profiles(profiles)


def get_warm_start_scores(project_path: str) -> dict[str, float]:
    """Get pre-populated scores for a project's turn 0.

    Returns scores capped at PROFILE_WARM_CAP (0.6) so HOT slots
    are reserved for files activated by the user's prompt.
    """
    key = _normalize_key(project_path)
    profiles = load_profiles()
    profile = profiles.get(key)
    if not profile:
        return {}

    top_files = profile.get("top_files", {})
    if not top_files:
        return {}

    # Recency factor: exponential decay from last_active
    try:
        last_active = datetime.fromisoformat(profile.get("last_active", "2000-01-01"))
        days_ago = (datetime.now() - last_active).days
    except (ValueError, TypeError):
        days_ago = MAX_PROFILE_AGE_DAYS

    recency = max(0.2, 1.0 - (days_ago / MAX_PROFILE_AGE_DAYS))

    scores = {}
    for path, stats in top_files.items():
        avg_score = stats.get("avg_score", 0)
        usage = stats.get("usage_count", 0)
        # sqrt ramp: 1 use→0.45, 2→0.63, 5→1.0 (fast early value)
        usage_factor = min(1.0, usage ** 0.5 / 5 ** 0.5)

        score = min(PROFILE_WARM_CAP, avg_score * recency * usage_factor * 0.7)
        if score >= WARM_THRESHOLD:
            scores[path] = round(score, 3)

    return scores
