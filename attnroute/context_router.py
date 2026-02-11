#!/usr/bin/env python3
"""
attnroute — Attentional Context Router
=======================================
Working-memory-inspired context injection for Claude Code.

Tiers:
  HOT  (>0.8)   Full file injection — active development focus
  WARM (0.25-0.8) Compressed TOC — background awareness
  COLD (<0.25)   Evicted from context window

Features:
  - Decay: Unmentioned files fade at configurable rates
  - Co-activation: Related files boost each other via keywords.json graph
  - Pinned files: Always at least WARM (config-driven)
  - Cache stability: HOT files sorted by streak length for API prompt cache hits
  - Self-tuning: Telemetry-driven parameter optimization
  - Domain-agnostic: All keywords loaded from keywords.json, zero hardcoded domains

Hook: UserPromptSubmit
Input: JSON from stdin {"prompt": "..."}
Output: Tiered context to stdout
"""

import copy
import io
import json
import os
import sys
from pathlib import Path

# Fix Windows encoding (cp1252 can't handle Unicode box-drawing/emoji chars)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import re
from datetime import datetime

# Import utilities for cleaner import handling
from attnroute.compat import LazyLoader, try_import

# Import telemetry lib
_telemetry_imports, TELEMETRY_LIB_AVAILABLE = try_import(
    "attnroute.telemetry_lib", "telemetry_lib",
    ["estimate_tokens_from_chars", "get_session_id", "get_project", "load_project_overrides", "rotate_jsonl"]
)
if TELEMETRY_LIB_AVAILABLE:
    estimate_tokens_from_chars = _telemetry_imports["estimate_tokens_from_chars"]
    get_session_id = _telemetry_imports["get_session_id"]
    get_project = _telemetry_imports["get_project"]
    load_project_overrides = _telemetry_imports["load_project_overrides"]
    rotate_jsonl = _telemetry_imports["rotate_jsonl"]

# Import plugin system
_plugin_imports, PLUGINS_AVAILABLE = try_import(
    "attnroute.plugins", "plugins", ["get_plugins"]
)
if PLUGINS_AVAILABLE:
    get_plugins = _plugin_imports["get_plugins"]

# Import learner with lazy initialization (avoids side effects at import time)
_learner_imports, _LEARNER_IMPORTABLE = try_import(
    "attnroute.learner", "learner", ["Learner", "auto_extract_keywords"]
)
if _LEARNER_IMPORTABLE:
    Learner = _learner_imports["Learner"]
    auto_extract_keywords = _learner_imports["auto_extract_keywords"]
    _learner_loader = LazyLoader(lambda: Learner())
    LEARNER_AVAILABLE = True
else:
    _learner_loader = None
    LEARNER_AVAILABLE = False

def get_learner():
    """Get the lazily-initialized Learner instance."""
    if _learner_loader is None:
        return None
    return _learner_loader.get()

# Import search index with lazy initialization
_indexer_imports, _INDEXER_IMPORTABLE = try_import(
    "attnroute.indexer", "indexer", ["SearchIndex", "BM25_AVAILABLE"]
)
if _INDEXER_IMPORTABLE:
    SearchIndex = _indexer_imports["SearchIndex"]
    BM25_AVAILABLE = _indexer_imports["BM25_AVAILABLE"]
    _search_loader = LazyLoader(lambda: SearchIndex())
    SEARCH_AVAILABLE = BM25_AVAILABLE
else:
    _search_loader = None
    SEARCH_AVAILABLE = False
    BM25_AVAILABLE = False

def get_search_index():
    """Get the lazily-initialized SearchIndex instance."""
    if _search_loader is None:
        return None
    return _search_loader.get()

def ensure_search_index_built():
    """Lazily build or update the search index if needed."""
    if not SEARCH_AVAILABLE:
        return

    idx = get_search_index()
    if idx is None:
        return

    try:
        status = idx.status()
        if status.get("indexed_documents", 0) == 0:
            # Index is empty - build it
            docs_root = resolve_docs_root()
            # Include source code if enabled
            code_roots = [Path.cwd()] if SOURCE_INDEXING_ENABLED else []
            idx.build(docs_root, code_roots=code_roots)
            doc_types = status.get("document_types", {})
            print(f"[attnroute] Built search index with {idx.status()['indexed_documents']} docs", file=sys.stderr)
            if SOURCE_INDEXING_ENABLED:
                code_count = doc_types.get("code", 0)
                print(f"[attnroute] Source code indexing: {code_count} files indexed", file=sys.stderr)
    except Exception as e:
        print(f"[attnroute] Search index build failed: {e}", file=sys.stderr)

# Try to import file predictor with lazy initialization (optional prefetching)
_predictor_imports, _PREDICTOR_IMPORTABLE = try_import(
    "attnroute.predictor", "predictor", ["FilePredictor"]
)
if _PREDICTOR_IMPORTABLE:
    FilePredictor = _predictor_imports["FilePredictor"]
    _predictor_loader = LazyLoader(lambda: FilePredictor())
    PREDICTOR_AVAILABLE = True
else:
    _predictor_loader = None
    PREDICTOR_AVAILABLE = False

def get_predictor():
    """Get the lazily-initialized FilePredictor instance."""
    if _predictor_loader is None:
        return None
    return _predictor_loader.get()

# Try to import repo mapper for symbol-level context (Aider-style)
try:
    from attnroute.repo_map import RepoMapper
    REPO_MAP_AVAILABLE = True
except ImportError:
    try:
        from repo_map import RepoMapper
        REPO_MAP_AVAILABLE = True
    except ImportError:
        RepoMapper = None
        REPO_MAP_AVAILABLE = False

# Try to import outliner for source code outline extraction
try:
    from attnroute.outliner import extract_outline
    OUTLINER_AVAILABLE = True
except ImportError:
    try:
        from outliner import extract_outline
        OUTLINER_AVAILABLE = True
    except ImportError:
        extract_outline = None
        OUTLINER_AVAILABLE = False

# Global repo mapper (lazily initialized)
_repo_mapper = None
_repo_mapper_project = None

def get_repo_mapper(project_root: str = None):
    """Get or create repo mapper for current project."""
    global _repo_mapper, _repo_mapper_project
    if not REPO_MAP_AVAILABLE:
        return None

    # Detect project root from CWD if not specified
    if project_root is None:
        project_root = str(Path.cwd())

    # Reuse if same project
    if _repo_mapper and _repo_mapper_project == project_root:
        return _repo_mapper

    try:
        _repo_mapper = RepoMapper(project_root, max_files=REPO_MAP_MAX_FILES)
        _repo_mapper.index(verbose=False)
        _repo_mapper_project = project_root
        return _repo_mapper
    except Exception as e:
        print(f"[attnroute] Repo map init failed: {e}", file=sys.stderr)
        return None

# Try to import networkx for graph-based co-activation
try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    nx = None
    NETWORKX_AVAILABLE = False

# Co-activation graph (built from CO_ACTIVATION dict using networkx)
_coactivation_graph = None

# ============================================================================
# DOCS ROOT RESOLUTION
# ============================================================================

