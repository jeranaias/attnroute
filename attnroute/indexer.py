#!/usr/bin/env python3
"""
attnroute.indexer — Hybrid BM25 + Semantic Search Index

Builds and maintains a search index over .md files and code outlines.
Uses BM25S for fast lexical search and Model2Vec for semantic reranking.

Features:
  - Hybrid search: BM25 sparse retrieval + semantic rerank
  - Incremental updates via mtime tracking
  - SQLite storage for persistence
  - Graceful degradation when dependencies unavailable

Search pipeline:
  1. BM25 retrieves top-20 candidates (sub-ms)
  2. Model2Vec reranks to top-10 (< 1ms)
  3. Final score: 0.6 * bm25_norm + 0.4 * cosine_sim
"""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from attnroute.telemetry_lib import TELEMETRY_DIR, windows_utf8_io
    windows_utf8_io()
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from telemetry_lib import TELEMETRY_DIR, windows_utf8_io
        windows_utf8_io()
    except ImportError:
        TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"

# Index storage location
INDEX_DB = TELEMETRY_DIR / "search_index.db"

# Source file configuration
SOURCE_MAX_FILE_SIZE = 100_000  # 100KB - skip files larger than this
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt"
}
SOURCE_EXCLUDED_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv",
    "dist", "build", "target", ".next", ".nuxt", "coverage",
    ".pytest_cache", ".mypy_cache", ".tox", "eggs", ".egg-info",
    ".cache", "vendor", "bower_components"
}

# ============================================================================
# DEPENDENCY DETECTION
# ============================================================================

# Try BM25S (fast sparse retrieval)
try:
    # Suppress bm25s's "resource module not available on Windows" warning
    # bm25s's C extension prints directly to stdout during import
    import os
    if os.name == 'nt':  # Windows
        # Temporarily redirect stdout FD to devnull
        # Must flush and recreate file object for FD redirect to work
        import sys as _sys
        _sys.stdout.flush()
        stdout_fd = _sys.stdout.fileno()
        old_stdout = os.dup(stdout_fd)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, stdout_fd)
        _sys.stdout = os.fdopen(stdout_fd, 'w')
        try:
            import bm25s
        finally:
            # Restore stdout
            _sys.stdout.flush()
            os.dup2(old_stdout, stdout_fd)
            _sys.stdout = os.fdopen(old_stdout, 'w')
            os.close(devnull_fd)
    else:
        import bm25s
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False

# Try Model2Vec (semantic embeddings)
try:
    from model2vec import StaticModel
    MODEL2VEC_AVAILABLE = True
except ImportError:
    MODEL2VEC_AVAILABLE = False

# Try numpy (required for semantic ops)
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# Try outliner for code summaries
try:
    from attnroute.outliner import extract_outline
    OUTLINER_AVAILABLE = True
except ImportError:
    try:
        from outliner import extract_outline
        OUTLINER_AVAILABLE = True
    except ImportError:
        OUTLINER_AVAILABLE = False


# ============================================================================
# TF-IDF FALLBACK (when bm25s unavailable)
# ============================================================================

