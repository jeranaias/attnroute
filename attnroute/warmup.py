#!/usr/bin/env python3
"""
Hard Warmup — Aggressive project analysis for instant context routing.

Unlike the soft warmup (SESSION_WARMUP_FACTOR=0.4 from previous session),
hard warmup scans the project RIGHT NOW using multiple signals:

1. Git recency    — recently committed/modified files (most likely to be active)
2. File timestamps — mtime-sorted files (recently touched = recently relevant)
3. Import graph   — files connected to hot files via imports
4. Editor state   — open files from .vscode, .idea, etc.
5. Directory focus — files near CWD or recently accessed directories

Usage:
    from attnroute.warmup import build_warmup_state
    state = build_warmup_state("/path/to/project")
    # state["scores"] is pre-populated with attention scores

CLI:
    attnroute warmup              # Run warmup for current project
    attnroute warmup --analyze    # Show analysis without applying
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# Tier thresholds (match context_router.py)
HOT_THRESHOLD = 0.8
WARM_THRESHOLD = 0.25

# Source file extensions to consider
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".cs", ".scala", ".dart", ".lua", ".r", ".jl",
    ".json", ".yaml", ".yml", ".toml", ".md", ".html", ".css",
}


# ============================================================================
# SIGNAL 1: Git Recency
# ============================================================================

def _git_recent_files(project_dir: Path, max_commits: int = 100) -> list[tuple[str, float]]:
    """
    Get recently modified files from git log.

    Returns (relative_path, recency_score) tuples.
    Score ranges from 1.0 (most recent commit) to 0.1 (oldest in window).
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}", "--name-only",
             "--pretty=format:---COMMIT---"],
            capture_output=True, text=True, cwd=str(project_dir),
            timeout=15
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    # Parse git log output: files appear after each ---COMMIT--- marker
    commits = result.stdout.split("---COMMIT---")
    file_first_seen: dict[str, int] = {}  # file → commit index (0 = most recent)

    for commit_idx, block in enumerate(commits):
        for line in block.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("---"):
                continue
            if line not in file_first_seen:
                file_first_seen[line] = commit_idx

    if not file_first_seen:
        return []

    # Score: most recent = 1.0, oldest = 0.1
    max_idx = max(file_first_seen.values()) or 1
    scored = []
    for path, idx in file_first_seen.items():
        # Skip files that no longer exist (deleted since that commit)
        if not (project_dir / path).exists():
            continue
        score = 1.0 - (idx / max_idx) * 0.9  # Range: 0.1 to 1.0
        scored.append((path, score))

    return scored


