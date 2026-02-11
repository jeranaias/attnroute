#!/usr/bin/env python3
"""
attnroute CLI - Unified command-line interface

Usage:
    attnroute init [--global]     Initialize attnroute for current project
    attnroute status              Show current status and configuration
    attnroute report [--days N]   Show efficiency report
    attnroute diagnostic [path]   Generate diagnostic report for bug reports
    attnroute benchmark           Run performance benchmarks
    attnroute compress stats      Show compression statistics
    attnroute graph stats         Show dependency graph statistics
    attnroute history [--last N]  Show attention history
    attnroute version             Show version information
"""

import argparse
import sys
from pathlib import Path


def cmd_init(args):
    """Initialize attnroute for current project."""
    from attnroute.installer import main as installer_main
    installer_main()


def cmd_status(args):
    """Show current status and configuration."""

    print("attnroute Status")
    print("=" * 50)

    # Check available features
    features = []

    try:
        from attnroute.indexer import BM25_AVAILABLE, MODEL2VEC_AVAILABLE
        if BM25_AVAILABLE:
            features.append("BM25 search")
        if MODEL2VEC_AVAILABLE:
            features.append("Semantic search")
    except ImportError:
        pass

    try:
        from attnroute.graph_retriever import GRAPH_AVAILABLE
        if GRAPH_AVAILABLE:
            features.append("Graph retrieval")
    except ImportError:
        pass

    try:
        from attnroute.compressor import ANTHROPIC_AVAILABLE
        if ANTHROPIC_AVAILABLE:
            features.append("Memory compression")
    except ImportError:
        pass

    try:
        from attnroute.learner import Learner  # noqa: F401
        features.append("Learning engine")
    except ImportError:
        pass

    print(f"Features: {', '.join(features) if features else 'Core only'}")

    # Check for keywords.json
    keywords_paths = [
        Path(".claude/keywords.json"),
        Path.home() / ".claude" / "keywords.json",
    ]

    keywords_found = None
    for kp in keywords_paths:
        if kp.exists():
            keywords_found = kp
            break

    if keywords_found:
        print(f"Keywords: {keywords_found}")
        import json
        try:
            data = json.loads(keywords_found.read_text(encoding='utf-8'))
            num_files = len(data.get("keywords", {}))
            num_pinned = len(data.get("pinned", []))
            print(f"  Files: {num_files}, Pinned: {num_pinned}")
        except Exception:
            pass
    else:
        print("Keywords: Not found (run 'attnroute init' to create)")

    # Check telemetry
    telemetry_dir = Path.home() / ".claude" / "telemetry"
    if telemetry_dir.exists():
        turns_file = telemetry_dir / "turns.jsonl"
        if turns_file.exists():
            content = turns_file.read_text(encoding="utf-8", errors="replace").strip()
            lines = [line for line in content.split("\n") if line] if content else []
            print(f"Telemetry: {len(lines)} turns recorded")
        else:
            print("Telemetry: No turns recorded yet")
    else:
        print("Telemetry: Not initialized")


def cmd_report(args):
    """Show efficiency report."""
    from attnroute.telemetry_report import main as report_main
    # Pass days as sys.argv
    original_argv = sys.argv
    sys.argv = ["attnroute-report"]
    if args.days:
        sys.argv.extend(["--days", str(args.days)])
    try:
        report_main()
    finally:
        sys.argv = original_argv


def cmd_benchmark(args):
    """Run performance benchmarks."""
    try:
        from benchmarks.runner import main as benchmark_main
        benchmark_main(
            scenario=args.scenario,
            output_file=args.output
        )
    except ImportError as e:
        print(f"Benchmark module not available: {e}")
        sys.exit(1)


def cmd_compress(args):
    """Memory compression commands."""
    try:
        from attnroute.compressor import main as compressor_main
    except ImportError as e:
        print(f"Memory compression not available: {e}", file=sys.stderr)
        print("Install compression dependencies: pip install attnroute[compression]", file=sys.stderr)
        sys.exit(1)

    original_argv = sys.argv
    sys.argv = ["attnroute-compress", args.subcommand]
    if hasattr(args, 'query') and args.query:
        sys.argv.append(args.query)
    try:
        compressor_main()
    finally:
        sys.argv = original_argv