def resolve_docs_root() -> Path:
    """
    Resolve documentation root with correct priority order.

    Priority:
    1. CONTEXT_DOCS_ROOT environment variable (explicit override)
    2. Project-local .claude/ directory (if exists with .md files)
    3. Global ~/.claude/ directory (fallback)

    Returns: Path to docs root
    Raises: FileNotFoundError if no valid docs directory found
    """
    # Priority 1: Explicit environment variable
    if env_root := os.getenv('CONTEXT_DOCS_ROOT'):
        env_path = Path(env_root).expanduser().resolve()
        if env_path.is_dir():
            print(f"[attnroute] Using CONTEXT_DOCS_ROOT: {env_path}", file=sys.stderr)
            return env_path
        else:
            print(f"[attnroute] WARN:CONTEXT_DOCS_ROOT set but not found: {env_path}", file=sys.stderr)

    # Priority 2: Project-local .claude/ (more explicit check)
    project_claude = Path.cwd() / ".claude"
    if project_claude.is_dir():
        # Check if it has any .md files (not just exists)
        md_files = list(project_claude.glob("**/*.md"))
        if md_files:
            print(f"[attnroute] Using project-local .claude: {project_claude}", file=sys.stderr)
            print(f"  Found {len(md_files)} .md files", file=sys.stderr)
            return project_claude
        else:
            print(f"[attnroute] WARN:Project .claude/ exists but has no .md files: {project_claude}", file=sys.stderr)

    # Priority 3: Global ~/.claude/ (last resort)
    global_claude = Path.home() / ".claude"
    if global_claude.is_dir():
        md_files = list(global_claude.glob("**/*.md"))
        if md_files:
            print(f"[attnroute] Using global ~/.claude: {global_claude}", file=sys.stderr)
            print(f"  Found {len(md_files)} .md files", file=sys.stderr)
            return global_claude
        else:
            print("[attnroute] WARN:Global ~/.claude/ exists but has no .md files", file=sys.stderr)

    # Priority 4: Fail with helpful error
    raise FileNotFoundError(
        "\n"
        "─────────────────────────────────────────────────────────\n"
        "  No .claude/ directory with documentation found.\n"
        "\n"
        "  Create .claude/ in your project root and add .md files:\n"
        "    mkdir -p .claude/\n"
        "    echo '# My Project' > .claude/README.md\n"
        "\n"
        "  Or set explicit path:\n"
        "    export CONTEXT_DOCS_ROOT=/path/to/docs\n"
        "\n"
        "  Priority order:\n"
        "    1. CONTEXT_DOCS_ROOT environment variable\n"
        "    2. Project-local .claude/ (current directory)\n"
        "    3. Global ~/.claude/ (home directory)\n"
        "─────────────────────────────────────────────────────────\n"
    )

# ============================================================================
# CONFIGURATION
# ============================================================================

# State file location
PROJECT_STATE = Path(".claude/attn_state.json")
GLOBAL_STATE = Path.home() / ".claude" / "attn_state.json"
HISTORY_FILE = Path.home() / ".claude" / "attention_history.jsonl"

# History retention
MAX_HISTORY_DAYS = 30  # Archive entries older than 30 days

# Decay rates per category (how fast files fade when not mentioned)
# Higher = slower decay (more persistent)
DECAY_RATES = {
    "systems/": 0.85,       # Hardware is stable, decay slow
    "modules/": 0.70,       # Code changes more frequently
    "integrations/": 0.80,  # APIs semi-stable
    "docs/": 0.75,          # Documentation medium decay
    "default": 0.70
}

# Attention thresholds
HOT_THRESHOLD = 0.8         # Full file injection
WARM_THRESHOLD = 0.25       # Header-only injection
# Below WARM = COLD (evicted)

# Boost amounts
KEYWORD_BOOST = 1.0         # Direct mention → score = 1.0
COACTIVATION_BOOST = 0.35   # Related file boost
TRANSITIVE_COACT_BOOST = 0.15  # 2-hop co-activation boost (A→B→C)
SEMANTIC_BOOST_WEIGHT = 0.8    # Weight for semantic search results

# Limits (prevent context explosion) - TIGHTENED for lower waste
MAX_HOT_FILES = 3           # Reduced from 4 - be more selective
MAX_WARM_FILES = 5          # Reduced from 8 - focus on truly relevant
WARM_HEADER_LINES = 25      # Lines to extract for warm context
MAX_TOTAL_CHARS = 20000     # Reduced from 25000 - tighter context

# Confidence thresholds for smarter selection
MIN_SEMANTIC_SCORE = 0.15   # Minimum semantic relevance to consider
MIN_KEYWORD_MATCHES = 1     # Minimum keyword matches to activate
SELECTION_CONFIDENCE = 0.6  # Files below this confidence are demoted

# Keyword matching thresholds
SHORT_KEYWORD_LENGTH = 4    # Keywords <= this length require word boundaries to avoid false positives
SEMANTIC_SEARCH_TOP_K = 6   # Number of semantic search results to retrieve (reduced for selectivity)

# Score adjustment factors
PINNED_FILE_FLOOR_BOOST = 0.1      # Extra boost above WARM_THRESHOLD for pinned files
DEMOTED_FILE_PENALTY = 0.5         # Multiplier for demoted files (0.5 = 50% reduction)
PREDICTIVE_PREWARM_TOP_K = 3       # Number of predicted files to pre-warm
PREDICTIVE_PREWARM_MAX_BOOST = 0.2 # Maximum boost from prediction
PREDICTIVE_PREWARM_CONFIDENCE_SCALE = 0.3  # Scale prediction probability by this
PREDICTIVE_PREWARM_MARGIN = 0.05   # Stay this far below WARM_THRESHOLD to avoid false activation

# Content compression
WARM_COMPRESSION_MAX_CHARS = 2000  # Maximum characters for warm TOC compression

# Repo map integration
REPO_MAP_MAX_FILES = 200           # Maximum files to index in repo mapper
REPO_MAP_TOKEN_BUDGET = 500        # Token budget for symbol-level context

# Notification handling
NOTIFICATION_SHORT_PROMPT_THRESHOLD = 200  # Prompts shorter than this after notification strip get clamped
NOTIFICATION_MAX_HOT_FILES = 1     # Limit hot files for notification-mixed prompts
NOTIFICATION_MAX_WARM_FILES = 2    # Limit warm files for notification-mixed prompts
NOTIFICATION_MAX_CHARS = 5000      # Limit total chars for notification-mixed prompts

# Log file rotation
LOG_MAX_SIZE_BYTES = 50_000        # Rotate log when it exceeds this size
LOG_KEEP_SIZE_BYTES = 25_000       # Keep this many bytes after rotation

# Pinned files (always at least WARM) — loaded from keywords.json "pinned" field
# Empty by default; config-driven only.
PINNED_FILES = []

# ============================================================================
# SOURCE CODE ROUTING CONFIGURATION
# ============================================================================

# Feature flag - enables source code indexing and routing
SOURCE_INDEXING_ENABLED = True

