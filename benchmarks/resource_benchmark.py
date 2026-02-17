#!/usr/bin/env python3
"""
Resource Benchmark — Measures attnroute's operational overhead.

Tracks memory, disk, CPU time, and startup latency to prove
attnroute is lightweight and suitable for real-time use.

Usage:
    python benchmarks/resource_benchmark.py              # Quick (3 repos)
    python benchmarks/resource_benchmark.py --mode full   # All 12 repos
"""

import gc
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from repo_config import clone_repo, get_repo_info, get_repos  # noqa: I001, E402


# ============================================================================
# MEMORY MEASUREMENT
# ============================================================================

def get_rss_mb() -> float:
    """Get current RSS memory in MB."""
    try:
        import resource
        # Unix
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        pass

    try:
        # Windows
        import ctypes
        import ctypes.wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(pmc), pmc.cb
        )
        return pmc.WorkingSetSize / (1024 * 1024)
    except Exception:
        return 0.0


def get_dir_size_mb(path: Path) -> float:
    """Get directory size in MB."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return total / (1024 * 1024)


# ============================================================================
# BENCHMARKS
# ============================================================================

def bench_import_latency(runs: int = 5) -> dict:
    """Measure import-to-ready time."""
    times = []

    for _ in range(runs):
        # Remove cached modules
        mods_to_remove = [k for k in sys.modules if k.startswith("attnroute")]
        for mod in mods_to_remove:
            del sys.modules[mod]

        gc.collect()

        t0 = time.perf_counter()
        import attnroute  # noqa: F401
        from attnroute.context_router import build_context_output, update_attention  # noqa: F401
        elapsed = (time.perf_counter() - t0) * 1000
        times.append(elapsed)

    return {
        "import_mean_ms": sum(times) / len(times),
        "import_min_ms": min(times),
        "import_max_ms": max(times),
    }


def bench_indexing(repo_path: Path, repo_name: str) -> dict:
    """Benchmark index build for a repo."""
    try:
        from attnroute.repo_map import RepoMapper
    except ImportError:
        return {"error": "RepoMapper not available"}

    gc.collect()
    mem_before = get_rss_mb()

    t0 = time.perf_counter()
    mapper = RepoMapper(str(repo_path), max_files=1000)
    mapper.index(verbose=False)
    elapsed = (time.perf_counter() - t0) * 1000

    mem_after = get_rss_mb()
    mem_delta = max(0, mem_after - mem_before)

    # Generate a map to measure output
    repo_map = mapper.get_map(query="main handler", token_budget=2000)

    return {
        "repo": repo_name,
        "index_time_ms": elapsed,
        "mem_before_mb": round(mem_before, 1),
        "mem_after_mb": round(mem_after, 1),
        "mem_delta_mb": round(mem_delta, 1),
        "files_indexed": len(mapper.file_symbols),
        "symbols_found": sum(len(f.symbols) for f in mapper.file_symbols.values()),
        "map_chars": len(repo_map),
    }


def bench_state_disk(repo_path: Path) -> dict:
    """Benchmark state file disk usage."""
    try:
        from attnroute.context_router import save_state, update_attention
    except ImportError:
        return {"error": "context_router not available"}

    tmp_dir = tempfile.mkdtemp(prefix="attnroute_bench_disk_")
    state_file = Path(tmp_dir) / "state.json"

    # Simulate a session with many file activations
    state = {"scores": {}, "consecutive_turns": {}, "turn_count": 0}

    prompts = [
        "Fix the authentication bug",
        "Add new search feature",
        "Refactor the database layer",
        "Update the test suite",
        "Deploy the application",
    ]

    old_cwd = os.getcwd()
    os.chdir(str(repo_path))
    try:
        for prompt in prompts:
            state, _ = update_attention(state, prompt)

        save_state(state_file, state)
    finally:
        os.chdir(old_cwd)

    state_size = state_file.stat().st_size / 1024 if state_file.exists() else 0

    # Cleanup
    try:
        state_file.unlink(missing_ok=True)
        Path(tmp_dir).rmdir()
    except Exception:
        pass

    return {
        "state_file_kb": round(state_size, 1),
        "files_tracked": len(state.get("scores", {})),
    }


def bench_pipeline_latency(repo_path: Path, runs: int = 5) -> dict:
    """Benchmark full pipeline latency (what happens per prompt)."""
    try:
        from attnroute.context_router import (
            build_context_output,
            resolve_docs_root,
            update_attention,
        )
    except ImportError:
        return {"error": "context_router not available"}

    old_cwd = os.getcwd()
    os.chdir(str(repo_path))

    times = []
    try:
        for _ in range(runs):
            state = {"scores": {}, "consecutive_turns": {}, "turn_count": 0}

            t0 = time.perf_counter()
            state, _ = update_attention(state, "Fix the main handler bug")
            docs_root = resolve_docs_root()
            output, _ = build_context_output(state, docs_root)
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
    finally:
        os.chdir(old_cwd)

    return {
        "pipeline_mean_ms": round(sum(times) / len(times), 1),
        "pipeline_min_ms": round(min(times), 1),
        "pipeline_max_ms": round(max(times), 1),
    }


# ============================================================================
# MAIN
# ============================================================================

def run_resource_benchmark(mode: str = "quick") -> dict:
    """Run all resource benchmarks."""
    print()
    print("=" * 70)
    print(f"RESOURCE BENCHMARK ({mode.upper()} mode)")
    print("=" * 70)
    print()

    results = {}

    # 1. Import latency
    print("  Import latency...")
    import_result = bench_import_latency()
    results["import"] = import_result
    print(f"    Mean: {import_result['import_mean_ms']:.0f}ms")
    print(f"    Range: {import_result['import_min_ms']:.0f}ms - {import_result['import_max_ms']:.0f}ms")
    print()

    # 2. Per-repo measurements
    repos = get_repos(mode)
    repo_results = []

    for repo in repos:
        print(f"  [{repo['name']}]")
        success, repo_path, sha = clone_repo(repo)
        if not success:
            print(f"    Clone failed: {sha}")
            continue

        info = get_repo_info(repo_path, repo["language"])
        print(f"    Files: {info['files']}, Lines: {info['lines']:,}")

        # Indexing benchmark
        idx = bench_indexing(repo_path, repo["name"])
        if "error" not in idx:
            print(f"    Index time: {idx['index_time_ms']:.0f}ms")
            print(f"    Memory delta: {idx['mem_delta_mb']:.1f}MB")
            print(f"    Symbols: {idx['symbols_found']}")

        # State disk usage
        disk = bench_state_disk(repo_path)
        if "error" not in disk:
            print(f"    State file: {disk['state_file_kb']:.1f}KB")

        # Pipeline latency
        pipeline = bench_pipeline_latency(repo_path, runs=3)
        if "error" not in pipeline:
            print(f"    Pipeline: {pipeline['pipeline_mean_ms']:.0f}ms avg")

        repo_results.append({
            "repo": repo["name"],
            "language": repo["language"],
            "files": info["files"],
            "lines": info["lines"],
            "indexing": idx,
            "disk": disk,
            "pipeline": pipeline,
        })
        print()

    results["repos"] = repo_results

    # Summary
    print("=" * 70)
    print("RESOURCE SUMMARY")
    print("=" * 70)
    print()

    print(f"  {'Repo':<20} {'Files':>6} {'Index ms':>10} {'Mem MB':>8} {'State KB':>10} {'Pipeline ms':>12}")
    print(f"  {'-' * 70}")

    for r in repo_results:
        idx = r.get("indexing", {})
        disk = r.get("disk", {})
        pipeline = r.get("pipeline", {})

        idx_ms = f"{idx.get('index_time_ms', 0):.0f}" if "error" not in idx else "N/A"
        mem = f"{idx.get('mem_delta_mb', 0):.1f}" if "error" not in idx else "N/A"
        state = f"{disk.get('state_file_kb', 0):.1f}" if "error" not in disk else "N/A"
        pipe = f"{pipeline.get('pipeline_mean_ms', 0):.0f}" if "error" not in pipeline else "N/A"

        print(f"  {r['repo']:<20} {r['files']:>6} {idx_ms:>10} {mem:>8} {state:>10} {pipe:>12}")

    print()

    # Save results
    output_file = Path(__file__).parent / "resource_results.json"
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "mode": mode,
            "results": results,
        }, f, indent=2)
    print(f"  Results saved to: {output_file}")
    print()

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Resource benchmark")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    args = parser.parse_args()

    run_resource_benchmark(mode=args.mode)
