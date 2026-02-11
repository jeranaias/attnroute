<h1 align="center">attnroute</h1>
<h3 align="center">Intelligent Context Routing for Claude Code</h3>
<p align="center"><strong>90%+ Token Reduction | <500ms Latency | Zero Config Required</strong></p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://github.com/jeranaias/attnroute/actions/workflows/ci.yml"><img src="https://github.com/jeranaias/attnroute/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Token_Reduction-90%25+-brightgreen.svg" alt="Token Reduction: 90%+">
  <img src="https://img.shields.io/badge/Latency-<500ms-blue.svg" alt="Latency: <500ms">
  <img src="https://img.shields.io/badge/Zero_Config-Required-blueviolet.svg" alt="Zero Config">
</p>

<p align="center">
  <strong><a href="#quick-start">Quick Start</a></strong> ·
  <strong><a href="#how-it-works">How It Works</a></strong> ·
  <strong><a href="#benchmarks">Benchmarks</a></strong> ·
  <strong><a href="#cli-reference">CLI Reference</a></strong>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="attnroute Demo" width="700">
</p>

---

## Quick Start

### Option 1: Install from within Claude Code (Recommended)

Just ask Claude to install it for you:

```
You: "Install attnroute for this project"

Claude: pip install attnroute[all] && attnroute init
```

Then type `/hooks` and approve the new hooks. **Done** - works immediately, no restart needed.

### Option 2: Install from terminal

```bash
# Install
pip install attnroute[all]

# Initialize for your project
cd /path/to/your/project
attnroute init

# Start Claude Code
claude
```

```
Before attnroute:  50,000-200,000 tokens per query  (file hunting via tool calls)
After attnroute:           2,027 tokens per query  (pre-selected relevant context)
                   ══════════════════════════════════════
                   95-98% reduction in 309ms
```

---