# Source file limits (separate from doc limits to prevent crowding out docs)
SOURCE_MAX_HOT_FILES = 2           # Max source files to inject as HOT
SOURCE_MAX_WARM_FILES = 3          # Max source files to inject as WARM
SOURCE_MAX_CHARS = 8000            # Max total chars for source context

# Source file size limit (skip large generated/minified files)
SOURCE_MAX_FILE_SIZE = 100_000     # 100KB - skip files larger than this

# Maximum tracked source files in state (prevent unbounded growth)
MAX_TRACKED_SOURCE_FILES = 50

# Supported source file extensions
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt"
}

# Directories to exclude from source indexing
SOURCE_EXCLUDED_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv",
    "dist", "build", "target", ".next", ".nuxt", "coverage",
    ".pytest_cache", ".mypy_cache", ".tox", "eggs", "*.egg-info"
}


def _is_source_file(path: str) -> bool:
    """Check if a path is a source code file based on extension."""
    return Path(path).suffix.lower() in SOURCE_EXTENSIONS


def _is_doc_file(path: str, docs_root: Path = None) -> bool:
    """Check if a path is a documentation file (under .claude/)."""
    return path.endswith(".md") or ".claude" in path


def _evict_lowest_source(state: dict, new_path: str, new_score: float) -> None:
    """
    Evict the lowest-scoring source file to make room for a new one.

    Only evicts if the new file's score is higher than the lowest.
    """
    source_files = [(p, s) for p, s in state["scores"].items() if _is_source_file(p)]
    if not source_files:
        return

    # Find lowest scoring source file
    lowest_path, lowest_score = min(source_files, key=lambda x: x[1])

    # Only evict if new score is higher
    if new_score > lowest_score:
        del state["scores"][lowest_path]
        if lowest_path in state.get("consecutive_turns", {}):
            del state["consecutive_turns"][lowest_path]
        # Add the new file
        state["scores"][new_path] = new_score
        state["consecutive_turns"][new_path] = 0

# ============================================================================
# CONFIG LOADING
# Load keywords and co-activation from external config if available
# ============================================================================

def load_keyword_config() -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """
    Load keywords, co-activation graph, and pinned files from keywords.json.
    Falls back to hardcoded defaults if config doesn't exist or fails to parse.

    Returns: (keywords_dict, co_activation_dict, pinned_list)
    """
    # Try project-local config first, then global
    config_paths = [
        Path(".claude/keywords.json"),
        Path.home() / ".claude" / "keywords.json"
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding='utf-8'))
                keywords = config.get("keywords", {})
                co_activation = config.get("co_activation", {})
                pinned = config.get("pinned", [])

                # Validate structure
                if keywords and isinstance(keywords, dict):
                    print(f"[attnroute] Loaded keywords from {config_path}", file=sys.stderr)
                    return keywords, co_activation, pinned
            except (json.JSONDecodeError, Exception) as e:
                print(f"[attnroute] WARN:Failed to load {config_path}: {e}", file=sys.stderr)
                continue

    # Fallback: auto-extract keywords from .md file content (zero-config mode)
    if LEARNER_AVAILABLE:
        try:
            docs_root = Path(".claude") if Path(".claude").is_dir() else Path.home() / ".claude"
            auto_kw = auto_extract_keywords(docs_root)
            if auto_kw:
                print(f"[attnroute] Auto-extracted keywords from {len(auto_kw)} files (no keywords.json)", file=sys.stderr)
                return auto_kw, {}, []
        except Exception:
            pass

    print("[attnroute] WARN: No keywords.json found — attnroute has no routing rules.", file=sys.stderr)
    print("  Run 'attnroute init' to generate a keywords.json template.", file=sys.stderr)
    return _DEFAULT_KEYWORDS, _DEFAULT_CO_ACTIVATION, []

# ============================================================================
# KEYWORD MAPPINGS
# What words/phrases activate which files
# ============================================================================

# Domain-agnostic defaults — zero hardcoded domain keywords.
# attnroute is a generic keyword→file routing engine.
# All domain-specific keywords belong in keywords.json config files.
# These structural defaults only match common doc organization patterns.
_DEFAULT_KEYWORDS: dict[str, list[str]] = {}

# ============================================================================
# CO-ACTIVATION GRAPH
# When one file activates, these related files get a boost
# ============================================================================

# Domain-agnostic: empty by default. All co-activation belongs in keywords.json.
_DEFAULT_CO_ACTIVATION: dict[str, list[str]] = {}

# Load actual configuration (from keywords.json or fallback to defaults)
KEYWORDS, CO_ACTIVATION, _LOADED_PINNED = load_keyword_config()

# Override hardcoded PINNED_FILES with config-loaded ones if available
if _LOADED_PINNED:
    PINNED_FILES = _LOADED_PINNED


def build_bidirectional_coactivation(co_act: dict) -> dict:
    """Ensure A->B implies B->A (symmetric co-activation)."""
    bidirectional = {}
    for source, targets in co_act.items():
        if source not in bidirectional:
            bidirectional[source] = set()
        for t in targets:
            bidirectional[source].add(t)
            if t not in bidirectional:
                bidirectional[t] = set()
            bidirectional[t].add(source)
    return {k: list(v) for k, v in bidirectional.items()}


CO_ACTIVATION = build_bidirectional_coactivation(CO_ACTIVATION)

# Merge learned co-activation from the intelligence engine
if LEARNER_AVAILABLE:
    _learner_instance = get_learner()
    if _learner_instance is not None:  # C2: Guard against None if Learner init fails
        _learned_coact = _learner_instance.get_learned_coactivation()
        for source, targets in _learned_coact.items():
            if source not in CO_ACTIVATION:
                CO_ACTIVATION[source] = []
            for t in targets:
                if t not in CO_ACTIVATION[source]:
                    CO_ACTIVATION[source].append(t)

# Build networkx graph for transitive co-activation (2-hop relationships)
def build_coactivation_graph(co_act: dict):
    """Build networkx graph for transitive co-activation queries."""
    global _coactivation_graph
    if not NETWORKX_AVAILABLE:
        return None

    G = nx.Graph()
    for source, targets in co_act.items():
        G.add_node(source)
        for target in targets:
            G.add_edge(source, target, weight=1.0)

    _coactivation_graph = G
    return G

def get_transitive_coactivation(activated_files: set[str], max_hops: int = 2) -> dict[str, float]:
    """
    Get transitively co-activated files up to max_hops away.

    Returns: dict of {file_path: boost_score} where boost decays with distance
    """
    if not NETWORKX_AVAILABLE or _coactivation_graph is None:
        return {}

    transitive = {}
    for source in activated_files:
        if source not in _coactivation_graph:
            continue

        # BFS to find files within max_hops
        try:
            lengths = nx.single_source_shortest_path_length(_coactivation_graph, source, cutoff=max_hops)
            for target, distance in lengths.items():
                if target == source or target in activated_files:
                    continue
                # Decay boost by distance: hop 1 = COACTIVATION_BOOST, hop 2 = TRANSITIVE_COACT_BOOST
                if distance == 1:
                    boost = COACTIVATION_BOOST
                else:
                    boost = TRANSITIVE_COACT_BOOST / distance
                # Take max if file reachable from multiple sources
                transitive[target] = max(transitive.get(target, 0), boost)
        except nx.NetworkXError:
            continue

    return transitive

