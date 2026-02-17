#!/usr/bin/env python3
"""
Pipeline Benchmark - Tests the actual attnroute hook pipeline.

Unlike verify_claims.py (which only tests RepoMapper), this benchmark
tests the full context_router.py pipeline that runs on every
UserPromptSubmit hook: decay → search → scoring → context build → output.

This is what actually runs in production. If you want to know how fast
attnroute is for real users, run this.

Usage:
    python benchmarks/pipeline_benchmark.py                    # Test on current dir
    python benchmarks/pipeline_benchmark.py /path/to/repo      # Test on specific repo
    python benchmarks/pipeline_benchmark.py --cold-only         # Skip warm runs
"""

import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# DEPENDENCY CHECK
# =============================================================================

def check_dependencies():
    """Report which optional dependencies are available."""
    deps = {}

    try:
        import tiktoken
        deps["tiktoken"] = tiktoken.__version__
    except (ImportError, AttributeError):
        deps["tiktoken"] = None

    try:
        import bm25s
        deps["bm25s"] = getattr(bm25s, "__version__", "installed")
    except ImportError:
        deps["bm25s"] = None

    try:
        import tree_sitter
        deps["tree-sitter"] = getattr(tree_sitter, "__version__", "installed")
    except ImportError:
        deps["tree-sitter"] = None

    try:
        import tree_sitter_languages
        deps["tree-sitter-languages"] = getattr(tree_sitter_languages, "__version__", "installed")
    except ImportError:
        deps["tree-sitter-languages"] = None

    try:
        import networkx
        deps["networkx"] = networkx.__version__
    except ImportError:
        deps["networkx"] = None

    try:
        import model2vec
        deps["model2vec"] = getattr(model2vec, "__version__", "installed")
    except ImportError:
        deps["model2vec"] = None

    try:
        import numpy
        deps["numpy"] = numpy.__version__
    except ImportError:
        deps["numpy"] = None

    return deps


def count_tokens(text, tokenizer=None):
    """Count tokens, preferring tiktoken if available."""
    if tokenizer:
        return len(tokenizer.encode(text))
    return len(text) // 4  # Conservative fallback


# =============================================================================
# BASELINE MEASUREMENT
# =============================================================================

def measure_baseline(repo_path, tokenizer=None):
    """Measure the baseline: all source files concatenated."""
    extensions = {'.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs',
                  '.java', '.c', '.cpp', '.h', '.rb', '.php', '.swift', '.kt'}
    excluded = {'node_modules', 'vendor', '.git', '__pycache__',
                'venv', '.venv', 'dist', 'build', 'target', '.tox'}

    total_content = ""
    file_count = 0

    for f in repo_path.rglob("*"):
        if not f.is_file() or f.suffix not in extensions:
            continue
        if any(part in excluded for part in f.parts):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            total_content += content
            file_count += 1
        except Exception:
            continue

    tokens = count_tokens(total_content, tokenizer)
    return {
        "files": file_count,
        "chars": len(total_content),
        "tokens": tokens,
    }


# =============================================================================
# COMPONENT BENCHMARKS
# =============================================================================

def bench_repo_mapper(repo_path, queries, tokenizer=None, runs=5):
    """Benchmark RepoMapper separately (what verify_claims.py tests)."""
    try:
        from attnroute.repo_map import RepoMapper
    except ImportError:
        return None

    results = []
    for i in range(runs):
        t0 = time.perf_counter()
        mapper = RepoMapper(str(repo_path), max_files=500)
        mapper.index(verbose=False)
        t_index = time.perf_counter()

        outputs = []
        for query in queries:
            repo_map = mapper.get_map(query=query, token_budget=2000)
            outputs.append(repo_map)
        t_map = time.perf_counter()

        # Token count on first query output
        output_tokens = count_tokens(outputs[0], tokenizer)

        results.append({
            "run": i,
            "index_ms": (t_index - t0) * 1000,
            "map_ms": (t_map - t_index) * 1000,
            "total_ms": (t_map - t0) * 1000,
            "output_tokens": output_tokens,
            "output_chars": len(outputs[0]),
        })

    return results


