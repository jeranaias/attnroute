#!/usr/bin/env python3
"""
attnroute.learner — Self-Improving Routing Intelligence

Learns from turn history to discover:
- Prompt-file associations (which words predict which files being useful)
- Co-activation patterns (which files appear together)
- File rhythms (per-file decay rates based on user revisit patterns)
- Session continuity (warm-start from previous session's focus)
- File usage patterns (injected vs accessed vs edited)

Maturity levels:
  observing  (0-25 turns)    Collecting data, no boosts applied
  active     (25+ turns)     Making predictions, learning from outcomes

The learner runs automatically:
  - boost_scores() called every turn by the router
  - learn_from_turns() triggered by the optimizer every 10-50 turns
  - save_session() / get_warmup() called by Stop / SessionStart hooks
  - infer_file_usage() maps tool calls to .md file usage
  - track_turn_usage() updates stats after each turn
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from attnroute.compat import try_import

# Import telemetry lib
_telem_imports, TELEMETRY_LIB_AVAILABLE = try_import(
    "attnroute.telemetry_lib", "telemetry_lib",
    ["LEARNED_STATE_FILE", "TELEMETRY_DIR", "ensure_telemetry_dir", "load_turns", "windows_utf8_io"]
)
if TELEMETRY_LIB_AVAILABLE:
    LEARNED_STATE_FILE = _telem_imports["LEARNED_STATE_FILE"]
    TELEMETRY_DIR = _telem_imports["TELEMETRY_DIR"]
    ensure_telemetry_dir = _telem_imports["ensure_telemetry_dir"]
    load_turns = _telem_imports["load_turns"]
    windows_utf8_io = _telem_imports["windows_utf8_io"]
    windows_utf8_io()
else:
    LEARNED_STATE_FILE = Path.home() / ".claude" / "telemetry" / "learned_state.json"
    TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"
    def ensure_telemetry_dir(): TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    def load_turns(n=25, project=None): return []


# ============================================================================
# CONSTANTS
# ============================================================================

# Words too common or generic to be useful routing signals
_STOP_WORDS = frozenset({
    # English grammar
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "ought",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "then",
    "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such",
    "nor", "not", "only", "own", "same", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about",
    "what", "which", "who", "whom", "this", "that", "these", "those",
    "it", "its", "my", "me", "we", "our", "you", "your", "he",
    "she", "they", "them", "his", "her", "their", "up", "down", "no", "so",
    # Interaction noise (common in prompts but not topic-discriminative)
    "please", "help", "want", "like", "think", "know", "see", "look",
    "make", "take", "get", "let", "say", "tell", "give", "use", "find",
    "show", "try", "ask", "work", "call", "put", "keep", "also",
    "file", "code", "change", "update", "add", "remove", "fix", "check",
    "run", "test", "new", "now", "still", "already", "done", "good",
    "right", "sure", "yeah", "yes", "okay", "thanks", "thank",
})

# Maturity thresholds and boost weights (simplified: 2 levels)
MATURITY_LEVELS = [
    (25,     "observing"),  # Collecting data, no boosts
    (999999, "active"),     # Making predictions, learning from outcomes
]

MATURITY_BOOST_WEIGHT = {
    "observing": 0.0,   # Don't boost until we have data
    "active":    0.35,  # Full confidence after 25 turns
}

# Learning parameters
ASSOCIATION_DECAY = 0.995       # Old associations fade each cycle
MIN_AFFINITY = 0.03             # Drop associations below this
COACTIVATION_THRESHOLD = 0.25   # Min Jaccard similarity for co-activation edge
SESSION_WARMUP_FACTOR = 0.4     # Scale previous session scores by this
WORD_FREQUENCY_DAMPEN = 0.30    # Dampen words appearing in >30% of turns
RHYTHM_SLOW = 0.88              # Decay rate for frequently revisited files
RHYTHM_FAST = 0.50              # Decay rate for rarely revisited files


# ============================================================================
# WORD EXTRACTION
# ============================================================================

def extract_prompt_words(prompt: str) -> list[str]:
    """
    Extract significant words from a prompt for association learning.

    Filters stop words, short words, and returns lowercase tokens.
    These are the "features" the learner uses to predict file relevance.
    """
    words = re.findall(r'[a-z][a-z0-9_-]{2,}', prompt.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) >= 3]


def auto_extract_keywords(docs_root: Path) -> dict[str, list[str]]:
    """
    Extract keywords from .md file content when no keywords.json exists.

    Scans headings, bold text, and filenames to build a keyword map.
    This is the zero-config fallback that makes attnroute work out of the box.
    """
    keywords = {}

    try:
        md_files = list(docs_root.rglob("*.md"))
    except Exception:
        return {}

    for md_file in md_files:
        if md_file.name == "CLAUDE.md":
            continue

        try:
            rel = str(md_file.relative_to(docs_root)).replace("\\", "/")
        except ValueError:
            continue

        parts = []

        # Keywords from filename
        stem = md_file.stem.lower().replace("-", " ").replace("_", " ")
        parts.extend(p for p in stem.split() if len(p) > 2)

        # Keywords from directory name
        if md_file.parent != docs_root:
            dir_name = md_file.parent.name.lower()
            if len(dir_name) > 2:
                parts.append(dir_name)

        # Keywords from content
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")

            # Headings
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#"):
                    heading = stripped.lstrip("#").strip().lower()
                    words = heading.replace("-", " ").replace("_", " ").split()
                    parts.extend(w for w in words if len(w) > 3 and w not in _STOP_WORDS)

                # Bold text (important terms)
                bold = re.findall(r'\*\*([^*]+)\*\*', stripped)
                for match in bold:
                    words = match.lower().replace("-", " ").replace("_", " ").split()
                    parts.extend(w for w in words if len(w) > 3 and w not in _STOP_WORDS)

            # Backtick references (technical terms)
            backtick = re.findall(r'`([a-zA-Z][a-zA-Z0-9_-]{3,})`', content)
            parts.extend(b.lower() for b in backtick[:20])  # Cap to avoid noise

        except Exception:
            pass

        # Deduplicate preserving order
        seen = set()
        unique = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        if unique:
            keywords[rel] = unique

    return keywords


# ============================================================================
# FILE RELATIONSHIPS (migrated from usage_tracker.py)
# ============================================================================

def extract_file_relationships(md_file: Path) -> dict[str, any]:
    """
    Extract file references and keywords from .claude/*.md file.

    Returns:
        {
            'describes': [list of source files mentioned],
            'keywords': [list of keywords from content]
        }
    """
    if not md_file.exists():
        return {'describes': [], 'keywords': []}

    try:
        content = md_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {'describes': [], 'keywords': []}

    # Find file references in backticks or code blocks
    # Matches: `filename.py`, `path/to/file.ts`, etc.
    file_refs = re.findall(r'`([a-zA-Z0-9_/.-]+\.[a-zA-Z]+)`', content)

    # Find file references in **Location**: lines
    location_refs = re.findall(r'\*\*Location\*\*:\s*`([^`]+)`', content)

    # Extract keywords from headers (## Keywords section if exists)
    keywords = []
    keyword_section = re.search(r'## (?:Auto-Generated )?Keywords?\s*\n([^\n#]+)', content)
    if keyword_section:
        keyword_line = keyword_section.group(1)
        keywords = [k.strip() for k in re.split(r'[,\s]+', keyword_line) if k.strip()]

    return {
        'describes': list(set(file_refs + location_refs)),
        'keywords': keywords
    }


def build_file_relationship_map(claude_dir: Path | None = None) -> dict[str, dict]:
    """
    Build map of .claude/*.md files → source files they describe.

    Returns:
        {
            'modules/pipeline.md': {
                'describes': ['refined_pipeline_integrated_v4_fixed.py', ...],
                'keywords': ['pipeline', 'process', 'message']
            }
        }
    """
    if claude_dir is None:
        claude_dir = Path(".claude")

    relationships = {}

    if not claude_dir.exists():
        return {}

    # Scan all .md files in .claude/
    try:
        for md_file in claude_dir.rglob("*.md"):
            try:
                rel_path = str(md_file.relative_to(claude_dir.parent))
                relationships[rel_path] = extract_file_relationships(md_file)
            except ValueError:
                continue
    except Exception:
        pass

    return relationships


# ============================================================================
# LEARNER
# ============================================================================

class Learner:
    """
    Self-improving routing intelligence.

    Learns from historical turn data to:
    1. Discover which prompt words predict which files being useful
    2. Find files that frequently appear together (co-activation)
    3. Learn per-file decay rates from user revisit patterns
    4. Remember session context for warm-start continuity

    The learner is conservative by design:
    - No boosts applied until 25+ turns observed ("observing" maturity)
    - Confidence scales with observation count (sqrt curve)
    - Old associations decay naturally if not reinforced
    - Rare/discriminative words weighted higher (IDF-style)
    """

    def __init__(self):
        self.state = self._load()
        self.maturity = self._compute_maturity()

    # ---- Persistence ----

    def _load(self) -> dict:
        """Load learned state from disk."""
        default_state = {
            "version": 2,
            "meta": {
                "turns_learned": 0,
                "last_learned": None,
                "created": datetime.now().isoformat(),
            },
            "prompt_file_affinity": {},     # word → {file → score}
            "word_frequency": {},           # word → fraction of turns containing it
            "coactivation_learned": {},     # file → [related files]
            "file_rhythm": {},              # file → learned decay rate
            "session_memory": {},           # file → score from last session
            "discoveries": [],              # recent notable learned associations
            "usefulness": {},               # file → {injected, accessed, edited, score}
        }

        if LEARNED_STATE_FILE.exists():
            try:
                data = json.loads(LEARNED_STATE_FILE.read_text(encoding="utf-8"))
                # Validate structure: ensure required keys exist
                if not isinstance(data, dict) or "meta" not in data:
                    return default_state
                # Merge with defaults to handle version upgrades
                for key in default_state:
                    if key not in data:
                        data[key] = default_state[key]
                return data
            except (json.JSONDecodeError, UnicodeDecodeError, Exception):
                # Corrupt file - return fresh state
                pass

        return default_state

    def _save(self):
        """Persist learned state to disk."""
        ensure_telemetry_dir()
        try:
            # Atomic write: write to temp file, then rename
            temp_file = LEARNED_STATE_FILE.with_suffix('.tmp')
            temp_file.write_text(
                json.dumps(self.state, indent=2, default=str),
                encoding="utf-8"
            )
            # Atomic rename (safe on POSIX and Windows)
            temp_file.replace(LEARNED_STATE_FILE)
        except Exception:
            pass

    def _compute_maturity(self) -> str:
        """Determine maturity level from observation count."""
        turns = self.state["meta"].get("turns_learned", 0)
        for threshold, level in MATURITY_LEVELS:
            if turns < threshold:
                return level
        return "wise"

    @property
    def turns_learned(self) -> int:
        return self.state["meta"].get("turns_learned", 0)

    @property
    def boost_weight(self) -> float:
        return MATURITY_BOOST_WEIGHT.get(self.maturity, 0.0)

    # ---- Learning (called by optimizer every 10-50 turns) ----

    def learn_from_turns(self, turns: list):
        """
        Core learning cycle. Analyzes turn history to update all learned models.

        Called by telemetry_optimizer at adaptive intervals.
        Each cycle:
        1. Decays old associations (preventing stale knowledge)
        2. Learns new prompt→file associations from successful matches
        3. Discovers co-activation patterns
        4. Calibrates per-file decay rates from revisit rhythms
        5. Computes word frequency for IDF dampening
        """
        if not turns:
            return

        # Only learn from turns with usage data
        usable = [t for t in turns if t.get("files_used") is not None]
        if len(usable) < 5:
            return

        # Phase 1: Decay old associations
        self._decay_associations()

        # Phase 2: Learn prompt→file associations
        self._learn_prompt_associations(usable)

        # Phase 3: Learn co-activation patterns
        self._learn_coactivation(usable)

        # Phase 4: Learn file revisit rhythms
        self._learn_file_rhythms(usable)

        # Phase 5: Update word frequency (for IDF dampening)
        self._update_word_frequency(usable)

        # Update metadata
        self.state["meta"]["turns_learned"] = self.state["meta"].get("turns_learned", 0) + len(usable)
        self.state["meta"]["last_learned"] = datetime.now().isoformat()
        self.maturity = self._compute_maturity()

        self._save()

    def _decay_associations(self):
        """Fade old associations. Knowledge not reinforced slowly disappears."""
        affinities = self.state.get("prompt_file_affinity", {})
        pruned = {}
        for word, files in affinities.items():
            new_files = {}
            for f, score in files.items():
                decayed = score * ASSOCIATION_DECAY
                if decayed >= MIN_AFFINITY:
                    new_files[f] = round(decayed, 4)
            if new_files:
                pruned[word] = new_files
        self.state["prompt_file_affinity"] = pruned

    def _learn_prompt_associations(self, turns: list):
        """
        Learn which prompt words predict which files being useful.

        For turns where files_used is populated:
          - Words in the prompt get affinity TOWARD files_used (reward)
          - Words get mild anti-affinity toward injected-but-unused files (penalty)

        The reward/penalty is scaled by a learning rate that decreases
        with observation count (bold early, cautious later).
        """
        affinities = self.state.get("prompt_file_affinity", {})
        discoveries = []

        # Learning rate: bold early to build associations, conservative later
        base_lr = 0.08 if self.turns_learned < 100 else 0.04

        for turn in turns:
            files_used = turn.get("files_used", [])
            files_injected = turn.get("files_injected", [])
            prompt = turn.get("prompt_keywords", [])

            # Reconstruct prompt words from keywords if available, else skip
            if isinstance(prompt, list):
                words = [w for w in prompt if w not in _STOP_WORDS and len(w) >= 3]
            else:
                continue

            if not words or not files_injected:
                continue

            unused = set(files_injected) - set(files_used)

            for word in words:
                if word not in affinities:
                    affinities[word] = {}

                # Reward: boost affinity toward files that were actually used
                for f in files_used:
                    old = affinities[word].get(f, 0.0)
                    new = min(1.0, old + base_lr)
                    affinities[word][f] = round(new, 4)

                    # Track notable discoveries (new strong associations)
                    if old < 0.1 and new >= 0.1:
                        discoveries.append({
                            "word": word, "file": f,
                            "affinity": round(new, 2),
                            "turn": turn.get("turn_id", "?"),
                        })

                # Mild penalty: reduce affinity toward unused files
                for f in unused:
                    if f in affinities[word]:
                        old = affinities[word][f]
                        new = max(0.0, old - base_lr * 0.3)
                        if new < MIN_AFFINITY:
                            del affinities[word][f]
                        else:
                            affinities[word][f] = round(new, 4)

        self.state["prompt_file_affinity"] = affinities

        # Keep last 20 discoveries
        existing = self.state.get("discoveries", [])
        self.state["discoveries"] = (existing + discoveries)[-20:]

    def _learn_coactivation(self, turns: list):
        """
        Discover files that frequently appear HOT together.

        Uses Jaccard similarity: J(A,B) = |A∩B| / |A∪B|
        If two files co-occur in >25% of turns where either appears,
        they get a learned co-activation edge.
        """
        # Count co-occurrences
        file_turns = defaultdict(set)       # file → set of turn indices
        pair_turns = defaultdict(set)       # (file_a, file_b) → set of turn indices

        for i, turn in enumerate(turns):
            hot_files = turn.get("files_injected", [])[:6]  # Top files only
            for f in hot_files:
                file_turns[f].add(i)
            # Track pairs
            for a in hot_files:
                for b in hot_files:
                    if a < b:  # Canonical ordering
                        pair_turns[(a, b)].add(i)

        # Compute Jaccard similarity
        coactivation = defaultdict(list)
        for (a, b), shared_turns in pair_turns.items():
            union = len(file_turns[a] | file_turns[b])
            if union == 0:
                continue
            jaccard = len(shared_turns) / union
            if jaccard >= COACTIVATION_THRESHOLD and len(shared_turns) >= 3:
                coactivation[a].append(b)
                coactivation[b].append(a)

        # Deduplicate
        self.state["coactivation_learned"] = {
            k: sorted(set(v)) for k, v in coactivation.items()
        }

    def _learn_file_rhythms(self, turns: list):
        """
        Learn per-file decay rates from user revisit patterns.

        Measures average gap (in turns) between a file being activated.
        Short gaps → user revisits often → slow decay (keep warm)
        Long gaps → user rarely returns → fast decay (let go quickly)

        This teaches the system each user's working rhythm.
        """
        # Track activation turns per file
        file_active_turns = defaultdict(list)
        for i, turn in enumerate(turns):
            for f in turn.get("files_injected", []):
                file_active_turns[f].append(i)

        rhythms = {}
        for f, active_turns in file_active_turns.items():
            if len(active_turns) < 3:
                continue  # Not enough data

            # Compute average gap between activations
            gaps = [active_turns[i+1] - active_turns[i] for i in range(len(active_turns) - 1)]
            avg_gap = sum(gaps) / len(gaps) if gaps else 10

            # Interpolate decay rate: short gap → slow decay, long gap → fast decay
            # Gap thresholds: <3 turns = slow decay, >12 turns = fast decay
            if avg_gap <= 3:
                decay = RHYTHM_SLOW
            elif avg_gap >= 12:
                decay = RHYTHM_FAST
            else:
                # Linear interpolation between gap=3 and gap=12
                t = (avg_gap - 3) / 9
                decay = RHYTHM_SLOW + t * (RHYTHM_FAST - RHYTHM_SLOW)

            rhythms[f] = round(decay, 3)

        self.state["file_rhythm"] = rhythms

    def _update_word_frequency(self, turns: list):
        """
        Track word frequency across turns for IDF-style dampening.

        Words appearing in >30% of turns are too common to be discriminative.
        Their boost weight gets reduced. Rare words get amplified.
        """
        word_turn_count = Counter()
        total = len(turns)

        for turn in turns:
            prompt = turn.get("prompt_keywords", [])
            if isinstance(prompt, list):
                unique_words = set(w for w in prompt if w not in _STOP_WORDS and len(w) >= 3)
                for w in unique_words:
                    word_turn_count[w] += 1

        if total > 0:
            self.state["word_frequency"] = {
                w: round(c / total, 3)
                for w, c in word_turn_count.items()
                if c >= 2  # Only track words seen more than once
            }

    # ---- Inference (called every turn by the router) ----

    def boost_scores(self, prompt: str, scores: dict) -> dict:
        """
        Apply learned intelligence to attention scores.

        This is called every turn by the router, between keyword activation
        and co-activation phases. It layers learned associations on top of
        the static keyword matches.

        The boost strength scales with:
        1. Maturity level (observing=0, wise=0.45)
        2. Association confidence (sqrt of observation count)
        3. IDF factor (rare discriminative words boosted, common dampened)
        """
        if self.boost_weight == 0:
            return scores  # Observing mode: don't interfere

        affinities = self.state.get("prompt_file_affinity", {})
        if not affinities:
            return scores

        word_freq = self.state.get("word_frequency", {})
        words = extract_prompt_words(prompt)

        for word in words:
            if word not in affinities:
                continue

            # IDF dampening: common words get reduced boost
            freq = word_freq.get(word, 0.0)
            if freq > WORD_FREQUENCY_DAMPEN:
                idf = max(0.1, 1.0 - freq)  # Dampen but never zero
            else:
                idf = min(1.5, 1.0 + (WORD_FREQUENCY_DAMPEN - freq))  # Slight boost for rare words

            for f, affinity in affinities[word].items():
                if f in scores:
                    boost = affinity * self.boost_weight * idf
                    scores[f] = min(1.0, scores[f] + boost)

        return scores

    def get_learned_coactivation(self) -> dict:
        """Return learned co-activation graph for merging with config."""
        return self.state.get("coactivation_learned", {})

    def get_file_decay(self, file: str) -> float | None:
        """
        Return learned per-file decay rate, or None to use default.

        The router should prefer this over category-based decay when available.
        """
        return self.state.get("file_rhythm", {}).get(file, None)

    # ---- Usage Tracking (migrated from usage_tracker.py) ----

    def infer_file_usage(self, tool_calls: list[dict]) -> set[str]:
        """
        Infer which .claude/*.md files were useful based on tool calls.

        Args:
            tool_calls: [
                {'tool': 'Read', 'target': 'scripts/pipeline.py'},
                {'tool': 'Edit', 'target': 'scripts/context-router-v2.py'},
                {'tool': 'Grep', 'pattern': 'process_message'},
                ...
            ]

        Returns:
            Set of .claude/*.md files that were useful
        """
        relationships = build_file_relationship_map()
        accessed_files = set()

        for tool_call in tool_calls:
            target = tool_call.get('target', '')
            pattern = tool_call.get('pattern', '')

            # Direct file access (Read, Edit, Write)
            if target:
                target_lower = target.lower().replace("\\", "/")
                # Find which .md files describe this target file
                for md_file, rel_info in relationships.items():
                    for described_file in rel_info.get('describes', []):
                        described_lower = described_file.lower()
                        # Match if target contains described file
                        if described_lower in target_lower or target_lower.endswith(described_lower):
                            accessed_files.add(md_file)

            # Pattern matching (Grep)
            if pattern:
                pattern_lower = pattern.lower()
                # Find which .md files mention this pattern
                for md_file, rel_info in relationships.items():
                    for keyword in rel_info.get('keywords', []):
                        if keyword.lower() in pattern_lower:
                            accessed_files.add(md_file)

        return accessed_files

    def calculate_usefulness(self, file: str) -> float:
        """
        Calculate usefulness score for a file (0.0 to 1.0).

        Based on: (accessed + 2*edited) / injected, capped at 1.0.
        Higher = more useful when injected.
        """
        usefulness = self.state.get("usefulness", {})
        if file not in usefulness:
            return 0.0

        stats = usefulness[file]
        injected = stats.get('injected', 0)
        if injected == 0:
            return 0.0

        accessed = stats.get('accessed', 0)
        edited = stats.get('edited', 0)

        # Edits count double (high-impact usage)
        score = (accessed + 2 * edited) / injected
        return min(1.0, round(score, 2))

    def log_injection(self, injected_files: list[dict], prompt: str = ""):
        """
        Log which files were injected this turn.

        Args:
            injected_files: [
                {'file': 'path/to/file.md', 'tier': 'HOT', 'score': 1.0, 'chars': 12450},
                ...
            ]
            prompt: User's query text (optional, for future pattern learning)
        """
        usefulness = self.state.get("usefulness", {})

        for file_info in injected_files:
            f = file_info.get('file', '') if isinstance(file_info, dict) else str(file_info)
            if not f:
                continue

            if f not in usefulness:
                usefulness[f] = {'injected': 0, 'accessed': 0, 'edited': 0}

            usefulness[f]['injected'] = usefulness[f].get('injected', 0) + 1

        self.state["usefulness"] = usefulness
        # Don't save on every injection - save is called by track_turn_usage

    def track_turn_usage(self, tool_calls: list[dict], injected_files: list[str] | None = None):
        """
        Analyze tool calls after turn completes, update usefulness statistics.

        Args:
            tool_calls: List of tool invocations
            injected_files: List of files that were injected this turn (optional)
        """
        usefulness = self.state.get("usefulness", {})

        # Infer which files were accessed
        accessed_files = self.infer_file_usage(tool_calls)

        # Track edits separately (high-impact usage)
        relationships = build_file_relationship_map()
        edited_files = set()
        for tool_call in tool_calls:
            if tool_call.get('tool') in ['Edit', 'Write']:
                target = tool_call.get('target', '')
                if target:
                    target_lower = target.lower().replace("\\", "/")
                    for md_file, rel_info in relationships.items():
                        for described_file in rel_info.get('describes', []):
                            if described_file.lower() in target_lower:
                                edited_files.add(md_file)

        # Update statistics
        for f in accessed_files:
            if f in usefulness:
                usefulness[f]['accessed'] = usefulness[f].get('accessed', 0) + 1
            else:
                usefulness[f] = {'injected': 0, 'accessed': 1, 'edited': 0}

        for f in edited_files:
            if f in usefulness:
                usefulness[f]['edited'] = usefulness[f].get('edited', 0) + 1
            else:
                usefulness[f] = {'injected': 0, 'accessed': 0, 'edited': 1}

        self.state["usefulness"] = usefulness
        self._save()

    def get_usefulness(self, file: str) -> float:
        """Get usefulness score for a file (alias for calculate_usefulness)."""
        return self.calculate_usefulness(file)

    def get_usefulness_stats(self) -> dict:
        """Get summary statistics for all tracked files."""
        usefulness = self.state.get("usefulness", {})
        if not usefulness:
            return {
                'total_files': 0,
                'average_usefulness': 0.0,
                'high_utility_files': [],
                'low_utility_files': []
            }

        scores = {f: self.calculate_usefulness(f) for f in usefulness}

        high_utility = [
            (f, s) for f, s in scores.items()
            if s > 0.75
        ]

        low_utility = [
            (f, s) for f, s in scores.items()
            if s < 0.25 and usefulness[f].get('injected', 0) > 5
        ]

        avg = sum(scores.values()) / len(scores) if scores else 0.0

        return {
            'total_files': len(usefulness),
            'average_usefulness': round(avg, 2),
            'high_utility_files': sorted(high_utility, key=lambda x: x[1], reverse=True),
            'low_utility_files': sorted(low_utility, key=lambda x: x[1])
        }

    # ---- Session Memory ----

    def save_session(self, scores: dict):
        """
        Save current attention state for warm-start in next session.

        Called by the Stop hook at end of session. Preserves the user's
        focus context so the next session can pick up where they left off.
        """
        # Only save files with meaningful scores
        memory = {f: round(s, 3) for f, s in scores.items() if s > 0.1}
        self.state["session_memory"] = memory
        self._save()

    def get_warmup_scores(self) -> dict:
        """
        Get warm-start scores from previous session.

        Returns attention scores scaled down by SESSION_WARMUP_FACTOR so they
        don't overpower fresh activations but still provide continuity.
        """
        memory = self.state.get("session_memory", {})
        return {f: round(s * SESSION_WARMUP_FACTOR, 3) for f, s in memory.items()}

    # ---- Reporting ----

    def merge_ingested_state(self, ingested: dict) -> None:
        """
        Merge transcript-ingested state into existing learned state.

        Rules:
        - prompt_file_affinity: max(existing, ingested) per word-file pair
        - coactivation_learned: union of related file lists
        - file_rhythm: prefer existing if present, else use ingested
        - usefulness: additive merge of counts
        - word_frequency: weighted average favoring existing
        - meta.turns_learned: add ingested count
        """
        # Prompt-file affinity: take max per pair
        existing_aff = self.state.get("prompt_file_affinity", {})
        for word, files in ingested.get("prompt_file_affinity", {}).items():
            if word not in existing_aff:
                existing_aff[word] = {}
            for f, score in files.items():
                existing_aff[word][f] = max(existing_aff[word].get(f, 0.0), score)
        self.state["prompt_file_affinity"] = existing_aff

        # Co-activation: union of related lists (deduplicated)
        existing_coact = self.state.get("coactivation_learned", {})
        for f, related in ingested.get("coactivation_learned", {}).items():
            if f not in existing_coact:
                existing_coact[f] = []
            existing_set = set(existing_coact[f])
            for r in related:
                if r not in existing_set:
                    existing_coact[f].append(r)
                    existing_set.add(r)
        self.state["coactivation_learned"] = existing_coact

        # File rhythm: prefer existing
        existing_rhythm = self.state.get("file_rhythm", {})
        for f, decay in ingested.get("file_rhythm", {}).items():
            if f not in existing_rhythm:
                existing_rhythm[f] = decay
        self.state["file_rhythm"] = existing_rhythm

        # Usefulness: additive
        existing_useful = self.state.get("usefulness", {})
        for f, stats in ingested.get("usefulness", {}).items():
            if f not in existing_useful:
                existing_useful[f] = {"injected": 0, "accessed": 0, "edited": 0}
            for key in ("injected", "accessed", "edited"):
                existing_useful[f][key] = existing_useful[f].get(key, 0) + stats.get(key, 0)
        self.state["usefulness"] = existing_useful

        # Word frequency: weighted average (favor existing 2:1)
        existing_freq = self.state.get("word_frequency", {})
        for word, freq in ingested.get("word_frequency", {}).items():
            if word in existing_freq:
                existing_freq[word] = round((existing_freq[word] * 2 + freq) / 3, 4)
            else:
                existing_freq[word] = freq
        self.state["word_frequency"] = existing_freq

        # Metadata
        self.state["meta"]["turns_learned"] = (
            self.state["meta"].get("turns_learned", 0)
            + ingested.get("meta", {}).get("turns_learned", 0)
        )
        self.state["meta"]["last_learned"] = datetime.now().isoformat()
        self.maturity = self._compute_maturity()

        self._save()

    def summary(self) -> str:
        """One-line summary for the session dashboard."""
        turns = self.state["meta"].get("turns_learned", 0)
        n_assoc = sum(len(v) for v in self.state.get("prompt_file_affinity", {}).values())
        n_coact = len(self.state.get("coactivation_learned", {}))
        n_rhythm = len(self.state.get("file_rhythm", {}))

        parts = []
        if n_assoc:
            parts.append(f"{n_assoc} associations")
        if n_coact:
            parts.append(f"{n_coact} co-activations")
        if n_rhythm:
            parts.append(f"{n_rhythm} rhythms")

        detail = ", ".join(parts) if parts else "collecting data"
        return f"Learned: {detail} | maturity: {self.maturity} ({turns} turns)"

    def discoveries_summary(self) -> str:
        """Recent notable discoveries for the dashboard."""
        recent = self.state.get("discoveries", [])[-3:]
        if not recent:
            return ""
        lines = []
        for d in recent:
            lines.append(f"  {d['word']} -> {Path(d['file']).stem} ({d['affinity']:.0%})")
        return "Recent: " + ", ".join(
            f"{d['word']}->{Path(d['file']).stem}" for d in recent
        )


# ============================================================================
# CLI
# ============================================================================

def main():
    """Manual learning trigger and status display."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute learner — self-improving intelligence")
    parser.add_argument("--status", action="store_true", help="Show learner status")
    parser.add_argument("--learn", action="store_true", help="Run learning cycle now")
    parser.add_argument("--reset", action="store_true", help="Reset learned state")
    parser.add_argument("--dump", action="store_true", help="Dump full learned state as JSON")
    args = parser.parse_args()

    W = 60

    def section(title):
        pad = W - len(title) - 4
        left = pad // 2
        right = pad - left
        print(f"{'─' * left}[ {title} ]{'─' * right}")

    if args.reset:
        if LEARNED_STATE_FILE.exists():
            LEARNED_STATE_FILE.unlink()
            print("  Learned state reset.")
        else:
            print("  No learned state to reset.")
        return

    learner = Learner()

    if args.dump:
        print(json.dumps(learner.state, indent=2, default=str))
        return

    if args.learn:
        turns = load_turns(n=200)
        print(f"  Learning from {len(turns)} turns...")
        learner.learn_from_turns(turns)
        print(f"  Done. {learner.summary()}")
        return

    # Default: status
    print()
    section("LEARNER STATUS")
    print()
    print(f"  {learner.summary()}")
    print()

    affinities = learner.state.get("prompt_file_affinity", {})
    if affinities:
        section("TOP ASSOCIATIONS")
        # Flatten and sort by strength
        flat = []
        for word, files in affinities.items():
            for f, score in files.items():
                flat.append((word, f, score))
        flat.sort(key=lambda x: x[2], reverse=True)
        for word, f, score in flat[:15]:
            bar = "█" * int(score * 20)
            print(f"  {word:<20} -> {Path(f).stem:<20} {bar} {score:.2f}")
        print()

    coact = learner.state.get("coactivation_learned", {})
    if coact:
        section("LEARNED CO-ACTIVATION")
        for f, related in sorted(coact.items())[:10]:
            related_names = ", ".join(Path(r).stem for r in related)
            print(f"  {Path(f).stem:<20} <-> {related_names}")
        print()

    rhythms = learner.state.get("file_rhythm", {})
    if rhythms:
        section("FILE RHYTHMS")
        for f, decay in sorted(rhythms.items(), key=lambda x: x[1]):
            speed = "fast" if decay < 0.65 else "medium" if decay < 0.80 else "slow"
            print(f"  {Path(f).stem:<24} decay={decay:.2f} ({speed})")
        print()

    discoveries = learner.state.get("discoveries", [])
    if discoveries:
        section("RECENT DISCOVERIES")
        for d in discoveries[-5:]:
            print(f"  '{d['word']}' -> {Path(d['file']).stem} (affinity: {d['affinity']:.0%})")
        print()


if __name__ == "__main__":
    main()
