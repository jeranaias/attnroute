# Changelog

All notable changes to attnroute will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-02-16

### Fixed
- `attnroute diagnostic` hangs indefinitely on large directories (capped file walk at 10K files)
- Import-time warning spam removed (tree-sitter fallback, auto-keyword extraction)
- `attnroute version` now shows correct install hints for each extra

### Changed
- README: Qualified token reduction claims with proper methodology context
- README: Added cold-start learning note (heuristics work immediately, learner activates after ~25 turns)
- README: Added Python 3.14 compatibility note (`[all]`/`[graph]` extras need 3.10-3.13)
- README: Default install command changed to `pip install attnroute` (zero deps, works on all Python versions)
- Hero demo: Numbers now match real benchmark results; "Stops edits" → "Flags edits"

## [1.0.0] - 2026-02-16

### Milestone
- **Production release**: All 4 plugins at v1.0+, 345+ tests passing, lint clean
- Development Status classifier changed from "4 - Beta" to "5 - Production/Stable"

### Added
- SECURITY.md with responsible disclosure policy
- GitHub issue templates (bug report, feature request)
- Python 3.13 to CI test matrix, Python 3.14-dev as allow-failure
- Expanded competitor comparison table (Aider, Repomix, .claudeignore)

### Changed
- README: Updated all plugin sections with v1.0 features and current version numbers
- README: Replaced broken demo.gif with install instructions
- README: Trimmed security section with link to SECURITY.md

## [0.7.0] - 2026-02-15

### Added
- **ContextGuard v1.0.0** — post-compaction amnesia prevention
  - Compaction prediction based on injection size trend + turn count
  - Recovery analytics with timestamps
  - CLAUDE.md recovery hint in injected context
  - Cross-plugin flag file (`compaction_occurred.flag`) for VerifyFirst integration
- **LoopBreaker v3.0.0** — multi-metric loop detection
  - Git-based progress detection (zero-change detection via `git diff`)
  - Multi-metric severity scoring (signature repetition + failures + git progress)
  - State machine stages: detected → escalated → cooling
- **VerifyFirst v2.1.0** — freshness tracking
  - Read registry summary with freshness labels (fresh/aging/STALE)
  - Edit velocity tracking with speculative editing alerts
  - Cross-plugin ContextGuard compaction flag integration
- **BurnRate v1.0.0** — budget system
  - Configurable daily/weekly token budgets via `~/.claude/plugins/config.json`
  - CSV/JSON export of usage history (`export_usage()`)
  - Weekly per-model token breakdown summaries
- Neural predictor module (experimental)
- Intent map module (experimental)
- Warmup module for pre-loading project context
- Project profiling module
- New benchmark suite: pipeline, plugin, multi-repo, resource, session replay

### Changed
- ContextGuard registered as entry point in pyproject.toml
- Core context router improvements (500+ lines)
- Indexer: expanded source file handling
- Session init: plugin lifecycle improvements

## [0.6.1] - 2026-02-12

### Fixed
- **BurnRate billing audit**: Cache tier awareness for more accurate cost estimates
  - Separate 5-minute and 1-hour cache write rates in MODEL_PRICING
  - Ephemeral cache tier extraction from JSONL objects
  - Per-tier cost breakdown in audit output

## [0.6.0] - 2026-02-12

### Added
- **BurnRate billing audit** — cross-references JSONL token data against published API rates
  - `get_billing_audit()` and `format_billing_audit()` for terminal reports
  - Real-time 5-hour sliding window rate limit tracking with burn rate and ETA
  - Per-project, per-model, per-day cost attribution
  - Insights: peak hours, session stats, cache hit rate, busiest day
- 112 tests (99 core + 13 billing audit)

### Fixed
- LoopBreaker: fuzzy signature similarity detection
- VerifyFirst: pattern matching for glob/grep targets

## [0.5.13] - 2026-02-12

### Fixed
- **Claims consistency**: Aligned token reduction claims to 90%+ across pyproject.toml, README, and benchmarks
- **LoopBreaker bug**: `SIMILARITY_THRESHOLD` was declared but never used — added `_signature_similarity()` with Jaccard similarity for fuzzy loop detection
- **Benchmark honesty**: Added methodology notes explaining baseline is theoretical maximum (all files concatenated), not a comparison to Claude Code's native behavior
- **Benchmark language**: Replaced "BULLETPROOF"/"IMPENETRABLE" with neutral descriptions

### Added
- `benchmarks/README.md` with transparent methodology documentation
- `tests/test_context_router.py` — 11 behavioral tests for tier classification, file detection, config coherence
- 8 new behavioral tests in `test_core.py` for RepoMapper and Predictor
- 5 new tests in `test_loopbreaker.py` for fuzzy similarity detection
- 2 new tests in `test_graph_retriever.py`