def cmd_graph(args):
    """Dependency graph commands."""
    try:
        from attnroute.graph_retriever import main as graph_main
    except ImportError as e:
        print(f"Graph retriever not available: {e}", file=sys.stderr)
        print("Install graph dependencies: pip install attnroute[graph]", file=sys.stderr)
        sys.exit(1)

    original_argv = sys.argv
    sys.argv = ["attnroute-graph", args.subcommand]
    if hasattr(args, 'query') and args.query:
        sys.argv.append(args.query)
    if hasattr(args, 'path') and args.path:
        sys.argv.extend(["--path", args.path])
    if hasattr(args, 'tokens') and args.tokens:
        sys.argv.extend(["--tokens", str(args.tokens)])
    try:
        graph_main()
    finally:
        sys.argv = original_argv


def cmd_history(args):
    """Show attention history."""
    from attnroute.history import main as history_main
    original_argv = sys.argv
    sys.argv = ["attnroute-history"]
    if args.last:
        sys.argv.extend(["--last", str(args.last)])
    try:
        history_main()
    finally:
        sys.argv = original_argv


def cmd_diagnostic(args):
    """Generate a diagnostic report for bug reports."""
    import json
    from pathlib import Path

    from attnroute.diagnostic import format_report_text, generate_report

    repo_path = Path(args.path).resolve() if args.path else Path.cwd()

    print(f"Generating diagnostic report for: {repo_path.name}")
    print("Collecting system info...")

    report = generate_report(repo_path, run_bench=not args.no_benchmark)

    if args.json:
        output = json.dumps(report, indent=2)
        default_filename = "attnroute_diagnostic.json"
    else:
        output = format_report_text(report)
        default_filename = "attnroute_diagnostic.txt"

    if args.print_only:
        print()
        print(output)
    else:
        output_path = Path(args.output) if args.output else Path(default_filename)
        output_path.write_text(output, encoding="utf-8")
        print(f"\nReport saved to: {output_path}")
        print("Include this file when reporting issues at:")
        print("https://github.com/jeranaias/attnroute/issues")


def cmd_version(args):
    """Show version information."""
    try:
        from attnroute import __version__
        print(f"attnroute version {__version__}")
    except ImportError:
        print("attnroute version 0.5.0")

    # Show feature availability
    print("\nFeature availability:")

    checks = [
        ("bm25s", "BM25 Search"),
        ("model2vec", "Semantic Search"),
        ("networkx", "Graph Retrieval"),
        ("tree_sitter_languages", "AST Parsing"),
        ("anthropic", "Memory Compression"),
    ]

    for module, name in checks:
        try:
            __import__(module)
            print(f"  {name}: Available")
        except ImportError:
            print(f"  {name}: Not installed")


def cmd_plugins(args):
    """Manage plugins."""
    try:
        from attnroute.plugins import (
            disable_plugin,
            discover_plugins,
            enable_plugin,
            get_plugin,
            get_plugins,
        )
    except ImportError:
        print("Error: Plugin system not available. Ensure attnroute is installed correctly.")
        return

    discover_plugins()

    if args.subcommand == "list":
        plugins = get_plugins()
        print("Installed plugins:")
        for p in plugins:
            status = "enabled" if p.is_enabled() else "disabled"
            print(f"  {p.name} v{p.version} - {p.description} [{status}]")
        if not plugins:
            print("  (none)")
    elif args.subcommand == "enable":
        if not hasattr(args, 'name') or not args.name:
            print("Error: Plugin name required. Usage: attnroute plugins enable <name>")
            return
        enable_plugin(args.name)
        print(f"Enabled: {args.name}")
    elif args.subcommand == "disable":
        if not hasattr(args, 'name') or not args.name:
            print("Error: Plugin name required. Usage: attnroute plugins disable <name>")
            return
        disable_plugin(args.name)
        print(f"Disabled: {args.name}")
    elif args.subcommand == "status":
        if not hasattr(args, 'name') or not args.name:
            print("Error: Plugin name required. Usage: attnroute plugins status <name>")
            return
        plugin = get_plugin(args.name)
        if plugin and hasattr(plugin, 'get_session_summary'):
            summary = plugin.get_session_summary()
            print(f"{plugin.name} status:")
            for k, v in summary.items():
                print(f"  {k}: {v}")
        else:
            print(f"Plugin not found or no status available: {args.name}")
    else:
        print("Usage: attnroute plugins [list|enable|disable|status] [name]")