# Initialize the graph
_coactivation_graph = build_coactivation_graph(CO_ACTIVATION) if NETWORKX_AVAILABLE else None

# Keyword weights placeholder (learner.boost_scores() handles learned associations instead)
_KEYWORD_WEIGHTS: dict[str, float] = {}

# ============================================================================
# COMPILED KEYWORD REGEX (performance optimization)
# Pre-compile one regex per file path. Replaces ~200 regex ops/turn with ~24.
# ============================================================================

def _build_compiled_keywords(keywords: dict[str, list[str]]) -> dict[str, re.Pattern]:
    """Build compiled regex for each file's keywords."""
    compiled = {}
    for path, kw_list in keywords.items():
        if not kw_list:
            continue
        # Escape keywords and join with | for alternation
        # Add word boundaries for short keywords (<=4 chars)
        patterns = []
        for kw in kw_list:
            escaped = re.escape(kw)
            if len(kw) <= SHORT_KEYWORD_LENGTH:
                patterns.append(r'\b' + escaped + r'\b')
            else:
                patterns.append(escaped)
        try:
            compiled[path] = re.compile('|'.join(patterns), re.IGNORECASE)
        except re.error:
            # Fallback: skip this file's regex if pattern is invalid
            pass
    return compiled

_COMPILED_KEYWORDS: dict[str, re.Pattern] = _build_compiled_keywords(KEYWORDS)

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

def get_state_file() -> Path:
    """Get appropriate state file (project-local preferred)."""
    if PROJECT_STATE.parent.exists():
        return PROJECT_STATE
    GLOBAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    return GLOBAL_STATE


def load_state(state_file: Path) -> dict:
    """Load attention state from file."""
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            pass

    # Initialize fresh state
    return {
        "scores": {path: 0.0 for path in KEYWORDS},
        "consecutive_turns": {path: 0 for path in KEYWORDS},  # Streak tracking for cache stability
        "turn_count": 0,
        "last_update": datetime.now().isoformat(),
    }


def save_state(state_file: Path, state: dict) -> None:
    """Save attention state to file (atomic write to prevent corruption)."""
    state["last_update"] = datetime.now().isoformat()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # H2: Atomic write - write to temp file then rename
    temp_file = state_file.with_suffix('.tmp')
    temp_file.write_text(json.dumps(state, indent=2), encoding='utf-8')
    temp_file.replace(state_file)


# ============================================================================
# ATTENTION DYNAMICS
# ============================================================================

def get_decay_rate(file_path: str) -> float:
    """Get decay rate for a file. Prefers learned rhythm, then category, then default."""
    # Priority 1: Learned per-file rhythm from the intelligence engine
    if LEARNER_AVAILABLE:
        learned = get_learner().get_file_decay(file_path)
        if learned is not None:
            return learned

    # Priority 2: Category-based decay from config
    for prefix, rate in DECAY_RATES.items():
        if file_path.startswith(prefix):
            return rate
    return DECAY_RATES["default"]


def keyword_matches(keyword: str, text: str) -> bool:
    """Match keyword with word boundaries for short words to prevent false positives."""
    if len(keyword) <= SHORT_KEYWORD_LENGTH:
        return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))
    return keyword in text


def _keyword_activate(state: dict, prompt_lower: str, directly_activated: set[str]):
    """Keyword-based activation using compiled regex (fallback path)."""
    for path in KEYWORDS:
        if path in _COMPILED_KEYWORDS:
            # Fast path: single compiled regex search
            if _COMPILED_KEYWORDS[path].search(prompt_lower):
                state["scores"][path] = KEYWORD_BOOST
                directly_activated.add(path)
        else:
            # Fallback: individual keyword matching (rarely used)
            keywords = KEYWORDS[path]
            if any(keyword_matches(kw, prompt_lower) for kw in keywords):
                state["scores"][path] = KEYWORD_BOOST
                directly_activated.add(path)


def update_attention(state: dict, prompt: str) -> tuple[dict, set[str]]:
    """
    Update attention scores based on prompt content.
    Returns updated state and set of directly activated files.

    Pipeline:
      1. Decay all scores
      2. Semantic search (if available) OR keyword activation (fallback)
      3. Learned association boost
      4. Co-activation boost (direct + transitive via graph)
      5. Pinned file floor
      6. Demoted file penalty
      7. Update streak counters
    """
    # Ensure search index is built (lazy initialization)
    if state.get("turn_count", 0) == 0:
        ensure_search_index_built()

    prompt_lower = prompt.lower()
    directly_activated: set[str] = set()

    # Ensure consecutive_turns dict exists (backwards compat with old state files)
    if "consecutive_turns" not in state:
        state["consecutive_turns"] = {path: 0 for path in state.get("scores", {})}

    # Phase 1: Decay all scores
    for path in state["scores"]:
        decay = get_decay_rate(path)
        state["scores"][path] *= decay

    # Phase 2: Semantic search (if available) OR keyword activation
    # Use semantic search with minimum score threshold for smarter selection
    if SEARCH_AVAILABLE and get_search_index():
        try:
            results = get_search_index().query(prompt, top_k=SEMANTIC_SEARCH_TOP_K)
            for path, relevance in results:
                # Only activate if above minimum semantic score
                if relevance >= MIN_SEMANTIC_SCORE:
                    boost = min(1.0, relevance * SEMANTIC_BOOST_WEIGHT)
                    # If path is already in state, update if higher
                    if path in state["scores"]:
                        if boost > state["scores"][path]:
                            state["scores"][path] = boost
                            directly_activated.add(path)
                    else:
                        # Dynamically add source files found by search
                        # This allows state to grow organically as user works
                        if _is_source_file(path) and SOURCE_INDEXING_ENABLED:
                            # Check if we're at capacity for tracked source files
                            source_count = sum(1 for p in state["scores"] if _is_source_file(p))
                            if source_count < MAX_TRACKED_SOURCE_FILES:
                                state["scores"][path] = boost
                                state["consecutive_turns"][path] = 0
                                directly_activated.add(path)
                            else:
                                # Evict lowest-score source file to make room
                                _evict_lowest_source(state, path, boost)
                                if path in state["scores"]:
                                    directly_activated.add(path)
        except Exception:
            # Fallback to keyword activation if search fails
            _keyword_activate(state, prompt_lower, directly_activated)
    else:
        # No search available - use keyword activation
        _keyword_activate(state, prompt_lower, directly_activated)

    # Phase 2.5: Learned association boost (from intelligence engine)
    if LEARNER_AVAILABLE:
        state["scores"] = get_learner().boost_scores(prompt, state["scores"])

    # Phase 3: Co-activation boost (direct neighbors)
    for activated_path in directly_activated:
        if activated_path in CO_ACTIVATION:
            for related_path in CO_ACTIVATION[activated_path]:
                if related_path in state["scores"]:
                    current = state["scores"][related_path]
                    state["scores"][related_path] = min(1.0, current + COACTIVATION_BOOST)

    # Phase 3.5: Transitive co-activation (2-hop neighbors via graph)
    if NETWORKX_AVAILABLE and _coactivation_graph is not None:
        transitive_boosts = get_transitive_coactivation(directly_activated, max_hops=2)
        for path, boost in transitive_boosts.items():
            if path in state["scores"]:
                current = state["scores"][path]
                # Only apply if file isn't already activated
                if current < WARM_THRESHOLD:
                    state["scores"][path] = min(1.0, current + boost)

    # Phase 4: Pinned file floor
    for pinned in PINNED_FILES:
        if pinned in state["scores"]:
            state["scores"][pinned] = max(state["scores"][pinned], WARM_THRESHOLD + PINNED_FILE_FLOOR_BOOST)

    # Phase 5: Apply demoted file penalty from telemetry overrides
    if TELEMETRY_LIB_AVAILABLE:
        try:
            proj_overrides = load_project_overrides()
            demoted = proj_overrides.get("demoted_files", [])
            for path in demoted:
                if path in state["scores"] and path not in directly_activated:
                    state["scores"][path] *= DEMOTED_FILE_PENALTY
        except Exception:
            pass

    # Phase 5.5: Predictive pre-warming (boost predicted files if currently COLD)
    predictor = get_predictor() if PREDICTOR_AVAILABLE else None
    if predictor:
        try:
            active_files = [p for p, s in state["scores"].items() if s >= WARM_THRESHOLD]
            predictions = predictor.predict(active_files, top_k=PREDICTIVE_PREWARM_TOP_K)
            for path, prob in predictions:
                if path in state["scores"]:
                    current = state["scores"][path]
                    if current < WARM_THRESHOLD:  # Only pre-warm COLD files
                        # Boost by prediction confidence, scaled and capped
                        boost = min(PREDICTIVE_PREWARM_MAX_BOOST, prob * PREDICTIVE_PREWARM_CONFIDENCE_SCALE)
                        state["scores"][path] = min(WARM_THRESHOLD - PREDICTIVE_PREWARM_MARGIN, current + boost)
        except Exception:
            pass

    # Phase 6: Update consecutive_turns counters for prompt cache stability
    # Files that are HOT or WARM this turn increment their streak.
    # Files that drop to COLD reset to 0.
    for path, score in state["scores"].items():
        tier = get_tier(score)
        if tier in ("HOT", "WARM"):
            state["consecutive_turns"][path] = state["consecutive_turns"].get(path, 0) + 1
        else:
            state["consecutive_turns"][path] = 0

    state["turn_count"] = state.get("turn_count", 0) + 1
    return state, directly_activated


