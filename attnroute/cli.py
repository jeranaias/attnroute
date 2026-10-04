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
import os
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

    # ── EVERY CAPABILITY WITH ITS STATE, INCLUDING THE ABSENT ONES ─────────────────────
    #
    # This block used to append only the features that WERE importable and print the list.
    # A degraded install therefore printed a SHORTER list and read as complete: nothing said
    # that outlining had fallen back to regex, or that semantic search was not installed.
    #
    # An absence that is not stated reads as "nothing to report", which is how a disabled
    # capability goes unnoticed. So each row is printed either way, and an absent row carries
    # the reason -- "off" without a reason is not actionable.
    capabilities = []

    def _probe(label, importer):
        """(label, ok, detail) -- never raises, so one missing extra cannot hide the rest."""
        try:
            ok, detail = importer()
        except Exception as exc:          # ImportError, and anything a broken extra raises
            ok, detail = False, f"could not be probed: {exc}"
        capabilities.append((label, ok, detail))

    def _bm25():
        from attnroute.indexer import BM25_AVAILABLE
        return BM25_AVAILABLE, "bm25s" if BM25_AVAILABLE else "bm25s is not installed (extra: search)"

    def _semantic():
        from attnroute.indexer import MODEL2VEC_AVAILABLE
        return (MODEL2VEC_AVAILABLE,
                "model2vec" if MODEL2VEC_AVAILABLE else "model2vec is not installed (extra: search)")

    def _graph():
        # ⚠ graph_retriever.TREE_SITTER_AVAILABLE IS MISNAMED: it is set by importing
        #   `get_parser` from tree_sitter_languages -- the LANGUAGE PACK, not tree_sitter
        #   itself. So a reader who sees it False concludes tree-sitter is missing when
        #   tree-sitter may be installed and working. The two causes are reported apart here
        #   rather than as one sentence, because they have different remedies: networkx
        #   installs anywhere, and the language pack publishes no wheel past cp312.
        from attnroute.graph_retriever import (
            GRAPH_AVAILABLE,
            NETWORKX_AVAILABLE,
            TREE_SITTER_AVAILABLE as LANGUAGE_PACK_AVAILABLE,
        )
        if GRAPH_AVAILABLE:
            return True, "networkx + tree_sitter_languages"
        why = []
        if not NETWORKX_AVAILABLE:
            why.append("networkx is not installed (extra: graph)")
        if not LANGUAGE_PACK_AVAILABLE:
            why.append("tree_sitter_languages is not importable -- it publishes no wheel for "
                       "this Python, and it is the same package the outliner needs")
        if not why:
            why.append("both dependencies import but the repo mapper did not load")
        return False, "; ".join(why)

    def _compression():
        from attnroute.compressor import ANTHROPIC_AVAILABLE
        return (ANTHROPIC_AVAILABLE,
                "anthropic" if ANTHROPIC_AVAILABLE else "anthropic is not installed (extra: compression)")

    def _learning():
        from attnroute.learner import Learner  # noqa: F401
        return True, "learner importable"

    def _outline():
        from attnroute.outliner import OUTLINE_BACKEND_TREE_SITTER, outline_backend
        backend, reason = outline_backend()
        return backend == OUTLINE_BACKEND_TREE_SITTER, f"{backend} -- {reason}"

    _probe("BM25 search", _bm25)
    _probe("Semantic search", _semantic)
    _probe("Graph retrieval", _graph)
    _probe("Memory compression", _compression)
    _probe("Learning engine", _learning)
    _probe("Source outlining", _outline)

    print("Capabilities:")
    for label, ok, detail in capabilities:
        print(f"  {'OK      ' if ok else 'DEGRADED'}  {label}: {detail}")

    missing = [label for label, ok, _ in capabilities if not ok]
    if missing:
        print(f"  {len(missing)} of {len(capabilities)} capabilities are not at full function: "
              f"{', '.join(missing)}")

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


def cmd_savings(args):
    """Measured saving from transcript usage, plus quality and the levers' own estimates."""
    from attnroute import savings
    res = savings.analyse(project_filter=args.project)
    if args.json:
        import json as _json
        print(_json.dumps(res, default=str, indent=2))
    else:
        print(savings.render(res))


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
        print("attnroute version (unknown - import failed)")

    # Show feature availability
    print("\nFeature availability:")

    checks = [
        ("bm25s", "BM25 Search", "pip install attnroute[search]"),
        ("model2vec", "Semantic Search", "pip install attnroute[search]"),
        ("networkx", "Graph Retrieval", "pip install attnroute[graph]"),
        ("tree_sitter_languages", "AST Parsing", "pip install attnroute[graph]"),
        ("anthropic", "Memory Compression", "pip install attnroute[compression]"),
    ]

    for module, name, install_hint in checks:
        try:
            __import__(module)
            print(f"  {name}: Available")
        except ImportError:
            print(f"  {name}: Not installed ({install_hint})")


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


