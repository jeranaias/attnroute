#!/usr/bin/env python3
"""
attnroute.compressor — Memory Compression Engine

Compresses tool outputs using Claude API for 10x token efficiency.
Implements 3-layer progressive retrieval (Index → Timeline → Full).

Architecture:
  PostToolUse Hook → Observation Queue → Background Worker → Compressed Storage
                                              ↓
  SessionStart Hook ← Progressive Retrieval ← SQLite + Chroma

Storage:
  - SQLite: Structured observation storage with FTS5 search
  - ChromaDB (optional): Semantic vector search

Compression Strategy:
  - Tool outputs → AI-compressed summaries (~500 tokens)
  - Key facts extraction (bullet points)
  - Concept tagging for clustering
  - Embeddings for semantic search
"""

import hashlib
import json
import os
import queue
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from attnroute.compat import try_import

# Import telemetry lib
_telem_imports, TELEMETRY_LIB_AVAILABLE = try_import(
    "attnroute.telemetry_lib", "telemetry_lib",
    ["TELEMETRY_DIR", "estimate_tokens", "windows_utf8_io"]
)
if TELEMETRY_LIB_AVAILABLE:
    TELEMETRY_DIR = _telem_imports["TELEMETRY_DIR"]
    estimate_tokens = _telem_imports["estimate_tokens"]
    windows_utf8_io = _telem_imports["windows_utf8_io"]
    windows_utf8_io()
else:
    TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"
    # Fallback estimate_tokens if telemetry_lib not available
    def estimate_tokens(text: str) -> int:
        """Estimate token count from text (rough: ~3 chars per token)."""
        return len(text) // 3

# Try to import Anthropic SDK for compression
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None
    ANTHROPIC_AVAILABLE = False

# Try to import ChromaDB for semantic search
# NOTE: ChromaDB support is prepared but not yet implemented
# Future enhancement: Add semantic vector search alongside FTS5
try:
    import chromadb
    from chromadb.config import Settings  # noqa: F401
    CHROMA_AVAILABLE = True
except ImportError:
    chromadb = None
    CHROMA_AVAILABLE = False

# Storage paths
OBSERVATIONS_DB = TELEMETRY_DIR / "observations.db"
CHROMA_DIR = TELEMETRY_DIR / "chroma"

# Compression settings
MAX_RAW_TOKENS_BEFORE_COMPRESS = 500  # Only compress outputs > 500 tokens
COMPRESSION_MODEL = "claude-3-haiku-20240307"  # Fast, cheap model for compression
MAX_SUMMARY_TOKENS = 500
MAX_FACTS_COUNT = 10
MAX_CONCEPTS_COUNT = 5
MAX_INPUT_CHARS = 10000            # Maximum input chars to send to compression API
API_MAX_TOKENS = 1024              # Maximum tokens in API response

# Search and retrieval limits
DEFAULT_SEARCH_LIMIT = 20          # Default FTS search result limit
DEFAULT_RECENT_LIMIT = 50          # Default recent observations limit
DEFAULT_INDEX_LIMIT = 10           # Default Layer 1 index search limit
TIMELINE_WINDOW_SIZE = 3           # Observations before/after in timeline
RECENT_CONTEXT_LIMIT = 5           # Default recent context limit
RECENT_CONTEXT_BUDGET = 2000       # Default token budget for recent context