def build_cochange_graph(
    project_dir: Path | str,
    max_commits: int = 200,
    min_cooccurrence: float = 0.3,
) -> dict[str, list[tuple[str, float]]]:
    """Build a co-change graph from git history.

    Analyzes commits to find files that are frequently changed together.
    Returns {file: [(partner, strength), ...]} where strength is the
    fraction of commits containing file that also contain partner.

    Only includes pairs with co-occurrence >= min_cooccurrence.
    """
    project_dir = Path(project_dir)
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}", "--name-only",
             "--pretty=format:---COMMIT---"],
            capture_output=True, text=True, cwd=str(project_dir),
            timeout=15,
        )
        if result.returncode != 0:
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}

    # Parse commits into file sets
    commits = result.stdout.split("---COMMIT---")
    commit_files: list[set[str]] = []
    file_commit_count: dict[str, int] = {}  # How many commits each file appears in

    for block in commits:
        files_in_commit = set()
        for line in block.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("---"):
                files_in_commit.add(line)
        if 2 <= len(files_in_commit) <= 15:  # Skip trivial and massive commits
            commit_files.append(files_in_commit)
            for f in files_in_commit:
                file_commit_count[f] = file_commit_count.get(f, 0) + 1

    if not commit_files:
        return {}

    # Count co-occurrences
    pair_count: dict[tuple[str, str], int] = {}
    for files in commit_files:
        file_list = sorted(files)
        for i, a in enumerate(file_list):
            for b in file_list[i + 1:]:
                key = (a, b)
                pair_count[key] = pair_count.get(key, 0) + 1

    # Filter out "hub" files that appear in too many commits — they co-change
    # with everything (CHANGELOG, pyproject.toml, lock files, etc.)
    hub_threshold = max(5, len(commit_files) * 0.4)  # 40% of commits = hub
    _HUB_PATTERNS = {
        "changelog", "changes", "history", "news",
        "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "package-lock.json", "yarn.lock",
        "cargo.toml", "cargo.lock", "go.sum",
        "version", "__version__",
    }
    hub_files: set[str] = set()
    for f, count in file_commit_count.items():
        if count >= hub_threshold:
            hub_files.add(f)
        else:
            fname = f.rsplit("/", 1)[-1].lower().replace(".md", "").replace(".txt", "")
            if fname in _HUB_PATTERNS:
                hub_files.add(f)

    # Build graph: for each file, find partners above threshold
    graph: dict[str, list[tuple[str, float]]] = {}
    for (a, b), count in pair_count.items():
        if a in hub_files or b in hub_files:
            continue  # Skip pairs involving hub files

        # Strength = co-occurrence / individual count
        # This measures "given A changed, how often does B also change?"
        strength_ab = count / file_commit_count.get(a, 1)
        strength_ba = count / file_commit_count.get(b, 1)

        if strength_ab >= min_cooccurrence:
            graph.setdefault(a, []).append((b, round(strength_ab, 3)))
        if strength_ba >= min_cooccurrence:
            graph.setdefault(b, []).append((a, round(strength_ba, 3)))

    # Sort partners by strength (descending), cap at 5 per file
    for f in graph:
        graph[f] = sorted(graph[f], key=lambda x: -x[1])[:5]

    return graph


