#!/usr/bin/env python3
"""
attnroute.diagnostic - Generate shareable diagnostic reports

Creates a comprehensive report file for bug reports and performance issues.
Output can be shared publicly to help debug issues.

Usage:
    attnroute diagnostic                    # Generate report for current directory
    attnroute diagnostic /path/to/repo      # Generate report for specific repo
    attnroute diagnostic --output report.json  # Custom output path
"""

import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from attnroute import __version__
except ImportError:
    __version__ = "unknown"


def get_system_info() -> dict:
    """Collect non-sensitive system information."""
    return {
        "platform": platform.system(),
        "platform_version": platform.version(),
        "python_version": platform.python_version(),
        "architecture": platform.machine(),
        "attnroute_version": __version__,
    }


def get_dependency_info() -> dict:
    """Check which optional dependencies are installed."""
    deps = {}

    checks = [
        ("tiktoken", "tiktoken"),
        ("bm25s", "bm25s"),
        ("model2vec", "model2vec"),
        ("networkx", "networkx"),
        ("tree_sitter_languages", "tree_sitter_languages"),
        ("anthropic", "anthropic"),
        ("chromadb", "chromadb"),
    ]

    for name, module in checks:
        try:
            mod = __import__(module)
            version = getattr(mod, "__version__", "installed")
            deps[name] = {"installed": True, "version": str(version)}
        except ImportError:
            deps[name] = {"installed": False}

    return deps


def get_repo_info(repo_path: Path) -> dict:
    """Get repository information without exposing file contents."""
    info = {
        "path_anonymized": str(repo_path.name),  # Only show folder name
        "exists": repo_path.exists(),
        "is_git": False,
        "file_count": 0,
        "file_types": {},
    }

    if not repo_path.exists():
        return info

    # Check if git repo
    info["is_git"] = (repo_path / ".git").exists()

    # Count files by extension
    file_types = {}
    file_count = 0

    try:
        for f in repo_path.rglob("*"):
            if f.is_file():
                # Skip hidden and build directories
                parts = f.relative_to(repo_path).parts
                if any(p.startswith(".") or p in {"node_modules", "__pycache__", "venv", "dist", "build"} for p in parts):
                    continue
                file_count += 1
                ext = f.suffix.lower() or "(no extension)"
                file_types[ext] = file_types.get(ext, 0) + 1
    except Exception:
        pass

    info["file_count"] = file_count
    # Top 10 file types
    info["file_types"] = dict(sorted(file_types.items(), key=lambda x: -x[1])[:10])

    return info


def run_benchmark(repo_path: Path) -> dict:
    """Run a quick benchmark on the repository."""
    result = {
        "ran": False,
        "error": None,
    }

    try:
        # Try to import repo_map
        from attnroute.repo_map import RepoMapper
    except ImportError:
        result["error"] = "RepoMapper not available (install attnroute[graph])"
        return result

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        has_tiktoken = True
    except ImportError:
        has_tiktoken = False

    try:
        mapper = RepoMapper(str(repo_path))

        # Time the repo map generation (index + get_map)
        start = time.perf_counter()
        mapper.index()
        repo_map = mapper.get_map(token_budget=2000)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Calculate tokens
        if has_tiktoken:
            output_tokens = len(enc.encode(repo_map))
        else:
            output_tokens = len(repo_map) // 4  # Rough estimate

        result["ran"] = True
        result["latency_ms"] = round(elapsed_ms, 1)
        result["output_tokens"] = output_tokens
        result["output_lines"] = len(repo_map.strip().split("\n"))
        result["files_indexed"] = len(mapper.file_symbols) if hasattr(mapper, 'file_symbols') else -1
        result["used_tiktoken"] = has_tiktoken

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)}"

    return result


def get_config_info() -> dict:
    """Get configuration without exposing sensitive data."""
    config = {
        "keywords_exists": False,
        "telemetry_exists": False,
        "num_keyword_files": 0,
        "num_pinned_files": 0,
    }

    # Check for keywords.json
    keywords_paths = [
        Path(".claude/keywords.json"),
        Path.home() / ".claude" / "keywords.json",
    ]

    for kp in keywords_paths:
        if kp.exists():
            config["keywords_exists"] = True
            try:
                data = json.loads(kp.read_text(encoding="utf-8"))
                config["num_keyword_files"] = len(data.get("keywords", {}))
                config["num_pinned_files"] = len(data.get("pinned", []))
            except Exception:
                pass
            break

    # Check telemetry
    telemetry_dir = Path.home() / ".claude" / "telemetry"
    if telemetry_dir.exists():
        config["telemetry_exists"] = True
        turns_file = telemetry_dir / "turns.jsonl"
        if turns_file.exists():
            try:
                content = turns_file.read_text(encoding="utf-8").strip()
                lines = [ln for ln in content.split("\n") if ln] if content else []
                config["num_turns_recorded"] = len(lines)
            except Exception:
                pass

    return config