# Token estimation
CHARS_PER_TOKEN_FALLBACK = 3       # Fallback estimation (conservative)


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class CompressedObservation:
    """A compressed tool observation stored in the memory system."""
    id: str                           # obs_xxxxx (unique ID)
    session_id: str                   # sess_xxxxx (session grouping)
    timestamp: datetime               # When the observation occurred
    tool_name: str                    # bash, read, edit, grep, etc.
    observation_type: str             # bugfix|feature|refactor|discovery|decision|change
    concepts: list[str]               # Technical concept tags
    raw_tokens: int                   # Original token count
    compressed_tokens: int            # Compressed token count
    semantic_summary: str             # AI-compressed summary (~500 tokens)
    key_facts: list[str]              # Extracted facts as bullets
    related_files: list[str]          # File references
    raw_content_hash: str             # Hash of original content (for dedup)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else self.timestamp,
            "tool_name": self.tool_name,
            "observation_type": self.observation_type,
            "concepts": self.concepts,
            "raw_tokens": self.raw_tokens,
            "compressed_tokens": self.compressed_tokens,
            "semantic_summary": self.semantic_summary,
            "key_facts": self.key_facts,
            "related_files": self.related_files,
            "raw_content_hash": self.raw_content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CompressedObservation":
        """Create from dictionary."""
        timestamp = data.get("timestamp", datetime.now().isoformat())
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        return cls(
            id=data.get("id", ""),
            session_id=data.get("session_id", ""),
            timestamp=timestamp,
            tool_name=data.get("tool_name", ""),
            observation_type=data.get("observation_type", "unknown"),
            concepts=data.get("concepts", []),
            raw_tokens=data.get("raw_tokens", 0),
            compressed_tokens=data.get("compressed_tokens", 0),
            semantic_summary=data.get("semantic_summary", ""),
            key_facts=data.get("key_facts", []),
            related_files=data.get("related_files", []),
            raw_content_hash=data.get("raw_content_hash", ""),
        )


@dataclass
class ObservationIndex:
    """Compact index entry for Layer 1 retrieval (~50-100 tokens)."""
    id: str
    date: str                         # YYYY-MM-DD
    type: str                         # observation_type
    title: str                        # First 80 chars of summary
    token_count: int                  # Original token count
    concepts: list[str]               # Top 3 concepts


# ============================================================================
# SQLITE STORAGE
# ============================================================================

