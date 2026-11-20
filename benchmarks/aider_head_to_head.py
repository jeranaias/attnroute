#!/usr/bin/env python3
"""
Aider vs attnroute - TRUE HEAD-TO-HEAD COMPARISON

This script calls Aider's RepoMap directly (bypassing the buggy console output)
to get a fair, apples-to-apples comparison.
"""

import os
import sys
import time
from pathlib import Path

# Add attnroute to path
sys.path.insert(0, str(Path(__file__).parent))

# Ensure UTF-8 encoding
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONUTF8'] = '1'


def get_aider_version():
    """Get Aider version."""
    try:
        from aider import __version__
        return __version__
    except Exception:
        return "unknown"


def analyze_aider_internals():
    """Analyze how Aider's repo map works."""
    print("=" * 70)
    print("AIDER INTERNALS ANALYSIS")
    print("=" * 70)
    print()

    try:
        import inspect

        from aider.repomap import RepoMap

        # Get the source file
        source_file = inspect.getfile(RepoMap)
        print(f"Aider RepoMap source: {source_file}")

        # Check the get_repo_map signature
        sig = inspect.signature(RepoMap.get_repo_map)
        print("\nget_repo_map signature:")
        for param_name, param in sig.parameters.items():
            print(f"  {param_name}: {param.annotation if param.annotation != inspect.Parameter.empty else 'Any'}")

        # Check for key methods
        print("\nKey methods:")
        print("  get_ranked_tags - Ranks files using PageRank")
        print("  get_tags - Extracts tags using tree-sitter")
        print("  render_tree - Renders the final map")

        return True

    except Exception as e:
        print(f"Error analyzing Aider: {e}")
        import traceback
        traceback.print_exc()
        return False


def run_aider_repomap(repo_path: str, max_tokens: int = 4096):
    """Run Aider's repo map directly, bypassing console output."""
    print()
    print("=" * 70)
    print("AIDER REPO MAP GENERATION")
    print("=" * 70)
    print()

    original_dir = os.getcwd()
    try:
        from aider.io import InputOutput
        from aider.models import Model
        from aider.repomap import RepoMap

        # Change to repo directory
        os.chdir(repo_path)

        print(f"Repository: {repo_path}")

        # Create a minimal IO object (no actual console output)
        io = InputOutput(
            yes=True,
            pretty=False,
            fancy_input=False,
        )

        # Get model for token counting (use a default)
        # Aider uses model's token counter for sizing
        try:
            main_model = Model("gpt-4")  # Just for token counting
        except Exception:
            main_model = None

        # Find all files in the repo using git
        import subprocess

        # Get git-tracked files
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, cwd=repo_path
        )
        all_files = [f for f in result.stdout.strip().split('\n') if f]
        print(f"Git files found: {len(all_files)}")

        # Time the repo map generation
        start = time.perf_counter()

        # Create RepoMap instance
        rm = RepoMap(
            root=repo_path,
            main_model=main_model,
            io=io,
            map_tokens=max_tokens,
            verbose=False,
        )

        # Generate the repo map
        # get_repo_map(chat_files, other_files, mentioned_fnames=None, mentioned_idents=None, force_refresh=False)
        repo_map = rm.get_repo_map(
            chat_files=[],  # Files in chat
            other_files=all_files,  # All other files to consider
        )

        elapsed_ms = (time.perf_counter() - start) * 1000

        os.chdir(original_dir)

        if repo_map:
            # Count tokens in the output
            try:
                import tiktoken
                enc = tiktoken.get_encoding("cl100k_base")
                output_tokens = len(enc.encode(repo_map))
            except Exception:
                output_tokens = len(repo_map) // 4

            print("\nAider Results:")
            print(f"  Output length: {len(repo_map)} chars")
            print(f"  Output tokens: {output_tokens:,}")
            print(f"  Generation time: {elapsed_ms:.1f}ms")
            print()
            print("First 2000 chars of repo map:")
            print("-" * 50)
            # Replace Unicode chars that can't be printed on Windows
            safe_output = repo_map[:2000].encode('ascii', 'replace').decode('ascii')
            print(safe_output)
            print("-" * 50)

            return {
                'success': True,
                'output': repo_map,
                'output_tokens': output_tokens,
                'time_ms': elapsed_ms,
                'files_considered': len(all_files),
            }
        else:
            print("Aider returned empty repo map")
            return {'success': False, 'error': 'Empty repo map'}

    except Exception as e:
        print(f"Error running Aider: {e}")
        import traceback
        traceback.print_exc()
        os.chdir(original_dir)
        return {'success': False, 'error': str(e)}