def cmd_ingest(args):
    """Bootstrap learner from Claude Code conversation history."""
    from attnroute.ingest import ingest_transcripts
    from attnroute.learner import Learner

    print("Ingesting Claude Code transcripts...")
    state = ingest_transcripts(project_filter=getattr(args, 'project', None))

    if state["meta"]["turns_learned"] == 0:
        print("No transcript data found in ~/.claude/projects/")
        return

    learner = Learner()
    learner.merge_ingested_state(state)

    turns = state["meta"]["turns_learned"]
    files = len(state.get("coactivation_learned", {}))
    affinities = len(state.get("prompt_file_affinity", {}))
    print(f"Ingested {turns} turns across {files} files")
    print(f"  Co-activation patterns: {files}")
    print(f"  Prompt-file associations: {affinities} keywords")
    print(f"  Learner maturity: {learner.maturity}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="attnroute",
        description="Attentional context routing for Claude Code - 98-99% token reduction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  attnroute init              Initialize for current project
  attnroute status            Check configuration and features
  attnroute report            Show efficiency metrics
  attnroute diagnostic        Generate report for bug reports
  attnroute benchmark         Run performance tests
  attnroute compress stats    Show compression statistics
  attnroute graph stats       Show dependency graph info

For more information, visit: https://github.com/jeranaias/attnroute
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize attnroute for current project")
    init_parser.add_argument("--global", dest="global_install", action="store_true",
                             help="Install globally instead of project-local")

    # status command
    subparsers.add_parser("status", help="Show current status and configuration")

    # report command
    report_parser = subparsers.add_parser("report", help="Show efficiency report")
    report_parser.add_argument("--days", type=int, default=7, help="Number of days to analyze")

    # benchmark command
    bench_parser = subparsers.add_parser("benchmark", help="Run performance benchmarks")
    bench_parser.add_argument("--scenario", choices=["all", "quick", "single_file", "multi_file"],
                              default="quick", help="Benchmark scenario to run")
    bench_parser.add_argument("--output", type=str, help="Output file for results")

    # compress command
    compress_parser = subparsers.add_parser("compress", help="Memory compression utilities")
    compress_parser.add_argument("subcommand", choices=["stats", "search", "recent", "test"],
                                 help="Compression subcommand")
    compress_parser.add_argument("query", nargs="?", help="Search query (for search command) or text (for test command)")

    # graph command
    graph_parser = subparsers.add_parser("graph", help="Dependency graph utilities")
    graph_parser.add_argument("subcommand", choices=["stats", "build", "rank", "map"],
                              help="Graph subcommand")
    graph_parser.add_argument("query", nargs="?", help="Query for rank/map commands")
    graph_parser.add_argument("--path", type=str, help="Repository path (default: current directory)")
    graph_parser.add_argument("--tokens", type=int, help="Token budget for map command (default: 2000)")

    # history command
    history_parser = subparsers.add_parser("history", help="Show attention history")
    history_parser.add_argument("--last", type=int, default=20, help="Number of entries to show")

    # version command
    subparsers.add_parser("version", help="Show version information")

    # diagnostic command
    diag_parser = subparsers.add_parser("diagnostic", help="Generate diagnostic report for bug reports")
    diag_parser.add_argument("path", nargs="?", default=None,
                             help="Repository path to analyze (default: current directory)")
    diag_parser.add_argument("--output", "-o", type=str,
                             help="Output file path")
    diag_parser.add_argument("--json", action="store_true",
                             help="Output as JSON instead of text")
    diag_parser.add_argument("--print", "-p", dest="print_only", action="store_true",
                             help="Print to stdout instead of file")
    diag_parser.add_argument("--no-benchmark", action="store_true",
                             help="Skip running the benchmark")

    # plugins command
    plugins_parser = subparsers.add_parser("plugins", help="Manage plugins")
    plugins_parser.add_argument("subcommand", nargs="?", default="list",
                                choices=["list", "enable", "disable", "status"],
                                help="Plugin subcommand (default: list)")
    plugins_parser.add_argument("name", nargs="?", help="Plugin name (for enable/disable/status)")

    # ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Bootstrap learner from Claude Code history")
    ingest_parser.add_argument("--project", type=str, default=None,
                               help="Filter to specific project (substring match)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Dispatch to command handlers
    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "report": cmd_report,
        "benchmark": cmd_benchmark,
        "compress": cmd_compress,
        "graph": cmd_graph,
        "history": cmd_history,
        "version": cmd_version,
        "diagnostic": cmd_diagnostic,
        "plugins": cmd_plugins,
        "ingest": cmd_ingest,
    }

    handler = commands.get(args.command)
    if handler:
        try:
            handler(args)
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(1)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