def bench_search_index(repo_path, queries, runs=3):
    """Benchmark SearchIndex query performance."""
    try:
        from attnroute.indexer import SearchIndex
    except ImportError:
        return None

    import tempfile

    results = []

    # Use a temp DB so we don't pollute real index
    tmp_dir = tempfile.mkdtemp(prefix="attnroute_bench_idx_")
    db_path = Path(tmp_dir) / "bench_index.db"

    try:
        # Cold start: build index from scratch
        t0 = time.perf_counter()
        idx = SearchIndex(db_path)
        # Build with repo_path as code root (and check for .claude/ docs)
        docs_root = repo_path / ".claude"
        if not docs_root.exists():
            docs_root = repo_path  # Fallback: just index source
        idx.build(docs_root=docs_root, code_roots=[repo_path])
        cold_build_ms = (time.perf_counter() - t0) * 1000

        for i in range(runs):
            query_times = []
            for query in queries:
                t0 = time.perf_counter()
                idx.query(query, top_k=6)
                query_times.append((time.perf_counter() - t0) * 1000)

            results.append({
                "run": i,
                "cold_build_ms": cold_build_ms if i == 0 else 0,
                "query_times_ms": query_times,
                "avg_query_ms": statistics.mean(query_times),
            })
    finally:
        try:
            db_path.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except Exception:
            pass

    return results


def bench_full_pipeline(repo_path, prompts, tokenizer=None, runs=5):
    """
    Benchmark the full context_router pipeline.

    This simulates what actually happens on each UserPromptSubmit hook:
    load state → update attention → build context → save state.
    """
    try:
        from attnroute.context_router import (
            build_context_output,
            resolve_docs_root,
            save_state,
            update_attention,
        )
    except ImportError as e:
        print(f"  Cannot import context_router: {e}")
        return None

    import copy
    import tempfile

    # Use a temp state file so we don't pollute real state
    tmp_dir = tempfile.mkdtemp(prefix="attnroute_bench_")
    state_file = Path(tmp_dir) / "bench_state.json"

    results = []

    for i in range(runs):
        run_timings = []

        # Fresh state each run
        state = {"scores": {}, "consecutive_turns": {}, "turn_count": 0}

        for prompt in prompts:
            t0 = time.perf_counter()

            # Load state (simulate real hook — deep copy like context_router does)
            copy.deepcopy(state)

            t1 = time.perf_counter()

            # Resolve docs root
            docs_root = resolve_docs_root()

            t2 = time.perf_counter()

            # Update attention (decay + search + scoring)
            state, activated = update_attention(state, prompt)

            t3 = time.perf_counter()

            # Build context output
            output, stats = build_context_output(state, docs_root)

            t4 = time.perf_counter()

            # Save state
            save_state(state_file, state)

            t5 = time.perf_counter()

            output_tokens = count_tokens(output, tokenizer) if output else 0

            run_timings.append({
                "prompt": prompt[:50] + "..." if len(prompt) > 50 else prompt,
                "state_copy_ms": (t1 - t0) * 1000,
                "resolve_docs_ms": (t2 - t1) * 1000,
                "update_attention_ms": (t3 - t2) * 1000,
                "build_context_ms": (t4 - t3) * 1000,
                "save_state_ms": (t5 - t4) * 1000,
                "total_ms": (t5 - t0) * 1000,
                "output_chars": len(output) if output else 0,
                "output_tokens": output_tokens,
                "hot": stats.get("hot", 0),
                "warm": stats.get("warm", 0),
                "cold": stats.get("cold", 0),
                "activated": len(activated),
            })

        results.append({
            "run": i,
            "timings": run_timings,
            "avg_total_ms": statistics.mean(t["total_ms"] for t in run_timings),
            "avg_attention_ms": statistics.mean(t["update_attention_ms"] for t in run_timings),
            "avg_build_ms": statistics.mean(t["build_context_ms"] for t in run_timings),
        })

    # Cleanup
    try:
        state_file.unlink(missing_ok=True)
        Path(tmp_dir).rmdir()
    except Exception:
        pass

    return results