### Changed
- README streamlined: removed straw-man scenarios, duplicate ASCII diagrams, marketing taglines (~300 lines removed)
- `integrations.py` adapters marked as experimental (currently unused by core pipeline)

## [0.5.12] - 2026-02-11

### Security
- **Critical**: Added `safe_read_stdin()` with 10MB size limit to prevent memory exhaustion DoS
  - Reads bytes directly via `sys.stdin.buffer` to prevent Unicode 4x memory amplification
- **Critical**: Added `validate_path()` helper for path traversal prevention
  - Windows Alternate Data Stream (ADS) detection (blocks `file.txt:hidden`)
  - Windows reserved device name blocking (CON, NUL, COM1, etc.)
  - Null byte injection prevention
- **Critical**: Added `validate_plugin_name()` to prevent path traversal via plugin names
- **Critical**: Improved atomic writes with `safe_atomic_write()` - cross-platform support with fallback
  - mkdir now inside try block to catch PermissionError

### Fixed
- **Reliability**: TOCTOU race conditions eliminated - replaced 20+ `exists()` checks with try/except patterns
  - Fixed in: context_router.py, learner.py, freshness.py, oracle.py, history.py, session_init.py,
    telemetry_lib.py, plugins/base.py, plugins/__init__.py
- **Reliability**: Windows atomic write failures now fall back to direct write instead of silent failure
- **Reliability**: Null-safe handling for `files_used`/`files_injected` in learner.py
  - Changed `turn.get("files_used", [])` to `turn.get("files_used") or []` pattern
- **Reliability**: Path normalization for cross-platform consistency
  - All relative paths now use forward slashes via `.replace("\\", "/")`
  - Fixed in: indexer.py (4 locations), learner.py, freshness.py
- **Reliability**: Missing `encoding="utf-8"` added to context_router.py history append
- **Reliability**: JSON type validation added to all `load_*()` functions to prevent crashes on corrupt data
  - `load_stats_cache()`, `load_router_overrides()`, `load_session_state()` now validate dict type
  - Plugin `load_state()`, `is_enabled()` now validate dict structure
  - Oracle `_load_costs()` validates nested dict structure
- **Performance**: Log rotation (`rotate_jsonl`) now uses seek-from-end to avoid loading entire file
  - Prevents memory issues on large turns.jsonl files (>500 entries)
- **Debugging**: Added error logging to all plugin lifecycle hooks (on_prompt_pre, on_prompt_post, on_stop, on_session_start)
- **Debugging**: Added error logging to search query failures (now logs before falling back to keywords)
- **Debugging**: Added error logging to learner docs root scanning
- **Debugging**: Plugin save_state failures now log warnings instead of silently failing

### Changed
- All file I/O now uses centralized `compat.py` security helpers
- Learner, plugins, context_router, and telemetry now use `safe_atomic_write()` for state persistence
- Plugin base class now validates plugin names on initialization
- Safer Windows stdout suppression in indexer using `contextlib.redirect_stdout`
- All fallback writes now use `flush()` for better durability
- `_set_plugin_enabled()` now validates plugin names before use
- Oracle and telemetry_record fallback writes use flush() for crash safety
- `atomic_jsonl_append()` now calls `flush()` after write for durability

## [0.5.11] - 2026-02-11

### Fixed
- **Critical**: Null learner guards added to prevent `AttributeError` crashes when learner fails to initialize
- **Critical**: BM25 division by zero fix now guards against both empty dict AND all-zero scores
- **Critical**: JSON input type validation - non-dict JSON no longer crashes with `AttributeError`
- **Performance**: Single directory traversal in indexer (10-50x faster for large codebases)
- **Performance**: Batch embedding encoding (5-10x faster semantic search)
- **Performance**: Cached `resolve_docs_root()` - no longer globs on every prompt
- **Performance**: Log rotation now uses seek from end instead of reading entire file
- **Plugins**: Thread-safe plugin registry with `threading.Lock()`
- **Plugins**: Atomic state file writes (temp file + rename pattern)
- **Plugins**: BurnRate quota reset detection improved
- **Plugins**: Error logging added for plugin load failures (no more silent exceptions)
- **CLI**: Added `attnroute validate` command to verify installation
- **CLI**: Exit codes now properly propagated from commands
- **CLI**: Help text expanded to list all 12 commands
- **UX**: Progress feedback during project scanning
- **UX**: Python detection now warns when falling back to `python3`
- Doc file detection fixed - no longer matches `my.claude.txt` as a doc file
- Stats inflation fixed - HOT count only increments when file actually injected
- Git Bash path prefix now uses dynamic drive letter (not hardcoded `/c`)
- Bare `except:` clauses replaced with `except Exception:` for proper error handling

