"""Tests for installer functionality including hooks merge."""

from pathlib import Path


class TestHooksMerge:
    """Test hooks merge functionality (v0.5.7 fix)."""

    def test_merge_hooks_preserves_existing(self):
        """merge_hooks should preserve existing hooks."""
        from attnroute.installer import merge_hooks

        existing = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "my-linter"},
            ]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "my-formatter"},
            ]}],
        }

        new = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "attnroute-router"},
            ]}],
        }

        merged = merge_hooks(existing, new)

        # Should have both UserPromptSubmit and Stop
        assert "UserPromptSubmit" in merged
        assert "Stop" in merged

        # Stop should be unchanged
        assert len(merged["Stop"]) == 1
        assert merged["Stop"][0]["hooks"][0]["command"] == "my-formatter"

    def test_merge_hooks_deduplicates(self):
        """merge_hooks should deduplicate by command string."""
        from attnroute.installer import merge_hooks

        existing = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "attnroute-router"},
            ]}],
        }

        new = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "attnroute-router"},
            ]}],
        }

        merged = merge_hooks(existing, new)

        # Should only have one attnroute-router, not two
        all_commands = []
        for group in merged["UserPromptSubmit"]:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "") if isinstance(hook, dict) else hook
                all_commands.append(cmd)

        assert all_commands.count("attnroute-router") == 1

    def test_merge_hooks_adds_new_events(self):
        """merge_hooks should add hooks for new event types."""
        from attnroute.installer import merge_hooks

        existing = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "my-linter"},
            ]}],
        }

        new = {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "attnroute-session"},
            ]}],
        }

        merged = merge_hooks(existing, new)

        # Should have both events
        assert "UserPromptSubmit" in merged
        assert "SessionStart" in merged

    def test_merge_hooks_empty_existing(self):
        """merge_hooks handles empty existing hooks."""
        from attnroute.installer import merge_hooks

        existing = {}
        new = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "attnroute-router"},
            ]}],
        }

        merged = merge_hooks(existing, new)
        assert "UserPromptSubmit" in merged

    def test_merge_hooks_empty_new(self):
        """merge_hooks handles empty new hooks."""
        from attnroute.installer import merge_hooks

        existing = {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": "my-linter"},
            ]}],
        }
        new = {}

        merged = merge_hooks(existing, new)
        assert "UserPromptSubmit" in merged
        assert merged["UserPromptSubmit"][0]["hooks"][0]["command"] == "my-linter"


class TestBuildSettings:
    """Test settings building."""

    def test_build_settings_creates_hooks(self):
        """build_settings creates expected hook structure."""
        from attnroute.installer import build_settings

        settings = build_settings("python", use_entry_points=False, include_pool=False)

        assert "hooks" in settings
        assert "UserPromptSubmit" in settings["hooks"]
        assert "SessionStart" in settings["hooks"]
        assert "Stop" in settings["hooks"]


class TestSourceRouting:
    """Test source code routing configuration."""

    def test_source_config_exists(self):
        """Source routing config constants exist."""
        from attnroute.context_router import (
            MAX_TRACKED_SOURCE_FILES,
            SOURCE_EXCLUDED_DIRS,
            SOURCE_EXTENSIONS,
            SOURCE_INDEXING_ENABLED,
            SOURCE_MAX_CHARS,
            SOURCE_MAX_FILE_SIZE,
            SOURCE_MAX_HOT_FILES,
            SOURCE_MAX_WARM_FILES,
        )

        assert isinstance(SOURCE_INDEXING_ENABLED, bool)
        assert isinstance(SOURCE_EXTENSIONS, set)
        assert isinstance(SOURCE_EXCLUDED_DIRS, set)
        assert SOURCE_MAX_HOT_FILES > 0
        assert SOURCE_MAX_WARM_FILES > 0
        assert SOURCE_MAX_CHARS > 0
        assert SOURCE_MAX_FILE_SIZE > 0
        assert MAX_TRACKED_SOURCE_FILES > 0

    def test_is_source_file(self):
        """_is_source_file correctly identifies source files."""
        from attnroute.context_router import _is_source_file

        assert _is_source_file("src/auth.py")
        assert _is_source_file("lib/utils.js")
        assert _is_source_file("main.go")
        assert _is_source_file("server.ts")
        assert not _is_source_file("README.md")
        assert not _is_source_file("docs/guide.md")

    def test_is_doc_file(self):
        """_is_doc_file correctly identifies doc files."""
        from attnroute.context_router import _is_doc_file

        assert _is_doc_file("README.md")
        assert _is_doc_file(".claude/guide.md")
        assert _is_doc_file("docs/api.md")


class TestIndexerSourceFiles:
    """Test indexer source file handling."""

    def test_indexer_config_exists(self):
        """Indexer source config exists."""
        from attnroute.indexer import (
            SOURCE_EXCLUDED_DIRS,
            SOURCE_EXTENSIONS,
            SOURCE_MAX_FILE_SIZE,
        )

        assert isinstance(SOURCE_EXTENSIONS, set)
        assert isinstance(SOURCE_EXCLUDED_DIRS, set)
        assert SOURCE_MAX_FILE_SIZE > 0

    def test_should_skip_path(self):
        """SearchIndex._should_skip_path filters correctly."""
        from attnroute.indexer import SearchIndex

        idx = SearchIndex()

        # Should skip these
        assert idx._should_skip_path(Path("/project/node_modules/pkg/index.js"))
        assert idx._should_skip_path(Path("/project/.git/objects/abc"))
        assert idx._should_skip_path(Path("/project/__pycache__/module.pyc"))
        assert idx._should_skip_path(Path("/project/venv/lib/python3.10/site.py"))

        # Should NOT skip these
        assert not idx._should_skip_path(Path("/project/src/auth.py"))
        assert not idx._should_skip_path(Path("/project/lib/utils.js"))