class SimpleTFIDF:
    """Minimal TF-IDF implementation as BM25 fallback."""

    def __init__(self):
        self.vocab = {}
        self.idf = {}
        self.doc_vecs = []
        self.doc_paths = []

    def index(self, documents: list[tuple[str, str]]):
        """Build index from (path, content) pairs."""
        self.doc_paths = [p for p, _ in documents]
        self.doc_vecs = []

        # Build vocabulary
        all_tokens = []
        for _, content in documents:
            tokens = self._tokenize(content)
            all_tokens.append(tokens)

        # Build vocab from all tokens
        vocab_set = set()
        for tokens in all_tokens:
            vocab_set.update(tokens)
        self.vocab = {t: i for i, t in enumerate(sorted(vocab_set))}

        # Compute IDF
        doc_count = len(documents)
        doc_freq = {}
        for tokens in all_tokens:
            for t in set(tokens):
                doc_freq[t] = doc_freq.get(t, 0) + 1

        import math
        self.idf = {t: math.log((doc_count + 1) / (df + 1)) + 1 for t, df in doc_freq.items()}

        # Build TF-IDF vectors
        for tokens in all_tokens:
            tf = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            vec = [0.0] * len(self.vocab)
            for t, count in tf.items():
                if t in self.vocab:
                    vec[self.vocab[t]] = count * self.idf.get(t, 1.0)
            self.doc_vecs.append(vec)

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """Search and return (path, score) pairs."""
        if not self.doc_vecs:
            return []

        tokens = self._tokenize(query)
        query_vec = [0.0] * len(self.vocab)
        for t in tokens:
            if t in self.vocab:
                query_vec[self.vocab[t]] = self.idf.get(t, 1.0)

        # Compute cosine similarity
        import math
        query_norm = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        results = []
        for i, doc_vec in enumerate(self.doc_vecs):
            dot = sum(q * d for q, d in zip(query_vec, doc_vec))
            doc_norm = math.sqrt(sum(x * x for x in doc_vec)) or 1.0
            score = dot / (query_norm * doc_norm)
            if score > 0:
                results.append((self.doc_paths[i], score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization."""
        return re.findall(r'[a-z][a-z0-9_]{2,}', text.lower())


# ============================================================================
# SEARCH INDEX
# ============================================================================

class SearchIndex:
    """
    Hybrid BM25 + semantic search index.

    Storage: SQLite database with documents table.
    Search: BM25 sparse retrieval → Model2Vec semantic rerank.
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or INDEX_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # BM25 index (in-memory, rebuilt from DB on load)
        self._bm25 = None
        self._bm25_paths = []

        # TF-IDF fallback
        self._tfidf = None

        # Semantic model (lazy loaded)
        self._model = None

    def _init_db(self):
        """Initialize SQLite schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY,
                    content TEXT,
                    outline TEXT,
                    mtime REAL,
                    doc_type TEXT,
                    indexed_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON documents(mtime)")

    def _load_model(self):
        """Lazy load Model2Vec model."""
        if self._model is None and MODEL2VEC_AVAILABLE:
            try:
                self._model = StaticModel.from_pretrained("minishlab/potion-base-8M")
            except Exception as e:
                print(f"[indexer] Failed to load Model2Vec: {e}", file=sys.stderr)
                self._model = False  # Mark as failed
        return self._model if self._model else None

    def _should_skip_path(self, file_path: Path) -> bool:
        """Check if a path should be skipped based on excluded directories."""
        path_str = str(file_path)
        for excluded in SOURCE_EXCLUDED_DIRS:
            # Check if any path component matches the excluded dir
            if f"/{excluded}/" in path_str or f"\\{excluded}\\" in path_str:
                return True
            if path_str.endswith(f"/{excluded}") or path_str.endswith(f"\\{excluded}"):
                return True
        return False

    def build(self, docs_root: Path, code_roots: list[Path] = None):
        """
        Full index rebuild.

        Args:
            docs_root: Root directory for .md documentation files
            code_roots: List of directories containing source code
        """
        code_roots = code_roots or []
        documents = []

        # Index .md files
        if docs_root.exists():
            for md_file in docs_root.rglob("*.md"):
                if md_file.name.startswith("."):
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8", errors="replace")
                    rel_path = str(md_file.relative_to(docs_root.parent))
                    documents.append({
                        "path": rel_path,
                        "content": content,
                        "outline": "",  # Markdown doesn't need outline
                        "mtime": md_file.stat().st_mtime,
                        "doc_type": "markdown"
                    })
                except Exception:
                    continue

        # Index code files with outlines
        for code_root in code_roots:
            if not code_root.exists():
                continue

            # Iterate over supported source extensions
            for ext in SOURCE_EXTENSIONS:
                pattern = f"*{ext}"
                for code_file in code_root.rglob(pattern):
                    # Skip hidden files
                    if code_file.name.startswith("."):
                        continue

                    # Skip excluded directories
                    if self._should_skip_path(code_file):
                        continue

                    # Skip large files (generated code, minified bundles, etc.)
                    try:
                        file_size = code_file.stat().st_size
                        if file_size > SOURCE_MAX_FILE_SIZE:
                            continue
                    except Exception:
                        continue

                    try:
                        rel_path = str(code_file.relative_to(code_root))

                        # Try to get outline if available
                        outline = ""
                        if OUTLINER_AVAILABLE:
                            try:
                                outline = extract_outline(code_file) or ""
                            except Exception:
                                pass

                        # If no outline, use first portion of content for indexing
                        if not outline:
                            try:
                                content = code_file.read_text(encoding="utf-8", errors="replace")
                                # Use first 2000 chars for search indexing
                                outline = content[:2000]
                            except Exception:
                                continue

                        if outline:
                            documents.append({
                                "path": rel_path,
                                "content": outline,
                                "outline": outline,
                                "mtime": code_file.stat().st_mtime,
                                "doc_type": "code"
                            })
                    except Exception:
                        continue

        # Store in SQLite
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM documents")
            for doc in documents:
                conn.execute("""
                    INSERT INTO documents (path, content, outline, mtime, doc_type, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (doc["path"], doc["content"], doc["outline"], doc["mtime"],
                      doc["doc_type"], datetime.now().isoformat()))
            conn.commit()

        # Rebuild in-memory index
        self._build_memory_index(documents)

        md_count = sum(1 for d in documents if d["doc_type"] == "markdown")
        code_count = sum(1 for d in documents if d["doc_type"] == "code")
        print(f"[indexer] Indexed {len(documents)} documents ({md_count} docs, {code_count} source)", file=sys.stderr)

    def update_incremental(self, docs_root: Path, code_roots: list[Path] = None):
        """Re-index only changed files (mtime check)."""
        code_roots = code_roots or []
        updated = 0

        with sqlite3.connect(self.db_path) as conn:
            # Get existing mtimes
            existing = {row[0]: row[1] for row in conn.execute(
                "SELECT path, mtime FROM documents"
            )}

            # Check .md files
            if docs_root.exists():
                for md_file in docs_root.rglob("*.md"):
                    if md_file.name.startswith("."):
                        continue
                    rel_path = str(md_file.relative_to(docs_root.parent))
                    try:
                        current_mtime = md_file.stat().st_mtime
                        if rel_path not in existing or existing[rel_path] < current_mtime:
                            content = md_file.read_text(encoding="utf-8", errors="replace")
                            conn.execute("""
                                INSERT OR REPLACE INTO documents (path, content, outline, mtime, doc_type, indexed_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (rel_path, content, "", current_mtime, "markdown", datetime.now().isoformat()))
                            updated += 1
                    except Exception:
                        continue

            # Check source files
            for code_root in code_roots:
                if not code_root.exists():
                    continue

                for ext in SOURCE_EXTENSIONS:
                    pattern = f"*{ext}"
                    for code_file in code_root.rglob(pattern):
                        if code_file.name.startswith("."):
                            continue
                        if self._should_skip_path(code_file):
                            continue

                        try:
                            file_size = code_file.stat().st_size
                            if file_size > SOURCE_MAX_FILE_SIZE:
                                continue

                            rel_path = str(code_file.relative_to(code_root))
                            current_mtime = code_file.stat().st_mtime

                            if rel_path not in existing or existing[rel_path] < current_mtime:
                                # Get outline or content
                                outline = ""
                                if OUTLINER_AVAILABLE:
                                    try:
                                        outline = extract_outline(code_file) or ""
                                    except Exception:
                                        pass

                                if not outline:
                                    content = code_file.read_text(encoding="utf-8", errors="replace")
                                    outline = content[:2000]

                                if outline:
                                    conn.execute("""
                                        INSERT OR REPLACE INTO documents (path, content, outline, mtime, doc_type, indexed_at)
                                        VALUES (?, ?, ?, ?, ?, ?)
                                    """, (rel_path, outline, outline, current_mtime, "code", datetime.now().isoformat()))
                                    updated += 1
                        except Exception:
                            continue

            conn.commit()

        if updated > 0:
            self._rebuild_memory_index()
            print(f"[indexer] Updated {updated} documents", file=sys.stderr)

    def _build_memory_index(self, documents: list[dict]):
        """Build in-memory BM25/TF-IDF index from documents."""
        if not documents:
            return

        paths = [d["path"] for d in documents]
        contents = [d["content"] for d in documents]

        if BM25_AVAILABLE:
            try:
                # Tokenize for BM25
                tokenized = [re.findall(r'[a-z][a-z0-9_]{2,}', c.lower()) for c in contents]
                self._bm25 = bm25s.BM25()
                self._bm25.index(tokenized)
                self._bm25_paths = paths
            except Exception as e:
                print(f"[indexer] BM25 index failed: {e}", file=sys.stderr)
                self._bm25 = None
        else:
            # Fallback to TF-IDF
            self._tfidf = SimpleTFIDF()
            self._tfidf.index(list(zip(paths, contents)))

    def _rebuild_memory_index(self):
        """Rebuild in-memory index from SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT path, content FROM documents").fetchall()
        documents = [{"path": p, "content": c} for p, c in rows]
        self._build_memory_index(documents)

    def query(self, prompt: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        Hybrid search: BM25 → semantic rerank.

        Args:
            prompt: Search query
            top_k: Number of results to return

        Returns:
            List of (path, score) tuples, sorted by relevance
        """
        # Ensure memory index is loaded
        if self._bm25 is None and self._tfidf is None:
            self._rebuild_memory_index()

        # Phase 1: BM25/TF-IDF sparse retrieval
        candidates = self._bm25_search(prompt, top_k=20)
        if not candidates:
            return []

        # Phase 2: Semantic rerank (if available)
        model = self._load_model()
        if model and NUMPY_AVAILABLE:
            candidates = self._semantic_rerank(prompt, candidates, top_k)
        else:
            # No semantic model - just return BM25 results
            candidates = candidates[:top_k]

        return candidates

    def _bm25_search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """BM25 sparse retrieval."""
        if self._bm25 is not None and BM25_AVAILABLE:
            try:
                tokenized = [re.findall(r'[a-z][a-z0-9_]{2,}', query.lower())]
                results, scores = self._bm25.retrieve(tokenized, k=top_k)
                candidates = []
                for idx, score in zip(results[0], scores[0]):
                    if idx < len(self._bm25_paths):
                        candidates.append((self._bm25_paths[idx], float(score)))
                return candidates
            except Exception:
                pass

        if self._tfidf is not None:
            return self._tfidf.search(query, top_k)

        return []

    def _semantic_rerank(self, query: str, candidates: list[tuple[str, float]],
                         top_k: int = 10) -> list[tuple[str, float]]:
        """Model2Vec semantic reranking."""
        model = self._load_model()
        if not model or not NUMPY_AVAILABLE:
            return candidates[:top_k]

        try:
            # Get document contents from DB
            paths = [p for p, _ in candidates]
            bm25_scores = {p: s for p, s in candidates}

            with sqlite3.connect(self.db_path) as conn:
                placeholders = ",".join(["?"] * len(paths))
                rows = conn.execute(
                    f"SELECT path, content FROM documents WHERE path IN ({placeholders})",
                    paths
                ).fetchall()

            path_to_content = {p: c for p, c in rows}

            # Embed query and documents
            query_emb = model.encode([query])[0]
            doc_embs = []
            valid_paths = []
            for p in paths:
                if p in path_to_content:
                    doc_embs.append(model.encode([path_to_content[p][:2000]])[0])
                    valid_paths.append(p)

            if not doc_embs:
                return candidates[:top_k]

            doc_embs = np.array(doc_embs)

            # Cosine similarity
            query_norm = query_emb / (np.linalg.norm(query_emb) + 1e-8)
            doc_norms = doc_embs / (np.linalg.norm(doc_embs, axis=1, keepdims=True) + 1e-8)
            similarities = np.dot(doc_norms, query_norm)

            # Normalize BM25 scores
            bm25_max = max(bm25_scores.values()) or 1.0
            norm_bm25 = {p: s / bm25_max for p, s in bm25_scores.items()}

            # Combine scores: 0.6 * bm25 + 0.4 * semantic
            combined = []
            for i, path in enumerate(valid_paths):
                bm25_s = norm_bm25.get(path, 0.0)
                sem_s = float(similarities[i])
                final = 0.6 * bm25_s + 0.4 * sem_s
                combined.append((path, final))

            combined.sort(key=lambda x: x[1], reverse=True)
            return combined[:top_k]

        except Exception as e:
            print(f"[indexer] Semantic rerank failed: {e}", file=sys.stderr)
            return candidates[:top_k]

    def status(self) -> dict:
        """Return index status."""
        with sqlite3.connect(self.db_path) as conn:
            doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            types = dict(conn.execute(
                "SELECT doc_type, COUNT(*) FROM documents GROUP BY doc_type"
            ).fetchall())

        return {
            "indexed_documents": doc_count,
            "document_types": types,
            "bm25_available": BM25_AVAILABLE,
            "model2vec_available": MODEL2VEC_AVAILABLE,
            "outliner_available": OUTLINER_AVAILABLE,
            "db_path": str(self.db_path),
        }

    def get_stats(self) -> dict:
        """Alias for status(), returns stats with total_documents key."""
        status = self.status()
        return {
            "total_documents": status["indexed_documents"],
            "document_types": status["document_types"],
            "bm25_available": status["bm25_available"],
            "model2vec_available": status["model2vec_available"],
        }


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI entry point for index management."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute search index")
    parser.add_argument("command", choices=["build", "update", "status", "query"],
                        help="Command to run")
    parser.add_argument("--docs", type=Path, default=Path(".claude"),
                        help="Documentation root")
    parser.add_argument("--code", type=Path, action="append", default=[],
                        help="Code roots (can specify multiple)")
    parser.add_argument("--query", "-q", type=str, help="Search query")
    parser.add_argument("--top", "-k", type=int, default=10, help="Number of results")
    args = parser.parse_args()

    index = SearchIndex()

    if args.command == "build":
        print(f"Building index from {args.docs}...")
        index.build(args.docs, args.code)
        print("Done.")

    elif args.command == "update":
        print(f"Updating index from {args.docs}...")
        index.update_incremental(args.docs, args.code)
        print("Done.")

    elif args.command == "status":
        status = index.status()
        print()
        print("Search Index Status")
        print("=" * 40)
        print(f"  Documents indexed: {status['indexed_documents']}")
        print(f"  Document types: {status['document_types']}")
        print(f"  BM25 available: {status['bm25_available']}")
        print(f"  Model2Vec available: {status['model2vec_available']}")
        print(f"  Outliner available: {status['outliner_available']}")
        print(f"  Database: {status['db_path']}")
        print()

    elif args.command == "query":
        if not args.query:
            print("Error: --query required for query command")
            sys.exit(1)
        results = index.query(args.query, args.top)
        print()
        print(f"Results for: {args.query}")
        print("=" * 40)
        for path, score in results:
            print(f"  {score:.3f}  {path}")
        print()


if __name__ == "__main__":
    main()
