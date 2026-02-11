# Changelog

All notable changes to attnroute will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
