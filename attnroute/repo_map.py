#!/usr/bin/env python3
"""
AttnRoute Repo Map - Aider-style symbol-level context.

Instead of injecting full files (1000s of tokens), we extract and inject
just the function/class signatures (~50 tokens per file).

This alone can provide 95%+ token reduction while maintaining code understanding.

Features:
- Tree-sitter parsing for 10+ languages
- Dependency graph from imports
- PageRank ranking for relevance
- Token-budgeted output

Usage:
    from attnroute.repo_map import RepoMapper

    mapper = RepoMapper("/path/to/project")
    context = mapper.get_map(query="fix the auth bug", token_budget=1000)
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Try to import tree-sitter
try:
    from tree_sitter_languages import get_language, get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    print("Warning: tree-sitter-languages not installed. Using regex fallback.", file=sys.stderr)

# Try to import networkx for PageRank
try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False

# Try to import telemetry_lib for accurate token counting
try:
    from attnroute.telemetry_lib import estimate_tokens
    TELEMETRY_LIB_AVAILABLE = True
except ImportError:
    try:
        from telemetry_lib import estimate_tokens
        TELEMETRY_LIB_AVAILABLE = True
    except ImportError:
        TELEMETRY_LIB_AVAILABLE = False
        def estimate_tokens(text: str) -> int:
            """Fallback token estimate."""
            return len(text) // 4


# Language configuration
LANGUAGE_MAP = {
    '.py': 'python',
    '.js': 'javascript',
    '.ts': 'typescript',
    '.tsx': 'tsx',
    '.jsx': 'javascript',
    '.go': 'go',
    '.rs': 'rust',
    '.java': 'java',
    '.c': 'c',
    '.cpp': 'cpp',
    '.h': 'c',
    '.hpp': 'cpp',
    '.rb': 'ruby',
    '.php': 'php',
}

# Token estimation constants
CHARS_PER_TOKEN = 4                # Rough estimate: 4 characters per token
SYMBOL_OVERHEAD_TOKENS = 5         # Extra tokens per symbol for formatting

# PageRank configuration
PAGERANK_ALPHA = 0.85              # Damping factor for PageRank algorithm (0.85 is standard)

# Relevance scoring weights
ACTIVE_FILE_BOOST = 10.0           # Boost score for files currently being edited
FILENAME_MATCH_BOOST = 5.0         # Boost score when query matches filename
SYMBOL_NAME_MATCH_BOOST = 3.0      # Boost score when query matches symbol name
SYMBOL_PART_MATCH_BOOST = 1.0      # Boost score when query matches part of symbol name

# Output limits
PARAM_TRUNCATE_LENGTH = 50         # Truncate function parameters longer than this
IMPORT_DISPLAY_LIMIT = 5           # Maximum imports to show in detailed view
SYMBOL_BRIEF_LIMIT = 5             # Maximum symbols to show in brief format
IMPORT_TRUNCATE_LENGTH = 60        # Truncate import statements longer than this
DOCSTRING_TRUNCATE_LENGTH = 100    # Truncate docstrings longer than this

# Default token budgets
DEFAULT_TOKEN_BUDGET = 1000        # Default budget for repo map generation
DEFAULT_CONTEXT_BUDGET = 2000      # Default budget for detailed context
FILE_HEADER_TOKENS = 5             # Token overhead for file header
TRUNCATE_MARKER_TOKENS = 10        # Token overhead for truncation marker


@dataclass
class Symbol:
    """A code symbol (function, class, method)."""
    name: str
    kind: str  # function, class, method, import
    signature: str  # Full signature line
    line: int
    docstring: str | None = None


@dataclass
class FileSymbols:
    """Symbols extracted from a file."""
    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    token_estimate: int = 0


class RepoMapper:
    """
    Generates Aider-style repo maps with symbol-level context.

    Uses tree-sitter for accurate parsing and PageRank for relevance ranking.
    """

    def __init__(self, repo_path: str, max_files: int = 500):
        self.repo_path = Path(repo_path)
        self.max_files = max_files
        self.file_symbols: dict[str, FileSymbols] = {}
        self.dependency_graph = nx.DiGraph() if NETWORKX_AVAILABLE else None
        self._indexed = False

    def index(self, verbose: bool = False) -> None:
        """Index all source files in the repository."""
        if verbose:
            print(f"Indexing {self.repo_path}...")

        files_indexed = 0

        for ext, lang in LANGUAGE_MAP.items():
            for filepath in self.repo_path.rglob(f"*{ext}"):
                # Skip common non-source directories
                path_str = str(filepath)
                if any(skip in path_str for skip in [
                    'node_modules', '.git', '__pycache__', 'venv',
                    '.env', 'dist', 'build', '.next', 'target'
                ]):
                    continue

                if files_indexed >= self.max_files:
                    break

                try:
                    symbols = self._parse_file(filepath, lang)
                    if symbols:
                        rel_path = str(filepath.relative_to(self.repo_path))
                        self.file_symbols[rel_path] = symbols
                        files_indexed += 1

                        # Add to dependency graph
                        if self.dependency_graph is not None:
                            self.dependency_graph.add_node(rel_path)
                except Exception as e:
                    if verbose:
                        print(f"  Error parsing {filepath}: {e}")

        # Build dependency edges
        self._build_dependencies()

        self._indexed = True
        if verbose:
            print(f"Indexed {files_indexed} files, {sum(len(f.symbols) for f in self.file_symbols.values())} symbols")

    def _parse_file(self, filepath: Path, language: str) -> FileSymbols | None:
        """Parse a file and extract symbols."""
        try:
            content = filepath.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return None

        if TREE_SITTER_AVAILABLE:
            return self._parse_with_tree_sitter(filepath, content, language)
        else:
            return self._parse_with_regex(filepath, content, language)

    def _parse_with_tree_sitter(self, filepath: Path, content: str, language: str) -> FileSymbols | None:
        """Parse using tree-sitter for accurate AST extraction."""
        try:
            parser = get_parser(language)
            tree = parser.parse(content.encode())
        except Exception:
            return self._parse_with_regex(filepath, content, language)

        symbols = []
        imports = []

        def extract_text(node) -> str:
            return content[node.start_byte:node.end_byte]

        def walk(node, depth=0):
            # Python
            if language == 'python':
                if node.type == 'function_definition':
                    name_node = node.child_by_field_name('name')
                    params_node = node.child_by_field_name('parameters')
                    if name_node:
                        name = extract_text(name_node)
                        params = extract_text(params_node) if params_node else "()"
                        sig = f"def {name}{params}:"
                        symbols.append(Symbol(name=name, kind='function',
                                             signature=sig, line=node.start_point[0]))

                elif node.type == 'class_definition':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = extract_text(name_node)
                        symbols.append(Symbol(name=name, kind='class',
                                             signature=f"class {name}:", line=node.start_point[0]))

                elif node.type in ('import_statement', 'import_from_statement'):
                    imports.append(extract_text(node).strip())

            # TypeScript/JavaScript
            elif language in ('typescript', 'javascript', 'tsx'):
                if node.type in ('function_declaration', 'function'):
                    name_node = node.child_by_field_name('name')
                    params_node = node.child_by_field_name('parameters')
                    if name_node:
                        name = extract_text(name_node)
                        params = extract_text(params_node) if params_node else "()"
                        symbols.append(Symbol(name=name, kind='function',
                                             signature=f"function {name}{params}", line=node.start_point[0]))

                elif node.type == 'class_declaration':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = extract_text(name_node)
                        symbols.append(Symbol(name=name, kind='class',
                                             signature=f"class {name}", line=node.start_point[0]))

                elif node.type in ('arrow_function', 'method_definition'):
                    # Get parent variable declaration for arrow functions
                    parent = node.parent
                    if parent and parent.type == 'variable_declarator':
                        name_node = parent.child_by_field_name('name')
                        if name_node:
                            name = extract_text(name_node)
                            symbols.append(Symbol(name=name, kind='function',
                                                 signature=f"const {name} = () =>", line=node.start_point[0]))

                elif node.type == 'import_statement':
                    imports.append(extract_text(node).strip())

            # Go
            elif language == 'go':
                if node.type == 'function_declaration':
                    name_node = node.child_by_field_name('name')
                    params_node = node.child_by_field_name('parameters')
                    if name_node:
                        name = extract_text(name_node)
                        params = extract_text(params_node) if params_node else "()"
                        symbols.append(Symbol(name=name, kind='function',
                                             signature=f"func {name}{params}", line=node.start_point[0]))

                elif node.type == 'type_declaration':
                    # Type declarations (structs, interfaces)
                    for child in node.children:
                        if child.type == 'type_spec':
                            name_node = child.child_by_field_name('name')
                            if name_node:
                                name = extract_text(name_node)
                                symbols.append(Symbol(name=name, kind='type',
                                                     signature=f"type {name}", line=node.start_point[0]))

            # Rust
            elif language == 'rust':
                if node.type == 'function_item':
                    name_node = node.child_by_field_name('name')
                    params_node = node.child_by_field_name('parameters')
                    if name_node:
                        name = extract_text(name_node)
                        params = extract_text(params_node) if params_node else "()"
                        symbols.append(Symbol(name=name, kind='function',
                                             signature=f"fn {name}{params}", line=node.start_point[0]))

                elif node.type in ('struct_item', 'enum_item', 'impl_item'):
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = extract_text(name_node)
                        kind = node.type.replace('_item', '')
                        symbols.append(Symbol(name=name, kind=kind,
                                             signature=f"{kind} {name}", line=node.start_point[0]))

            for child in node.children:
                walk(child, depth + 1)

        walk(tree.root_node)

        # Estimate tokens using improved estimation
        token_estimate = sum(estimate_tokens(s.signature) + 1 for s in symbols)

        return FileSymbols(
            path=str(filepath),
            language=language,
            symbols=symbols,
            imports=imports,
            token_estimate=token_estimate
        )

    def _parse_with_regex(self, filepath: Path, content: str, language: str) -> FileSymbols | None:
        """Fallback regex-based parsing when tree-sitter unavailable."""
        symbols = []
        imports = []

        lines = content.split('\n')

        # Python patterns
        if language == 'python':
            for i, line in enumerate(lines):
                if match := re.match(r'^(async\s+)?def\s+(\w+)\s*\(([^)]*)\)', line):
                    name = match.group(2)
                    params = match.group(3)
                    sig = f"def {name}({params[:PARAM_TRUNCATE_LENGTH]}{'...' if len(params) > PARAM_TRUNCATE_LENGTH else ''}):"
                    symbols.append(Symbol(name=name, kind='function', signature=sig, line=i))
                elif match := re.match(r'^class\s+(\w+)', line):
                    name = match.group(1)
                    symbols.append(Symbol(name=name, kind='class', signature=f"class {name}:", line=i))
                elif match := re.match(r'^(from\s+\S+\s+)?import\s+', line):
                    imports.append(line.strip())

        # TypeScript/JavaScript patterns
        elif language in ('typescript', 'javascript', 'tsx'):
            for i, line in enumerate(lines):
                if match := re.match(r'^\s*(export\s+)?(async\s+)?function\s+(\w+)\s*\(', line):
                    name = match.group(3)
                    symbols.append(Symbol(name=name, kind='function', signature=f"function {name}()", line=i))
                elif match := re.match(r'^\s*(export\s+)?class\s+(\w+)', line):
                    name = match.group(2)
                    symbols.append(Symbol(name=name, kind='class', signature=f"class {name}", line=i))
                elif match := re.match(r'^\s*(export\s+)?(const|let|var)\s+(\w+)\s*=\s*(async\s+)?\(?\s*(\([^)]*\))?\s*=>', line):
                    name = match.group(3)
                    symbols.append(Symbol(name=name, kind='function', signature=f"const {name} = () =>", line=i))
                elif match := re.match(r'^import\s+', line):
                    imports.append(line.strip())

        # Go patterns
        elif language == 'go':
            for i, line in enumerate(lines):
                if match := re.match(r'^func\s+(\w+)\s*\(', line):
                    name = match.group(1)
                    symbols.append(Symbol(name=name, kind='function', signature=f"func {name}()", line=i))
                elif match := re.match(r'^type\s+(\w+)\s+(struct|interface)', line):
                    name = match.group(1)
                    kind = match.group(2)
                    symbols.append(Symbol(name=name, kind=kind, signature=f"type {name} {kind}", line=i))

        token_estimate = sum(estimate_tokens(s.signature) + 1 for s in symbols)

        return FileSymbols(
            path=str(filepath),
            language=language,
            symbols=symbols,
            imports=imports,
            token_estimate=token_estimate
        )

    def _build_dependencies(self) -> None:
        """Build dependency graph from imports."""
        if self.dependency_graph is None:
            return

        for filepath, file_info in self.file_symbols.items():
            for imp in file_info.imports:
                # Try to resolve import to a file in the repo
                target = self._resolve_import(imp, filepath, file_info.language)
                if target and target in self.file_symbols:
                    self.dependency_graph.add_edge(filepath, target)

    def _resolve_import(self, import_stmt: str, source_file: str, language: str) -> str | None:
        """Resolve an import statement to a file path."""
        if language == 'python':
            # from foo.bar import baz -> foo/bar.py
            if match := re.match(r'from\s+([\w.]+)', import_stmt):
                module = match.group(1).replace('.', '/')
                candidates = [f"{module}.py", f"{module}/__init__.py"]
                for c in candidates:
                    if c in self.file_symbols:
                        return c
            elif match := re.match(r'import\s+([\w.]+)', import_stmt):
                module = match.group(1).split('.')[0]
                if f"{module}.py" in self.file_symbols:
                    return f"{module}.py"

        elif language in ('typescript', 'javascript', 'tsx'):
            # import { x } from './foo' or from 'foo'
            if match := re.search(r'from\s+[\'"]([^"\']+)[\'"]', import_stmt):
                path = match.group(1)
                if path.startswith('.'):
                    # Relative import
                    source_dir = str(Path(source_file).parent)
                    candidates = [
                        f"{source_dir}/{path}.ts",
                        f"{source_dir}/{path}.tsx",
                        f"{source_dir}/{path}/index.ts",
                        f"{source_dir}/{path}.js",
                    ]
                    for c in candidates:
                        norm = str(Path(c).as_posix())
                        if norm in self.file_symbols:
                            return norm

        return None

    def get_pagerank_scores(self) -> dict[str, float]:
        """Get PageRank scores for all files."""
        if self.dependency_graph is None or len(self.dependency_graph) == 0:
            # Fallback: equal scores
            return {f: 1.0 for f in self.file_symbols}

        try:
            scores = nx.pagerank(self.dependency_graph, alpha=PAGERANK_ALPHA)
            return scores
        except Exception:
            return {f: 1.0 for f in self.file_symbols}

    def get_map(
        self,
        query: str | None = None,
        active_files: list[str] | None = None,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> str:
        """
        Generate a repo map within the token budget.

        Args:
            query: Optional query to bias relevance
            active_files: Files currently being edited (get priority)
            token_budget: Maximum tokens for the map

        Returns:
            Formatted repo map string
        """
        if not self._indexed:
            self.index()

        if not self.file_symbols:
            return "# No source files found\n"

        # Score files
        scores = self._score_files(query, active_files)

        # Sort by score
        ranked_files = sorted(scores.items(), key=lambda x: -x[1])

        # Build map within budget
        lines = ["# Repository Map\n"]
        tokens_used = TRUNCATE_MARKER_TOKENS  # Header overhead

        for filepath, score in ranked_files:
            file_info = self.file_symbols.get(filepath)
            if not file_info or not file_info.symbols:
                continue

            # Estimate tokens for this file
            file_tokens = file_info.token_estimate + FILE_HEADER_TOKENS

            if tokens_used + file_tokens > token_budget:
                # Try to fit at least the file name
                if tokens_used + TRUNCATE_MARKER_TOKENS < token_budget:
                    lines.append(f"\n## {filepath}")
                    lines.append("  # ... (truncated)")
                break

            # Add file content
            lines.append(f"\n## {filepath}")
            for symbol in file_info.symbols:
                if symbol.kind == 'class':
                    lines.append(f"  {symbol.signature}")
                elif symbol.kind == 'function':
                    lines.append(f"    {symbol.signature}")
                elif symbol.kind in ('struct', 'type', 'enum', 'impl'):
                    lines.append(f"  {symbol.signature}")

            tokens_used += file_tokens

        lines.append(f"\n# ({tokens_used} tokens)")
        return '\n'.join(lines)

    def _score_files(
        self,
        query: str | None,
        active_files: list[str] | None
    ) -> dict[str, float]:
        """Score files by relevance."""
        scores = {}

        # Start with PageRank scores
        pagerank = self.get_pagerank_scores()

        for filepath in self.file_symbols:
            score = pagerank.get(filepath, 0.1)

            # Boost active files
            if active_files:
                for active in active_files:
                    if active in filepath or filepath in active:
                        score += ACTIVE_FILE_BOOST

            # Query matching
            if query:
                query_lower = query.lower()
                # Match file name
                if Path(filepath).stem.lower() in query_lower:
                    score += FILENAME_MATCH_BOOST
                # Match symbols
                file_info = self.file_symbols[filepath]
                for symbol in file_info.symbols:
                    if symbol.name.lower() in query_lower:
                        score += SYMBOL_NAME_MATCH_BOOST
                    # Fuzzy match (underscore/camel case)
                    name_parts = re.split(r'[_\s]', symbol.name.lower())
                    name_parts += re.findall(r'[A-Z][a-z]+', symbol.name)
                    for part in name_parts:
                        if part.lower() in query_lower:
                            score += SYMBOL_PART_MATCH_BOOST

            scores[filepath] = score

        return scores

    def get_context_for_files(
        self,
        files: list[str],
        include_related: bool = True,
        token_budget: int = DEFAULT_CONTEXT_BUDGET,
    ) -> str:
        """
        Get detailed context for specific files.

        Args:
            files: Files to get context for
            include_related: Include files that import/are imported by these
            token_budget: Maximum tokens

        Returns:
            Formatted context string
        """
        if not self._indexed:
            self.index()

        target_files = set(files)

        # Add related files
        if include_related and self.dependency_graph:
            for f in files:
                if f in self.dependency_graph:
                    # Files this imports
                    target_files.update(self.dependency_graph.successors(f))
                    # Files that import this
                    target_files.update(self.dependency_graph.predecessors(f))

        lines = []
        tokens_used = 0

        # Primary files first
        for filepath in files:
            if filepath in self.file_symbols:
                file_info = self.file_symbols[filepath]
                file_lines = self._format_file_detailed(filepath, file_info)
                file_tokens = len('\n'.join(file_lines)) // 4

                if tokens_used + file_tokens < token_budget:
                    lines.extend(file_lines)
                    tokens_used += file_tokens

        # Then related files (brief)
        remaining_budget = token_budget - tokens_used
        if include_related and remaining_budget > 100:
            lines.append("\n# Related files:")
            for filepath in target_files - set(files):
                if filepath in self.file_symbols:
                    file_info = self.file_symbols[filepath]
                    brief = f"  {filepath}: {', '.join(s.name for s in file_info.symbols[:SYMBOL_BRIEF_LIMIT])}"
                    if len(brief) // 4 < remaining_budget:
                        lines.append(brief)
                        remaining_budget -= len(brief) // 4

        return '\n'.join(lines)

    def _format_file_detailed(self, filepath: str, file_info: FileSymbols) -> list[str]:
        """Format a file with full symbol details."""
        lines = [f"\n## {filepath}"]

        if file_info.imports:
            lines.append("  # Imports:")
            for imp in file_info.imports[:IMPORT_DISPLAY_LIMIT]:
                lines.append(f"  #   {imp[:IMPORT_TRUNCATE_LENGTH]}")

        for symbol in file_info.symbols:
            lines.append(f"  {symbol.signature}")
            if symbol.docstring:
                lines.append(f"    \"\"\"{symbol.docstring[:DOCSTRING_TRUNCATE_LENGTH]}...\"\"\"")

        return lines


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for repo map testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate repository map")
    parser.add_argument("path", nargs="?", default=".", help="Repository path")
    parser.add_argument("--query", "-q", help="Query to focus the map")
    parser.add_argument("--tokens", "-t", type=int, default=DEFAULT_TOKEN_BUDGET, help="Token budget")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    mapper = RepoMapper(args.path)
    mapper.index(verbose=args.verbose)

    print(mapper.get_map(query=args.query, token_budget=args.tokens))


if __name__ == "__main__":
    main()