# =============================================================================
# REPORT GENERATION
# =============================================================================

def print_dep_report(deps):
    """Print dependency availability report."""
    print("=" * 70)
    print("DEPENDENCY STATUS")
    print("=" * 70)

    critical = ["tiktoken", "bm25s", "tree-sitter-languages"]
    optional = ["tree-sitter", "networkx", "model2vec", "numpy"]

    for name in critical + optional:
        version = deps.get(name)
        if version:
            print(f"  [OK]   {name:<25} {version}")
        else:
            tag = "CRITICAL" if name in critical else "optional"
            impact = {
                "tiktoken": "token counts will use char/4 estimate",
                "bm25s": "no BM25 search, keyword fallback only",
                "tree-sitter-languages": "no AST parsing, regex fallback (slower)",
                "tree-sitter": "no tree-sitter base library",
                "networkx": "no transitive co-activation (2-hop)",
                "model2vec": "no semantic reranking",
                "numpy": "no vector math for embeddings",
            }
            print(f"  [MISS] {name:<25} ({tag}: {impact.get(name, 'N/A')})")

    print()


def print_baseline_report(baseline):
    """Print baseline measurement."""
    print("=" * 70)
    print("BASELINE (all source files concatenated)")
    print("=" * 70)
    print(f"  Files:  {baseline['files']}")
    print(f"  Chars:  {baseline['chars']:,}")
    print(f"  Tokens: {baseline['tokens']:,}")
    print()
    print("  NOTE: This baseline is a theoretical maximum, not how Claude Code")
    print("  actually works. Claude reads files selectively via tool calls.")
    print()


def print_repo_mapper_report(results, baseline):
    """Print RepoMapper benchmark results."""
    if not results:
        print("  [SKIPPED] RepoMapper not available")
        return

    print("=" * 70)
    print("REPO MAPPER (symbol-level compression)")
    print("=" * 70)

    cold_run = results[0]
    warm_runs = results[1:] if len(results) > 1 else results

    # Cold start
    print(f"  Cold start:  {cold_run['total_ms']:>8.1f}ms  (index: {cold_run['index_ms']:.1f}ms + map: {cold_run['map_ms']:.1f}ms)")

    # Warm average
    if warm_runs:
        avg_total = statistics.mean(r["total_ms"] for r in warm_runs)
        avg_index = statistics.mean(r["index_ms"] for r in warm_runs)
        avg_map = statistics.mean(r["map_ms"] for r in warm_runs)
        print(f"  Warm avg:    {avg_total:>8.1f}ms  (index: {avg_index:.1f}ms + map: {avg_map:.1f}ms)")

    # Output size
    output_tokens = results[0]["output_tokens"]
    reduction = (1 - output_tokens / baseline["tokens"]) * 100 if baseline["tokens"] > 0 else 0
    print(f"  Output:      {output_tokens:,} tokens ({reduction:.1f}% reduction vs baseline)")
    print()

    # Verdict
    if reduction >= 90:
        print(f"  [PASS] Token reduction {reduction:.1f}% >= 90% claim")
    else:
        print(f"  [BELOW] Token reduction {reduction:.1f}% < 90% claim")

    all_times = [r["total_ms"] for r in results]
    median_time = statistics.median(all_times)
    if median_time <= 500:
        print(f"  [PASS] Median time {median_time:.0f}ms <= 500ms claim")
    else:
        print(f"  [ABOVE] Median time {median_time:.0f}ms > 500ms claim")
        if not any(check_dependencies().get(d) for d in ["tree-sitter-languages"]):
            print("         (tree-sitter-languages not installed — regex fallback is slower)")

    print()