# ============================================================================
# CONTENT EXTRACTION
# ============================================================================

def compress_warm(content: str, max_chars: int = WARM_COMPRESSION_MAX_CHARS) -> str:
    """
    Compress markdown content to TOC-style summary for WARM tier.

    Extracts:
    - All heading lines (# ## ### etc.)
    - First 2 non-empty lines after each heading (context lines)
    - Bullet points at top level (- or * prefixed)

    Target: ~50% size reduction compared to raw header extraction.
    """
    lines = content.split('\n')
    result = []
    chars = 0
    after_heading = 0  # Counter for lines after last heading

    for line in lines:
        stripped = line.strip()

        # Always include headings
        if stripped.startswith('#'):
            if chars + len(line) + 1 > max_chars:
                break
            result.append(line)
            chars += len(line) + 1
            after_heading = 0
            continue

        # Include first 2 non-empty lines after a heading
        if after_heading < 2 and stripped:
            if chars + len(line) + 1 > max_chars:
                break
            result.append(line)
            chars += len(line) + 1
            after_heading += 1
            continue

        # Include top-level bullet points
        if stripped.startswith(('-', '*')) and not stripped.startswith('---'):
            if chars + len(line) + 1 > max_chars:
                break
            result.append(line)
            chars += len(line) + 1
            continue

    compressed = '\n'.join(result)
    if len(compressed) < len(content):
        compressed += "\n\n... [WARM: Compressed TOC, mention to expand] ..."
    return compressed


def extract_warm_header(file_path: str, docs_root: Path, use_compression: bool = True) -> str | None:
    """
    Extract structured header for warm context.

    If use_compression=True (default), uses compress_warm() for TOC-style extraction.
    Otherwise falls back to first WARM_HEADER_LINES lines.
    Returns None if file missing.
    """
    full_path = docs_root / file_path
    if not full_path.exists():
        return None

    try:
        content = full_path.read_text(encoding='utf-8')

        if use_compression:
            return compress_warm(content)

        # Fallback: raw line-based extraction
        lines = content.split('\n')[:WARM_HEADER_LINES]
        header = '\n'.join(lines)

        # Add truncation marker if we cut content
        if len(content.split('\n')) > WARM_HEADER_LINES:
            header += "\n\n... [WARM: Content truncated, mention to expand] ..."

        return header
    except Exception as e:
        return f"[Error reading {file_path}: {e}]"


def get_full_content(file_path: str, docs_root: Path) -> str | None:
    """Get full file content for hot context."""
    full_path = docs_root / file_path
    if not full_path.exists():
        return None

    try:
        return full_path.read_text(encoding='utf-8')
    except Exception as e:
        return f"[Error reading {file_path}: {e}]"


# ============================================================================
# TIERED INJECTION
# ============================================================================

def get_tier(score: float) -> str:
    """Classify attention score into tier."""
    if score >= HOT_THRESHOLD:
        return "HOT"
    elif score >= WARM_THRESHOLD:
        return "WARM"
    return "COLD"


def _cache_sort_key(item: tuple[str, float], state: dict) -> tuple[int, int, float]:
    """
    Sort key for prompt cache stability.

    Priority (descending):
    1. Pinned files first (always present → always cached by API)
    2. Longest consecutive streak (most stable in context → highest cache hit)
    3. Highest attention score (tiebreaker)

    Returns tuple for descending sort (negate values since sorted() is ascending).
    """
    path, score = item
    is_pinned = 1 if path in PINNED_FILES else 0
    streak = state.get("consecutive_turns", {}).get(path, 0)
    return (-is_pinned, -streak, -score)


def _get_source_outline(file_path: str) -> str | None:
    """
    Get tree-sitter outline for a source file.

    Returns outline string or None if unavailable.
    Falls back to first 50 lines if outliner unavailable.
    """
    if not _is_source_file(file_path):
        return None

    full_path = Path.cwd() / file_path
    if not full_path.exists():
        # Try as absolute path
        full_path = Path(file_path)
        if not full_path.exists():
            return None

    # Check file size limit
    try:
        if full_path.stat().st_size > SOURCE_MAX_FILE_SIZE:
            return None
    except Exception:
        return None

    # Try tree-sitter outline first
    if OUTLINER_AVAILABLE and extract_outline:
        try:
            outline = extract_outline(full_path)
            if outline:
                return outline
        except Exception:
            pass

    # Fallback: first 50 lines
    try:
        content = full_path.read_text(encoding='utf-8', errors='replace')
        lines = content.split('\n')[:50]
        if len(content.split('\n')) > 50:
            lines.append("\n... [Source file truncated - use Read for full content] ...")
        return '\n'.join(lines)
    except Exception:
        return None


