#!/usr/bin/env python3
"""
AttnRoute Repo Map - Aider-style symbol-level context.

Instead of injecting full files (1000s of tokens), we extract and inject
just the function/class signatures (~50 tokens per file).

The map is 95%+ smaller than the source it summarises while maintaining code
understanding. That ratio is against the CONCATENATED SOURCE, which nobody ever sends;
it is not a saving on the bill. `attnroute savings` is the figure that is.

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

import os
import posixpath
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# Try to import tree-sitter
try:
    from tree_sitter_languages import get_language, get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

# ═══ networkx IS IMPORTED ON USE, NOT ON IMPORT ════════════════════════════════════════
#
# WARNING: `import networkx` COSTS 790 ms, and this module is on the UserPromptSubmit hook
#   path through context_router -- so every prompt paid for it. It is used in exactly two
#   places: one DiGraph() and one pagerank() call.
#
#   Availability is decided with `find_spec`, which looks the module up WITHOUT executing
#   it, so NETWORKX_AVAILABLE keeps meaning exactly what it meant before (the name was
#   importable) at none of the cost. A path that only asks "is PageRank available?" now
#   does no work at all.
def _spec(name: str) -> bool:
    """Is `name` importable? Answered WITHOUT executing it."""
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


NETWORKX_AVAILABLE = _spec("networkx")

nx = None


def _nx():
    """networkx, imported on first use. None when it is not installed."""
    global nx
    if nx is None and NETWORKX_AVAILABLE:
        try:
            import networkx as _module
            nx = _module
        except ImportError:                 # installed but broken: same as absent
            return None
    return nx

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
    '.dart': 'dart',
}

# Identifiers too common to be meaningful cross-file structural edges
_GENERIC_REF_NAMES = frozenset({
    'if', 'for', 'while', 'switch', 'catch', 'return', 'throw', 'new', 'typeof',
    'await', 'async', 'def', 'class', 'import', 'from', 'try', 'except', 'with',
    'len', 'str', 'int', 'float', 'bool', 'dict', 'list', 'set', 'tuple', 'sum',
    'min', 'max', 'abs', 'any', 'all', 'map', 'zip', 'iter', 'next', 'open',
    'print', 'range', 'type', 'super', 'isinstance', 'issubclass', 'getattr',
    'setattr', 'hasattr', 'format', 'join', 'split', 'strip', 'replace', 'append',
    'extend', 'update', 'items', 'keys', 'values', 'get', 'pop', 'push', 'add',
    'remove', 'clear', 'copy', 'sort', 'find', 'index', 'count', 'read', 'write',
    'close', 'send', 'recv', 'json', 'parse', 'stringify', 'then', 'finally',
    'log', 'warn', 'error', 'info', 'debug', 'trace', 'assert', 'expect', 'describe',
    'test', 'main', 'init', 'new', 'run', 'start', 'stop', 'build', 'create',
    'delete', 'save', 'load', 'render', 'use', 'call', 'apply', 'bind', 'filter',
    'reduce', 'forEach', 'slice', 'splice', 'includes', 'startsWith', 'endsWith',
    'toString', 'valueOf', 'Object', 'Array', 'String', 'Number', 'Boolean',
    'Promise', 'Error', 'Date', 'Math', 'RegExp', 'Map', 'Set', 'Path', 'Exception',
})

# Token estimation constants
CHARS_PER_TOKEN = 4                # Rough estimate: 4 characters per token
SYMBOL_OVERHEAD_TOKENS = 5         # Extra tokens per symbol for formatting

# PageRank configuration
PAGERANK_ALPHA = 0.85              # Damping factor for PageRank algorithm (0.85 is standard)

# Relevance scoring weights
ACTIVE_FILE_BOOST = 10.0           # Boost score for files currently being edited
STRUCTURAL_NEIGHBOR_BOOST = 4.0    # Boost score for 1-hop structural callers/callees of active files
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
    references: set[str] = field(default_factory=set)
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
        self.dependency_graph = _nx().DiGraph() if NETWORKX_AVAILABLE else None
        # Directed structural adjacency: caller/importer -> callee/imported (works without networkx)
        self.structural_edges: dict[str, set[str]] = {}
        self.reverse_structural_edges: dict[str, set[str]] = {}
        self._indexed = False

    #: Directories never worth descending into. Pruned BEFORE the walk enters them.
    SKIP_DIRS = frozenset({
        'node_modules', '.git', '__pycache__', 'venv', '.venv', '.env', 'dist', 'build',
        '.next', 'target', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox',
        'eggs', '.eggs', 'site-packages',
    })

    def _walk_source_files(self):
        """Yield (path, language) for every source file, in ONE pruned pass.

        The extension becomes a dict lookup, so a new language costs nothing. It used to
        cost a full recursive walk of the repository.
        """
        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for name in filenames:
                lang = LANGUAGE_MAP.get(os.path.splitext(name)[1])
                if lang is not None:
                    yield Path(dirpath) / name, lang

    def index(self, verbose: bool = False) -> None:
        """Index all source files in the repository."""
        if verbose:
            print(f"Indexing {self.repo_path}...")

        files_indexed = 0

        # ═══ ONE WALK, PRUNED AS IT GOES ═══════════════════════════════════════════════
        #
        # WARNING: THIS USED TO BE FOURTEEN FULL RECURSIVE WALKS -- one `rglob(f"*{ext}")`
        #   per entry in LANGUAGE_MAP -- and the skip list was applied to the RESULT, so
        #   `node_modules`, `.git` and `target` were descended into fourteen times and
        #   discarded fourteen times. Measured from the UserPromptSubmit hook: 10,908
        #   `os.scandir` calls from this one method, 3.1 s of a 9 s event.
        #
        #   `os.walk` with `dirnames[:]` assignment prunes BEFORE descending, which is the
        #   point: a skipped directory is never read at all.
        #
        # WARNING: `max_files` ALSO MEANT SOMETHING DIFFERENT FROM WHAT IT SAID. The old
        #   `break` left the OUTER loop running, so the cap applied per extension and the
        #   real ceiling was up to fourteen times the stated one. It now stops the walk.
        for filepath, lang in self._walk_source_files():
            if files_indexed >= self.max_files:
                break

            try:
                symbols = self._parse_file(filepath, lang)
                if symbols:
                    rel_path = filepath.relative_to(self.repo_path).as_posix()
                    self.file_symbols[rel_path] = symbols
                    self.structural_edges.setdefault(rel_path, set())
                    self.reverse_structural_edges.setdefault(rel_path, set())
                    files_indexed += 1

                    # Add to dependency graph
                    if self.dependency_graph is not None:
                        self.dependency_graph.add_node(rel_path)
            except Exception as e:
                if verbose:
                    print(f"  Error parsing {filepath}: {e}")

        # Build dependency edges (imports) and structural caller/callee/middleware edges
        self._build_dependencies()
        self._build_structural_edges()

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
            result = self._parse_with_tree_sitter(filepath, content, language)
        else:
            result = self._parse_with_regex(filepath, content, language)

        if result is not None:
            result.references = self._extract_references(content, language)
        return result

    @staticmethod
    def _extract_references(content: str, language: str) -> set[str]:
        """Extract cross-file call, middleware/route hook, and inheritance references.

        Runs on the already-loaded file text with zero extra disk I/O.
        """
        refs: set[str] = set()

        # Direct function/constructor calls: foo( or .foo(
        for m in re.finditer(r'(?:\b|\.)([A-Za-z_][A-Za-z0-9_]{2,})\s*\(', content):
            name = m.group(1)
            if name not in _GENERIC_REF_NAMES:
                refs.add(name)

        # Middleware / route handler / dependency injection references (passed as value, not called):
        # e.g. app.use(requireAuth), router.get("/x", requireRole, handler),
        # preHandler: [verifyJwt, requireAdmin], Depends(get_current_user)
        for m in re.finditer(
            r'\b(?:preHandler|onRequest|beforeHandler|middleware|middlewares|guards|hooks)\s*[:=]\s*\[([^\]]+)\]',
            content,
        ):
            for ident in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{2,})\b', m.group(1)):
                if ident not in _GENERIC_REF_NAMES:
                    refs.add(ident)

        for m in re.finditer(
            r'\.(?:use|get|post|put|patch|delete|all|route|addHook|before)\s*\(([^)\n]{1,240})\)',
            content,
        ):
            for ident in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{2,})\b', m.group(1)):
                if ident not in _GENERIC_REF_NAMES:
                    refs.add(ident)

        # Class inheritance / mixins / interfaces
        for m in re.finditer(r'\b(?:extends|implements|with)\s+([A-Za-z_][A-Za-z0-9_]{2,})', content):
            name = m.group(1)
            if name not in _GENERIC_REF_NAMES:
                refs.add(name)

        # Python class bases: class Foo(Bar, Baz):
        if language == 'python':
            for m in re.finditer(r'^\s*class\s+\w+\s*\(([^)]+)\)\s*:', content, re.MULTILINE):
                for ident in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{2,})\b', m.group(1)):
                    if ident not in _GENERIC_REF_NAMES:
                        refs.add(ident)

        return refs

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

                elif node.type in ('import_statement', 'export_statement'):
                    text = extract_text(node).strip()
                    if node.type == 'import_statement' or ' from ' in text:
                        imports.append(text)

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

                elif node.type == 'import_spec':
                    imports.append(extract_text(node).strip())

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
                elif match := re.match(r'^(?:import|export\s+.*from)\s+', line):
                    imports.append(line.strip())

        # Go patterns
        elif language == 'go':
            in_import_block = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if in_import_block:
                    if stripped.startswith(')'):
                        in_import_block = False
                    elif '"' in stripped:
                        imports.append(stripped)
                    continue
                if re.match(r'^import\s*\(', stripped):
                    in_import_block = True
                elif stripped.startswith('import ') and '"' in stripped:
                    imports.append(stripped)
                elif match := re.match(r'^func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(', line):
                    name = match.group(1)
                    symbols.append(Symbol(name=name, kind='function', signature=f"func {name}()", line=i))
                elif match := re.match(r'^type\s+(\w+)\s+(struct|interface)', line):
                    name = match.group(1)
                    kind = match.group(2)
                    symbols.append(Symbol(name=name, kind=kind, signature=f"type {name} {kind}", line=i))

        # Dart / Flutter patterns
        elif language == 'dart':
            for i, line in enumerate(lines):
                if match := re.match(r'^\s*(?:abstract\s+|base\s+|final\s+|sealed\s+)?(?:class|mixin|extension)\s+(\w+)', line):
                    name = match.group(1)
                    symbols.append(Symbol(name=name, kind='class', signature=f"class {name}", line=i))
                elif match := re.match(r'^\s*(?:Future<[^>]+>|Stream<[^>]+>|void|[A-Z][\w<>?]*|bool|int|double|String)\s+(\w+)\s*\(', line):
                    name = match.group(1)
                    if name not in _GENERIC_REF_NAMES:
                        symbols.append(Symbol(name=name, kind='function', signature=f"{name}()", line=i))
                elif re.match(r'^\s*(?:import|export|part)\s+[\'"]', line):
                    imports.append(line.strip())

        token_estimate = sum(estimate_tokens(s.signature) + 1 for s in symbols)

        return FileSymbols(
            path=str(filepath),
            language=language,
            symbols=symbols,
            imports=imports,
            token_estimate=token_estimate
        )

    def _add_structural_edge(self, source: str, target: str) -> None:
        """Register a directed structural edge source -> target (caller/importer -> callee/imported)."""
        if not source or not target or source == target:
            return
        if target not in self.file_symbols or source not in self.file_symbols:
            return
        self.structural_edges.setdefault(source, set()).add(target)
        self.reverse_structural_edges.setdefault(target, set()).add(source)
        if self.dependency_graph is not None:
            self.dependency_graph.add_edge(source, target)

    def _build_dependencies(self) -> None:
        """Build dependency graph and structural adjacency from imports."""
        for filepath, file_info in self.file_symbols.items():
            for imp in file_info.imports:
                targets = self._resolve_import_targets(imp, filepath, file_info.language)
                for target in targets:
                    self._add_structural_edge(filepath, target)

    def _build_structural_edges(self) -> None:
        """Index 1-hop caller/callee, middleware/route hook, and inheritance edges.

        Links files that call, register as middleware, or subclass symbols defined in
        other repository files. Ambiguous symbols defined in >4 files are skipped to
        avoid noisy false-positive hubs.
        """
        symbol_defs: dict[str, set[str]] = {}
        for filepath, file_info in self.file_symbols.items():
            for sym in file_info.symbols:
                if len(sym.name) >= 3 and sym.name not in _GENERIC_REF_NAMES:
                    symbol_defs.setdefault(sym.name, set()).add(filepath)

        # Drop overly generic symbol names shared by too many files
        unambiguous = {
            name: definers
            for name, definers in symbol_defs.items()
            if len(definers) <= 4
        }

        for filepath, file_info in self.file_symbols.items():
            if not file_info.references:
                continue
            for ref in file_info.references:
                definers = unambiguous.get(ref)
                if not definers:
                    continue
                for target in definers:
                    if target != filepath:
                        self._add_structural_edge(filepath, target)

    def _resolve_import(self, import_stmt: str, source_file: str, language: str) -> str | None:
        """Resolve an import statement to a single file path (legacy single-target API)."""
        targets = self._resolve_import_targets(import_stmt, source_file, language)
        return targets[0] if targets else None

    def _resolve_import_targets(self, import_stmt: str, source_file: str, language: str) -> list[str]:
        """Resolve an import statement to one or more indexed POSIX file paths."""
        source_posix = source_file.replace('\\', '/')
        source_dir = posixpath.dirname(source_posix)

        if language == 'python':
            # Relative imports: from .foo import bar | from ..lib.auth import x | from . import utils
            if match := re.match(r'from\s+(\.+)([\w.]*)\s+import\s+(.+)', import_stmt):
                dots = len(match.group(1))
                rel_mod = match.group(2)
                imported_names = [
                    part.strip().split(' as ')[0].strip()
                    for part in match.group(3).split(',')
                ]
                base_dir = source_dir
                for _ in range(dots - 1):
                    base_dir = posixpath.dirname(base_dir)
                candidates: list[str] = []
                if rel_mod:
                    mod_path = posixpath.normpath(
                        posixpath.join(base_dir, rel_mod.replace('.', '/'))
                    ).lstrip('./')
                    candidates.extend([f"{mod_path}.py", f"{mod_path}/__init__.py"])
                else:
                    for name in imported_names:
                        if name and name != '*':
                            mod_path = posixpath.normpath(
                                posixpath.join(base_dir, name)
                            ).lstrip('./')
                            candidates.extend([f"{mod_path}.py", f"{mod_path}/__init__.py"])
                hits = [c for c in candidates if c in self.file_symbols]
                if hits:
                    return list(dict.fromkeys(hits))
                return []

            # Absolute from-imports: from foo.bar import baz
            if match := re.match(r'from\s+([\w.]+)', import_stmt):
                module = match.group(1).replace('.', '/')
                for c in (f"{module}.py", f"{module}/__init__.py"):
                    if c in self.file_symbols:
                        return [c]
            elif match := re.match(r'import\s+([\w.]+)', import_stmt):
                mod_full = match.group(1).replace('.', '/')
                mod_top = match.group(1).split('.')[0]
                for c in (f"{mod_full}.py", f"{mod_full}/__init__.py", f"{mod_top}.py", f"{mod_top}/__init__.py"):
                    if c in self.file_symbols:
                        return [c]

        elif language in ('typescript', 'javascript', 'tsx'):
            # import { x } from '../lib/auth.js' | import './setup' | export * from './foo.js'
            match = re.search(r'(?:from|import)\s+[\'"]([^"\']+)[\'"]', import_stmt)
            if match:
                spec = match.group(1)
                if spec.startswith('.'):
                    joined = posixpath.normpath(posixpath.join(source_dir, spec))
                    # Strip ESM .js/.mjs/.cjs extension so TS sources (.ts/.tsx) resolve cleanly
                    stem, ext = posixpath.splitext(joined)
                    bases = [joined]
                    if ext in ('.js', '.mjs', '.cjs', '.jsx'):
                        bases.insert(0, stem)
                    candidates = []
                    for base in bases:
                        candidates.extend([
                            base,
                            f"{base}.ts",
                            f"{base}.tsx",
                            f"{base}.js",
                            f"{base}.jsx",
                            f"{base}/index.ts",
                            f"{base}/index.tsx",
                            f"{base}/index.js",
                        ])
                    for c in candidates:
                        norm = c.lstrip('./')
                        if norm in self.file_symbols:
                            return [norm]

        elif language == 'go':
            # Go imports are package paths: "github.com/org/repo/internal/auth"
            if match := re.search(r'"([^"]+)"', import_stmt):
                pkg_path = match.group(1).strip('/')
                parts = pkg_path.split('/')
                # Match longest directory suffix against indexed Go files
                for start in range(len(parts)):
                    suffix = '/'.join(parts[start:])
                    matches = [
                        f for f, info in self.file_symbols.items()
                        if info.language == 'go' and posixpath.dirname(f) == suffix
                    ]
                    if matches:
                        return sorted(matches)[:5]

        elif language == 'dart':
            if match := re.search(r'[\'"]([^"\']+)[\'"]', import_stmt):
                spec = match.group(1)
                if spec.startswith('package:'):
                    # package:my_app/services/auth.dart -> lib/services/auth.dart (or apps/*/lib/...)
                    _, _, rest = spec.partition('/')
                    if rest:
                        direct = f"lib/{rest}"
                        if direct in self.file_symbols:
                            return [direct]
                        suffix = f"/lib/{rest}"
                        for f in self.file_symbols:
                            if f.endswith(suffix):
                                return [f]
                elif not spec.startswith('dart:'):
                    joined = posixpath.normpath(posixpath.join(source_dir, spec)).lstrip('./')
                    if joined in self.file_symbols:
                        return [joined]

        return []

    def _normalize_indexed_path(self, filepath: str) -> str | None:
        """Resolve a user/caller-supplied file path to a key in self.file_symbols."""
        norm = filepath.replace('\\', '/').lstrip('./')
        if norm in self.file_symbols:
            return norm
        # Suffix match (e.g. caller passed absolute path or short relative path)
        for key in self.file_symbols:
            if norm.endswith('/' + key) or key.endswith('/' + norm):
                return key
        return None

    def get_structural_neighbors(
        self,
        filepath: str,
        *,
        hops: int = 1,
        include_callers: bool = True,
        include_callees: bool = True,
        max_neighbors: int = 15,
    ) -> list[str]:
        """Return 1-hop (or k-hop) structural neighbors: callers, callees, imports, middleware.

        Uses the stdlib adjacency maps built during `index()`, so this works even when
        `networkx` is not installed and pays zero import cost.
        """
        if not self._indexed:
            self.index()

        root = self._normalize_indexed_path(filepath)
        if root is None or hops < 1 or max_neighbors <= 0:
            return []

        visited: set[str] = {root}
        neighbors: list[str] = []
        queue: deque[tuple[str, int]] = deque([(root, 0)])

        while queue and len(neighbors) < max_neighbors:
            current, depth = queue.popleft()
            if depth >= hops:
                continue

            next_nodes: set[str] = set()
            if include_callees:
                next_nodes.update(self.structural_edges.get(current, ()))
            if include_callers:
                next_nodes.update(self.reverse_structural_edges.get(current, ()))

            for nb in sorted(next_nodes):
                if nb not in visited:
                    visited.add(nb)
                    neighbors.append(nb)
                    if len(neighbors) >= max_neighbors:
                        break
                    queue.append((nb, depth + 1))

        return neighbors

    def get_pagerank_scores(self) -> dict[str, float]:
        """Get PageRank scores for all files."""
        if self.dependency_graph is None or len(self.dependency_graph) == 0:
            # Fallback: equal scores
            return {f: 1.0 for f in self.file_symbols}

        try:
            scores = _nx().pagerank(self.dependency_graph, alpha=PAGERANK_ALPHA)
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

        # Precompute 1-hop structural neighbors (callers, callees, imports, middleware)
        # of active files so editing a file surfaces its structural boundary.
        structural_neighbors: set[str] = set()
        active_norms: list[str] = []
        if active_files:
            for active in active_files:
                norm_active = active.replace('\\', '/').lstrip('./')
                active_norms.append(norm_active)
                for nb in self.get_structural_neighbors(norm_active, hops=1):
                    structural_neighbors.add(nb)

        for filepath in self.file_symbols:
            score = pagerank.get(filepath, 0.1)

            # Boost active files and their 1-hop structural neighbors
            is_active = False
            if active_norms:
                for active in active_norms:
                    if active in filepath or filepath in active:
                        score += ACTIVE_FILE_BOOST
                        is_active = True
            if not is_active and filepath in structural_neighbors:
                score += STRUCTURAL_NEIGHBOR_BOOST

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
            include_related: Include 1-hop structural callers, callees, imports, and middleware
            token_budget: Maximum tokens

        Returns:
            Formatted context string
        """
        if not self._indexed:
            self.index()

        resolved_primary: list[str] = []
        for f in files:
            norm = self._normalize_indexed_path(f)
            if norm and norm not in resolved_primary:
                resolved_primary.append(norm)

        primary_set = set(resolved_primary)
        related_files: list[str] = []

        # Add 1-hop structural neighbors (works with or without networkx)
        if include_related:
            seen_related: set[str] = set(primary_set)
            for f in resolved_primary:
                for nb in self.get_structural_neighbors(f, hops=1):
                    if nb not in seen_related:
                        seen_related.add(nb)
                        related_files.append(nb)

        lines = []
        tokens_used = 0

        # Primary files first
        for filepath in resolved_primary:
            file_info = self.file_symbols[filepath]
            file_lines = self._format_file_detailed(filepath, file_info)
            file_tokens = len('\n'.join(file_lines)) // 4

            if tokens_used + file_tokens < token_budget:
                lines.extend(file_lines)
                tokens_used += file_tokens

        # Then related files (brief)
        remaining_budget = token_budget - tokens_used
        if include_related and related_files and remaining_budget > 100:
            lines.append("\n# Related files:")
            for filepath in related_files:
                file_info = self.file_symbols.get(filepath)
                if file_info is not None:
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