def print_search_report(results):
    """Print SearchIndex benchmark results."""
    if not results:
        print("  [SKIPPED] SearchIndex not available")
        return

    print("=" * 70)
    print("SEARCH INDEX (BM25 + semantic rerank)")
    print("=" * 70)

    cold_build = results[0]["cold_build_ms"]
    print(f"  Index build:  {cold_build:>8.1f}ms (cold start)")

    all_query_times = []
    for r in results:
        all_query_times.extend(r["query_times_ms"])

    if all_query_times:
        print(f"  Query avg:    {statistics.mean(all_query_times):>8.1f}ms")
        print(f"  Query median: {statistics.median(all_query_times):>8.1f}ms")
        print(f"  Query p95:    {sorted(all_query_times)[int(len(all_query_times) * 0.95)]:>8.1f}ms")

    print()


def print_pipeline_report(results, baseline, tokenizer):
    """Print full pipeline benchmark results."""
    if not results:
        print("  [SKIPPED] Full pipeline not available")
        return

    print("=" * 70)
    print("FULL PIPELINE (what runs on every prompt)")
    print("=" * 70)
    print("  Simulates UserPromptSubmit hook: state → attention → context → save")
    print()

    # Separate cold (run 0) vs warm (runs 1+)
    cold_run = results[0]
    warm_runs = results[1:] if len(results) > 1 else []

    # Cold run detail
    print("  COLD START (first run, no cached state):")
    for t in cold_run["timings"]:
        print(f"    \"{t['prompt']}\"")
        print(f"      update_attention: {t['update_attention_ms']:>7.1f}ms")
        print(f"      build_context:    {t['build_context_ms']:>7.1f}ms")
        print(f"      save_state:       {t['save_state_ms']:>7.1f}ms")
        print(f"      TOTAL:            {t['total_ms']:>7.1f}ms")
        print(f"      output: {t['output_tokens']} tokens, {t['hot']} HOT / {t['warm']} WARM / {t['cold']} COLD")
    print()

    # Warm runs summary
    if warm_runs:
        print("  WARM RUNS (subsequent, with cached state):")
        all_totals = []
        all_attention = []
        all_build = []
        all_output_tokens = []

        for run in warm_runs:
            for t in run["timings"]:
                all_totals.append(t["total_ms"])
                all_attention.append(t["update_attention_ms"])
                all_build.append(t["build_context_ms"])
                all_output_tokens.append(t["output_tokens"])

        print(f"    Avg total:          {statistics.mean(all_totals):>7.1f}ms")
        print(f"    Avg attention:      {statistics.mean(all_attention):>7.1f}ms")
        print(f"    Avg build:          {statistics.mean(all_build):>7.1f}ms")
        print(f"    Median total:       {statistics.median(all_totals):>7.1f}ms")

        if all_output_tokens and any(t > 0 for t in all_output_tokens):
            avg_output = statistics.mean(t for t in all_output_tokens if t > 0)
            reduction = (1 - avg_output / baseline["tokens"]) * 100 if baseline["tokens"] > 0 else 0
            print(f"    Avg output tokens:  {avg_output:>7.0f} ({reduction:.1f}% reduction)")

        print()

    # Component breakdown
    print("  LATENCY BREAKDOWN (all runs):")
    all_timings = []
    for run in results:
        all_timings.extend(run["timings"])

    if all_timings:
        components = [
            ("state_copy_ms", "State copy"),
            ("resolve_docs_ms", "Resolve docs"),
            ("update_attention_ms", "Update attention"),
            ("build_context_ms", "Build context"),
            ("save_state_ms", "Save state"),
        ]
        total_avg = statistics.mean(t["total_ms"] for t in all_timings)
        for key, label in components:
            avg = statistics.mean(t[key] for t in all_timings)
            pct = (avg / total_avg * 100) if total_avg > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"    {label:<20} {avg:>7.1f}ms  ({pct:>4.1f}%)  {bar}")
        print(f"    {'TOTAL':<20} {total_avg:>7.1f}ms")

    print()


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Run the pipeline benchmark."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute pipeline benchmark")
    parser.add_argument("repo", nargs="?", default=".", help="Repository path to benchmark")
    parser.add_argument("--runs", type=int, default=5, help="Number of runs (default: 5)")
    parser.add_argument("--cold-only", action="store_true", help="Only run cold start (1 run)")
    args = parser.parse_args()

    repo_path = Path(args.repo).resolve()
    num_runs = 1 if args.cold_only else args.runs

    print()
    print("=" * 70)
    print("ATTNROUTE PIPELINE BENCHMARK v1.0.0")
    print("=" * 70)
    print(f"  Repository: {repo_path}")
    print(f"  Runs:       {num_runs}")
    print(f"  Python:     {sys.version.split()[0]}")
    print()

    # Test queries that exercise different parts of the pipeline
    queries = [
        "main entry point handler",
        "authentication login session",
        "search index query BM25",
        "plugin system hooks",
    ]

    # Prompts for full pipeline test (what a user might actually type)
    prompts = [
        "How does the search index work?",
        "Fix the bug in the authentication handler",
        "Add a new plugin for rate limiting",
    ]

    # 1. Dependencies
    deps = check_dependencies()
    print_dep_report(deps)

    # Setup tokenizer
    tokenizer = None
    try:
        import tiktoken
        tokenizer = tiktoken.get_encoding("cl100k_base")
    except ImportError:
        pass

    # 2. Baseline
    print("Measuring baseline...")
    baseline = measure_baseline(repo_path, tokenizer)
    print_baseline_report(baseline)

    if baseline["files"] == 0:
        print("  ERROR: No source files found. Is this a code repository?")
        return

    # 3. RepoMapper benchmark
    print("Benchmarking RepoMapper...")
    mapper_results = bench_repo_mapper(repo_path, queries, tokenizer, runs=num_runs)
    print_repo_mapper_report(mapper_results, baseline)

    # 4. SearchIndex benchmark
    print("Benchmarking SearchIndex...")
    search_results = bench_search_index(repo_path, queries, runs=min(num_runs, 3))
    print_search_report(search_results)

    # 5. Full pipeline benchmark
    print("Benchmarking full pipeline...")

    # Set CWD to repo so context_router finds the right project
    old_cwd = os.getcwd()
    os.chdir(str(repo_path))
    try:
        pipeline_results = bench_full_pipeline(repo_path, prompts, tokenizer, runs=num_runs)
        print_pipeline_report(pipeline_results, baseline, tokenizer)
    finally:
        os.chdir(old_cwd)

    # 6. Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()

    if mapper_results:
        warm_mapper = mapper_results[1:] if len(mapper_results) > 1 else mapper_results
        mapper_median = statistics.median(r["total_ms"] for r in warm_mapper)
        output_tokens = mapper_results[0]["output_tokens"]
        reduction = (1 - output_tokens / baseline["tokens"]) * 100 if baseline["tokens"] > 0 else 0
        print(f"  RepoMapper:     {reduction:.1f}% token reduction, {mapper_median:.0f}ms median")

    if pipeline_results:
        all_totals = [t["total_ms"] for r in pipeline_results for t in r["timings"]]
        pipeline_median = statistics.median(all_totals)
        print(f"  Full pipeline:  {pipeline_median:.0f}ms median per prompt")

    print()

    missing = [d for d in ["bm25s", "tree-sitter-languages", "model2vec"] if not deps.get(d)]
    if missing:
        print(f"  Missing deps: {', '.join(missing)}")
        print("  Install with: pip install attnroute[all]")
        print("  Results may improve with all dependencies installed.")
    else:
        print("  All optional dependencies installed.")

    print()
    print("  NOTE: These benchmarks measure attnroute's internal performance.")
    print("  The '90%+ token reduction' claim compares against all source files")
    print("  concatenated (theoretical max), not Claude Code's native behavior.")
    print()


if __name__ == "__main__":
    main()
