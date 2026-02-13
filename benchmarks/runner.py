#!/usr/bin/env python3
"""
attnroute benchmark runner - CLI entry point for benchmarks.

Usage:
    attnroute benchmark                          # Quick claim verification
    attnroute benchmark --scenario pipeline      # Full pipeline benchmark
    attnroute benchmark --scenario all           # Everything (statistical + pipeline)
    attnroute benchmark --scenario quick         # Fast claim verification (default)
"""

import argparse
import sys
from pathlib import Path


def main(scenario: str = None, repo_path: str = None):
    """Main benchmark entry point."""
    benchmark_dir = Path(__file__).parent
    sys.path.insert(0, str(benchmark_dir.parent))
    sys.path.insert(0, str(benchmark_dir))

    if scenario == "pipeline":
        from pipeline_benchmark import main as pipeline_main
        # Override sys.argv for the pipeline benchmark's argparse
        old_argv = sys.argv
        sys.argv = ["pipeline_benchmark.py"]
        if repo_path:
            sys.argv.append(repo_path)
        try:
            pipeline_main()
        finally:
            sys.argv = old_argv

    elif scenario == "all":
        # Run statistical benchmark
        try:
            from bulletproof_benchmark import run_bulletproof_benchmark
            local_paths = [repo_path] if repo_path else None
            run_bulletproof_benchmark(use_public_repos=False, local_paths=local_paths,
                                      include_aider=True)
        except ImportError as e:
            print(f"Could not import bulletproof_benchmark: {e}")

        # Then run pipeline benchmark
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
    parser.add_argument("--scenario", choices=["all", "quick", "pipeline"],
                        default="quick", help="Benchmark scenario to run")
    parser.add_argument("--repo", type=str, help="Repository path to benchmark")

    args = parser.parse_args()
    main(scenario=args.scenario, repo_path=args.repo)