def _git_frequently_changed(project_dir: Path, days: int = 30) -> list[tuple[str, float]]:
    """
    Get files changed most frequently in the last N days.

    Returns (relative_path, frequency_score) tuples.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--name-only",
             "--pretty=format:"],
            capture_output=True, text=True, cwd=str(project_dir),
            timeout=15
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    counts = Counter()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            counts[line] += 1

    if not counts:
        return []

    max_count = counts.most_common(1)[0][1]
    return [(path, count / max_count) for path, count in counts.most_common(50)]


# ============================================================================
# SIGNAL 2: File Timestamps
# ============================================================================

def _file_timestamps(project_dir: Path, max_files: int = 200) -> list[tuple[str, float]]:
    """
    Get source files sorted by modification time (most recent first).

    Returns (relative_path, recency_score) tuples.
    """
    files_with_mtime = []

    for root, dirs, filenames in os.walk(project_dir):
        # Skip hidden/build directories
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", "__pycache__", ".git", "venv",
                                 "env", "build", "dist", "target", ".tox")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(fpath)
                rel = os.path.relpath(fpath, project_dir).replace("\\", "/")
                files_with_mtime.append((rel, mtime))
            except OSError:
                continue

    if not files_with_mtime:
        return []

    # Sort by mtime (newest first)
    files_with_mtime.sort(key=lambda x: x[1], reverse=True)

    # Score: newest = 1.0, oldest = 0.1
    now = time.time()
    oldest = files_with_mtime[-1][1] if files_with_mtime else now
    time_range = now - oldest or 1

    scored = []
    for path, mtime in files_with_mtime[:max_files]:
        age = now - mtime
        score = 1.0 - (age / time_range) * 0.9
        scored.append((path, max(0.1, score)))

    return scored


# ============================================================================
# SIGNAL 3: Import Graph
# ============================================================================

def _build_import_graph(project_dir: Path) -> dict[str, set[str]]:
    """
    Build a file→imports graph using regex-based import detection.

    Returns {file_path: set(imported_file_paths)}.
    """
    # Map module names to file paths
    module_to_file: dict[str, str] = {}
    all_files: list[str] = []

    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", "__pycache__", ".git", "venv",
                                 "env", "build", "dist", "target", ".tox")]
        for fname in filenames:
            fpath = os.path.join(root, fname)
            try:
                rel = os.path.relpath(fpath, project_dir).replace("\\", "/")
            except ValueError:
                continue
            all_files.append(rel)

            # Map Python modules
            if fname.endswith(".py"):
                # attnroute/context_router.py → attnroute.context_router
                module = rel.replace("/", ".").replace("\\", ".")[:-3]
                module_to_file[module] = rel
                # Also map short name: context_router
                stem = Path(fname).stem
                if stem not in module_to_file:
                    module_to_file[stem] = rel

            # Map JS/TS modules
            elif fname.endswith((".js", ".ts", ".tsx", ".jsx")):
                stem = Path(fname).stem
                if stem == "index":
                    # Use directory name for index files
                    stem = Path(root).name
                if stem not in module_to_file:
                    module_to_file[stem] = rel

    # Parse imports
    graph: dict[str, set[str]] = defaultdict(set)

    # Python import patterns
    py_import = re.compile(
        r'(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))'
    )
    # JS/TS import patterns
    js_import = re.compile(
        r'''(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"])'''
    )

    for rel in all_files:
        ext = os.path.splitext(rel)[1].lower()
        if ext not in (".py", ".js", ".ts", ".tsx", ".jsx"):
            continue

        fpath = os.path.join(project_dir, rel)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                content = f.read(50000)  # Cap at 50KB
        except OSError:
            continue

        if ext == ".py":
            for m in py_import.finditer(content):
                imported = m.group(1) or m.group(2)
                if imported:
                    # Try full path first, then progressively shorter
                    parts = imported.split(".")
                    for i in range(len(parts), 0, -1):
                        candidate = ".".join(parts[:i])
                        if candidate in module_to_file:
                            graph[rel].add(module_to_file[candidate])
                            break
        else:
            for m in js_import.finditer(content):
                imported = m.group(1) or m.group(2)
                if imported and imported.startswith("."):
                    # Relative import — resolve
                    base_dir = os.path.dirname(rel)
                    target = os.path.normpath(os.path.join(base_dir, imported)).replace("\\", "/")
                    stem = Path(target).stem
                    if target in module_to_file:
                        graph[rel].add(module_to_file[target])
                    elif stem in module_to_file:
                        graph[rel].add(module_to_file[stem])

    return dict(graph)


def _expand_import_neighbors(
    hot_files: set[str],
    import_graph: dict[str, set[str]],
    max_depth: int = 2,
) -> dict[str, float]:
    """
    From a set of hot files, find their import neighbors.

    Returns {file: score} where score decays with distance.
    """
    scores: dict[str, float] = {}
    frontier = set(hot_files)
    visited = set()

    for depth in range(1, max_depth + 1):
        next_frontier = set()
        decay = 0.5 ** depth  # 0.5, 0.25, ...

        for f in frontier:
            if f in visited:
                continue
            visited.add(f)

            # Forward edges (what f imports)
            for imported in import_graph.get(f, set()):
                if imported not in hot_files:  # Don't re-score already hot files
                    scores[imported] = max(scores.get(imported, 0), decay)
                    next_frontier.add(imported)

            # Reverse edges (what imports f)
            for src, targets in import_graph.items():
                if f in targets and src not in hot_files:
                    scores[src] = max(scores.get(src, 0), decay * 0.7)
                    next_frontier.add(src)

        frontier = next_frontier

    return scores


# ============================================================================
# SIGNAL 4: Editor State
# ============================================================================

def _editor_open_files(project_dir: Path) -> list[str]:
    """
    Detect files open in the user's editor from workspace configs.

    Checks .vscode, .idea, and other editor state files.
    """
    open_files = []

    # VS Code: .vscode/settings.json or workspace storage
    # Check for recently opened in .vscode
    vscode_dir = project_dir / ".vscode"
    if vscode_dir.is_dir():
        # VS Code doesn't store open files in .vscode directly,
        # but the launch configs and tasks often reference key files
        for config in ("launch.json", "tasks.json", "settings.json"):
            config_file = vscode_dir / config
            if config_file.exists():
                try:
                    content = config_file.read_text(encoding="utf-8", errors="replace")
                    # Extract file paths from JSON values
                    paths = re.findall(
                        r'"(?:\./|)([^"]+\.(?:py|js|ts|tsx|jsx|go|rs|java))"',
                        content
                    )
                    open_files.extend(paths)
                except OSError:
                    pass

    # JetBrains .idea: workspace.xml has recent file list
    idea_dir = project_dir / ".idea"
    if idea_dir.is_dir():
        workspace = idea_dir / "workspace.xml"
        if workspace.exists():
            try:
                content = workspace.read_text(encoding="utf-8", errors="replace")
                # Extract file references from workspace.xml
                paths = re.findall(r'file://\$PROJECT_DIR\$/([^"<]+)', content)
                open_files.extend(paths)
            except OSError:
                pass

    return list(set(open_files))


# ============================================================================
# SIGNAL 5: Directory Focus
# ============================================================================

def _directory_focus_files(project_dir: Path, cwd: str = None) -> list[tuple[str, float]]:
    """
    Boost files near the current working directory.

    If CWD is inside the project, files in that directory and its parent get a boost.
    """
    if not cwd:
        cwd = str(Path.cwd())

    cwd_path = Path(cwd)
    if not str(cwd_path).startswith(str(project_dir)):
        return []

    try:
        rel_cwd = cwd_path.relative_to(project_dir)
    except ValueError:
        return []

    scored = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", "__pycache__", ".git", "venv",
                                 "env", "build", "dist", "target", ".tox")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            try:
                rel = Path(fpath).relative_to(project_dir)
            except ValueError:
                continue

            # Score based on path similarity to CWD
            rel_str = str(rel).replace("\\", "/")
            cwd_str = str(rel_cwd).replace("\\", "/")

            if rel_str.startswith(cwd_str + "/") or cwd_str == ".":
                # Same directory: high boost
                depth = rel_str.count("/") - cwd_str.count("/")
                score = max(0.3, 0.8 - depth * 0.15)
                scored.append((rel_str, score))
            elif cwd_str.startswith(str(rel.parent).replace("\\", "/")):
                # Parent directory
                scored.append((rel_str, 0.2))

    return scored


# ============================================================================
# COMBINE ALL SIGNALS
# ============================================================================

def build_warmup_state(
    project_dir: str | Path,
    cwd: str = None,
    verbose: bool = False,
    quick: bool = False,
) -> dict:
    """
    Build a pre-populated attention state from project analysis.

    Combines git recency, file timestamps, import graph, editor state,
    and directory focus into a warmup state dict compatible with
    context_router.update_attention().

    Args:
        project_dir: Path to the project root
        cwd: Current working directory (for directory focus signal)
        verbose: Print analysis details
        quick: Skip expensive signals (import graph, editor state) for <100ms

    Returns:
        State dict with pre-populated scores
    """
    project_dir = Path(project_dir)
    t0 = time.perf_counter()

    scores: dict[str, float] = defaultdict(float)
    signal_contributions: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    # --- Signal 1: Git recency (weight: 0.35) ---
    if verbose:
        print("  Analyzing git history...", file=sys.stderr, flush=True)

    git_recent = _git_recent_files(project_dir, max_commits=50 if quick else 100)
    git_freq = _git_frequently_changed(project_dir)

    for path, score in git_recent:
        boost = score * 0.40
        scores[path] += boost
        signal_contributions[path]["git_recency"] = boost

    for path, score in git_freq:
        boost = score * 0.20
        scores[path] += boost
        signal_contributions[path]["git_frequency"] = boost

    # --- Signal 2: File timestamps (weight: 0.25) ---
    if verbose:
        print("  Scanning file timestamps...", file=sys.stderr, flush=True)

    mtime_files = _file_timestamps(project_dir)
    for path, score in mtime_files:
        boost = score * 0.25
        scores[path] += boost
        signal_contributions[path]["mtime"] = boost

    # --- Signal 3: Import graph expansion (weight: 0.15) ---
    # Skip in quick mode (most expensive signal, <100ms budget)
    if not quick:
        if verbose:
            print("  Building import graph...", file=sys.stderr, flush=True)

        # Identify "hot" files from git + mtime signals
        hot_candidates = {path for path, score in scores.items() if score >= 0.4}
        if hot_candidates:
            import_graph = _build_import_graph(project_dir)
            neighbor_scores = _expand_import_neighbors(hot_candidates, import_graph)
            for path, score in neighbor_scores.items():
                boost = score * 0.15
                scores[path] += boost
                signal_contributions[path]["imports"] = boost

    # --- Signal 4: Editor state (weight: 0.10) ---
    # Skip in quick mode (requires file parsing, low signal value)
    if not quick:
        if verbose:
            print("  Checking editor state...", file=sys.stderr, flush=True)

        editor_files = _editor_open_files(project_dir)
        for path in editor_files:
            scores[path] += 0.10
            signal_contributions[path]["editor"] = 0.10

    # --- Signal 5: Directory focus (weight: 0.05) ---
    if verbose:
        print("  Analyzing directory focus...", file=sys.stderr, flush=True)

    dir_files = _directory_focus_files(project_dir, cwd)
    for path, score in dir_files:
        boost = score * 0.05
        scores[path] += boost
        signal_contributions[path]["dir_focus"] = boost

    # --- Signal 6: Test↔Source pairing ---
    # If a source file is hot, boost its test file (and vice versa)
    all_paths = set(scores.keys())
    test_boosts: dict[str, float] = {}
    for path, score in scores.items():
        if score < 0.4:
            continue
        basename = Path(path).stem
        ext = Path(path).suffix

        # Source → Test: foo.py → test_foo.py / tests/test_foo.py
        if not basename.startswith("test_"):
            test_name = f"test_{basename}{ext}"
            for candidate in all_paths:
                if Path(candidate).name == test_name:
                    test_boosts[candidate] = max(test_boosts.get(candidate, 0), score * 0.4)

        # Test → Source: test_foo.py → foo.py
        if basename.startswith("test_"):
            source_name = f"{basename[5:]}{ext}"
            for candidate in all_paths:
                if Path(candidate).name == source_name:
                    test_boosts[candidate] = max(test_boosts.get(candidate, 0), score * 0.4)

    for path, boost in test_boosts.items():
        scores[path] = max(scores[path], boost)
        signal_contributions[path]["test_pair"] = boost

    # --- Cap to top MAX_WARMUP_FILES and apply score floor ---
    MAX_WARMUP_FILES = 25
    MIN_WARMUP_SCORE = 0.30

    # Sort by score, keep only top files above floor
    # IMPORTANT: Cap warmup scores below HOT threshold (0.8) so HOT slots
    # are reserved for files directly activated by the user's prompt.
    # Warmup files should start WARM and promote to HOT only via prompt activation.
    MAX_WARMUP_SCORE = HOT_THRESHOLD - 0.05  # 0.75 — high WARM, not HOT
    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])
    final_scores = {}
    for path, score in sorted_scores[:MAX_WARMUP_FILES]:
        clamped = min(MAX_WARMUP_SCORE, max(0.0, score))
        if clamped >= MIN_WARMUP_SCORE:
            final_scores[path] = clamped

    elapsed = (time.perf_counter() - t0) * 1000

    # Build state dict
    state = {
        "scores": final_scores,
        "consecutive_turns": {},
        "turn_count": 0,
        "warmup_applied": True,
        "warmup_ms": round(elapsed, 1),
        "warmup_files": len(final_scores),
    }

    if verbose:
        # Print analysis summary
        hot = sum(1 for s in final_scores.values() if s >= HOT_THRESHOLD)
        warm = sum(1 for s in final_scores.values() if WARM_THRESHOLD <= s < HOT_THRESHOLD)
        cold = sum(1 for s in final_scores.values() if s < WARM_THRESHOLD)

        print(f"\n  Hard Warmup Complete ({elapsed:.0f}ms)", file=sys.stderr)
        print(f"  Files analyzed: {len(final_scores)}", file=sys.stderr)
        print(f"  Tiers: {hot} HOT, {warm} WARM, {cold} COLD", file=sys.stderr)

        # Top files
        top = sorted(final_scores.items(), key=lambda x: -x[1])[:15]
        if top:
            print("\n  Top 15 files:", file=sys.stderr)
            for path, score in top:
                tier = "HOT" if score >= HOT_THRESHOLD else "WARM" if score >= WARM_THRESHOLD else "COLD"
                contribs = signal_contributions.get(path, {})
                signals = ", ".join(f"{k}={v:.2f}" for k, v in sorted(contribs.items(), key=lambda x: -x[1]))
                print(f"    [{tier:4s}] {score:.2f}  {path:<50s}  ({signals})", file=sys.stderr)

    return state


def analyze_warmup(project_dir: str | Path) -> dict:
    """
    Analyze and return detailed warmup information without applying it.

    Returns a dict with signal breakdowns for each file.
    """
    project_dir = Path(project_dir)

    analysis = {
        "project_dir": str(project_dir),
        "signals": {},
    }

    # Git recency
    git_recent = _git_recent_files(project_dir)
    analysis["signals"]["git_recency"] = {
        "files": len(git_recent),
        "top_5": [(p, round(s, 3)) for p, s in git_recent[:5]],
    }

    # Git frequency
    git_freq = _git_frequently_changed(project_dir)
    analysis["signals"]["git_frequency"] = {
        "files": len(git_freq),
        "top_5": [(p, round(s, 3)) for p, s in git_freq[:5]],
    }

    # File timestamps
    mtime_files = _file_timestamps(project_dir)
    analysis["signals"]["file_mtime"] = {
        "files": len(mtime_files),
        "top_5": [(p, round(s, 3)) for p, s in mtime_files[:5]],
    }

    # Import graph
    import_graph = _build_import_graph(project_dir)
    most_imported = Counter()
    for targets in import_graph.values():
        for t in targets:
            most_imported[t] += 1
    analysis["signals"]["import_graph"] = {
        "files_with_imports": len(import_graph),
        "total_edges": sum(len(v) for v in import_graph.values()),
        "most_imported": most_imported.most_common(5),
    }

    # Editor state
    editor_files = _editor_open_files(project_dir)
    analysis["signals"]["editor_state"] = {
        "files": len(editor_files),
        "paths": editor_files[:10],
    }

    # Combined warmup
    state = build_warmup_state(project_dir, verbose=False)
    scores = state["scores"]
    hot = [(p, s) for p, s in sorted(scores.items(), key=lambda x: -x[1]) if s >= HOT_THRESHOLD]
    warm = [(p, s) for p, s in sorted(scores.items(), key=lambda x: -x[1]) if WARM_THRESHOLD <= s < HOT_THRESHOLD]

    analysis["result"] = {
        "total_files": len(scores),
        "hot_count": len(hot),
        "warm_count": len(warm),
        "hot_files": [(p, round(s, 3)) for p, s in hot[:10]],
        "warm_files": [(p, round(s, 3)) for p, s in warm[:10]],
        "warmup_ms": state.get("warmup_ms", 0),
    }

    return analysis


def apply_warmup_to_state(project_dir: str | Path, attn_state_path: Path = None):
    """
    Apply hard warmup to the current attention state file.

    This is called from the CLI 'warmup' command or can be integrated
    into session_init.py for automatic hard warmup on first prompt.
    """
    project_dir = Path(project_dir)

    # Build warmup state
    warmup = build_warmup_state(project_dir, verbose=True)
    warmup_scores = warmup["scores"]

    if not warmup_scores:
        print("  No files found for warmup.", file=sys.stderr)
        return

    # Load existing attention state
    if attn_state_path is None:
        attn_state_path = project_dir / ".claude" / "attn_state.json"

    try:
        attn = json.loads(attn_state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        attn = {"scores": {}, "consecutive_turns": {}, "turn_count": 0}

    # Merge: warmup scores only boost, never reduce
    applied = 0
    for path, warmup_score in warmup_scores.items():
        current = attn.get("scores", {}).get(path, 0)
        if warmup_score > current:
            attn.setdefault("scores", {})[path] = warmup_score
            applied += 1

    # Save
    attn_state_path.parent.mkdir(parents=True, exist_ok=True)
    attn_state_path.write_text(json.dumps(attn, indent=2), encoding="utf-8")

    hot = sum(1 for s in attn["scores"].values() if s >= HOT_THRESHOLD)
    warm = sum(1 for s in attn["scores"].values() if WARM_THRESHOLD <= s < HOT_THRESHOLD)
    print(f"\n  Applied warmup: {applied} files boosted ({hot} HOT, {warm} WARM)", file=sys.stderr)
