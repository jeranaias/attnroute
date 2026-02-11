#!/usr/bin/env python3
"""
attnroute.graph_retriever - Graph-based code retrieval

Wraps RepoMapper to provide dependency graph-based context retrieval.
Uses tree-sitter and networkx for graph analysis.

Features:
- Dependency graph from imports
- PageRank-based relevance ranking
- Token-budgeted context generation

Availability depends on optional dependencies:
- tree-sitter-languages for AST parsing
- networkx for graph algorithms
"""

import sys
from pathlib import Path

# Check for required dependencies
try:
    import networkx as nx  # noqa: F401
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False

try:
    from tree_sitter_languages import get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

# Graph retrieval is only available if both dependencies are present
GRAPH_AVAILABLE = NETWORKX_AVAILABLE and TREE_SITTER_AVAILABLE

# Import RepoMapper if available
if GRAPH_AVAILABLE:
    try:
        from attnroute.repo_map import RepoMapper
    except ImportError:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from repo_map import RepoMapper
        except ImportError:
            GRAPH_AVAILABLE = False


def get_graph_context(query: str, repo_path: str = ".", token_budget: int = 1000) -> str | None:
    """
    Get graph-based context for a query.

    Args:
        query: Query string to focus the context
        repo_path: Path to repository
        token_budget: Maximum tokens to use

    Returns:
        Context string or None if graph retrieval unavailable
    """
    if not GRAPH_AVAILABLE:
        return None

    mapper = RepoMapper(repo_path)
    return mapper.get_map(query=query, token_budget=token_budget)


def get_stats(repo_path: str = ".") -> dict:
    """
    Get repository graph statistics.

    Args:
        repo_path: Path to repository

    Returns:
        Dictionary with graph stats
    """
    if not GRAPH_AVAILABLE:
        return {
            "available": False,
            "reason": "Missing dependencies (install attnroute[graph])"
        }

    mapper = RepoMapper(repo_path)

    return {
        "available": True,
        "files": len(mapper.file_symbols),
        "symbols": sum(len(f.symbols) for f in mapper.file_symbols.values()),
        "dependencies": mapper.dep_graph.number_of_edges() if hasattr(mapper, 'dep_graph') else 0,
    }


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for graph retrieval commands."""
    import argparse

    parser = argparse.ArgumentParser(
        description="attnroute graph retrieval",
        epilog="Requires: pip install attnroute[graph]"
    )
    parser.add_argument(
        "subcommand",
        choices=["stats", "build", "rank", "map"],
        help="Graph command to run"
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Query for rank/map commands"
    )
    parser.add_argument(
        "--path",
        default=".",
        help="Repository path (default: current directory)"
    )
    parser.add_argument(
        "--tokens",
        type=int,
        default=1000,
        help="Token budget for map command"
    )

    args = parser.parse_args()

    if not GRAPH_AVAILABLE:
        print("Graph retrieval not available.", file=sys.stderr)
        print("Missing dependencies:", file=sys.stderr)
        if not NETWORKX_AVAILABLE:
            print("  - networkx (for graph algorithms)", file=sys.stderr)
        if not TREE_SITTER_AVAILABLE:
            print("  - tree-sitter-languages (for AST parsing)", file=sys.stderr)
        print("\nInstall with: pip install attnroute[graph]", file=sys.stderr)
        sys.exit(1)

    # Import RepoMapper for actual operations
    from attnroute.repo_map import RepoMapper

    if args.subcommand == "stats":
        stats = get_stats(args.path)
        print("\nRepository Graph Statistics")
        print("=" * 50)
        if stats["available"]:
            print(f"Files indexed: {stats['files']}")
            print(f"Symbols extracted: {stats['symbols']}")
            print(f"Dependencies: {stats['dependencies']}")
        else:
            print(f"Error: {stats['reason']}")

    elif args.subcommand == "build":
        print(f"Building graph for {args.path}...")
        mapper = RepoMapper(args.path)
        stats = get_stats(args.path)
        print(f"Done! Indexed {stats['files']} files with {stats['symbols']} symbols")

    elif args.subcommand == "rank":
        if not args.query:
            print("Error: query required for rank command", file=sys.stderr)
            sys.exit(1)
        mapper = RepoMapper(args.path)
        # Get ranked files (top 10)
        context = mapper.get_map(query=args.query, token_budget=500)
        print("\nTop ranked files for query:", args.query)
        print("=" * 50)
        print(context)

    elif args.subcommand == "map":
        if not args.query:
            print("Error: query required for map command", file=sys.stderr)
            sys.exit(1)
        context = get_graph_context(args.query, args.path, args.tokens)
        print("\nRepository Map")
        print("=" * 50)
        print(context)


if __name__ == "__main__":
    main()
