#!/usr/bin/env python3
"""
attnroute benchmark runner - CLI entry point for benchmarks.

Usage:
    python -m benchmarks.runner quick          # Quick claim verification (default)
    python -m benchmarks.runner plugins        # Plugin scenario benchmarks (~30s)
    python -m benchmarks.runner multi          # Multi-repo token benchmark (3 repos)
    python -m benchmarks.runner multi-full     # Multi-repo token benchmark (12 repos)
    python -m benchmarks.runner resources      # Resource measurements
    python -m benchmarks.runner pipeline       # Full pipeline benchmark
    python -m benchmarks.runner replay          # Session replay (the real proof)
    python -m benchmarks.runner report         # Generate report from cached results
    python -m benchmarks.runner full           # Everything (multi-full + plugins + resources + replay)
    python -m benchmarks.runner all            # Legacy: statistical + pipeline
"""

import argparse
import sys
from pathlib import Path


def main(scenario: str = None, repo_path: str = None):
    """Main benchmark entry point."""
    benchmark_dir = Path(__file__).parent
    sys.path.insert(0, str(benchmark_dir.parent))
    sys.path.insert(0, str(benchmark_dir))

    if scenario == "plugins":
        from plugin_benchmark import run_all_plugins
        results = run_all_plugins()
        all_pass = all(r["passed"] == r["total"] for r in results)
        sys.exit(0 if all_pass else 1)

    elif scenario == "multi":
        from multi_repo_benchmark import run_multi_repo_benchmark
        run_multi_repo_benchmark(mode="quick")

    elif scenario == "multi-full":
        from multi_repo_benchmark import run_multi_repo_benchmark
        run_multi_repo_benchmark(mode="full")

    elif scenario == "resources":
        from resource_benchmark import run_resource_benchmark
        run_resource_benchmark(mode="quick")

    elif scenario == "replay":
        from session_replay import run_session_replay
        run_session_replay(limit=10)

    elif scenario == "report":
        from report_generator import main as report_main
        report_main()

    elif scenario == "pipeline":
        from pipeline_benchmark import main as pipeline_main
        old_argv = sys.argv
        sys.argv = ["pipeline_benchmark.py"]
        if repo_path:
            sys.argv.append(repo_path)
        try:
            pipeline_main()
        finally:
            sys.argv = old_argv

    elif scenario == "full":
        # Run everything: multi-repo (full) + plugins + resources + report
        print("\n" + "=" * 70)
        print("FULL BENCHMARK SUITE")
        print("=" * 70 + "\n")

        print("Phase 1: Plugin Benchmarks")
        print("-" * 40)
        from plugin_benchmark import run_all_plugins
        run_all_plugins()

        print("\nPhase 2: Multi-Repo Token Benchmark (12 repos)")
        print("-" * 40)
        from multi_repo_benchmark import run_multi_repo_benchmark
        run_multi_repo_benchmark(mode="full")

        print("\nPhase 3: Resource Benchmark")
        print("-" * 40)
        from resource_benchmark import run_resource_benchmark
        run_resource_benchmark(mode="full")

        print("\nPhase 4: Session Replay")
        print("-" * 40)
        from session_replay import run_session_replay
        run_session_replay(limit=10)

        print("\nPhase 5: Report Generation")
        print("-" * 40)
        from report_generator import main as report_main
        report_main()

    elif scenario == "all":
        # Legacy: statistical benchmark + pipeline
        try:
            from bulletproof_benchmark import run_bulletproof_benchmark
            local_paths = [repo_path] if repo_path else None
            run_bulletproof_benchmark(use_public_repos=False, local_paths=local_paths,
                                      include_aider=True)
        except ImportError as e:
            print(f"Could not import bulletproof_benchmark: {e}")

        print("\n" + "=" * 70)
        print("PIPELINE BENCHMARK")
        print("=" * 70 + "\n")
        from pipeline_benchmark import main as pipeline_main
        old_argv = sys.argv
        sys.argv = ["pipeline_benchmark.py"]
        if repo_path:
            sys.argv.append(repo_path)
        try:
            pipeline_main()
        finally:
            sys.argv = old_argv

    else:
        # Default: quick claim verification
        from verify_claims import verify_claims
        if repo_path:
            old_argv = sys.argv
            sys.argv = ["verify_claims.py", repo_path]
            try:
                verify_claims()
            finally:
                sys.argv = old_argv
        else:
            verify_claims()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run attnroute benchmarks")
    parser.add_argument("scenario", nargs="?", default="quick",
                        choices=["quick", "plugins", "multi", "multi-full",
                                 "resources", "replay", "pipeline", "report", "full", "all"],
                        help="Benchmark scenario to run (default: quick)")
    parser.add_argument("--repo", type=str, help="Repository path to benchmark")

    args = parser.parse_args()
    main(scenario=args.scenario, repo_path=args.repo)