def generate_report(repo_path: Path = None, run_bench: bool = True) -> dict:
    """Generate a complete diagnostic report."""
    if repo_path is None:
        repo_path = Path.cwd()

    report = {
        "generated_at": datetime.now().isoformat(),
        "report_version": "1.0",
        "system": get_system_info(),
        "dependencies": get_dependency_info(),
        "repository": get_repo_info(repo_path),
        "configuration": get_config_info(),
    }

    if run_bench:
        report["benchmark"] = run_benchmark(repo_path)

    return report


def format_report_text(report: dict) -> str:
    """Format report as human-readable text."""
    lines = []
    lines.append("=" * 60)
    lines.append("attnroute Diagnostic Report")
    lines.append("=" * 60)
    lines.append(f"Generated: {report['generated_at']}")
    lines.append("")

    # System
    lines.append("SYSTEM")
    lines.append("-" * 40)
    sys_info = report["system"]
    lines.append(f"  Platform: {sys_info['platform']} ({sys_info['architecture']})")
    lines.append(f"  Python: {sys_info['python_version']}")
    lines.append(f"  attnroute: {sys_info['attnroute_version']}")
    lines.append("")

    # Dependencies
    lines.append("DEPENDENCIES")
    lines.append("-" * 40)
    deps = report["dependencies"]
    for name, info in deps.items():
        if info["installed"]:
            lines.append(f"  {name}: {info.get('version', 'yes')}")
        else:
            lines.append(f"  {name}: not installed")
    lines.append("")

    # Repository
    lines.append("REPOSITORY")
    lines.append("-" * 40)
    repo = report["repository"]
    lines.append(f"  Directory: {repo['path_anonymized']}")
    lines.append(f"  Files: {repo['file_count']}")
    lines.append(f"  Is git repo: {repo['is_git']}")
    if repo["file_types"]:
        types_str = ", ".join(f"{k}:{v}" for k, v in list(repo["file_types"].items())[:5])
        lines.append(f"  Top types: {types_str}")
    lines.append("")

    # Configuration
    lines.append("CONFIGURATION")
    lines.append("-" * 40)
    cfg = report["configuration"]
    lines.append(f"  keywords.json: {'found' if cfg['keywords_exists'] else 'not found'}")
    if cfg["keywords_exists"]:
        lines.append(f"    Files with keywords: {cfg['num_keyword_files']}")
        lines.append(f"    Pinned files: {cfg['num_pinned_files']}")
    lines.append(f"  Telemetry: {'active' if cfg['telemetry_exists'] else 'not initialized'}")
    if cfg.get("num_turns_recorded"):
        lines.append(f"    Turns recorded: {cfg['num_turns_recorded']}")
    lines.append("")

    # Benchmark
    if "benchmark" in report:
        lines.append("BENCHMARK")
        lines.append("-" * 40)
        bench = report["benchmark"]
        if bench["ran"]:
            lines.append(f"  Latency: {bench['latency_ms']}ms")
            lines.append(f"  Output tokens: {bench['output_tokens']}")
            lines.append(f"  Output lines: {bench['output_lines']}")
            lines.append(f"  Files indexed: {bench['files_indexed']}")
            lines.append(f"  Used tiktoken: {bench['used_tiktoken']}")
        elif bench["error"]:
            lines.append(f"  Error: {bench['error']}")
        else:
            lines.append("  Benchmark not run")

    lines.append("")
    lines.append("=" * 60)
    lines.append("Include this report when filing issues at:")
    lines.append("https://github.com/jeranaias/attnroute/issues")
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a diagnostic report for attnroute",
        epilog="The report can be shared to help debug issues."
    )
    parser.add_argument("path", nargs="?", default=".",
                        help="Repository path to analyze (default: current directory)")
    parser.add_argument("--output", "-o", type=str,
                        help="Output file path (default: attnroute_diagnostic.json)")
    parser.add_argument("--format", "-f", choices=["json", "text"], default="text",
                        help="Output format (default: text)")
    parser.add_argument("--no-benchmark", action="store_true",
                        help="Skip running the benchmark")
    parser.add_argument("--print", "-p", action="store_true",
                        help="Print to stdout instead of file")

    args = parser.parse_args()

    repo_path = Path(args.path).resolve()

    print(f"Generating diagnostic report for: {repo_path.name}")
    print("Collecting system info...")

    report = generate_report(repo_path, run_bench=not args.no_benchmark)

    if args.format == "json":
        output = json.dumps(report, indent=2)
        default_filename = "attnroute_diagnostic.json"
    else:
        output = format_report_text(report)
        default_filename = "attnroute_diagnostic.txt"

    if args.print:
        print()
        print(output)
    else:
        output_path = Path(args.output) if args.output else Path(default_filename)
        try:
            output_path.write_text(output, encoding="utf-8")
            print(f"Report saved to: {output_path}")
            print("Include this file when reporting issues.")
        except PermissionError:
            print(f"Error: Permission denied writing to {output_path}", file=sys.stderr)
            print("Try a different location or run with elevated permissions.", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"Error writing report: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