def _get_source_summary(file_path: str) -> str | None:
    """
    Get a one-line summary for WARM source files.

    Extracts first docstring or comment from the file.
    """
    full_path = Path.cwd() / file_path
    if not full_path.exists():
        full_path = Path(file_path)
        if not full_path.exists():
            return None

    try:
        content = full_path.read_text(encoding='utf-8', errors='replace')
        lines = content.split('\n')

        # Look for docstring or comment in first 10 lines
        for line in lines[:10]:
            stripped = line.strip()
            # Python docstring
            if stripped.startswith('"""') or stripped.startswith("'''"):
                doc = stripped.strip('"\'').strip()
                if doc:
                    return doc[:100]
            # Multi-line comment start
            if stripped.startswith('/*'):
                doc = stripped.lstrip('/*').strip()
                if doc:
                    return doc[:100]
            # Single-line comment
            if stripped.startswith('//') or stripped.startswith('#'):
                doc = stripped.lstrip('/#').strip()
                if doc and not doc.startswith('!'):  # Skip shebang
                    return doc[:100]

        return f"Source file: {Path(file_path).suffix} ({len(lines)} lines)"
    except Exception:
        return None


def build_context_output(state: dict, docs_root: Path) -> tuple[str, dict]:
    """
    Build tiered context output respecting limits.

    HOT files sorted for prompt cache stability:
    - Pinned files first (always present → always in cache prefix)
    - Then by consecutive_turns streak (longest streak = most stable = best cache hit)
    - Then by score (tiebreaker)

    Source files get tree-sitter outlines, not full content injection.
    Doc files get full content (HOT) or compressed TOC (WARM).

    Returns (output_string, stats_dict).
    """
    # Sort files by attention score (highest first) for tier assignment
    sorted_files = sorted(
        state["scores"].items(),
        key=lambda x: x[1],
        reverse=True
    )

    # Separate files into tiers AND by type (doc vs source)
    hot_docs = []
    hot_sources = []
    warm_docs = []
    warm_sources = []
    stats = {"hot": 0, "warm": 0, "cold": 0, "hot_src": 0, "warm_src": 0}
    total_chars = 0
    source_chars = 0

    for file_path, score in sorted_files:
        tier = get_tier(score)
        is_source = _is_source_file(file_path)

        if tier == "HOT":
            if is_source:
                hot_sources.append((file_path, score))
            else:
                hot_docs.append((file_path, score))
        elif tier == "WARM":
            if is_source:
                warm_sources.append((file_path, score))
            else:
                warm_docs.append((file_path, score))
        else:
            stats["cold"] += 1

    # Sort for prompt cache stability
    hot_docs.sort(key=lambda item: _cache_sort_key(item, state))
    hot_docs = hot_docs[:MAX_HOT_FILES]

    warm_docs.sort(key=lambda item: _cache_sort_key(item, state))
    warm_docs = warm_docs[:MAX_WARM_FILES]

    # Limit source files separately
    hot_sources.sort(key=lambda item: _cache_sort_key(item, state))
    hot_sources = hot_sources[:SOURCE_MAX_HOT_FILES]

    warm_sources.sort(key=lambda item: _cache_sort_key(item, state))
    warm_sources = warm_sources[:SOURCE_MAX_WARM_FILES]

    hot_blocks = []
    warm_blocks = []
    source_blocks = []
    repo_map_block = None

    # Try to get repo map for symbol-level context (Aider-style efficiency)
    mapper = get_repo_mapper() if REPO_MAP_AVAILABLE else None

    # === DOC FILES ===
    # SMART INJECTION STRATEGY for docs:
    # 1. First HOT doc: full content
    # 2. Additional HOT docs: repo map only (symbol-level)
    # 3. WARM docs: headers only

    first_hot = True
    hot_files_for_repomap = []

    for file_path, score in hot_docs:
        streak = state.get("consecutive_turns", {}).get(file_path, 0)

        if first_hot:
            content = get_full_content(file_path, docs_root)
            if content and total_chars + len(content) < MAX_TOTAL_CHARS:
                hot_blocks.append(f"─── [HOT] {file_path} (score: {score:.2f}, streak: {streak}) ───\n{content}")
                total_chars += len(content)
                stats["hot"] += 1
                first_hot = False
            elif content:
                hot_files_for_repomap.append(file_path)
                stats["hot"] += 1
        else:
            hot_files_for_repomap.append(file_path)
            stats["hot"] += 1

    # Generate repo map for additional hot doc files
    if mapper and hot_files_for_repomap:
        try:
            repo_map_content = mapper.get_context_for_files(
                hot_files_for_repomap,
                include_related=True,
                token_budget=REPO_MAP_TOKEN_BUDGET
            )
            if repo_map_content:
                repo_map_block = f"─── [REPO MAP] Symbol-level context ───\n{repo_map_content}"
                total_chars += len(repo_map_content)
        except Exception as e:
            print(f"[attnroute] Repo map generation failed: {e}", file=sys.stderr)

    for file_path, score in warm_docs:
        if stats["warm"] >= MAX_WARM_FILES:
            break
        header = extract_warm_header(file_path, docs_root)
        if header and total_chars + len(header) < MAX_TOTAL_CHARS:
            warm_blocks.append(f"─── [WARM] {file_path} (score: {score:.2f}) ───\n{header}")
            total_chars += len(header)
            stats["warm"] += 1

    # === SOURCE FILES ===
    # HOT sources: inject tree-sitter outline (not full content!)
    # WARM sources: inject file path + one-line summary

    for file_path, score in hot_sources:
        if source_chars >= SOURCE_MAX_CHARS:
            break
        outline = _get_source_outline(file_path)
        if outline and source_chars + len(outline) < SOURCE_MAX_CHARS:
            source_blocks.append(f"─── [HOT:SRC] {file_path} (score: {score:.2f}) ───\n{outline}")
            source_chars += len(outline)
            stats["hot_src"] += 1

    for file_path, score in warm_sources:
        if source_chars >= SOURCE_MAX_CHARS:
            break
        summary = _get_source_summary(file_path)
        if summary:
            line = f"─── [WARM:SRC] {file_path} (score: {score:.2f}) ───\n{summary}"
            if source_chars + len(line) < SOURCE_MAX_CHARS:
                source_blocks.append(line)
                source_chars += len(line)
                stats["warm_src"] += 1

    # Add source chars to total
    total_chars += source_chars

    # Combine output
    output_parts = []

    # Status header
    turn_label = f"Turn {state['turn_count']}"
    tier_line = f"Hot: {stats['hot']}  Warm: {stats['warm']}  Cold: {stats['cold']}"
    src_line = f"Src: {stats['hot_src']}H/{stats['warm_src']}W" if (stats['hot_src'] + stats['warm_src']) > 0 else ""
    chars_line = f"Chars: {total_chars:,} / {MAX_TOTAL_CHARS:,}"
    header_w = max(len(turn_label), len(tier_line), len(chars_line), len(src_line)) + 4
    header_lines = [
        f"┌─ {turn_label} {'─' * (header_w - len(turn_label) - 3)}┐",
        f"│ {tier_line:<{header_w - 2}} │",
    ]
    if src_line:
        header_lines.append(f"│ {src_line:<{header_w - 2}} │")
    header_lines.extend([
        f"│ {chars_line:<{header_w - 2}} │",
        f"└{'─' * header_w}┘",
    ])
    header = "\n".join(header_lines)
    output_parts.append(header)

    # Hot doc files first (stable order for cache)
    output_parts.extend(hot_blocks)

    # Repo map for additional hot doc files
    if repo_map_block:
        output_parts.append(repo_map_block)

    # Warm doc files
    output_parts.extend(warm_blocks)

    # Source files (with [HOT:SRC] / [WARM:SRC] labels)
    output_parts.extend(source_blocks)

    return "\n\n".join(output_parts), stats


