"""Tests for VerifyFirst plugin."""

import pytest


class TestPluginBase:
    """Test base plugin functionality."""

    def test_plugin_import(self):
        from attnroute.plugins.base import AttnroutePlugin
        assert AttnroutePlugin is not None

    def test_plugin_state_management(self, tmp_path, monkeypatch):
        from attnroute.plugins.base import AttnroutePlugin

        # Override state dir
        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)

        class TestPlugin(AttnroutePlugin):
            name = "test"

        plugin = TestPlugin()
        plugin.save_state({"key": "value"})
        loaded = plugin.load_state()
        assert loaded["key"] == "value"


class TestPluginRegistry:
    """Test plugin discovery and registration."""

    def test_discover_plugins(self):
        from attnroute.plugins import discover_plugins, get_plugins
        discover_plugins()
        plugins = get_plugins()
        assert isinstance(plugins, list)

    def test_get_plugin_by_name(self):
        from attnroute.plugins import discover_plugins, get_plugin
        discover_plugins()
        plugin = get_plugin("verifyfirst")
        assert plugin is not None
        assert plugin.name == "verifyfirst"


class TestVerifyFirst:
    """Test VerifyFirst plugin functionality."""

    @pytest.fixture
    def plugin(self, tmp_path, monkeypatch):
        from attnroute.plugins.base import AttnroutePlugin
        from attnroute.plugins.verifyfirst import VerifyFirstPlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)
        return VerifyFirstPlugin()

    def test_session_start_resets_state(self, plugin):
        result = plugin.on_session_start({"session_id": "test123"})
        assert "Active" in result

        state = plugin.load_state()
        assert state["files_read"] == []
        assert state["session_id"] == "test123"

    def test_read_tracking(self, plugin):
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Read", "target": "/path/to/file.py"},
            {"tool": "Read", "target": "/path/to/other.py"},
        ]
        plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert len(state["files_read"]) == 2

    def test_violation_detection(self, plugin):
        plugin.on_session_start({})

        # Edit without reading first
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py"},
        ]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is not None
        assert "VIOLATION" in warning

        state = plugin.load_state()
        assert len(state["violations"]) == 1

    def test_no_violation_after_read(self, plugin):
        plugin.on_session_start({})

        # Read first, then edit
        read_calls = [{"tool": "Read", "target": "/path/to/file.py"}]
        plugin.on_stop(read_calls, {})

        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is None

        state = plugin.load_state()
        assert len(state["violations"]) == 0

    def test_policy_context_injection(self, plugin):
        plugin.on_session_start({})

        # Track some reads
        tool_calls = [{"tool": "Read", "target": "/path/to/file.py"}]
        plugin.on_stop(tool_calls, {})

        # Get policy context
        context = plugin.on_prompt_post("test prompt", "existing context", {})

        assert "VerifyFirst Policy" in context
        assert "file.py" in context

    def test_session_summary(self, plugin):
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Read", "target": "/a.py"},
            {"tool": "Edit", "target": "/b.py"},  # Violation (Edit without Read)
        ]
        plugin.on_stop(tool_calls, {})

        summary = plugin.get_session_summary()
        assert summary["files_read"] == 1
        assert summary["violations"] == 1

    def test_path_normalization_windows(self, plugin, monkeypatch):
        """Test that Windows paths are normalized correctly."""
        import sys
        monkeypatch.setattr(sys, "platform", "win32")

        plugin.on_session_start({})

        # Read with backslashes
        read_calls = [{"tool": "Read", "target": "C:\\path\\to\\file.py"}]
        plugin.on_stop(read_calls, {})

        # Edit with forward slashes (should match)
        edit_calls = [{"tool": "Edit", "target": "c:/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        # Should not be a violation because paths are normalized
        assert warning is None

    def test_empty_tool_calls(self, plugin):
        """Test handling of empty tool calls."""
        plugin.on_session_start({})
        warning = plugin.on_stop([], {})
        assert warning is None

    def test_tool_calls_without_target(self, plugin):
        """Test handling of tool calls without target."""
        plugin.on_session_start({})
        tool_calls = [
            {"tool": "Read"},  # No target
            {"tool": "Edit", "target": ""},  # Empty target
        ]
        warning = plugin.on_stop(tool_calls, {})
        assert warning is None  # Should not crash

    def test_same_batch_read_then_edit_no_violation(self, plugin):
        """Test that Read+Edit in same batch (Read first) is not a violation."""
        plugin.on_session_start({})
        # Read and Edit in same batch, Read comes first
        tool_calls = [
            {"tool": "Read", "target": "/path/to/file.py"},
            {"tool": "Edit", "target": "/path/to/file.py"},
        ]
        warning = plugin.on_stop(tool_calls, {})
        # Should NOT be a violation because Read came before Edit
        assert warning is None
        state = plugin.load_state()
        assert len(state["violations"]) == 0

    def test_same_batch_edit_then_read_is_violation(self, plugin):
        """Test that Edit+Read in same batch (Edit first) IS a violation."""
        plugin.on_session_start({})
        # Edit comes before Read in the batch
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py"},
            {"tool": "Read", "target": "/path/to/file.py"},
        ]
        warning = plugin.on_stop(tool_calls, {})
        # Should BE a violation because Edit came before Read
        assert warning is not None
        assert "VIOLATION" in warning
        state = plugin.load_state()
        assert len(state["violations"]) == 1

    def test_grep_counts_as_read(self, plugin):
        """Grep should count as reading a file."""
        plugin.on_session_start({})

        # Grep a file, then edit it
        grep_calls = [{"tool": "Grep", "target": "/path/to/file.py"}]
        plugin.on_stop(grep_calls, {})

        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is None  # No violation — Grep counts as read
        state = plugin.load_state()
        assert len(state["violations"]) == 0

    def test_glob_counts_as_read(self, plugin):
        """Glob should count as reading a file."""
        plugin.on_session_start({})

        glob_calls = [{"tool": "Glob", "target": "/path/to/file.py"}]
        plugin.on_stop(glob_calls, {})

        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is None
        state = plugin.load_state()
        assert len(state["violations"]) == 0

    def test_write_no_violation_without_read(self, plugin):
        """Write tool should NOT trigger violations (used for new file creation)."""
        plugin.on_session_start({})

        # Write without reading first — should be fine
        tool_calls = [{"tool": "Write", "target": "/path/to/new_file.py"}]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is None
        state = plugin.load_state()
        assert len(state["violations"]) == 0
        # But file should still be tracked as written
        assert len(state["files_written"]) == 1

    def test_edit_still_requires_read(self, plugin):
        """Edit tool should still require reading first."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/existing.py"}]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is not None
        assert "VIOLATION" in warning

    def test_multiedit_requires_read(self, plugin):
        """MultiEdit tool should require reading first."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "MultiEdit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is not None
        assert "VIOLATION" in warning

    # --- Pattern matching tests ---

    def test_glob_pattern_covers_edit(self, plugin):
        """Glob with wildcard pattern should cover matching files for edit."""
        plugin.on_session_start({})

        # Grep with glob pattern
        grep_calls = [{"tool": "Grep", "target": "*.py"}]
        plugin.on_stop(grep_calls, {})

        state = plugin.load_state()
        assert len(state["read_patterns"]) == 1

        # Edit a .py file — should match the pattern
        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is None
        assert len(plugin.load_state()["violations"]) == 0

    def test_glob_pattern_no_match(self, plugin):
        """Glob pattern should NOT cover non-matching files."""
        plugin.on_session_start({})

        grep_calls = [{"tool": "Grep", "target": "*.js"}]
        plugin.on_stop(grep_calls, {})

        # Edit a .py file — does NOT match *.js
        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is not None
        assert "VIOLATION" in warning

    def test_read_patterns_stored_in_state(self, plugin):
        """Glob/wildcard targets stored separately in read_patterns."""
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Glob", "target": "**/*.py"},
            {"tool": "Read", "target": "/specific/file.py"},
        ]
        plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert len(state["read_patterns"]) == 1
        assert len(state["files_read"]) == 1

    def test_compact_context_few_files(self, plugin):
        """Context lists file names when <=5 files read."""
        plugin.on_session_start({})

        for i in range(3):
            plugin.on_stop([{"tool": "Read", "target": f"/path/to/file{i}.py"}], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "Verified (3):" in context
        assert "file0.py" in context

    def test_compact_context_many_files(self, plugin):
        """Context shows count when >5 files read."""
        plugin.on_session_start({})

        for i in range(8):
            plugin.on_stop([{"tool": "Read", "target": f"/path/to/f{i}.py"}], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "8 files read" in context
        # Should NOT list individual filenames
        assert "f0.py" not in context

    def test_no_files_read_context(self, plugin):
        """Context shows guidance when no files read yet."""
        plugin.on_session_start({})

        context = plugin.on_prompt_post("test", "", {})
        assert "No files read yet" in context

    def test_directory_glob_covers_nested_file(self, plugin):
        """Directory glob pattern should cover nested files."""
        plugin.on_session_start({})

        # Grep with directory glob
        grep_calls = [{"tool": "Grep", "target": "src/**/*.py"}]
        plugin.on_stop(grep_calls, {})

        # Edit a nested file matching the pattern
        edit_calls = [{"tool": "Edit", "target": "src/utils/helpers.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is None