def run_attnroute_repomap(repo_path: str, max_tokens: int = 4096):
    """Run attnroute's repo map for comparison."""
    print()
    print("=" * 70)
    print("ATTNROUTE REPO MAP GENERATION")
    print("=" * 70)
    print()

    try:
        import tiktoken
        from repo_map import RepoMapper

        print(f"Repository: {repo_path}")

        # Time the full operation
        start = time.perf_counter()

        mapper = RepoMapper(repo_path, max_files=1000)
        mapper.index(verbose=False)
        repo_map = mapper.get_map(query="main entry point", token_budget=max_tokens)

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Count tokens
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            output_tokens = len(enc.encode(repo_map))
        except Exception:
            output_tokens = len(repo_map) // 4

        files_indexed = len(mapper.file_symbols)
        symbols_found = sum(len(f.symbols) for f in mapper.file_symbols.values())

        print("\nattnroute Results:")
        print(f"  Files indexed: {files_indexed}")
        print(f"  Symbols found: {symbols_found}")
        print(f"  Output length: {len(repo_map)} chars")
        print(f"  Output tokens: {output_tokens:,}")
        print(f"  Generation time: {elapsed_ms:.1f}ms")
        print()
        print("First 2000 chars of repo map:")
        print("-" * 50)
        print(repo_map[:2000])
        print("-" * 50)

        return {
            'success': True,
            'output': repo_map,
            'output_tokens': output_tokens,
            'time_ms': elapsed_ms,
            'files_indexed': files_indexed,
            'symbols_found': symbols_found,
        }

    except Exception as e:
        print(f"Error running attnroute: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


def measure_baseline(repo_path: str):
    """Measure baseline token count."""
    import tiktoken

    print()
    print("=" * 70)
    print("BASELINE MEASUREMENT")
    print("=" * 70)
    print()

    enc = tiktoken.get_encoding("cl100k_base")

    extensions = ['.py', '.go', '.js', '.ts', '.tsx', '.rs', '.java', '.c', '.cpp', '.h']
    total_content = ""
    file_count = 0

    for ext in extensions:
        for file_path in Path(repo_path).rglob(f"*{ext}"):
            if any(part in file_path.parts for part in [
                'node_modules', 'vendor', '.git', '__pycache__', 'venv', '.venv'
            ]):
                continue
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                total_content += content
                file_count += 1
            except Exception:
                continue

    baseline_tokens = len(enc.encode(total_content))

    print(f"Repository: {repo_path}")
    print(f"Source files: {file_count}")
    print(f"Total chars: {len(total_content):,}")
    print(f"Baseline tokens: {baseline_tokens:,}")

    return {
        'files': file_count,
        'chars': len(total_content),
        'tokens': baseline_tokens,
    }


def head_to_head(repo_path: str):
    """Run head-to-head comparison."""
    print()
    print("=" * 70)
    print("AIDER vs ATTNROUTE - HEAD TO HEAD")
    print("=" * 70)
    print(f"Aider version: {get_aider_version()}")
    print(f"Repository: {repo_path}")
    print()

    # Measure baseline
    baseline = measure_baseline(repo_path)

    # Run both tools
    aider_result = run_aider_repomap(repo_path)
    attn_result = run_attnroute_repomap(repo_path)

    # Compare results
    print()
    print("=" * 70)
    print("HEAD-TO-HEAD RESULTS")
    print("=" * 70)
    print()

    print(f"Baseline: {baseline['tokens']:,} tokens ({baseline['files']} files)")
    print()

    print(f"{'Metric':<25} {'Aider':<20} {'attnroute':<20}")
    print("-" * 65)

    if aider_result['success'] and attn_result['success']:
        aider_tokens = aider_result['output_tokens']
        attn_tokens = attn_result['output_tokens']

        aider_reduction = (1 - aider_tokens / baseline['tokens']) * 100
        attn_reduction = (1 - attn_tokens / baseline['tokens']) * 100

        print(f"{'Output tokens':<25} {aider_tokens:,} {' '*10} {attn_tokens:,}")
        print(f"{'Token reduction':<25} {aider_reduction:.2f}% {' '*10} {attn_reduction:.2f}%")
        print(f"{'Time (ms)':<25} {aider_result['time_ms']:.1f} {' '*14} {attn_result['time_ms']:.1f}")

        print()
        print("=" * 70)
        print("WINNER ANALYSIS")
        print("=" * 70)
        print()

        if attn_reduction > aider_reduction:
            print(f"Token Reduction: attnroute wins ({attn_reduction:.2f}% vs {aider_reduction:.2f}%)")
        else:
            print(f"Token Reduction: Aider wins ({aider_reduction:.2f}% vs {attn_reduction:.2f}%)")

        if attn_result['time_ms'] < aider_result['time_ms']:
            speedup = aider_result['time_ms'] / attn_result['time_ms']
            print(f"Speed: attnroute wins ({speedup:.1f}x faster)")
        else:
            speedup = attn_result['time_ms'] / aider_result['time_ms']
            print(f"Speed: Aider wins ({speedup:.1f}x faster)")

    else:
        if not aider_result['success']:
            print(f"Aider: FAILED - {aider_result.get('error', 'Unknown error')}")
        if not attn_result['success']:
            print(f"attnroute: FAILED - {attn_result.get('error', 'Unknown error')}")

    return aider_result, attn_result, baseline


if __name__ == "__main__":
    # Analyze Aider's internals first
    analyze_aider_internals()

    # Run head-to-head on test repo
    if len(sys.argv) > 1:
        repo = sys.argv[1]
    else:
        repo = str(Path.cwd())
        print(f"No repo specified, using current directory: {repo}")
    head_to_head(repo)