## [0.5.10] - 2026-02-11

### Fixed
- **UX**: Clarified that `.claude/` directories are optional for source code routing
  - Source code routing works automatically on any project without any setup
  - `.claude/*.md` doc routing is the only feature that requires `.claude/` directory
  - "No projects found" message no longer implies hooks aren't installed
  - Final output now clearly states "Source code routing is active"

## [0.5.9] - 2026-02-11

### Fixed
- `attnroute init` now shows helpful diagnostics when "No projects found"
  - Shows the CWD that was checked
  - Warns if `.claude` exists but is a file instead of directory
  - Detects case-sensitivity mismatches (e.g., `.Claude` vs `.claude`)
  - Tells user how to create `.claude/` directory

## [0.5.8] - 2026-02-11

### Fixed
- **Critical**: BM25 search failed silently on projects with <20 indexed files
  - `_bm25_search()` hardcoded `k=20` which raises `ValueError` when corpus is smaller
  - Fix: `k=min(top_k, len(self._bm25_paths))` to clamp to available documents
  - This broke source code routing for most users in v0.5.7

## [0.5.7] - 2026-02-11

### Added
- **Source Code Routing** - Search index now covers the actual project source tree, not just `.claude/*.md` docs
  - Source files matched by BM25 search get tree-sitter outline injection (function signatures, class definitions, imports) — not raw file content
  - No `keywords.json` required for source routing — BM25 handles discovery automatically from the prompt
  - Separate limits for source context: `SOURCE_MAX_HOT_FILES=2`, `SOURCE_MAX_WARM_FILES=3`, `SOURCE_MAX_CHARS=8000`
  - Large files (>100KB) and excluded directories (node_modules, .git, __pycache__, venv, dist, build, target) are skipped
  - State dynamically grows when search finds new source files (capped at 50 tracked source files)
  - `.claude/*.md` doc routing continues to work exactly as before — source routing is additive
- Visual distinction in output: `[HOT:SRC]` and `[WARM:SRC]` labels for source context blocks

### Fixed
- **Hooks overwrite bug** - `attnroute init` now properly merges hooks per-event instead of replacing all existing hooks
  - Previous behavior destroyed user's other hooks (e.g., linters, formatters)
  - Now deduplicates by command string and preserves existing hook configurations
  - Creates `settings.json.bak` backup before modifying

## [0.5.6] - 2026-02-10

### Fixed
- **C1**: `UnboundLocalError` in notification clamping — added `global` declaration
  for `MAX_HOT_FILES`, `MAX_WARM_FILES`, `MAX_TOTAL_CHARS` in `main()`
- **C2**: `AttributeError` if Learner init fails — added None guard for `get_learner()`
  at module level
- **C3**: Double `sys.stdin.read()` breaking fallback path — now buffers stdin before
  parsing JSON
- **C4**: `ingest.py` hardcoded import — added dual import fallback pattern with
  inline normalize_path fallback
- **H1**: Telemetry project identity mismatch — `record_turn_telemetry()` now uses
  `get_project()` for worktree-aware project identity
- **H2**: Non-atomic state file writes — `save_state()` now uses temp file + replace
  pattern to prevent corruption on interrupted writes
- **H3**: Missing `encoding='utf-8'` on 11 file I/O calls causing Windows encoding
  issues with non-ASCII content
- **H4**: `load_telemetry_overrides()` log spam — moved log statement inside cache-miss
  branch so it only prints when values actually change
- **M1**: `FilePredictor` eager instantiation — now uses `LazyLoader` like Learner
  and SearchIndex for consistent lazy initialization
- **M2/M3**: Installer suggested wrong pip package name (`tree-sitter-language-pack`
  instead of `tree-sitter-languages`)
- Version sync test now works on Python 3.10 (uses regex fallback instead of
  tomllib which is 3.11+ only)

## [0.5.5] - 2026-02-10

### Fixed
- `update_attention()` referenced removed global `_search_index` instead of
  `get_search_index()`, causing NameError for users with bm25s installed.
  This was the same lazy-init bug partially fixed in 0.5.4 — second
  occurrence at line 624 was missed. The crash caused the UserPromptSubmit
  hook to silently fail, preventing all context injection.

## [0.5.4] - 2026-02-10

### Added
- `attnroute ingest` — bootstrap learner from Claude Code conversation history
  in ~/.claude/projects/. Parses JSONL transcripts to seed co-activation patterns,
  prompt-file affinity, and file rhythms so you don't start cold on established projects
