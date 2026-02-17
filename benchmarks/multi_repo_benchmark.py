#!/usr/bin/env python3
"""
Multi-Repo Token Benchmark — Measures attnroute's token reduction
across 12 diverse public repos spanning 6 languages.

Uses the repo_config definitions and bulletproof_benchmark patterns
for statistical rigor (multiple runs, mean +/- std).

Usage:
    python benchmarks/multi_repo_benchmark.py              # Quick (3 repos)
    python benchmarks/multi_repo_benchmark.py --mode full   # All 12 repos
    python benchmarks/multi_repo_benchmark.py --runs 3      # Custom run count
"""

import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from repo_config import (
    clone_repo,
    get_all_source_content,
    get_repo_info,
    get_repos,
)

NUM_RUNS = 3


# ============================================================================
# TOKEN COUNTING
# ============================================================================

def get_tokenizer():
    """Get tiktoken tokenizer if available."""
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except ImportError:
        return None


def count_tokens(text: str, tokenizer=None) -> int:
    """Count tokens with tiktoken or char/4 fallback."""
    if tokenizer:
        return len(tokenizer.encode(text))
    return len(text) // 4


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class RepoResult:
    """Results for a single repo."""
    name: str
    language: str
    category: str
    files: int
    lines: int
    baseline_tokens: int
    output_tokens: int
    reduction_mean: float
    reduction_std: float
    time_mean_ms: float
    time_std_ms: float
    commit_sha: str


# ============================================================================
# BENCHMARK
# ============================================================================

def benchmark_repo(repo: dict, tokenizer, num_runs: int) -> RepoResult | None:
    """Benchmark a single repo with multiple runs."""
    # Clone/get repo
    print(f"\n  [{repo['name']}] ({repo['language']}, {repo['category']})")
    success, repo_path, sha = clone_repo(repo)
    if not success:
        print(f"    FAILED to clone: {sha}")
        return None

    # Get repo stats
    info = get_repo_info(repo_path, repo["language"])
    if info["files"] == 0:
        print("    No source files found")
        return None

    print(f"    Files: {info['files']}, Lines: {info['lines']:,}")

    # Baseline measurement
    baseline_content = get_all_source_content(repo_path)
    baseline_tokens = count_tokens(baseline_content, tokenizer)
    print(f"    Baseline: {baseline_tokens:,} tokens")

    if baseline_tokens == 0:
        print("    Empty baseline, skipping")
        return None

    # Import RepoMapper
    try:
        from attnroute.repo_map import RepoMapper
    except ImportError:
        print("    Cannot import RepoMapper")
        return None

    # Multiple runs
    reductions = []
    times = []
    last_output_tokens = 0

    for i in range(num_runs):
        start = time.perf_counter()
        mapper = RepoMapper(str(repo_path), max_files=1000)
        mapper.index(verbose=False)
        repo_map = mapper.get_map(query="main entry point handler", token_budget=2000)
        elapsed = (time.perf_counter() - start) * 1000

        output_tokens = count_tokens(repo_map, tokenizer)
        reduction = (1 - output_tokens / baseline_tokens) * 100

        reductions.append(reduction)
        times.append(elapsed)
        last_output_tokens = output_tokens

    reduction_mean = statistics.mean(reductions)
    reduction_std = statistics.stdev(reductions) if len(reductions) > 1 else 0
    time_mean = statistics.mean(times)
    time_std = statistics.stdev(times) if len(times) > 1 else 0

    print(f"    Reduction: {reduction_mean:.1f}% +/- {reduction_std:.1f}%")
    print(f"    Time: {time_mean:.0f}ms +/- {time_std:.0f}ms")

    return RepoResult(
        name=repo["name"],
        language=repo["language"],
        category=repo["category"],
        files=info["files"],
        lines=info["lines"],
        baseline_tokens=baseline_tokens,
        output_tokens=last_output_tokens,
        reduction_mean=reduction_mean,
        reduction_std=reduction_std,
        time_mean_ms=time_mean,
        time_std_ms=time_std,
        commit_sha=sha[:10],
    )


