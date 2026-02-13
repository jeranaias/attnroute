# attnroute Benchmarks

## Quick start

```bash
# Verify claims (token reduction + latency)
python benchmarks/verify_claims.py

# Full pipeline benchmark with component breakdown
python benchmarks/pipeline_benchmark.py

# Test on a specific repo
python benchmarks/verify_claims.py /path/to/any/repo
python benchmarks/pipeline_benchmark.py /path/to/any/repo
```

## What the benchmarks measure

### verify_claims.py
Verifies the two headline claims:
1. **Token reduction >= 90%** — RepoMapper output vs all source files concatenated
2. **Latency <= 500ms** — warm median (after cold start)

Separates cold start (first run, includes indexing) from warm runs (subsequent).
Reports which optional dependencies are missing and how they affect results.

### pipeline_benchmark.py
Tests the **actual hook pipeline** that runs on every `UserPromptSubmit`:
- Full `context_router.py` path: state load → attention update → context build → state save
- Per-component latency breakdown (what's slow and why)
- SearchIndex query performance (BM25 + semantic rerank)
- RepoMapper compression (symbol extraction)
- Cold vs warm separation for all components

### bulletproof_benchmark.py
Statistical benchmark with confidence intervals, multiple tokenizers,
and optional Aider head-to-head comparison.

```bash
python benchmarks/bulletproof_benchmark.py --runs 10
python benchmarks/bulletproof_benchmark.py --repos /path/to/repo1 /path/to/repo2
```

## What the benchmarks do NOT measure

- **Comparison to Claude Code's native behavior**: Claude Code selectively reads files
  via tool calls (`Read`, `Grep`, `Glob`), not by dumping the entire codebase into
  context. The "all files concatenated" baseline is a theoretical maximum, not what
  actually happens without attnroute.
- **Task completion quality**: Whether the right files were selected for a given task.
- **End-to-end session performance**: Whether attnroute makes coding sessions faster
  or more accurate overall.

## Baseline methodology

| Component | Method |
|-----------|--------|
| **Baseline** | All source files (`.py`, `.js`, `.ts`, `.go`, etc.) concatenated, tokenized with tiktoken `cl100k_base` |
| **attnroute output** | Context string produced by the pipeline for a sample query |
| **Reduction** | `(1 - output_tokens / baseline_tokens) * 100%` |
| **Latency** | Wall clock time, separated into cold (first run) and warm (subsequent runs) |

## Dependency impact on results

| Dependency | Impact when missing |
|-----------|---------------------|
| `tiktoken` | Token counts use char/4 estimate instead of real tokenizer |
| `tree-sitter-languages` | Symbol extraction uses regex fallback (3-5x slower) |
| `bm25s` | No BM25 search, falls back to keyword matching only |
| `model2vec` | No semantic reranking of search results |
| `networkx` | No transitive co-activation (2-hop graph boost) |

Install all: `pip install attnroute[all]`

## Honest expectations

| Codebase size | Typical reduction | Note |
|---------------|-------------------|------|
| Small (< 20 files) | 80-95% | Lower because denominator is small |
| Medium (20-200 files) | 95-98% | Sweet spot for most projects |
| Large (200+ files) | 98-99%+ | Denominator grows, output stays budgeted |

The reduction percentage is higher for larger repos because the denominator
(total codebase tokens) grows while attnroute's output stays within its
token budget (default 2000 tokens for repo map).

## Interpreting latency

- **Cold start**: First run includes file discovery, indexing, and symbol extraction. This is a one-time cost.
- **Warm runs**: Subsequent runs reuse cached data. This is what users experience on most prompts.
- **tree-sitter-languages**: If not installed, regex fallback is used for symbol extraction, which is 3-5x slower. The 500ms claim assumes tree-sitter is available.
- **Python version**: `tree-sitter-languages` may not have wheels for the latest Python (e.g., 3.14). Check PyPI for compatibility.