- Git worktree support — worktrees sharing the same repo now share project
  identity, attention state, and keywords.json (resolves via git rev-parse --git-common-dir)
- Version sync test to prevent __init__.py / pyproject.toml drift
- SWE-Pruner (arxiv 2601.16746) added to related work acknowledgments

### Changed
- README benchmark framing: realistic per-query token comparison (50-200K → 2-5K)
  with methodology-labeled 99.87% figure in benchmarks section
- Extended compat.py usage to session_init, learner, compressor

### Fixed
- `scan_projects()` now checks CWD first and searches two levels deep from home,
  fixing "No projects found" for projects in ~/code/*, ~/dev/*, etc.
- Warning message referenced nonexistent `attnroute-setup` command, now correctly
  says `attnroute init`
- Stale `global _search_index` reference in ensure_search_index_built() now
  uses get_search_index() accessor

## [0.5.3] - 2026-02-10

### Added
- `attnroute/compat.py` - Centralized import utilities (`try_import`, `LazyLoader`)
- Prediction accuracy metrics in README (Precision ~45%, Recall ~60%, F1 0.35-0.42)

### Changed
- Lazy initialization for `Learner` and `SearchIndex` (no side effects at import time)
- Replaced dual import boilerplate with `try_import()` utility in context_router.py
- Updated predictor.py docstring with honest benchmark metrics

### Fixed
- Module-level instantiation side effects that could affect testing

## [0.5.2] - 2026-02-10

### Added
- CHANGELOG.md for version history tracking

### Fixed
- Version sync between `__init__.py` and `pyproject.toml`

## [0.5.1] - 2026-02-10

### Changed
- Toned down README stats to more conservative "90%+" claims
- Fixed CI linting configuration

### Fixed
- Ruff linting errors (import sorting, deprecated type hints)

## [0.5.0] - 2026-02-09

### Added
- **Plugin System** - Extensible architecture for behavioral guardrails
  - `VerifyFirst` - Enforces read-before-write policy (addresses GitHub #23833)
  - `LoopBreaker` - Detects and breaks repetitive failure loops (addresses GitHub #21431)
  - `BurnRate` - Predicts rate limit exhaustion with early warnings (addresses GitHub #22435)
- Plugin CLI: `attnroute plugins list|enable|disable|status`
- Plugin state persistence in `~/.claude/plugins/`
- Entry points for external plugin discovery

### Changed
- Updated pyproject.toml to include plugin subpackages
- Added plugin hooks to context_router, session_init, and telemetry_record

## [0.4.0] - 2026-02-08

### Added
- Graph-based retrieval with PageRank ranking
- Tree-sitter AST parsing for 14+ languages
- Dependency graph caching
- `attnroute graph stats` command

## [0.3.0] - 2026-02-07

### Added
- Memory compression with Claude API (optional)
- 3-layer progressive retrieval (index → timeline → full)
- ChromaDB integration for semantic search
- `attnroute compress stats` command

## [0.2.0] - 2026-02-06

### Added
- BM25 keyword search (bm25s)
- Semantic search with model2vec embeddings
- Graceful degradation when optional deps missing
- `attnroute benchmark` command

## [0.1.0] - 2026-02-05

### Added
- Initial release
- HOT/WARM/COLD context tiering
- Exponential heat decay with co-activation boosting
- Attention state persistence
- Claude Code hook integration (UserPromptSubmit, SessionStart, Stop)
- `attnroute init` and `attnroute status` commands
- Zero required dependencies

[1.0.0]: https://github.com/jeranaias/attnroute/compare/v0.7.0...v1.0.0
[0.7.0]: https://github.com/jeranaias/attnroute/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/jeranaias/attnroute/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/jeranaias/attnroute/compare/v0.5.13...v0.6.0
[0.5.13]: https://github.com/jeranaias/attnroute/compare/v0.5.12...v0.5.13
[0.5.12]: https://github.com/jeranaias/attnroute/compare/v0.5.11...v0.5.12
[0.5.11]: https://github.com/jeranaias/attnroute/compare/v0.5.10...v0.5.11
[0.5.10]: https://github.com/jeranaias/attnroute/compare/v0.5.9...v0.5.10
[0.5.9]: https://github.com/jeranaias/attnroute/compare/v0.5.8...v0.5.9
[0.5.8]: https://github.com/jeranaias/attnroute/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/jeranaias/attnroute/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/jeranaias/attnroute/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/jeranaias/attnroute/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/jeranaias/attnroute/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/jeranaias/attnroute/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/jeranaias/attnroute/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/jeranaias/attnroute/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jeranaias/attnroute/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jeranaias/attnroute/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jeranaias/attnroute/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jeranaias/attnroute/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jeranaias/attnroute/releases/tag/v0.1.0
