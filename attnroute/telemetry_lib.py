#!/usr/bin/env python3
"""
attnroute.telemetry_lib — Shared utilities for token efficiency tracking.

Provides constants, I/O helpers, session state, and token estimation
used by all other attnroute modules.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# ============================================================================
# CONSTANTS
# ============================================================================

TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"
TURNS_FILE = TELEMETRY_DIR / "turns.jsonl"
SESSION_STATE_FILE = TELEMETRY_DIR / "session_state.json"
ROUTER_OVERRIDES_FILE = TELEMETRY_DIR / "router_overrides.json"
OPTIMIZATION_LOG_FILE = TELEMETRY_DIR / "optimization_log.jsonl"
LEARNED_STATE_FILE = TELEMETRY_DIR / "learned_state.json"
STATS_CACHE_FILE = Path.home() / ".claude" / "stats-cache.json"
ATTN_STATE_PROJECT = Path(".claude/attn_state.json")
ATTN_STATE_GLOBAL = Path.home() / ".claude" / "attn_state.json"


# ============================================================================
# WINDOWS ENCODING FIX
# ============================================================================

def windows_utf8_io():
    """Fix Windows cp1252 encoding for stdout/stderr. Call once at script top."""
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


# ============================================================================
# DIRECTORY SETUP
# ============================================================================

def ensure_telemetry_dir():
    """Create telemetry directory if it doesn't exist."""
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# PROJECT DETECTION
# ============================================================================

def get_project() -> str:
    """Get canonical project root, handling git worktrees.

    Uses `git rev-parse --git-common-dir` to resolve worktrees to their
    shared root. Falls back to CWD for non-git projects.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            common_git = Path(result.stdout.strip())
            # --git-common-dir returns the shared .git dir
            # For worktrees: /path/to/main/.git
            # For normal repos: .git (relative)
            # The parent of the resolved path is the canonical project root
            if common_git.is_absolute():
                return str(common_git.parent).lower().replace("\\", "/")
            else:
                # Relative path — resolve from CWD
                return str((Path.cwd() / common_git).resolve().parent).lower().replace("\\", "/")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # Fallback: CWD (non-git projects)
    return str(Path.cwd()).lower().replace("\\", "/")


def project_overrides_key() -> str:
    """Short hash of project path for keying per-project overrides."""
    return hashlib.md5(get_project().encode()).hexdigest()[:8]


# ============================================================================
# SESSION ID
# ============================================================================

def get_session_id() -> str:
    """Derive session ID from available signals."""
    for env_var in ["CLAUDE_SESSION_ID", "CLAUDE_CONVERSATION_ID", "SESSION_ID"]:
        val = os.environ.get(env_var)
        if val:
            return val
    # Fallback: hash of session_start timestamp
    state = load_session_state()
    start = state.get("session_start", "")
    if start:
        return hashlib.md5(start.encode()).hexdigest()[:12]
    return "unknown"


# ============================================================================
# JSONL I/O
# ============================================================================

def atomic_jsonl_append(path: Path, record: dict):
    """Append a JSON record to a JSONL file with basic file safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def rotate_jsonl(path: Path, max_lines: int = 500):
    """Keep only the last max_lines entries in a JSONL file."""
    if not path.exists():
        return
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(path, 'w', encoding='utf-8') as f:
                f.writelines(lines[-max_lines:])
    except Exception:
        pass


def load_turns(n: int = 25, project: str = None) -> list:
    """Load last N turn records, optionally filtered by project."""
    if not TURNS_FILE.exists():
        return []
    entries = []
    try:
        with open(TURNS_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if project and entry.get("project", "") != project:
                        continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return entries[-n:]


# ============================================================================
# STATS CACHE
# ============================================================================

def load_stats_cache() -> dict:
    """Load stats-cache.json (Claude Code's token tracking)."""
    if not STATS_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(STATS_CACHE_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


# ============================================================================
# ROUTER OVERRIDES (per-project aware)
# ============================================================================

def load_router_overrides() -> dict:
    """Load auto-tuned router parameter overrides."""
    if not ROUTER_OVERRIDES_FILE.exists():
        return {}
    try:
        return json.loads(ROUTER_OVERRIDES_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def load_project_overrides() -> dict:
    """Load merged overrides: global defaults + project-specific on top."""
    raw = load_router_overrides()
    global_params = raw.get("global", raw.get("overrides", {}))
    proj_key = project_overrides_key()
    proj_params = raw.get("projects", {}).get(proj_key, {})
    merged = dict(global_params)
    merged.update(proj_params)
    # Also include demoted_files from either level
    demoted = set(raw.get("demoted_files", []))
    demoted.update(raw.get("projects", {}).get(proj_key, {}).get("demoted_files", []))
    return {"overrides": merged, "demoted_files": list(demoted)}


def save_router_overrides(data: dict):
    """Save router overrides."""
    ensure_telemetry_dir()
    ROUTER_OVERRIDES_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ============================================================================
# SESSION STATE
# ============================================================================

def load_session_state() -> dict:
    """Load current session state."""
    if not SESSION_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def save_session_state(data: dict):
    """Save session state."""
    ensure_telemetry_dir()
    SESSION_STATE_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ============================================================================
# TURN ID
# ============================================================================

def make_turn_id() -> str:
    """Generate a short unique turn ID."""
    return uuid.uuid4().hex[:12]


# ============================================================================
# TOKEN ESTIMATION
# ============================================================================

# Try to import tiktoken for accurate token counting
try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    _enc = None


def estimate_tokens(text: str) -> int:
    """
    Estimate BPE token count from text.

    Uses tiktoken (cl100k_base encoding) when available for accurate counts.
    Falls back to heuristic ratios when tiktoken is not installed:
    - Code-heavy content: ~2.5 chars/token (lots of short identifiers, brackets)
    - Natural language: ~4.0 chars/token (English prose)
    - Markdown headers/formatting: ~3.0 chars/token

    Returns estimated token count.
    """
    if not text:
        return 0

    # Use tiktoken if available
    if TIKTOKEN_AVAILABLE and _enc:
        return len(_enc.encode(text))

    # Fallback to heuristic estimation
    total_chars = len(text)
    if total_chars == 0:
        return 0

    # Count code indicators
    code_chars = sum(1 for c in text if c in '{}[]();=<>|&!@#$%^*~`\\')
    # Count markdown indicators
    md_chars = sum(1 for c in text if c in '#-*_>')
    # Count whitespace-heavy indentation (code indicator)
    indent_lines = sum(1 for line in text.split('\n') if line.startswith('    ') or line.startswith('\t'))
    total_lines = max(text.count('\n'), 1)
    indent_ratio = indent_lines / total_lines

    # Estimate code fraction
    code_fraction = min(1.0, (code_chars / total_chars) * 10 + indent_ratio * 0.5)
    md_fraction = min(1.0 - code_fraction, (md_chars / total_chars) * 8)
    prose_fraction = 1.0 - code_fraction - md_fraction

    # Weighted average chars-per-token
    chars_per_token = (
        code_fraction * 2.5 +
        md_fraction * 3.0 +
        prose_fraction * 4.0
    )

    return max(1, int(total_chars / chars_per_token))


def estimate_tokens_from_chars(chars: int, content_type: str = "mixed") -> int:
    """
    Quick token estimate from character count when text isn't available.

    content_type: "code" (2.5), "prose" (4.0), "markdown" (3.0), "mixed" (3.3)
    """
    ratios = {"code": 2.5, "prose": 4.0, "markdown": 3.0, "mixed": 3.3}
    ratio = ratios.get(content_type, 3.3)
    return max(0, int(chars / ratio))