class ObservationDB:
    """SQLite storage for compressed observations."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        observation_type TEXT NOT NULL,
        concepts TEXT NOT NULL,  -- JSON array
        raw_tokens INTEGER NOT NULL,
        compressed_tokens INTEGER NOT NULL,
        semantic_summary TEXT NOT NULL,
        key_facts TEXT NOT NULL,  -- JSON array
        related_files TEXT NOT NULL,  -- JSON array
        raw_content_hash TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_session ON observations(session_id);
    CREATE INDEX IF NOT EXISTS idx_timestamp ON observations(timestamp);
    CREATE INDEX IF NOT EXISTS idx_type ON observations(observation_type);
    CREATE INDEX IF NOT EXISTS idx_hash ON observations(raw_content_hash);

    CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
        semantic_summary, key_facts,
        content='observations',
        content_rowid='rowid'
    );

    CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
        INSERT INTO observations_fts(rowid, semantic_summary, key_facts)
        VALUES (new.rowid, new.semantic_summary, new.key_facts);
    END;

    CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
        INSERT INTO observations_fts(observations_fts, rowid, semantic_summary, key_facts)
        VALUES('delete', old.rowid, old.semantic_summary, old.key_facts);
    END;
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or OBSERVATIONS_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(self.SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def store(self, obs: CompressedObservation) -> bool:
        """Store a compressed observation."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO observations
                (id, session_id, timestamp, tool_name, observation_type, concepts,
                 raw_tokens, compressed_tokens, semantic_summary, key_facts,
                 related_files, raw_content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                obs.id,
                obs.session_id,
                obs.timestamp.isoformat() if isinstance(obs.timestamp, datetime) else obs.timestamp,
                obs.tool_name,
                obs.observation_type,
                json.dumps(obs.concepts),
                obs.raw_tokens,
                obs.compressed_tokens,
                obs.semantic_summary,
                json.dumps(obs.key_facts),
                json.dumps(obs.related_files),
                obs.raw_content_hash,
            ))
            conn.commit()
            return True
        except Exception as e:
            print(f"[compressor] Failed to store observation: {e}", file=sys.stderr)
            return False
        finally:
            conn.close()

    def get(self, obs_id: str) -> CompressedObservation | None:
        """Retrieve an observation by ID."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM observations WHERE id = ?", (obs_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_observation(row)
            return None
        finally:
            conn.close()

    def get_by_session(self, session_id: str) -> list[CompressedObservation]:
        """Get all observations for a session."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM observations WHERE session_id = ? ORDER BY timestamp",
                (session_id,)
            )
            return [self._row_to_observation(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_before(self, timestamp: datetime, limit: int = RECENT_CONTEXT_LIMIT) -> list[CompressedObservation]:
        """Get observations before a timestamp."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM observations
                WHERE timestamp < ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (timestamp.isoformat(), limit))
            return [self._row_to_observation(row) for row in cursor.fetchall()][::-1]
        finally:
            conn.close()

    def get_after(self, timestamp: datetime, limit: int = RECENT_CONTEXT_LIMIT) -> list[CompressedObservation]:
        """Get observations after a timestamp."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM observations
                WHERE timestamp > ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (timestamp.isoformat(), limit))
            return [self._row_to_observation(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def search_fts(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[CompressedObservation]:
        """Full-text search across summaries and facts."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            # Escape special FTS5 characters
            safe_query = query.replace('"', '""')
            cursor.execute("""
                SELECT o.* FROM observations o
                JOIN observations_fts f ON o.rowid = f.rowid
                WHERE observations_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (f'"{safe_query}"', limit))
            return [self._row_to_observation(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            # FTS query failed, fall back to LIKE
            return self.search_like(query, limit)
        finally:
            conn.close()

    def search_like(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[CompressedObservation]:
        """Fallback LIKE search."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            pattern = f"%{query}%"
            cursor.execute("""
                SELECT * FROM observations
                WHERE semantic_summary LIKE ? OR key_facts LIKE ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (pattern, pattern, limit))
            return [self._row_to_observation(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_recent(self, limit: int = DEFAULT_RECENT_LIMIT) -> list[CompressedObservation]:
        """Get most recent observations."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM observations
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
            return [self._row_to_observation(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def has_hash(self, content_hash: str) -> bool:
        """Check if content with this hash already exists (dedup)."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM observations WHERE raw_content_hash = ? LIMIT 1",
                (content_hash,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()

    def count(self) -> int:
        """Count total observations."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM observations")
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Get storage statistics."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(raw_tokens) as total_raw,
                    SUM(compressed_tokens) as total_compressed,
                    COUNT(DISTINCT session_id) as sessions
                FROM observations
            """)
            row = cursor.fetchone()
            total_raw = row[1] or 0
            total_compressed = row[2] or 0
            return {
                "total_observations": row[0],
                "total_raw_tokens": total_raw,
                "total_compressed_tokens": total_compressed,
                "compression_ratio": total_raw / total_compressed if total_compressed > 0 else 0,
                "sessions": row[3],
            }
        finally:
            conn.close()

    def _row_to_observation(self, row: tuple) -> CompressedObservation:
        """Convert database row to observation object."""
        return CompressedObservation(
            id=row[0],
            session_id=row[1],
            timestamp=datetime.fromisoformat(row[2]),
            tool_name=row[3],
            observation_type=row[4],
            concepts=json.loads(row[5]),
            raw_tokens=row[6],
            compressed_tokens=row[7],
            semantic_summary=row[8],
            key_facts=json.loads(row[9]),
            related_files=json.loads(row[10]),
            raw_content_hash=row[11],
        )


# ============================================================================
# COMPRESSION ENGINE
# ============================================================================

COMPRESSION_PROMPT = """Analyze this tool output and extract structured information.

Tool: {tool_name}
Output:
```
{tool_output}
```

Respond with a JSON object containing:
1. "type": One of: bugfix, feature, refactor, discovery, decision, change, error, info
2. "concepts": 3-5 key technical concepts (short tags like "authentication", "caching", "error-handling")
3. "summary": A 2-4 sentence semantic summary capturing the essential information
4. "facts": Array of 3-10 bullet points with specific technical details (file paths, function names, error messages, key values)
5. "files": Array of file paths mentioned or affected

Be concise but preserve critical technical details that would be needed to understand this work later."""


def make_observation_id() -> str:
    """Generate unique observation ID."""
    import random
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    suffix = "".join(random.choices(chars, k=8))
    return f"obs_{suffix}"


def hash_content(content: str) -> str:
    """Create content hash for deduplication."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class ObservationCompressor:
    """Compresses tool outputs using Claude API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.client = None
        if ANTHROPIC_AVAILABLE and self.api_key:
            try:
                self.client = anthropic.Anthropic(api_key=self.api_key)
            except Exception as e:
                print(f"[compressor] Failed to init Anthropic client: {e}", file=sys.stderr)

        self.db = ObservationDB()
        self._queue = queue.Queue()
        self._worker_thread = None
        self._stop_event = threading.Event()

    def is_available(self) -> bool:
        """Check if compression is available."""
        return self.client is not None

    def start_worker(self):
        """Start background compression worker."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def stop_worker(self):
        """Stop background worker."""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)

    def enqueue(self, tool_use: dict):
        """Add tool output to compression queue (non-blocking)."""
        self._queue.put(tool_use)

    def _worker_loop(self):
        """Background worker that processes the compression queue."""
        while not self._stop_event.is_set():
            try:
                tool_use = self._queue.get(timeout=1)
                self._process_tool_use(tool_use)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[compressor] Worker error: {e}", file=sys.stderr)

    def _process_tool_use(self, tool_use: dict):
        """Process a single tool use and store compressed observation."""
        tool_name = tool_use.get("tool_name", "unknown")
        tool_output = tool_use.get("output", "")
        session_id = tool_use.get("session_id", "unknown")

        # Skip small outputs
        raw_tokens = estimate_tokens(tool_output)
        if raw_tokens < MAX_RAW_TOKENS_BEFORE_COMPRESS:
            return

        # Check for duplicates
        content_hash = hash_content(tool_output)
        if self.db.has_hash(content_hash):
            return

        # Compress using Claude API
        try:
            compressed = self.compress(tool_name, tool_output, session_id)
            if compressed:
                compressed.raw_content_hash = content_hash
                self.db.store(compressed)
        except Exception as e:
            print(f"[compressor] Compression failed: {e}", file=sys.stderr)

    def compress(self, tool_name: str, tool_output: str,
                 session_id: str = "unknown") -> CompressedObservation | None:
        """Compress a tool output using Claude API."""
        if not self.client:
            return self._fallback_compress(tool_name, tool_output, session_id)

        # Truncate very long outputs for API call
        truncated_output = tool_output[:MAX_INPUT_CHARS]
        if len(tool_output) > MAX_INPUT_CHARS:
            truncated_output += f"\n\n[...truncated {len(tool_output) - MAX_INPUT_CHARS} chars...]"

        prompt = COMPRESSION_PROMPT.format(
            tool_name=tool_name,
            tool_output=truncated_output
        )

        try:
            response = self.client.messages.create(
                model=COMPRESSION_MODEL,
                max_tokens=API_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text

            # Parse JSON response
            # Handle markdown code blocks
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            parsed = json.loads(content)

            summary = parsed.get("summary", "")
            facts = parsed.get("facts", [])
            compressed_tokens = estimate_tokens(summary + " ".join(facts))

            return CompressedObservation(
                id=make_observation_id(),
                session_id=session_id,
                timestamp=datetime.now(),
                tool_name=tool_name,
                observation_type=parsed.get("type", "info"),
                concepts=parsed.get("concepts", [])[:MAX_CONCEPTS_COUNT],
                raw_tokens=estimate_tokens(tool_output),
                compressed_tokens=compressed_tokens,
                semantic_summary=summary,
                key_facts=facts[:MAX_FACTS_COUNT],
                related_files=parsed.get("files", []),
                raw_content_hash="",
            )

        except json.JSONDecodeError as e:
            print(f"[compressor] Failed to parse response: {e}", file=sys.stderr)
            return self._fallback_compress(tool_name, tool_output, session_id)
        except Exception as e:
            # Provide better error messages for common failures
            error_msg = str(e)
            if "rate_limit" in error_msg.lower() or "429" in error_msg:
                print("[compressor] Rate limit exceeded, using fallback", file=sys.stderr)
            elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                print("[compressor] API timeout, using fallback", file=sys.stderr)
            elif "connection" in error_msg.lower() or "network" in error_msg.lower():
                print("[compressor] Network error, using fallback", file=sys.stderr)
            elif "401" in error_msg or "403" in error_msg or "invalid" in error_msg.lower():
                print("[compressor] Authentication error, using fallback", file=sys.stderr)
            else:
                print(f"[compressor] API error: {e}", file=sys.stderr)
            return self._fallback_compress(tool_name, tool_output, session_id)

    def _fallback_compress(self, tool_name: str, tool_output: str,
                           session_id: str) -> CompressedObservation:
        """Fallback compression without API (just truncate)."""
        # Extract first few lines as summary
        lines = tool_output.strip().split("\n")
        summary = " ".join(lines[:5])[:500]

        # Extract file paths
        import re
        file_pattern = r'["\']?([/\\]?(?:\w+[/\\])*\w+\.\w+)["\']?'
        files = list(set(re.findall(file_pattern, tool_output)))[:10]

        return CompressedObservation(
            id=make_observation_id(),
            session_id=session_id,
            timestamp=datetime.now(),
            tool_name=tool_name,
            observation_type="info",
            concepts=[],
            raw_tokens=estimate_tokens(tool_output),
            compressed_tokens=estimate_tokens(summary),
            semantic_summary=summary,
            key_facts=[],
            related_files=files,
            raw_content_hash="",
        )


# ============================================================================
# PROGRESSIVE RETRIEVAL (3-Layer)
# ============================================================================

class ProgressiveRetriever:
    """3-layer retrieval for token efficiency.

    Layer 1: Compact index only (~50-100 tokens per result)
    Layer 2: Timeline context around observation
    Layer 3: Full observation details (~500-1000 tokens each)
    """

    def __init__(self, db: ObservationDB | None = None):
        self.db = db or ObservationDB()

    def layer1_search(self, query: str, limit: int = DEFAULT_INDEX_LIMIT) -> list[ObservationIndex]:
        """Layer 1: Return compact index entries only."""
        results = self.db.search_fts(query, limit=limit * 2)

        return [ObservationIndex(
            id=r.id,
            date=r.timestamp.strftime("%Y-%m-%d"),
            type=r.observation_type,
            title=r.semantic_summary[:80],
            token_count=r.raw_tokens,
            concepts=r.concepts[:3],
        ) for r in results[:limit]]

    def layer2_timeline(self, obs_id: str, window: int = TIMELINE_WINDOW_SIZE) -> list[CompressedObservation]:
        """Layer 2: Get timeline context around an observation."""
        obs = self.db.get(obs_id)
        if not obs:
            return []

        before = self.db.get_before(obs.timestamp, limit=window)
        after = self.db.get_after(obs.timestamp, limit=window)
        return before + [obs] + after

    def layer3_full(self, obs_ids: list[str]) -> list[CompressedObservation]:
        """Layer 3: Get full observation details."""
        return [self.db.get(oid) for oid in obs_ids if self.db.get(oid)]

    def get_session_summary(self, session_id: str) -> str:
        """Get formatted summary for a session."""
        observations = self.db.get_by_session(session_id)
        if not observations:
            return ""

        lines = [f"# Session {session_id} Summary ({len(observations)} observations)"]
        for obs in observations:
            lines.append(f"\n## [{obs.timestamp.strftime('%H:%M')}] {obs.observation_type}: {obs.tool_name}")
            lines.append(obs.semantic_summary)
            if obs.key_facts:
                lines.append("\nKey facts:")
                for fact in obs.key_facts[:5]:
                    lines.append(f"  - {fact}")

        return "\n".join(lines)

    def get_recent_context(self, limit: int = RECENT_CONTEXT_LIMIT, token_budget: int = RECENT_CONTEXT_BUDGET) -> str:
        """Get recent observations within token budget."""
        observations = self.db.get_recent(limit=limit)
        lines = []
        tokens_used = 0

        for obs in observations:
            entry = f"[{obs.timestamp.strftime('%Y-%m-%d %H:%M')}] {obs.observation_type}: {obs.semantic_summary}"
            entry_tokens = estimate_tokens(entry)

            if tokens_used + entry_tokens > token_budget:
                break

            lines.append(entry)
            tokens_used += entry_tokens

        return "\n".join(lines)


# ============================================================================
# HOOK INTEGRATION
# ============================================================================

# Global compressor instance
_compressor: ObservationCompressor | None = None


def get_compressor() -> ObservationCompressor:
    """Get or create the global compressor instance."""
    global _compressor
    if _compressor is None:
        _compressor = ObservationCompressor()
        _compressor.start_worker()
    return _compressor


def capture_tool_output(tool_name: str, output: str, session_id: str = "unknown"):
    """Capture a tool output for compression (called from hook)."""
    compressor = get_compressor()
    compressor.enqueue({
        "tool_name": tool_name,
        "output": output,
        "session_id": session_id,
    })


def get_retriever() -> ProgressiveRetriever:
    """Get a progressive retriever instance."""
    return ProgressiveRetriever()


# ============================================================================
# CLI INTERFACE
# ============================================================================

def main():
    """CLI for testing compression."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute memory compression")
    subparsers = parser.add_subparsers(dest="command")

    # Stats command
    subparsers.add_parser("stats", help="Show storage statistics")

    # Search command
    search_parser = subparsers.add_parser("search", help="Search observations")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=10, help="Max results")

    # Recent command
    recent_parser = subparsers.add_parser("recent", help="Show recent observations")
    recent_parser.add_argument("--limit", type=int, default=10, help="Max results")

    # Test command
    test_parser = subparsers.add_parser("test", help="Test compression")
    test_parser.add_argument("--text", help="Text to compress", default="Sample tool output for testing")

    args = parser.parse_args()

    db = ObservationDB()

    if args.command == "stats":
        stats = db.get_stats()
        print(f"Observations: {stats['total_observations']}")
        print(f"Raw tokens: {stats['total_raw_tokens']:,}")
        print(f"Compressed tokens: {stats['total_compressed_tokens']:,}")
        print(f"Compression ratio: {stats['compression_ratio']:.1f}x")
        print(f"Sessions: {stats['sessions']}")

    elif args.command == "search":
        retriever = ProgressiveRetriever(db)
        results = retriever.layer1_search(args.query, limit=args.limit)
        for r in results:
            print(f"[{r.date}] {r.type}: {r.title}")
            print(f"  ID: {r.id}, Tokens: {r.token_count}, Concepts: {r.concepts}")

    elif args.command == "recent":
        observations = db.get_recent(limit=args.limit)
        for obs in observations:
            print(f"[{obs.timestamp.strftime('%Y-%m-%d %H:%M')}] {obs.tool_name}: {obs.observation_type}")
            print(f"  {obs.semantic_summary[:100]}...")

    elif args.command == "test":
        compressor = ObservationCompressor()
        if compressor.is_available():
            print("Testing API compression...")
            obs = compressor.compress("test", args.text, "test_session")
            if obs:
                print(f"Type: {obs.observation_type}")
                print(f"Concepts: {obs.concepts}")
                print(f"Summary: {obs.semantic_summary}")
                print(f"Facts: {obs.key_facts}")
        else:
            print("Anthropic API not available. Using fallback compression...")
            obs = compressor._fallback_compress("test", args.text, "test_session")
            print(f"Fallback summary: {obs.semantic_summary[:100]}...")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