# ============================================================================
# ATTENTION HISTORY TRACKING
# ============================================================================

def compute_transitions(prev_state: dict, curr_state: dict) -> dict:
    """Compute what moved between tiers."""
    transitions = {"to_hot": [], "to_warm": [], "to_cold": []}

    for path, score in curr_state["scores"].items():
        prev_score = prev_state.get("scores", {}).get(path, 0.0)
        prev_tier = get_tier(prev_score)
        curr_tier = get_tier(score)

        if curr_tier != prev_tier:
            if curr_tier == "HOT":
                transitions["to_hot"].append(path)
            elif curr_tier == "WARM":
                transitions["to_warm"].append(path)
            else:
                transitions["to_cold"].append(path)

    return transitions


def append_history(state: dict, prev_state: dict, activated: set[str], prompt: str, stats: dict):
    """Append structured entry to history log."""

    # Extract keywords from prompt (simple: first 8 significant words)
    stop_words = {"the", "a", "an", "is", "are", "to", "for", "and", "or", "in", "on", "it", "this", "that", "with", "of"}
    words = [w.lower() for w in prompt.split() if len(w) > 2 and w.lower() not in stop_words][:8]

    entry = {
        "turn": state["turn_count"],
        "timestamp": datetime.now().isoformat(),
        "instance_id": os.environ.get("CLAUDE_INSTANCE", "default"),
        "prompt_keywords": words,
        "activated": sorted(list(activated)),
        "hot": sorted([p for p, s in state["scores"].items() if get_tier(s) == "HOT"]),
        "warm": sorted([p for p, s in state["scores"].items() if get_tier(s) == "WARM"]),
        "cold_count": stats["cold"],
        "transitions": compute_transitions(prev_state, state),
        "total_chars": stats.get("total_chars", 0)
    }

    try:
        # Ensure history file exists
        if not HISTORY_FILE.exists():
            HISTORY_FILE.parent.mkdir(exist_ok=True)
            HISTORY_FILE.touch()

        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Don't fail hook on history write error


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def strip_notifications(prompt: str) -> tuple:
    """
    Strip <task-notification> and <system-reminder> XML from prompt.
    Returns (cleaned_prompt, was_notification).
    """
    original_len = len(prompt)
    cleaned = re.sub(r'<task-notification>.*?</task-notification>', '', prompt, flags=re.DOTALL)
    cleaned = re.sub(r'<system-reminder>.*?</system-reminder>', '', cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip()
    was_notification = len(cleaned) < original_len
    return cleaned, was_notification


# Cache for router overrides (avoid disk read every turn)
_OVERRIDES_CACHE: dict = {"params": {}, "mtime": 0.0, "path": None}

def load_telemetry_overrides():
    """Load auto-tuned parameter overrides from telemetry system (per-project aware).

    Uses mtime-based caching to avoid disk reads on every turn.
    """
    global MAX_HOT_FILES, MAX_WARM_FILES, MAX_TOTAL_CHARS, COACTIVATION_BOOST, DECAY_RATES

    overrides_file = Path.home() / ".claude" / "telemetry" / "router_overrides.json"

    # Check cache validity
    try:
        if overrides_file.exists():
            current_mtime = overrides_file.stat().st_mtime
            if (str(overrides_file) == _OVERRIDES_CACHE["path"] and
                current_mtime == _OVERRIDES_CACHE["mtime"]):
                params = _OVERRIDES_CACHE["params"]
            else:
                # Cache miss or stale - reload
                if TELEMETRY_LIB_AVAILABLE:
                    proj = load_project_overrides()
                    params = proj.get("overrides", {})
                else:
                    data = json.loads(overrides_file.read_text(encoding='utf-8'))
                    params = data.get("overrides", {})
                # Update cache
                _OVERRIDES_CACHE["params"] = params
                _OVERRIDES_CACHE["mtime"] = current_mtime
                _OVERRIDES_CACHE["path"] = str(overrides_file)
                # H4: Only log on cache miss (when we actually load new values)
                if params:
                    print(f"[attnroute] Loaded overrides: HOT={params.get('MAX_HOT_FILES', 'default')} WARM={params.get('MAX_WARM_FILES', 'default')} CHARS={params.get('MAX_TOTAL_CHARS', 'default')}", file=sys.stderr)
        else:
            return
    except Exception:
        return

    if "MAX_HOT_FILES" in params:
        MAX_HOT_FILES = int(params["MAX_HOT_FILES"])
    if "MAX_WARM_FILES" in params:
        MAX_WARM_FILES = int(params["MAX_WARM_FILES"])
    if "MAX_TOTAL_CHARS" in params:
        MAX_TOTAL_CHARS = int(params["MAX_TOTAL_CHARS"])
    if "COACTIVATION_BOOST" in params:
        COACTIVATION_BOOST = float(params["COACTIVATION_BOOST"])
    if "DECAY_RATES.default" in params:
        DECAY_RATES["default"] = float(params["DECAY_RATES.default"])


def measure_claude_md() -> int:
    """Measure total CLAUDE.md injection size (global + project-local)."""
    total = 0
    # Global CLAUDE.md
    global_md = Path.home() / ".claude" / "CLAUDE.md"
    if global_md.exists():
        try:
            total += global_md.stat().st_size
        except Exception:
            pass
    # Project-local CLAUDE.md
    project_md = Path.cwd() / "CLAUDE.md"
    if project_md.exists():
        try:
            total += project_md.stat().st_size
        except Exception:
            pass
    # Also check .claude/CLAUDE.md in project
    project_claude_md = Path.cwd() / ".claude" / "CLAUDE.md"
    if project_claude_md.exists() and project_claude_md != project_md:
        try:
            total += project_claude_md.stat().st_size
        except Exception:
            pass
    return total


def measure_pool_size() -> int:
    """Estimate pool injection size from instance_state.jsonl."""
    pool_files = [
        Path.cwd() / ".claude" / "pool" / "instance_state.jsonl",
        Path.home() / ".claude" / "pool" / "instance_state.jsonl",
    ]
    for pool_file in pool_files:
        if pool_file.exists():
            try:
                # Pool loader takes last 20 entries within 1 hour
                lines = pool_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
                recent = lines[-20:] if len(lines) > 20 else lines
                return sum(len(line) for line in recent)
            except Exception:
                pass
    return 0


def record_turn_telemetry(prompt: str, was_notification: bool, stats: dict,
                          activated: set, state: dict, injection_chars: int):
    """Record turn data to telemetry/turns.jsonl for analysis."""
    try:
        telemetry_dir = Path.home() / ".claude" / "telemetry"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        turns_file = telemetry_dir / "turns.jsonl"

        hot_files = sorted([p for p, s in state["scores"].items() if s >= HOT_THRESHOLD])
        warm_files = sorted([p for p, s in state["scores"].items() if WARM_THRESHOLD <= s < HOT_THRESHOLD])

        # Measure external context sources
        claude_md_chars = measure_claude_md()
        pool_chars = measure_pool_size()

        # Token estimates (markdown content → ~3.0 chars/token)
        est_fn = estimate_tokens_from_chars if TELEMETRY_LIB_AVAILABLE else lambda c, t="markdown": max(0, int(c / 3.3))
        injection_tokens = est_fn(injection_chars, "markdown")
        claude_md_tokens = est_fn(claude_md_chars, "markdown")
        pool_tokens = est_fn(pool_chars, "mixed")
        total_tokens = injection_tokens + claude_md_tokens + pool_tokens

        record = {
            "turn_id": os.urandom(6).hex(),
            "timestamp": datetime.now().isoformat(),
            "project": get_project() if TELEMETRY_LIB_AVAILABLE else str(Path.cwd()).lower().replace("\\", "/"),
            "session_id": get_session_id() if TELEMETRY_LIB_AVAILABLE else "unknown",
            "prompt_length": len(prompt),
            "was_notification": was_notification,
            "injection_chars": injection_chars,
            "claude_md_chars": claude_md_chars,
            "pool_chars": pool_chars,
            "total_context_chars": injection_chars + claude_md_chars + pool_chars,
            "injection_tokens_est": injection_tokens,
            "claude_md_tokens_est": claude_md_tokens,
            "pool_tokens_est": pool_tokens,
            "total_context_tokens_est": total_tokens,
            "hot_count": stats.get("hot", 0),
            "warm_count": stats.get("warm", 0),
            "files_injected": hot_files[:MAX_HOT_FILES] + warm_files[:MAX_WARM_FILES],
            "files_used": [],  # Populated later by telemetry-record.py Stop hook
            "waste_ratio": -1,  # Populated later
            "tool_calls": 0,  # Populated later
        }
        with open(turns_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # Never fail the hook


def main():
    """
    Main entry point for Claude Code hook.
    Reads JSON from stdin, outputs tiered context to stdout.
    """
    global MAX_HOT_FILES, MAX_WARM_FILES, MAX_TOTAL_CHARS  # C1: Allow modification in notification clamping

    # Parse input - C3: Buffer stdin before parsing to allow fallback
    raw_input = sys.stdin.read() if sys.stdin else ""
    try:
        input_data = json.loads(raw_input)
        prompt = input_data.get("prompt", "")
    except (json.JSONDecodeError, ValueError):
        # Fallback: treat entire stdin as prompt
        prompt = raw_input

    if not prompt.strip():
        return

    # === TELEMETRY: Strip notifications before keyword matching ===
    prompt, was_notification = strip_notifications(prompt)
    if not prompt.strip():
        # Pure notification with no user text — skip routing entirely
        record_turn_telemetry("", True, {"hot": 0, "warm": 0, "cold": 0}, set(), {"scores": {}, "turn_count": 0}, 0)
        return

    # === TELEMETRY: Load auto-tuned parameter overrides ===
    load_telemetry_overrides()

    # === FIX: Clamp injection for notification-mixed prompts ===
    # Short residual text after notification strip = likely system text, not a real query
    if was_notification and len(prompt) < NOTIFICATION_SHORT_PROMPT_THRESHOLD:
        MAX_HOT_FILES = min(MAX_HOT_FILES, NOTIFICATION_MAX_HOT_FILES)
        MAX_WARM_FILES = min(MAX_WARM_FILES, NOTIFICATION_MAX_WARM_FILES)
        MAX_TOTAL_CHARS = min(MAX_TOTAL_CHARS, NOTIFICATION_MAX_CHARS)

    # Determine docs root with proper priority order
    # Priority 1: Explicit CONTEXT_DOCS_ROOT environment variable
    # Priority 2: Project-local .claude/ (if exists with .md files)
    # Priority 3: Global ~/.claude/
    docs_root = resolve_docs_root()

    # Load state
    state_file = get_state_file()
    prev_state = load_state(state_file)  # Keep copy before mutation
    state = copy.deepcopy(prev_state)  # Deep copy for modification

    # Update attention based on prompt
    state, activated = update_attention(state, prompt)

    # === PLUGIN: on_prompt_pre ===
    if PLUGINS_AVAILABLE:
        for plugin in get_plugins():
            try:
                prompt, should_continue = plugin.on_prompt_pre(prompt, state)
                if not should_continue:
                    return
            except Exception:
                pass  # Never fail the hook due to plugins

    # Build output
    output, stats = build_context_output(state, docs_root)
    stats["total_chars"] = len(output)  # Add total chars to stats

    # === PLUGIN: on_prompt_post ===
    if PLUGINS_AVAILABLE:
        for plugin in get_plugins():
            try:
                additional = plugin.on_prompt_post(prompt, output, state)
                if additional:
                    output = output + additional
            except Exception:
                pass  # Never fail the hook due to plugins

    # Append to history log (before save, so turn_count is correct)
    append_history(state, prev_state, activated, prompt, stats)

    # Save state for next turn
    save_state(state_file, state)

    # Compact debug log (one-liner per turn, with size rotation)
    log_file = Path.home() / ".claude" / "context_injection.log"
    try:
        # Rotate log when it exceeds max size
        if log_file.exists() and log_file.stat().st_size > LOG_MAX_SIZE_BYTES:
            content = log_file.read_text(encoding='utf-8', errors='replace')
            log_file.write_text(content[-LOG_KEEP_SIZE_BYTES:], encoding='utf-8')

        with open(log_file, "a", encoding='utf-8') as f:
            f.write(f"[{datetime.now().isoformat()[:19]}] T{state['turn_count']} "
                    f"H={stats['hot']} W={stats['warm']} C={stats['cold']} "
                    f"chars={len(output)} notif={was_notification} "
                    f"activated={','.join(activated) or 'none'}\n")
    except Exception:
        pass

    # Rotate attention_history.jsonl every 100 turns
    if TELEMETRY_LIB_AVAILABLE and state.get("turn_count", 0) % 100 == 0:
        try:
            rotate_jsonl(HISTORY_FILE, 1000)
        except Exception:
            pass

    # Output to Claude Code
    if stats["hot"] > 0 or stats["warm"] > 0:
        print(output)

    # === TELEMETRY: Record turn data ===
    record_turn_telemetry(prompt, was_notification, stats, activated, state, len(output))
    # Note: Usage tracking (log_injection, track_turn_usage) moved to telemetry_record.py Stop hook


if __name__ == "__main__":
    main()