def run_multi_repo_benchmark(mode: str = "quick", num_runs: int = None) -> list[RepoResult]:
    """Run benchmark across multiple repos."""
    global NUM_RUNS
    if num_runs:
        NUM_RUNS = num_runs

    repos = get_repos(mode)

    print()
    print("=" * 75)
    print(f"MULTI-REPO TOKEN BENCHMARK ({mode.upper()} mode, {len(repos)} repos, {NUM_RUNS} runs each)")
    print("=" * 75)

    tokenizer = get_tokenizer()
    token_method = "tiktoken cl100k" if tokenizer else "char/4 estimate"
    print(f"  Tokenizer: {token_method}")

    results = []
    for repo in repos:
        try:
            result = benchmark_repo(repo, tokenizer, NUM_RUNS)
            if result:
                results.append(result)
        except Exception as e:
            print(f"    ERROR: {e}")

    # Print summary table
    print()
    print("=" * 75)
    print("RESULTS")
    print("=" * 75)
    print()

    if not results:
        print("  No results collected.")
        return results

    header = f"{'Repo':<20} {'Lang':<8} {'Files':>6} {'Baseline':>10} {'Output':>8} {'Reduction':>14} {'Time':>12}"
    print(f"  {header}")
    print(f"  {'-' * len(header)}")

    for r in results:
        reduction = f"{r.reduction_mean:.1f}% +/- {r.reduction_std:.1f}"
        time_str = f"{r.time_mean_ms:.0f}ms +/- {r.time_std_ms:.0f}"
        print(f"  {r.name:<20} {r.language:<8} {r.files:>6} {r.baseline_tokens:>10,} {r.output_tokens:>8,} {reduction:>14} {time_str:>12}")

    # Aggregate
    print()
    all_reductions = [r.reduction_mean for r in results]
    all_times = [r.time_mean_ms for r in results]
    print(f"  AGGREGATE ({len(results)} repos):")
    print(f"    Mean reduction:  {statistics.mean(all_reductions):.1f}%")
    print(f"    Min reduction:   {min(all_reductions):.1f}%")
    print(f"    Max reduction:   {max(all_reductions):.1f}%")
    print(f"    Mean time:       {statistics.mean(all_times):.0f}ms")
    print(f"    Median time:     {statistics.median(all_times):.0f}ms")

    # Markdown table for README
    print()
    print("  MARKDOWN TABLE (for README):")
    print()
    print("  | Repo | Language | Files | Token Reduction | Latency |")
    print("  |------|----------|-------|----------------|---------|")
    for r in results:
        print(f"  | {r.name} | {r.language} | {r.files} | {r.reduction_mean:.1f}% +/- {r.reduction_std:.1f} | {r.time_mean_ms:.0f}ms |")

    print()

    # Claim verification
    print("  CLAIM VERIFICATION:")
    repos_above_90 = sum(1 for r in results if r.reduction_mean >= 90)
    repos_below_500ms = sum(1 for r in results if r.time_mean_ms <= 500)
    print(f"    90%+ token reduction: {repos_above_90}/{len(results)} repos")
    print(f"    <=500ms latency:      {repos_below_500ms}/{len(results)} repos")
    print()

    # Save results
    output_file = Path(__file__).parent / "multi_repo_results.json"
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "num_runs": NUM_RUNS,
            "tokenizer": token_method,
            "results": [asdict(r) for r in results],
        }, f, indent=2)
    print(f"  Results saved to: {output_file}")
    print()

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Multi-repo token benchmark")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick",
                        help="Benchmark mode (quick=3 repos, full=12 repos)")
    parser.add_argument("--runs", type=int, default=3, help="Runs per repo (default: 3)")
    args = parser.parse_args()

    results = run_multi_repo_benchmark(mode=args.mode, num_runs=args.runs)

    # Exit with failure if any repo below 85% reduction
    if results and any(r.reduction_mean < 85 for r in results):
        sys.exit(1)