def cmd_validate(args):
    """Validate attnroute installation and configuration."""
    import json
    from pathlib import Path

    errors = []
    warnings = []

    print("Validating attnroute installation...")
    print()

    # Check settings.json exists and has hooks
    settings_file = Path.home() / ".claude" / "settings.json"
    if not settings_file.exists():
        errors.append(f"settings.json not found: {settings_file}")
    else:
        try:
            data = json.loads(settings_file.read_text(encoding='utf-8'))
            hooks = data.get("hooks", {})
            if not hooks:
                warnings.append("No hooks registered in settings.json")
            else:
                # Check for attnroute hooks
                attnroute_hooks = 0
                for event, groups in hooks.items():
                    for g in groups:
                        for h in g.get("hooks", []):
                            cmd = h.get("command", "") if isinstance(h, dict) else h
                            if "attnroute" in cmd:
                                attnroute_hooks += 1
                if attnroute_hooks == 0:
                    errors.append("No attnroute hooks found in settings.json")
                else:
                    print(f"  Found {attnroute_hooks} attnroute hook(s)")
        except json.JSONDecodeError:
            errors.append("settings.json is invalid JSON")

    # Check telemetry directory
    telemetry_dir = Path.home() / ".claude" / "telemetry"
    if not telemetry_dir.exists():
        warnings.append(f"Telemetry directory missing: {telemetry_dir}")
    else:
        print(f"  Telemetry directory exists: {telemetry_dir}")

    # Report results
    print()
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  x {e}")
    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  ! {w}")
    if not errors and not warnings:
        print("Installation valid")

    return 1 if errors else 0


def cmd_note(args):
    """Record something this session decided, so compaction cannot lose it."""
    from attnroute.session_state import (
        KINDS,
        add_note,
        check_promotion,
        load,
        save,
        session_from_env,
    )

    session = args.session
    if not session:
        found = session_from_env()
        if not found["session"]:
            print(f"[attnroute] {found['why']}", file=sys.stderr)
            return 1
        session = found["session"]
    state = load(session)
    if args.kind not in KINDS:
        print(f"[attnroute] kind must be one of: {', '.join(KINDS)}", file=sys.stderr)
        return 1
    text = args.text
    if not text or not text.strip():
        print("[attnroute] refusing to record an empty note", file=sys.stderr)
        return 1

    note = add_note(state, text, kind=args.kind, promoted_to=args.promoted_to,
                    source=args.source)
    save(session, state)

    promotion = check_promotion(note, args.repo or ".")
    print(f"[attnroute] recorded {note['kind']} {note['id']}: {promotion['state']} "
          f"({promotion['why']})", file=sys.stderr)
    # ⚠ An unsupported claim is reported as a FAILURE of the command, not a detail in
    #   passing. "promoted_to: docs/x.md" when docs/x.md says nothing about this note is
    #   worse than no claim at all, because every listing afterwards reads it as filed.
    return 1 if promotion["state"] == "CLAIMED" else 0


def cmd_state(args):
    """Show what would be handed to the next context window."""
    import json as _json

    from attnroute.session_state import (
        TOKEN_BUDGET,
        check_promotion,
        handback,
        load,
        session_from_env,
    )

    session = args.session
    if not session:
        found = session_from_env()
        if not found["session"]:
            print(f"[attnroute] {found['why']}", file=sys.stderr)
            return 1
        session = found["session"]
    state = load(session)
    repo = args.repo or "."

    if args.subcommand == "handback":
        built = handback(state, {}, repo=repo)
        if args.json:
            print(_json.dumps(built, indent=2))
            return 0
        print(built["text"])
        print(f"[attnroute] {built['tokens']} of {TOKEN_BUDGET} tokens, "
              f"{built['dropped']} item(s) dropped", file=sys.stderr)
        return 0

    # `show` lists EVERYTHING, which is what the handback's drop notice points people to.
    notes = state.get("notes") or []
    if not notes:
        print(f"[attnroute] no notes recorded for session {session}", file=sys.stderr)
        return 1
    counts = {}
    for note in notes:
        promotion = check_promotion(note, repo)
        counts[promotion["state"]] = counts.get(promotion["state"], 0) + 1
        print(f"{promotion['state']:<11} {note['kind']:<12} {note['at']}  {note['text']}")
        if promotion["state"] == "CLAIMED":
            print(f"            ^ {promotion['why']}")
    print("[attnroute] " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
          file=sys.stderr)
    return 0