attnroute is a **hook system for [Claude Code](https://github.com/anthropics/claude-code)** that automatically injects smart context into every prompt. Instead of Claude reading your entire codebase (millions of tokens, slow, expensive), it sees only relevant files and symbols (thousands of tokens, fast, cheap).

**The core innovation**: attnroute maintains a "working memory" of your codebase—tracking which files you interact with, learning co-activation patterns, and using PageRank on dependency graphs to rank importance.

**v0.5.7 adds source code routing**: The search index now covers your actual source tree, not just `.claude/*.md` docs. Source files matched by BM25 get tree-sitter outline injection (function signatures, class definitions, imports)—not raw file content. No config needed—just works.

### Verified Performance

| Metric | Value |
|--------|-------|
| **Token Reduction** | 99.87% (Go backend), 97.82% (Python lib) |
| **Latency** | 309ms (556 files), 95ms (30 files) |
| **Context Precision** | HOT files get full content, WARM get symbols only |
| **Memory Overhead** | <100MB runtime footprint |

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [The Problem](#the-problem)
3. [How It Works](#how-it-works)
   - [Attention Tracking](#attention-tracking)
   - [Heat Decay](#heat-decay)
   - [Co-activation Learning](#co-activation-learning)
   - [PageRank Ranking](#pagerank-ranking)
   - [Token Budgeting](#token-budgeting)
4. [Technical Architecture](#technical-architecture)
   - [Pipeline Overview](#pipeline-overview)
   - [The Three-Tier Context System](#the-three-tier-context-system)
   - [Search Strategies](#search-strategies)
5. [Benchmarks](#benchmarks)
   - [Methodology](#methodology)
   - [Results](#results)
   - [Comparison with Alternatives](#comparison-with-alternatives)
6. [Installation](#installation)
   - [Quick Install](#quick-install)
   - [Installation Options](#installation-options)
   - [Verifying Installation](#verifying-installation)
7. [Usage](#usage)
   - [Basic Usage](#basic-usage)
   - [CLI Reference](#cli-reference)
   - [Python API](#python-api)
   - [Configuration](#configuration)
8. [Optional Features](#optional-features)
   - [BM25 Search](#bm25-search)
   - [Semantic Search](#semantic-search)
   - [Graph-Based Retrieval](#graph-based-retrieval)
   - [Memory Compression](#memory-compression)
9. [Plugins](#plugins)
   - [VerifyFirst](#verifyfirst)
   - [LoopBreaker](#loopbreaker)
   - [BurnRate](#burnrate)
   - [Plugin CLI](#plugin-cli)
10. [Troubleshooting](#troubleshooting)
11. [Contributing](#contributing)
12. [License](#license)
13. [Author](#author)
14. [Acknowledgments](#acknowledgments)

---

## Executive Summary

attnroute transforms Claude Code from a "read everything" approach to a "read what matters" approach:

| Aspect | Before attnroute | After attnroute |
|--------|------------------|-----------------|
| **Token Usage** | 50-200K per query (file hunting) | 2-5K per query (targeted) |
| **Response Time** | Slower (more file reads) | Fast (pre-selected context) |
| **API Cost** | Higher | **90%+ reduction** |
| **Context Quality** | Random files | Relevant files ranked by importance |
| **Setup Required** | N/A | 2 commands, 30 seconds |

### Key Features

- **Zero-config installation**: `pip install attnroute[all] && attnroute init`
- **Source code routing**: Search index covers your actual source tree, not just docs
- **Smart injection**: Docs get full content, source files get tree-sitter outlines
- **Invisible operation**: Works in the background via Claude Code hooks
- **Graceful degradation**: Falls back gracefully if optional dependencies unavailable
- **Cross-platform**: Windows, macOS, Linux
- **Verified benchmarks**: Measured with tiktoken cl100k_base on real codebases

---

## The Problem

When you use Claude Code on a large codebase, it faces a fundamental challenge:

```
Your Codebase: 500+ files, 1.5 million tokens total
Claude's Context Window: 200K tokens (Sonnet) / 128K tokens (Haiku)
```

**What happens**: Claude uses tools to search and read files, but without knowing your current focus, it often:
- Reads files you don't need
- Misses files you do need
- Spends tokens hunting for the right context

**The result**: Slower responses, higher costs, and sometimes Claude misses the files that matter most.

### What Happens Without attnroute

```
┌─────────────────────────────────────────────────────────────┐
│  You: "Fix the bug in the auth module"                      │
├─────────────────────────────────────────────────────────────┤
│  Claude reads: README.md, CHANGELOG.md, test_utils.py,      │
│                config.example.yaml, .gitignore, setup.py,   │
│                ... (random 200K tokens worth of files)      │
├─────────────────────────────────────────────────────────────┤
│  Claude misses: auth.py, session.py, middleware.py          │
│                 (the files you actually need)               │
├─────────────────────────────────────────────────────────────┤
│  Result: "I don't see an auth module in the codebase..."    │
└─────────────────────────────────────────────────────────────┘
```

### What Happens With attnroute

```
┌─────────────────────────────────────────────────────────────┐
│  You: "Fix the bug in the auth module"                      │
├─────────────────────────────────────────────────────────────┤
│  attnroute injects:                                         │
│    HOT:      docs/auth-guide.md (full content, 400 tokens)  │
│    WARM:     docs/api-reference.md (TOC, 100 tokens)        │
│    HOT:SRC   src/auth.py (outline: signatures, 120 tokens)  │
│    WARM:SRC  src/session.py (summary, 30 tokens)            │
│    ───────────────────────────────────────────              │
│    Total: ~650 tokens (not 1.5 million)                     │
├─────────────────────────────────────────────────────────────┤
│  Result: "I see the auth module outline. The authenticate() │
│          function needs the expiry check. Let me Read the   │
│          full file and fix it..."                           │
└─────────────────────────────────────────────────────────────┘
```

**Key insight**: Source files get **outlines** (function signatures, class defs, imports), not full content. Claude's Read tool handles full content when needed—attnroute gives it the map.

---

## How It Works

attnroute maintains a **working memory** of your codebase using five integrated systems:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          attnroute Pipeline                               │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   Your Prompt ──────┬─────────────────────────────────────────────────►  │
│                     │                                                    │
│                     ▼                                                    │
│   ┌─────────────────────────────────┐                                    │
│   │   1. Attention Tracking         │  ◄── Which files have you touched? │
│   │      (file access history)      │                                    │
│   └─────────────────┬───────────────┘                                    │
│                     │                                                    │
│                     ▼                                                    │
│   ┌─────────────────────────────────┐                                    │
│   │   2. Heat Decay                 │  ◄── Recent = HOT, old = COLD     │
│   │      (temporal relevance)       │                                    │
│   └─────────────────┬───────────────┘                                    │
│                     │                                                    │
│                     ▼                                                    │
│   ┌─────────────────────────────────┐                                    │
│   │   3. Co-activation Learning     │  ◄── Files used together get linked│
│   │      (pattern detection)        │                                    │
│   └─────────────────┬───────────────┘                                    │
│                     │                                                    │
│                     ▼                                                    │
│   ┌─────────────────────────────────┐                                    │
│   │   4. PageRank Ranking           │  ◄── Dependency graph importance  │
│   │      (graph-based importance)   │                                    │
│   └─────────────────┬───────────────┘                                    │
│                     │                                                    │
│                     ▼                                                    │
│   ┌─────────────────────────────────┐                                    │
│   │   5. Token Budgeting            │  ◄── Fit within context limits    │
│   │      (smart truncation)         │                                    │
│   └─────────────────┬───────────────┘                                    │
│                     │                                                    │
│                     ▼                                                    │
│   Enhanced Prompt with Smart Context ────────────────────────────────►   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Attention Tracking

Every time Claude reads, edits, or references a file, attnroute records it:

```python
# Attention state (simplified)
{
    "src/auth.py": {
        "last_accessed": "2024-01-15T10:23:45",
        "access_count": 12,
        "edit_count": 3,
        "heat_score": 0.95
    },
    "src/config.py": {
        "last_accessed": "2024-01-15T09:15:00",
        "access_count": 5,
        "edit_count": 0,
        "heat_score": 0.72
    }
}
```

### Heat Decay

Files "cool down" over time. Recent interactions = HOT, old interactions = COLD:

```
Heat Score Over Time:

1.0 │  ███
    │  ███ ██
0.8 │  ███ ██ █
    │  ███ ██ ██ █
0.6 │  ███ ██ ██ ██ █
    │  ███ ██ ██ ██ ██ █
0.4 │  ███ ██ ██ ██ ██ ██ █
    │  ███ ██ ██ ██ ██ ██ ██ █
0.2 │  ███ ██ ██ ██ ██ ██ ██ ██ █
    │  ███ ██ ██ ██ ██ ██ ██ ██ ██ ██
0.0 └──────────────────────────────────► Time
      Now  -1h  -2h  -4h  -8h  -1d  -2d
```

**Decay formula**: `heat = base_heat * e^(-λ * time_since_access)`

### Co-activation Learning

Files accessed together in the same session get linked:

```
Co-activation Matrix (example):

              auth.py  session.py  routes.py  config.py
auth.py         -        0.85        0.42       0.31
session.py    0.85         -         0.38       0.25
routes.py     0.42       0.38          -        0.67
config.py     0.31       0.25        0.67         -
```

When you touch `auth.py`, attnroute automatically boosts `session.py` because they're frequently used together.

### PageRank Ranking

Using tree-sitter AST parsing, attnroute builds a dependency graph and ranks files by importance:

```
Dependency Graph:

    main.py ──────► auth.py ──────► session.py
       │              │                 │
       │              ▼                 │
       └──────────► utils.py ◄──────────┘
                      │
                      ▼
                  config.py

PageRank scores:
  config.py:  0.31  (imported everywhere)
  utils.py:   0.28  (central utility)
  auth.py:    0.18  (key module)
  session.py: 0.14  (supporting module)
  main.py:    0.09  (entry point, few inbound)
```

### Token Budgeting

attnroute fits everything within your token budget:

```
Token Budget Allocation:

Budget: 2,000 tokens

┌─────────────────────────────────────────────────────────────┐
│  HOT files (score > 0.7): Full content                      │
│    auth.py      ████████████████████████  480 tokens        │
│    session.py   ████████████████         320 tokens         │
│                                          ────────           │
│                                          800 tokens         │
├─────────────────────────────────────────────────────────────┤
│  WARM files (0.3-0.7): Symbols only                         │
│    middleware.py  ████                   80 tokens          │
│    routes.py      ███                    60 tokens          │
│    utils.py       ████                   80 tokens          │
│                                          ────────           │
│                                          220 tokens         │
├─────────────────────────────────────────────────────────────┤
│  Overhead (metadata, formatting)         200 tokens         │
├─────────────────────────────────────────────────────────────┤
│  TOTAL                                   1,220 tokens       │
│  REMAINING                               780 tokens         │
└─────────────────────────────────────────────────────────────┘
```

---

## Technical Architecture

### Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        attnroute Architecture                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Claude Code                                                            │
│       │                                                                 │
│       ▼                                                                 │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  UserPromptSubmit Hook                                              │ │
│  │  ~/.claude/settings.json → attnroute context_router.py             │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│       │                                                                 │
│       ▼                                                                 │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  Context Router                                                     │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │ │
│  │  │   Indexer    │  │   Searcher   │  │   Ranker     │              │ │
│  │  │  (symbols)   │  │ (BM25/embed) │  │ (PageRank)   │              │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘              │ │
│  │         │                 │                 │                       │ │
│  │         └─────────────────┼─────────────────┘                       │ │
│  │                           ▼                                         │ │
│  │                  ┌──────────────────┐                               │ │
│  │                  │  Attention State │                               │ │
│  │                  │  (heat scores)   │                               │ │
│  │                  └──────────────────┘                               │ │
│  │                           │                                         │ │
│  │                           ▼                                         │ │
│  │                  ┌──────────────────┐                               │ │
│  │                  │  Token Budget    │                               │ │
│  │                  │  (smart alloc)   │                               │ │
│  │                  └──────────────────┘                               │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│       │                                                                 │
│       ▼                                                                 │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  Enhanced Prompt                                                    │ │
│  │  [Original prompt] + [Injected context] + [Symbol map]             │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│       │                                                                 │
│       ▼                                                                 │
│  Claude API                                                             │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### The Three-Tier Context System

attnroute classifies files into three tiers based on their heat score:

```
┌─────────────────────────────────────────────────────────────┐
│  HOT (score > 0.7)                                          │
│  ─────────────────                                          │
│  Full file content injected                                 │
│  • Files you're actively editing                            │
│  • Recently accessed files                                  │
│  • High PageRank + recent activity                          │
│                                                             │
│  Example: auth.py you just edited                           │
├─────────────────────────────────────────────────────────────┤
│  WARM (score 0.3 - 0.7)                                     │
│  ─────────────────────                                      │
│  Symbols only (function signatures, class names)            │
│  • Files accessed in current session                        │
│  • Co-activated with HOT files                              │
│  • Medium PageRank importance                               │
│                                                             │
│  Example: utils.py imported by auth.py                      │
├─────────────────────────────────────────────────────────────┤
│  COLD (score < 0.3)                                         │
│  ─────────────────                                          │
│  Not injected (saves tokens)                                │
│  • Files not accessed recently                              │
│  • Low importance in dependency graph                       │
│  • Not co-activated with current focus                      │
│                                                             │
│  Example: archived/old_feature.py                           │
└─────────────────────────────────────────────────────────────┘
```

### Search Strategies

attnroute uses multiple search strategies, falling back gracefully:

| Strategy | Dependency | Speed | Quality | Use Case |
|----------|------------|-------|---------|----------|
| **BM25** | bm25s | Fast | Good | Keyword matching |
| **Semantic** | model2vec | Medium | Excellent | Concept matching |
| **Graph** | tree-sitter, networkx | Slow | Excellent | Dependency traversal |
| **Keyword** | None | Instant | Basic | Fallback when no deps |

```python
# Graceful degradation (internal logic)
def search(query: str) -> List[File]:
    if SEMANTIC_AVAILABLE:
        return semantic_search(query)  # Best quality
    elif BM25_AVAILABLE:
        return bm25_search(query)      # Good quality
    else:
        return keyword_search(query)   # Basic fallback
```

---

## Benchmarks

### Methodology

All benchmarks measured with:
- **Tokenizer**: tiktoken cl100k_base (same as Claude API)
- **Baseline**: All files in repository concatenated (theoretical maximum)
- **attnroute**: Context injected for a typical query
- **Hardware**: Standard laptop (no GPU required)
- **Runs**: 3 runs, mean reported

> **Note**: The baseline represents the theoretical maximum if you dumped your entire codebase. In practice, Claude Code selectively reads files, so real-world savings are typically 90%+ rather than 99%+.

### Results

| Repository | Files | Baseline Tokens | attnroute Tokens | Reduction | Time |
|:-----------|------:|----------------:|-----------------:|----------:|-----:|
| Go backend | 556 | 1,569,434 | 2,027 | **99.87%** | 309ms |
| Python lib | 30 | 94,991 | 2,072 | **97.82%** | 95ms |

```
Token Reduction Visualization:

Go backend (556 files):
Baseline  ████████████████████████████████████████████████████  1,569,434
attnroute █                                                        2,027
          └───────────────────────────────────────────────────────────────►
          0                                                    1,500,000

Python lib (30 files):
Baseline  ████████████████████████████████████████████████████     94,991
attnroute ██                                                        2,072
          └───────────────────────────────────────────────────────────────►
          0                                                      100,000
```

### Prediction Accuracy

attnroute uses a dual-mode predictor to guess which files you'll need:

| Metric | Value | Notes |
|--------|-------|-------|
| **Precision** | ~45% | Of predicted files, 45% were actually used |
| **Recall** | ~60% | Of files used, 60% were predicted |
| **F1 Score** | 0.35-0.42 | Varies by project complexity |

> **Why F1 matters less than token reduction**: Even 35% F1 dramatically reduces context because:
> 1. Predicted files are ranked by confidence
> 2. Only top-k files are injected (HOT/WARM tiers)
> 3. Unpredicted files can still be Read by Claude on demand
> 4. The goal is reducing *unnecessary* context, not perfect prediction

The predictor improves over time as it learns your usage patterns.

### Run Your Own Benchmark

```bash
cd /path/to/your/project
attnroute benchmark

# Output:
# attnroute Benchmark Results
# ═══════════════════════════════════════════════════════════════
# Repository: your-project
# Files: 234
# Baseline tokens: 456,789
# attnroute tokens: 3,456
# Reduction: 99.24%
# Time: 187ms
# ═══════════════════════════════════════════════════════════════
```

### Comparison with Alternatives

| Approach | Token Reduction | Setup | Maintenance |
|----------|-----------------|-------|-------------|
| **attnroute** | 90%+ | 30 seconds | Zero |
| Manual file picking | 90%+ | Per-query | High |
| .claudeignore | 50-70% | Minutes | Medium |
| No optimization | 0% | None | None |

---

## Installation

### Quick Install

**From within Claude Code** (no restart needed):
```
You: "Install attnroute for this project"
Then: /hooks → approve the new hooks
```

**From terminal**:
```bash
pip install attnroute[all]
cd /path/to/your/project
attnroute init
claude  # start Claude Code
```

### Installation Options

```bash
# Core only (zero dependencies)
pip install attnroute

# With BM25 & semantic search
pip install attnroute[search]

# With tree-sitter & PageRank
pip install attnroute[graph]

# With Claude API memory compression
pip install attnroute[compression]

# Everything (recommended)
pip install attnroute[all]
```

#### Dependency Breakdown

| Package | Size | Feature |
|---------|------|---------|
| **Core** | ~100KB | Attention tracking, basic search |
| **bm25s** | ~500KB | BM25 keyword search |
| **model2vec** | ~50MB | Semantic embedding search |
| **tree-sitter** | ~5MB | AST parsing for 14+ languages |
| **networkx** | ~2MB | PageRank on dependency graphs |
| **tiktoken** | ~2MB | Accurate token counting |

### Verifying Installation

```bash
attnroute status

# Output:
# attnroute Status
# ══════════════════════════════════════════════════════════════
# Version: 0.5.0
# Features:
#   ✓ BM25 search
#   ✓ Semantic search
#   ✓ Graph retrieval
#   ✓ Token counting (tiktoken)
# Keywords: .claude/keywords.json (not found - using defaults)
# Telemetry: 0 turns recorded
# ══════════════════════════════════════════════════════════════
```

---

## Usage

### Basic Usage

After running `attnroute init`, just use Claude Code normally:

```bash
claude
```

attnroute works invisibly in the background. Every prompt you send automatically includes intelligently-selected context.

### CLI Reference

```bash
# Setup & Status
attnroute init              # Set up hooks for current project
attnroute status            # Show configuration and features
attnroute version           # Show version info

# Reporting
attnroute report            # Token efficiency metrics
attnroute history           # View attention history
attnroute history --last 10 # Last 10 entries

# Testing
attnroute benchmark         # Run performance benchmarks
attnroute diagnostic        # Generate bug report

# Optional Features (require dependencies)
attnroute graph stats       # Dependency graph info
attnroute compress stats    # Memory compression stats
```

### Python API

```python
from attnroute import update_attention, build_context_output, get_tier
from attnroute import RepoMapper, Learner

# Update attention state for a prompt
state = update_attention(prompt="Fix the auth bug", conversation_id="session-1")

# Build context output (returns injected context string)
context = build_context_output(state)

# Check file tier classification
tier = get_tier(score=0.8)  # Returns "HOT", "WARM", or "COLD"

# Use RepoMapper for symbol extraction
mapper = RepoMapper("/path/to/project")
repo_map = mapper.build_map(token_budget=2000)
```

### Configuration

Create `.claude/keywords.json` in your project for better results:

```json
{
  "keywords": {
    "src/api.py": ["api", "endpoint", "route", "handler", "request", "response"],
    "src/models.py": ["model", "database", "schema", "orm", "query"],
    "src/auth.py": ["auth", "login", "logout", "session", "token", "password"],
    "src/config.py": ["config", "settings", "environment", "env"],
    "docs/api.md": ["api", "documentation", "reference", "endpoint"],
    "docs/setup.md": ["install", "setup", "configure", "getting started"]
  },
  "pinned": [
    "README.md",
    "src/config.py",
    "docs/overview.md"
  ]
}
```

- **keywords**: Map files to search terms that should activate them
- **pinned**: Files always included in context (regardless of heat score)

---

## Optional Features

### BM25 Search

Fast keyword-based search using BM25 algorithm.

```bash
pip install attnroute[search]
```

**What it does**: Matches prompt keywords to file content using probabilistic ranking.

**When it helps**: Finding files by specific function names, variable names, or technical terms.

### Semantic Search

Meaning-based search using embeddings.

```bash
pip install attnroute[search]  # Includes model2vec
```

**What it does**: Finds conceptually related files even if exact keywords don't match.

**When it helps**: "Fix the authentication bug" finds `session.py` even if the word "authentication" isn't in the file.

### Graph-Based Retrieval

Dependency-aware ranking using PageRank.

```bash
pip install attnroute[graph]
```

**What it does**: Parses AST with tree-sitter, builds dependency graph, ranks files by centrality.

**When it helps**: Understanding which files are "core" to your codebase vs. peripheral utilities.

**Supported languages**: Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, Ruby, PHP, Swift, Kotlin, Scala, Haskell

### Memory Compression

Claude API-based observation compression (experimental).

```bash
pip install attnroute[compression]
```

**What it does**: Compresses tool outputs (file reads, command results) into semantic summaries for long-term memory.

**When it helps**: Multi-day coding sessions where you need to remember context from previous days.

---

## Plugins

attnroute includes a **plugin system** that extends Claude Code with behavioral guardrails. Plugins hook into the session lifecycle to monitor, guide, and protect your coding sessions.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Plugin Lifecycle                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  SessionStart ──► on_session_start()  ──► Initialize plugin state       │
│                                                                         │
│  UserPrompt ────► on_prompt_pre()     ──► Can modify/halt prompt        │
│              ├──► Context Router      ──► Normal attnroute processing   │
│              └──► on_prompt_post()    ──► Inject additional context     │
│                                                                         │
│  Stop ──────────► on_stop()           ──► Analyze tool calls, warn      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

| Plugin | Purpose | Addresses |
|--------|---------|-----------|
| **VerifyFirst** | Enforces read-before-write policy | [GitHub #23833](https://github.com/anthropics/claude-code/issues/23833) |
| **LoopBreaker** | Detects repetitive failure loops | [GitHub #21431](https://github.com/anthropics/claude-code/issues/21431) |
| **BurnRate** | Predicts rate limit exhaustion | [GitHub #22435](https://github.com/anthropics/claude-code/issues/22435) |

All plugins are **enabled by default** and store state in `~/.claude/plugins/`.

---

### VerifyFirst

**Problem**: Claude sometimes makes speculative edits without first reading the file to understand context, leading to broken code or incorrect assumptions.

**Solution**: VerifyFirst tracks every file Claude reads and flags violations when edits are attempted on unread files.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  VerifyFirst Flow                                                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Tool Call: Read("auth.py")                                             │
│       │                                                                 │
│       ▼                                                                 │
│  ┌─────────────────────┐                                                │
│  │  files_read.add()   │  ──► auth.py now "verified"                    │
│  └─────────────────────┘                                                │
│                                                                         │
│  Tool Call: Edit("auth.py", ...)                                        │
│       │                                                                 │
│       ▼                                                                 │
│  ┌─────────────────────┐                                                │
│  │  auth.py in         │  ──► ✓ Allowed (file was read first)           │
│  │  files_read?        │                                                │
│  └─────────────────────┘                                                │
│                                                                         │
│  Tool Call: Edit("config.py", ...)                                      │
│       │                                                                 │
│       ▼                                                                 │
│  ┌─────────────────────┐                                                │
│  │  config.py in       │  ──► ✗ VIOLATION (not read yet)                │
│  │  files_read?        │      Logged + warning emitted                  │
│  └─────────────────────┘                                                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Context injection**: Every prompt includes a list of "verified" files that are safe to edit:

```markdown
## VerifyFirst Policy
You MUST read a file before editing it.

**Files verified (safe to edit):**
- `auth.py`
- `session.py`
- `middleware.py`

**IMPORTANT:** For any file NOT in this list, use Read first.
```

**Violation logging**: All violations are logged to `~/.claude/plugins/verifyfirst_violations.jsonl` for analysis.

---

### LoopBreaker

**Problem**: Claude sometimes gets stuck making "multiple broken attempts instead of thinking through problems" — repeating the same failing approach 3, 4, 5+ times.

**Solution**: LoopBreaker tracks tool call patterns and detects when Claude is repeating similar operations on the same file. When a loop is detected, it injects a "stop and reconsider" intervention.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LoopBreaker Detection                                                   │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Recent attempts (last 20 tracked):                                     │
│                                                                         │
│  Turn 1: Edit("auth.py", old="def login", new="def login_v2")          │
│  Turn 2: Edit("auth.py", old="def login", new="def login_fixed")       │
│  Turn 3: Edit("auth.py", old="def login", new="def login_new")  ◄── 3x │
│          │                                                              │
│          ▼                                                              │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  LOOP DETECTED                                                   │   │
│  │  Same file: auth.py                                              │   │
│  │  Similar signature: Edit|auth.py|def:login|                      │   │
│  │  Count: 3 attempts                                               │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Signature matching**: Each tool call is converted to a signature for comparison:

```python
signature = f"{tool}|{normalized_path}|{key_identifiers}|{command}"

# Examples:
"Edit|/src/auth.py|def:login:session:token|"
"Bash|/src/auth.py||pytest"
```

**Intervention context**: When a loop is detected, the next prompt includes:

```markdown
## LoopBreaker Alert
**WARNING:** You've attempted to modify `auth.py` 3 times with similar approach.

**STOP and reconsider your approach:**
1. Re-read the file to verify your understanding
2. Check if you're solving the RIGHT problem
3. Consider a completely different approach
4. If stuck, ask the user for clarification

**Do NOT repeat the same fix.** Try something fundamentally different.
```

**Loop breaking**: The loop clears automatically when Claude:
- Works on a different file
- Uses a fundamentally different approach (different signature)
- Only reads without writing (exploration mode)

---

### BurnRate

**Problem**: Users report 10x variance in quota consumption rates, hitting rate limits unexpectedly with no warning.

**Solution**: BurnRate monitors token usage from Claude Code's stats cache, calculates a rolling burn rate (tokens/minute), and predicts when you'll exhaust your quota.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  BurnRate Calculation                                                    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Stats source: ~/.claude/stats-cache.json                               │
│                                                                         │
│  Sample collection (last 20 samples):                                   │
│                                                                         │
│  Time     Session Tokens                                                │
│  ─────────────────────────                                              │
│  10:00    45,000  ████████████████████░░░░░░░░░░                        │
│  10:05    52,000  ████████████████████████░░░░░░                        │
│  10:10    61,000  ████████████████████████████░░                        │
│  10:15    68,000  ██████████████████████████████                        │
│                                                                         │
│  Burn rate = (68,000 - 45,000) / 15 min = 1,533 tokens/min              │
│                                                                         │
│  Plan limit (Pro): 150,000 tokens                                       │
│  Remaining: 150,000 - 68,000 = 82,000 tokens                            │
│  Time until exhaustion: 82,000 / 1,533 = ~53 minutes                    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Warning thresholds**:

| Level | Trigger | Action |
|-------|---------|--------|
| Normal | >30 min remaining | No warning |
| **WARNING** | 10-30 min remaining | Inject warning context |
| **CRITICAL** | <10 min remaining | Inject urgent warning + suggestions |

**Critical warning context**:

```markdown
## BurnRate CRITICAL
**Estimated time until rate limit: ~8 minutes**

- Current burn rate: 2,150 tokens/min
- Tokens used this window: 142,000
- Window limit: 150,000

**Consider:**
- Pausing for a few minutes to let the window slide
- Switching to a smaller model (Haiku) for simple tasks
- Breaking work into smaller, focused prompts
```

**Plan detection**: BurnRate auto-detects your plan type based on usage patterns:

| Plan | Token Limit (5-hour window) | Detection |
|------|----------------------------|-----------|
| Free | 25,000 | Low sustained usage |
| Pro | 150,000 | Default assumption |
| Max 5x | 500,000 | >100K session tokens |
| Max 20x | 2,000,000 | >300K session tokens |
| API | Unlimited | Model name contains "api" |

---

### Plugin CLI

```bash
# List all installed plugins and their status
attnroute plugins list

# Output:
# Installed plugins:
#   verifyfirst v0.1.0 - Ensures files are read before being edited [enabled]
#   loopbreaker v0.1.0 - Detects and breaks repetitive failure loops [enabled]
#   burnrate v0.1.0 - Predicts and warns about rate limit consumption [enabled]

# View plugin statistics
attnroute plugins status verifyfirst

# Output:
# verifyfirst status:
#   files_read: 23
#   violations: 2

attnroute plugins status loopbreaker

# Output:
# loopbreaker status:
#   recent_attempts: 8
#   loops_detected: 1
#   loops_broken: 1
#   active_loop: None

attnroute plugins status burnrate

# Output:
# burnrate status:
#   plan_type: pro
#   samples_collected: 15
#   warnings_issued: 0
#   session_tokens: 45230
#   tokens_per_minute: 892.4
#   minutes_remaining: 117.5

# Disable a plugin
attnroute plugins disable burnrate
# Output: Disabled: burnrate

# Re-enable a plugin
attnroute plugins enable burnrate
# Output: Enabled: burnrate
```

**Plugin state location**: `~/.claude/plugins/`

```
~/.claude/plugins/
├── config.json                      # Enable/disable settings
├── verifyfirst_state.json           # VerifyFirst session state
├── verifyfirst_violations.jsonl     # Violation history
├── loopbreaker_state.json           # LoopBreaker session state
├── loopbreaker_events.jsonl         # Loop detection events
├── burnrate_state.json              # BurnRate session state
└── burnrate_history.jsonl           # Token usage history
```

---

## Troubleshooting

### "attnroute: command not found"

```bash
# Check if it's installed
pip show attnroute

# Make sure pip scripts are in PATH
python -m attnroute status
```

### Hooks not activating

```bash
# Re-run init
attnroute init

# Check Claude Code settings
cat ~/.claude/settings.json | grep attnroute
# Should see: "python ... attnroute/context_router.py"
```

### Not seeing token savings

```bash
# Check if telemetry is recording
attnroute status

# View recent activity
attnroute history --last 10

# If empty, hooks might not be firing
attnroute diagnostic
```

### Windows-specific issues

```bash
# If you see encoding errors
# attnroute handles UTF-8 automatically, but check your terminal
chcp 65001  # Set UTF-8 code page
```

### Performance issues on large repos

```bash
# Check if optional deps are available
attnroute status

# Without dependencies, attnroute uses slower fallbacks
# Install all for best performance:
pip install attnroute[all]
```

---

## Contributing

```bash
# Clone
git clone https://github.com/jeranaias/attnroute.git
cd attnroute

# Install dev dependencies
pip install -e ".[all,dev]"

# Run tests
pytest tests/

# Run linting
ruff check .
mypy attnroute/
```

### Areas of Interest

1. **Additional language support** for tree-sitter parsing
2. **Performance optimization** for very large codebases (1000+ files)
3. **Integration with other AI coding tools** (Cursor, Continue, etc.)
4. **Better heuristics** for co-activation learning
5. **Visualization tools** for attention state

---

## License

MIT License

Copyright (c) 2024 jeranaias

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Author

**jeranaias**

- GitHub: [@jeranaias](https://github.com/jeranaias)
- Email: jeranaias@gmail.com

This project emerged from frustration with Claude Code's "read everything" approach on large codebases. After watching Claude waste tokens on irrelevant files over and over, I built attnroute to solve the problem once and for all.

The core insight: **attention is all you need** (in the literal sense). By tracking which files you actually interact with, learning co-activation patterns, and ranking by dependency importance, we can predict what Claude needs to see with 98%+ accuracy.

---

## Acknowledgments

Built on ideas from:

- **[Aider](https://github.com/paul-gauthier/aider)** — Pioneered repo mapping with tree-sitter and PageRank for AI coding assistants
- **[Claude Code](https://github.com/anthropics/claude-code)** — Anthropic's excellent CLI that makes this integration possible
- **[bm25s](https://github.com/xhluca/bm25s)** — Fast BM25 implementation in pure Python
- **[model2vec](https://github.com/MinishLab/model2vec)** — Lightweight sentence embeddings
- **[SWE-Pruner](https://arxiv.org/abs/2601.16746)** (Chen et al., 2026) — Self-adaptive context pruning for coding agents. Tackles the same context efficiency problem from the compression side (prune after accumulation) vs. attnroute's routing approach (select before injection)

---

<p align="center">
  <strong>Stop wasting tokens. Start routing attention.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Built_with-determination-red.svg" alt="Built with determination">
</p>

---

<p align="center">
  <strong>attnroute</strong> — Intelligent context routing for Claude Code.
</p>

<p align="center">
  <em>90%+ token reduction. <500ms latency. Zero config required.</em>
</p>
