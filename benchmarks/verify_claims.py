#!/usr/bin/env python3
"""
AttnRoute - Independent Claim Verification

Verifies two claims:
  1. Token reduction >= 90% (vs baseline of all source files concatenated)
  2. Latency <= 500ms for most repos

Run it yourself. No trust required.

Usage:
    python verify_claims.py                    # Test on current directory
    python verify_claims.py /path/to/your/repo # Test on your own repo

Requirements:
    pip install tiktoken tree-sitter-languages networkx
"""

import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def check_deps():
    """Check and report available dependencies."""
    deps = {}

    try:
        import tiktoken  # noqa: F401
        deps["tiktoken"] = True
    except ImportError:
        deps["tiktoken"] = False

    try:
        from attnroute.repo_map import TREE_SITTER_AVAILABLE
        deps["tree-sitter"] = TREE_SITTER_AVAILABLE
    except (ImportError, Exception):
        deps["tree-sitter"] = False

    return deps


def count_tokens(text, tokenizer):
    """Count tokens using tiktoken or fallback."""
    if tokenizer:
        return len(tokenizer.encode(text))
    return len(text) // 4


def measure_repo(repo_path, tokenizer):
    """Measure a repository with cold/warm separation."""
    try:
        from attnroute.repo_map import RepoMapper
    except ImportError:
        from repo_map import RepoMapper

    print(f"\n  Measuring: {repo_path}")
    print("  " + "-" * 50)

    # Measure baseline (all source files concatenated)
    extensions = ['.py', '.go', '.js', '.ts', '.tsx', '.rs', '.java', '.c', '.cpp', '.h']
    excluded = {'node_modules', 'vendor', '.git', '__pycache__', 'venv', '.venv'}
    baseline_content = ""
    file_count = 0

    for ext in extensions:
        for file_path in repo_path.rglob(f"*{ext}"):
            if any(part in excluded for part in file_path.parts):
                continue
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                baseline_content += content
                file_count += 1
            except Exception:
                continue

    baseline_tokens = count_tokens(baseline_content, tokenizer)

    # Run attnroute: 1 cold + 4 warm runs
    NUM_RUNS = 5
    runs = []

    for i in range(NUM_RUNS):
        start = time.perf_counter()
        mapper = RepoMapper(str(repo_path), max_files=500)
        mapper.index(verbose=False)
        repo_map = mapper.get_map(query="main entry", token_budget=2000)
        elapsed = (time.perf_counter() - start) * 1000
        output_tokens = count_tokens(repo_map, tokenizer)
        runs.append({
            'time_ms': elapsed,
            'output_tokens': output_tokens,
        })

    # Separate cold vs warm
    cold_run = runs[0]
    warm_runs = runs[1:]

    output_tokens = runs[0]['output_tokens']
    reduction = (1 - output_tokens / baseline_tokens) * 100 if baseline_tokens > 0 else 0

    cold_time = cold_run['time_ms']
    warm_avg = sum(r['time_ms'] for r in warm_runs) / len(warm_runs) if warm_runs else cold_time
    warm_median = sorted(r['time_ms'] for r in warm_runs)[len(warm_runs) // 2] if warm_runs else cold_time

    # Verification hash
    verification_data = f"{repo_path}:{file_count}:{baseline_tokens}:{output_tokens}"
    verification_hash = hashlib.sha256(verification_data.encode()).hexdigest()[:16]

    print(f"  Files:             {file_count}")
    print(f"  Baseline tokens:   {baseline_tokens:,}")
    print(f"  Output tokens:     {output_tokens:,}")
    print(f"  REDUCTION:         {reduction:.2f}%")
    print(f"  Cold start:        {cold_time:.1f}ms")
    print(f"  Warm average:      {warm_avg:.1f}ms")
    print(f"  Warm median:       {warm_median:.1f}ms")
    print(f"  Verify hash:       {verification_hash}")

    return {
        'path': str(repo_path),
        'files': file_count,
        'baseline_tokens': baseline_tokens,
        'output_tokens': output_tokens,
        'reduction_percent': reduction,
        'cold_time_ms': cold_time,
        'warm_avg_ms': warm_avg,
        'warm_median_ms': warm_median,
        'verification_hash': verification_hash,
    }


def verify_claims():
    """Main verification routine."""
    print()
    print("=" * 70)
    print("ATTNROUTE - INDEPENDENT CLAIM VERIFICATION")
    print("=" * 70)
    print()

    # Step 1: Check dependencies
    print("DEPENDENCIES:")
    deps = check_deps()
    tokenizer = None

    if deps["tiktoken"]:
        import tiktoken
        tokenizer = tiktoken.get_encoding("cl100k_base")
        print("  [OK]   tiktoken cl100k_base (accurate token counts)")
    else:
        print("  [MISS] tiktoken (using char/4 estimate — less accurate)")

    if deps["tree-sitter"]:
        print("  [OK]   tree-sitter (AST parsing for symbol extraction)")
    else:
        print("  [MISS] tree-sitter-languages (using regex fallback — SLOWER)")
        print("         This will increase latency. Install for faster results:")
        print("         pip install tree-sitter-languages")

    # Step 2: Determine repos to test
    print()
    print("MEASUREMENT:")

    repos_to_test = []

    if len(sys.argv) > 1:
        user_path = Path(sys.argv[1])
        if user_path.exists():
            repos_to_test.append(user_path)
        else:
            print(f"  Path not found: {user_path}")
    else:
        cwd = Path.cwd()
        repos_to_test.append(cwd)
        print(f"  Testing current directory: {cwd}")

    if not repos_to_test:
        print("  No repositories found to test!")
        return

    results = []
    for repo_path in repos_to_test:
        try:
            result = measure_repo(repo_path, tokenizer)
            results.append(result)
        except Exception as e:
            print(f"  Error testing {repo_path}: {e}")

    # Step 3: Verify claims
    print()
    print("=" * 70)
    print("CLAIM VERIFICATION")
    print("=" * 70)
    print()

    all_pass = True

    for r in results:
        repo_name = Path(r['path']).name or "current directory"
        print(f"  Repository: {repo_name}")
        print(f"    Files: {r['files']}, Baseline: {r['baseline_tokens']:,} tokens")
        print(f"    Output: {r['output_tokens']:,} tokens")
        print()

        # Claim 1: Token reduction >= 90%
        if r['reduction_percent'] >= 90:
            print(f"    [PASS] Token reduction {r['reduction_percent']:.1f}% >= 90%")
        else:
            print(f"    [FAIL] Token reduction {r['reduction_percent']:.1f}% < 90%")
            if r['files'] < 20:
                print(f"           (small repos have lower reduction — only {r['files']} files)")
            all_pass = False

        # Claim 2: Latency <= 500ms (warm, since cold includes one-time setup)
        if r['warm_median_ms'] <= 500:
            print(f"    [PASS] Warm median {r['warm_median_ms']:.0f}ms <= 500ms")
        else:
            print(f"    [FAIL] Warm median {r['warm_median_ms']:.0f}ms > 500ms")
            if not deps["tree-sitter"]:
                print("           (tree-sitter not installed — regex fallback is 3-5x slower)")
                print("           Install: pip install tree-sitter-languages")
            all_pass = False

        if r['cold_time_ms'] > 500:
            print(f"    [INFO] Cold start {r['cold_time_ms']:.0f}ms (one-time cost, not repeated)")

        print(f"    Hash: {r['verification_hash']}")
        print()

    # Summary
    print("=" * 70)
    if all_pass:
        print("ALL CLAIMS VERIFIED")
    else:
        print("SOME CLAIMS NOT MET — see details above")
    print("=" * 70)
    print()

    print("METHODOLOGY:")
    print("  Baseline = all source files concatenated (theoretical maximum)")
    print("  This is NOT how Claude Code works — it reads files selectively.")
    print("  The reduction measures compression effectiveness, not real savings.")
    print()
    print("  Latency is measured warm (after first run). Cold start includes")
    print("  one-time indexing that doesn't repeat on subsequent prompts.")
    print()
    print("To verify yourself:")
    print("  pip install tiktoken tree-sitter-languages")
    print("  python verify_claims.py /path/to/any/repo")
    print()


if __name__ == "__main__":
    verify_claims()