def cmd_board(args):
    """Read or publish a row on the shared team board.

    WRITING IS A COMMAND AND NOT A HOOK, on purpose. A write pushes, and a hook may not
    touch the network: see attnroute/no_egress.py for what happened the last time one did.
    Reading is the opposite -- the SessionStart hook reads the local `origin/board` ref and
    never fetches, so a row carries its own age instead of pretending to be current.
    """
    import json as _json

    from attnroute.board import BoardError, read, write, writers

    repo = args.repo or "."
    try:
        if args.subcommand == "get":
            row = read(args.team, repo=repo)
            if args.json:
                print(_json.dumps(row, indent=2))
                return 0
            if not row["text"]:
                print(f"[attnroute] no row: {row['why']}", file=sys.stderr)
                return 1
            age = "" if row["age_hours"] is None else f" ({row['age_hours']:.1f}h old)"
            flag = "  STALE: " + row["why"] if row["stale"] else ""
            print(f"# {args.team}{age}{flag}", file=sys.stderr)
            print(row["text"], end="")
            return 0

        if args.subcommand == "list":
            names = writers(repo=repo)
            if not names:
                print("[attnroute] no board rows in this clone", file=sys.stderr)
                return 1
            for name in names:
                row = read(name.replace(".md", ""), repo=repo)
                mark = "STALE" if row["stale"] else "ok   "
                age = "?" if row["age_hours"] is None else f"{row['age_hours']:.1f}h"
                print(f"{mark}  {name:<12} {age:>8}  {len(row['text']):>6} chars")
            return 0

        if args.subcommand == "set":
            if args.file in (None, "-"):
                text = sys.stdin.read()
            else:
                text = Path(args.file).read_text(encoding="utf-8")
            if not text.strip():
                print("[attnroute] refusing to write an empty row", file=sys.stderr)
                return 1
            result = write(args.team, text, repo=repo, message=args.message)
            where = "created the board branch" if result["created_branch"] else "updated"
            print(f"[attnroute] {where}: {result['path']} at {result['commit'][:12]} "
                  f"(attempt {result['attempts']})", file=sys.stderr)
            return 0
    except BoardError as exc:
        print(f"[attnroute] board: {exc}", file=sys.stderr)
        return 1
    return 1


def cmd_warmup(args):
    """Hard warmup: analyze project for instant context routing."""
    from attnroute.warmup import analyze_warmup, apply_warmup_to_state, build_warmup_state

    project = args.project or os.getcwd()

    if args.analyze:
        analysis = analyze_warmup(project)
        print()
        print("=" * 60)
        print("HARD WARMUP ANALYSIS")
        print("=" * 60)
        print()
        for signal_name, info in analysis.get("signals", {}).items():
            print(f"  {signal_name}:")
            if "top_5" in info:
                for path, score in info["top_5"]:
                    print(f"    {score:.3f}  {path}")
            elif "paths" in info:
                for p in info["paths"][:5]:
                    print(f"    {p}")
            elif "most_imported" in info:
                for path, count in info["most_imported"]:
                    print(f"    {count:>3}x  {path}")
            print(f"    ({info.get('files', info.get('files_with_imports', 0))} files)")
            print()

        result = analysis.get("result", {})
        print(f"  Combined: {result.get('hot_count', 0)} HOT, "
              f"{result.get('warm_count', 0)} WARM, "
              f"{result.get('total_files', 0)} total "
              f"({result.get('warmup_ms', 0):.0f}ms)")
        print()
        print("  Top HOT files:")
        for path, score in result.get("hot_files", [])[:5]:
            print(f"    {score:.3f}  {path}")
        print()
        print("  Top WARM files:")
        for path, score in result.get("warm_files", [])[:5]:
            print(f"    {score:.3f}  {path}")
    elif args.apply:
        apply_warmup_to_state(project)
    else:
        # Default: show warmup state
        build_warmup_state(project, verbose=True)


