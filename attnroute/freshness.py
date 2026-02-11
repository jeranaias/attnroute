#!/usr/bin/env python3
"""
attnroute.freshness — Documentation Staleness Guard

Checks .md documentation files for stale code references before injection.
Cross-references backtick code references against the actual codebase.

Features:
  - Extracts backtick references (`function_name`, `ClassName`, `path/to/file.py`)
  - Verifies against filesystem and tree-sitter AST symbols (if available)
  - staleness_score = stale_refs / total_refs

Routing integration:
  - staleness > 0.8 -> skip injection entirely
  - staleness > 0.5 -> demote to WARM with [STALE] warning prefix
  - Cached per file, recheck every 10 turns
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from attnroute.telemetry_lib import TELEMETRY_DIR, ensure_telemetry_dir, windows_utf8_io
    windows_utf8_io()
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from telemetry_lib import TELEMETRY_DIR, ensure_telemetry_dir, windows_utf8_io
        windows_utf8_io()
    except ImportError:
        TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"
        def ensure_telemetry_dir(): TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)

# Cache file
FRESHNESS_CACHE = TELEMETRY_DIR / "freshness_cache.json"

# Try to import outliner for symbol extraction
try:
    from attnroute.outliner import TREE_SITTER_AVAILABLE, extract_outline
except ImportError:
    try:
        from outliner import TREE_SITTER_AVAILABLE, extract_outline
    except ImportError:
        TREE_SITTER_AVAILABLE = False
        def extract_outline(path): return None


# ============================================================================
# STALENESS CHECKER
# ============================================================================

class StalenessChecker:
    """
    Check documentation freshness by validating code references.

    For each .md file, extracts backtick references and checks if they
    exist in the codebase (files, functions, classes).
    """

    def __init__(self, codebase_root: Path = None):
        self.codebase_root = codebase_root or Path.cwd()
        self.cache = self._load_cache()
        self._turn_counter = 0
        self._recheck_interval = 10  # Recheck every N turns

        # Build symbol index on first use
        self._symbols: set[str] | None = None
        self._files: set[str] | None = None

    def _load_cache(self) -> dict:
        """Load freshness cache."""
        if FRESHNESS_CACHE.exists():
            try:
                return json.loads(FRESHNESS_CACHE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_cache(self):
        """Save freshness cache."""
        ensure_telemetry_dir()
        try:
            FRESHNESS_CACHE.write_text(
                json.dumps(self.cache, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _build_file_index(self) -> set[str]:
        """Build index of all files in codebase."""
        if self._files is not None:
            return self._files

        self._files = set()
        try:
            # Index common source files
            for ext in ["*.py", "*.js", "*.ts", "*.tsx", "*.go", "*.rs", "*.java", "*.c", "*.cpp", "*.h"]:
                for f in self.codebase_root.rglob(ext):
                    if "node_modules" in str(f) or ".git" in str(f):
                        continue
                    # Add filename and relative path
                    self._files.add(f.name)
                    try:
                        self._files.add(str(f.relative_to(self.codebase_root)))
                    except ValueError:
                        pass
        except Exception:
            pass

        return self._files

    def _build_symbol_index(self) -> set[str]:
        """Build index of code symbols (functions, classes) using tree-sitter."""
        if self._symbols is not None:
            return self._symbols

        self._symbols = set()

        if not TREE_SITTER_AVAILABLE:
            return self._symbols

        try:
            # Extract symbols from Python files (most common)
            for py_file in self.codebase_root.rglob("*.py"):
                if "node_modules" in str(py_file) or ".git" in str(py_file):
                    continue
                try:
                    outline = extract_outline(py_file)
                    if outline:
                        # Extract function/class names from outline
                        for line in outline.split("\n"):
                            # Match "def func_name(" or "class ClassName"
                            match = re.search(r'(?:def|class)\s+(\w+)', line)
                            if match:
                                self._symbols.add(match.group(1))
                except Exception:
                    continue
        except Exception:
            pass

        return self._symbols

    def extract_references(self, md_content: str) -> list[str]:
        """
        Extract code references from markdown content.

        Looks for:
          - Backtick references: `function_name`, `ClassName`, `path/to/file.py`
          - Inline code: `code`
        """
        # Match backtick content (not inside code blocks)
        refs = re.findall(r'`([^`\n]+)`', md_content)

        # Filter to likely code references
        code_refs = []
        for ref in refs:
            ref = ref.strip()
            # Skip if too short or looks like command/string
            if len(ref) < 3:
                continue
            if ref.startswith("-") or ref.startswith("$"):
                continue
            if " " in ref and "/" not in ref:  # Multi-word but not a path
                continue

            code_refs.append(ref)

        return code_refs

    def check_reference(self, ref: str) -> bool:
        """
        Check if a reference exists in the codebase.

        Returns True if the reference is valid (exists).
        """
        # Check if it's a file path
        files = self._build_file_index()
        if ref in files:
            return True

        # Check file with common extensions
        for ext in [".py", ".js", ".ts", ".go", ".rs"]:
            if ref + ext in files:
                return True

        # Check if it's a symbol (function/class name)
        symbols = self._build_symbol_index()
        if ref in symbols:
            return True

        # Check as direct file path
        path = self.codebase_root / ref
        if path.exists():
            return True

        return False

    def check_staleness(self, md_path: Path, force: bool = False) -> tuple[float, list[str]]:
        """
        Check staleness of a markdown file.

        Args:
            md_path: Path to markdown file
            force: Force recheck, ignoring cache

        Returns:
            (staleness_score, list_of_stale_references)
            staleness_score is 0.0-1.0 (higher = more stale)
        """
        path_key = str(md_path)

        # Check cache
        if not force and path_key in self.cache:
            cached = self.cache[path_key]
            # Check if still valid (within recheck interval)
            checks_since = cached.get("checks_since", 0)
            if checks_since < self._recheck_interval:
                self.cache[path_key]["checks_since"] = checks_since + 1
                self._save_cache()
                return cached.get("staleness", 0.0), cached.get("stale_refs", [])

        # Read and check
        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return 0.0, []

        refs = self.extract_references(content)
        if not refs:
            return 0.0, []

        stale_refs = []
        for ref in refs:
            if not self.check_reference(ref):
                stale_refs.append(ref)

        staleness = len(stale_refs) / len(refs) if refs else 0.0

        # Update cache
        self.cache[path_key] = {
            "staleness": round(staleness, 3),
            "stale_refs": stale_refs[:10],  # Limit stored refs
            "total_refs": len(refs),
            "checked_at": datetime.now().isoformat(),
            "checks_since": 0,
        }
        self._save_cache()

        return staleness, stale_refs

    def should_skip(self, md_path: Path) -> bool:
        """Check if file should be skipped entirely (staleness > 0.8)."""
        staleness, _ = self.check_staleness(md_path)
        return staleness > 0.8

    def should_demote(self, md_path: Path) -> bool:
        """Check if file should be demoted to WARM (staleness > 0.5)."""
        staleness, _ = self.check_staleness(md_path)
        return 0.5 < staleness <= 0.8

    def get_warning_prefix(self, md_path: Path) -> str:
        """Get warning prefix for stale files."""
        staleness, stale_refs = self.check_staleness(md_path)
        if staleness > 0.5:
            return f"[STALE: {staleness:.0%} refs outdated] "
        return ""

    def get_stats(self) -> dict:
        """Get freshness check statistics."""
        total_files = len(self.cache)
        stale_files = sum(1 for c in self.cache.values() if c.get("staleness", 0) > 0.5)
        avg_staleness = sum(c.get("staleness", 0) for c in self.cache.values()) / total_files if total_files else 0

        return {
            "files_checked": total_files,
            "stale_files": stale_files,
            "avg_staleness": round(avg_staleness, 3),
        }


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for freshness checking."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute documentation freshness checker")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "check", "clear"],
                        help="Command to run")
    parser.add_argument("--file", "-f", type=Path, help="Specific file to check")
    parser.add_argument("--force", action="store_true", help="Force recheck")
    args = parser.parse_args()

    checker = StalenessChecker()

    if args.command == "status":
        stats = checker.get_stats()
        print()
        print("Freshness Checker Status")
        print("=" * 40)
        print(f"  Files checked: {stats['files_checked']}")
        print(f"  Stale files (>50%): {stats['stale_files']}")
        print(f"  Average staleness: {stats['avg_staleness']:.0%}")
        print()

        if checker.cache:
            print("Recent checks:")
            sorted_cache = sorted(checker.cache.items(),
                                  key=lambda x: x[1].get("staleness", 0), reverse=True)
            for path, data in sorted_cache[:10]:
                staleness = data.get("staleness", 0)
                status = "STALE" if staleness > 0.5 else "OK"
                print(f"  {staleness:.0%}  [{status}] {Path(path).name}")
        print()

    elif args.command == "check":
        if args.file:
            staleness, stale_refs = checker.check_staleness(args.file, force=args.force)
            print()
            print(f"Staleness: {staleness:.0%}")
            if stale_refs:
                print(f"Stale references ({len(stale_refs)}):")
                for ref in stale_refs[:20]:
                    print(f"  - {ref}")
            print()
        else:
            # Check all .md files in .claude/
            docs_root = Path(".claude")
            if not docs_root.exists():
                docs_root = Path.home() / ".claude"

            print(f"Checking files in {docs_root}...")
            for md_file in docs_root.rglob("*.md"):
                if md_file.name == "CLAUDE.md":
                    continue
                staleness, stale_refs = checker.check_staleness(md_file, force=args.force)
                status = "STALE" if staleness > 0.5 else "OK"
                print(f"  {staleness:.0%}  [{status}] {md_file.name}")
            print()

    elif args.command == "clear":
        if FRESHNESS_CACHE.exists():
            FRESHNESS_CACHE.unlink()
            print("Freshness cache cleared.")
        else:
            print("No cache to clear.")


if __name__ == "__main__":
    main()
