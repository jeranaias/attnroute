# attnroute Benchmarks

## What the benchmarks measure

The benchmarks compare attnroute's context injection size against a **baseline of
all source files in a repository concatenated together**. This measures attnroute's
compression effectiveness -- how much smaller the injected context is compared
to the full codebase.

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
| **attnroute output** | Context string produced by `RepoMapper` for a sample query, tokenized with same tokenizer |
| **Reduction** | `(1 - attnroute_tokens / baseline_tokens) * 100%` |

## Running benchmarks

```bash
# Quick verification on current directory
python benchmarks/verify_claims.py

# Full benchmark with statistical analysis (5 runs per repo)
python benchmarks/bulletproof_benchmark.py --full

# Test on a specific repo
python benchmarks/verify_claims.py /path/to/any/repo
```

## Honest expectations

| Codebase size | Typical reduction |
|---------------|-------------------|
| Small (< 50 files) | 90-97% |
| Medium (50-500 files) | 97-99% |
| Large (500+ files) | 99%+ |

The reduction percentage is higher for larger repos because the denominator
(total codebase tokens) grows while attnroute's output stays within its
token budget.

## Interpreting results

A "98% token reduction" means attnroute's repo map is 98% smaller than all source
files combined. This is useful for understanding compression, but keep in mind:

- Claude Code doesn't read all files anyway -- it selectively reads what it needs
- The practical benefit is that attnroute **pre-selects** relevant files so Claude
  spends fewer tool calls hunting for context
- The real value is in context quality (right files surfaced), not just size reduction