def cmd_train(args):
    """Train neural file predictor from session history."""
    if args.v2:
        print("FileGRUv2 (feature-based) training is experimental.")
        print("Use --data-dir to provide external training data (GH Archive / local git repos).")
        if args.data_dir:
            from attnroute.data.gh_archive_etl import extract_from_repos_dir
            samples = extract_from_repos_dir(args.data_dir, max_commits_per_repo=500)
            print(f"Extracted {len(samples)} samples from {args.data_dir}")
        else:
            print("No --data-dir provided. Use `attnroute data extract` to gather training data first.")
        return 0

    from attnroute.neural_predictor import (
        benchmark_neural,
        save_neural_model,
        train_model,
    )

    model, vocab, metrics = train_model(
        max_sessions=args.sessions,
        epochs=args.epochs,
    )
    if model is not None:
        save_neural_model(model, vocab)
        benchmark_neural(model, vocab)
    else:
        print("Training failed — not enough data.")
        return 1


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="attnroute",
        description="Attentional context routing for Claude Code - 98-99% token reduction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  attnroute init              Initialize for current project
  attnroute status            Check configuration and features
  attnroute report            Show efficiency metrics
  attnroute diagnostic        Generate report for bug reports
  attnroute benchmark         Run performance tests
  attnroute compress stats    Show compression statistics
  attnroute graph stats       Show dependency graph info
  attnroute history           Show attention history
  attnroute plugins           Manage plugins
  attnroute ingest            Bootstrap learner from Claude Code history
  attnroute version           Show version information
  attnroute validate          Validate installation and configuration

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

    # savings command
    sav_parser = subparsers.add_parser(
        "savings", help="Measured saving from transcript usage (treated vs baseline), with CI")
    sav_parser.add_argument("--project", default=None,
                            help="Only transcripts whose project folder contains this text")
    sav_parser.add_argument("--json", action="store_true", help="Machine-readable output")

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

    # note command
    note_parser = subparsers.add_parser(
        "note", help="Record a ruling or decision so compaction cannot lose it")
    note_parser.add_argument("subcommand", nargs="?", default="add", choices=["add"],
                             help="Only `add` for now")
    note_parser.add_argument("text", nargs="?", default=None, help="The note itself")
    note_parser.add_argument("--kind", type=str, default="note",
                             help="ruling, decision, blocker, measurement, next or note")
    note_parser.add_argument("--promoted-to", dest="promoted_to", type=str, default=None,
                             help="Path where this now lives permanently (checked)")
    note_parser.add_argument("--source", type=str, default=None,
                             help="Where it came from, e.g. a PR or a message")
    note_parser.add_argument("--session", type=str, default=None,
                             help="Session id (default: $CLAUDE_SESSION_ID, else 'local')")
    note_parser.add_argument("--repo", type=str, default=None,
                             help="Repository the promotion path is relative to")

    # state command
    state_parser = subparsers.add_parser(
        "state", help="Show the session state, or the handback it would produce")
    state_parser.add_argument("subcommand", nargs="?", default="show",
                              choices=["show", "handback"])
    state_parser.add_argument("--session", type=str, default=None)
    state_parser.add_argument("--repo", type=str, default=None)
    state_parser.add_argument("--json", action="store_true")

    # board command
    board_parser = subparsers.add_parser(
        "board", help="Read or publish a row on the shared team board")
    board_parser.add_argument("subcommand", nargs="?", default="list",
                              choices=["get", "set", "list"],
                              help="Board subcommand (default: list)")
    board_parser.add_argument("--team", type=str, default=None,
                              help="Writer name, e.g. T4, or _lead")
    board_parser.add_argument("--file", type=str, default=None,
                              help="File to publish, or - for stdin (set only)")
    board_parser.add_argument("--message", type=str, default=None,
                              help="Commit message for the write")
    board_parser.add_argument("--repo", type=str, default=None,
                              help="Repository holding the board (default: cwd)")
    board_parser.add_argument("--json", action="store_true",
                              help="Print the row with its age and staleness as JSON")

    # ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Bootstrap learner from Claude Code history")
    ingest_parser.add_argument("--project", type=str, default=None,
                               help="Filter to specific project (substring match)")

    # validate command
    subparsers.add_parser("validate", help="Validate installation and configuration")

    # warmup command
    warmup_parser = subparsers.add_parser("warmup", help="Hard warmup: analyze project for instant context routing")
    warmup_parser.add_argument("--project", type=str, default=None,
                               help="Project path (default: current directory)")
    warmup_parser.add_argument("--analyze", action="store_true",
                               help="Show analysis without applying")
    warmup_parser.add_argument("--apply", action="store_true",
                               help="Apply warmup to attention state")

    # train command
    train_parser = subparsers.add_parser("train", help="Train neural file predictor from session history")
    train_parser.add_argument("--sessions", type=int, default=200,
                              help="Max sessions to train on")
    train_parser.add_argument("--epochs", type=int, default=15,
                              help="Training epochs")
    train_parser.add_argument("--v2", action="store_true",
                              help="Train feature-based v2 model (experimental)")
    train_parser.add_argument("--data-dir", type=str, default=None,
                              help="Directory with external training data (for v2)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Dispatch to command handlers
    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "report": cmd_report,
        "savings": cmd_savings,
        "benchmark": cmd_benchmark,
        "compress": cmd_compress,
        "graph": cmd_graph,
        "history": cmd_history,
        "version": cmd_version,
        "diagnostic": cmd_diagnostic,
        "plugins": cmd_plugins,
        "board": cmd_board,
        "note": cmd_note,
        "state": cmd_state,
        "ingest": cmd_ingest,
        "validate": cmd_validate,
        "warmup": cmd_warmup,
        "train": cmd_train,
    }

    handler = commands.get(args.command)
    if handler:
        try:
            result = handler(args)
            if result is not None and isinstance(result, int):
                sys.exit(result)
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
