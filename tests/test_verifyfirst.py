"""Tests for VerifyFirst plugin v2.0."""

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

    def test_load_state_with_non_dict_json(self, tmp_path, monkeypatch):
        """load_state returns {} if JSON is valid but not a dict."""
        from attnroute.plugins.base import AttnroutePlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)

        class TestPlugin(AttnroutePlugin):
            name = "test"

        plugin = TestPlugin()
        # Write a JSON array instead of a dict
        plugin._state_file.write_text('[1, 2, 3]', encoding="utf-8")
        loaded = plugin.load_state()
        assert loaded == {}

    def test_is_enabled_caching(self, tmp_path, monkeypatch):
        """is_enabled caches result after first read."""
        import json

        from attnroute.plugins.base import AttnroutePlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)

        class TestPlugin(AttnroutePlugin):
            name = "test"

        # Create config that disables the plugin
        config_dir = tmp_path.parent / ".claude" / "plugins"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.json"

        # Temporarily override home to use our config
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path.parent)

        config_file.write_text(
            json.dumps({"enabled": {"test": False}}), encoding="utf-8"
        )

        plugin = TestPlugin()
        assert plugin.is_enabled() is False

        # Change config to enable — but cache should still say disabled
        config_file.write_text(
            json.dumps({"enabled": {"test": True}}), encoding="utf-8"
        )
        assert plugin.is_enabled() is False  # Cached

    def test_is_enabled_default_true(self, tmp_path, monkeypatch):
        """is_enabled defaults to True when config missing."""
        from attnroute.plugins.base import AttnroutePlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)

        class TestPlugin(AttnroutePlugin):
            name = "test"

        plugin = TestPlugin()
        assert plugin.is_enabled() is True


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
        assert state["read_registry"] == {}
        assert state["turn_number"] == 0

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
        assert "CRITICAL" in warning
        assert "never read" in warning

        state = plugin.load_state()
        assert len(state["violations"]) == 1
        assert state["violations"][0]["severity"] == "CRITICAL"

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
        assert summary["critical_violations"] == 1

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
        assert "CRITICAL" in warning
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

        assert warning is None  # No violation -- Grep counts as read
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

        # Write without reading first -- should be fine
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
        assert "CRITICAL" in warning

    def test_multiedit_requires_read(self, plugin):
        """MultiEdit tool should require reading first."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "MultiEdit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is not None
        assert "CRITICAL" in warning

    # --- Pattern matching tests ---

    def test_glob_pattern_covers_edit(self, plugin):
        """Glob with wildcard pattern should cover matching files for edit."""
        plugin.on_session_start({})

        # Grep with glob pattern
        grep_calls = [{"tool": "Grep", "target": "*.py"}]
        plugin.on_stop(grep_calls, {})

        state = plugin.load_state()
        assert len(state["read_patterns"]) == 1

        # Edit a .py file -- should match the pattern
        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is None
        assert len(plugin.load_state()["violations"]) == 0

    def test_glob_pattern_no_match(self, plugin):
        """Glob pattern should NOT cover non-matching files."""
        plugin.on_session_start({})

        grep_calls = [{"tool": "Grep", "target": "*.js"}]
        plugin.on_stop(grep_calls, {})

        # Edit a .py file -- does NOT match *.js
        edit_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        warning = plugin.on_stop(edit_calls, {})

        assert warning is not None
        assert "CRITICAL" in warning

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
        """Context shows registry summary when <=8 files, count when >8."""
        plugin.on_session_start({})

        # 8 files: uses registry summary (v2.1 change)
        for i in range(8):
            plugin.on_stop([{"tool": "Read", "target": f"/path/to/f{i}.py"}], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "Verified (8):" in context
        # With registry format, files are listed with freshness
        assert "fresh" in context or "aging" in context

        # Add more to exceed 8
        for i in range(8, 12):
            plugin.on_stop([{"tool": "Read", "target": f"/path/to/f{i}.py"}], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "12 files read" in context

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

    # === v2.0 Tests: Read Freshness ===

    def test_read_registry_tracks_turn_number(self, plugin):
        """Read registry should record turn numbers for each file."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Read", "target": "/a.py"}], {})
        plugin.on_stop([{"tool": "Read", "target": "/b.py"}], {})

        state = plugin.load_state()
        registry = state["read_registry"]

        # Both should be in registry with different turn numbers
        a_key = [k for k in registry if "a.py" in k][0]
        b_key = [k for k in registry if "b.py" in k][0]
        assert registry[a_key]["turn"] == 1
        assert registry[b_key]["turn"] == 2

    def test_stale_read_detection(self, plugin):
        """Files read many turns ago should be flagged as stale."""
        plugin.on_session_start({})

        # Read a file on turn 1
        plugin.on_stop([{"tool": "Read", "target": "/old.py"}], {})

        # Simulate many turns passing (do 11 more stops to advance turn counter)
        for i in range(11):
            plugin.on_stop([{"tool": "Read", "target": f"/other{i}.py"}], {})

        # Check stale reads in context
        context = plugin.on_prompt_post("test", "", {})
        assert "Stale reads" in context

    def test_severity_critical_never_read(self, plugin):
        """Editing a file never read should be CRITICAL severity."""
        plugin.on_session_start({})

        warning = plugin.on_stop([{"tool": "Edit", "target": "/never_read.py"}], {})
        assert "CRITICAL" in warning

        state = plugin.load_state()
        assert state["violations"][0]["severity"] == "CRITICAL"

    def test_severity_warning_stale_read(self, plugin):
        """Editing a file read many turns ago should be WARNING severity."""
        plugin.on_session_start({})

        # Read the file first
        plugin.on_stop([{"tool": "Read", "target": "/stale.py"}], {})

        # Advance 11 turns
        for i in range(11):
            plugin.on_stop([{"tool": "Read", "target": f"/filler{i}.py"}], {})

        # Now edit the stale file -- remove from files_read to trigger violation
        state = plugin.load_state()
        state["files_read"] = [f for f in state["files_read"] if "stale" not in f]
        plugin.save_state(state)

        warning = plugin.on_stop([{"tool": "Edit", "target": "/stale.py"}], {})
        assert warning is not None
        assert "WARNING" in warning

    # === v2.0 Tests: Proactive File References ===

    def test_extract_file_references_from_prompt(self, plugin):
        """Should extract file paths from user prompt text."""
        refs = plugin._extract_file_references("Please fix the bug in src/utils/helpers.py")
        assert any("helpers.py" in r for r in refs)

    def test_extract_windows_paths(self, plugin):
        """Should extract Windows-style file paths."""
        refs = plugin._extract_file_references("Edit C:\\Users\\jesse\\project\\main.py")
        assert any("main.py" in r for r in refs)

    def test_proactive_unread_warning(self, plugin):
        """Context should warn about files mentioned in prompt but not read."""
        plugin.on_session_start({})

        # User mentions a file they haven't read
        context = plugin.on_prompt_post("Fix the bug in auth.py", "", {})
        assert "Referenced but unread" in context
        assert "auth.py" in context

    def test_no_proactive_warning_if_read(self, plugin):
        """No warning if referenced file was already read."""
        plugin.on_session_start({})

        # Read the file first
        plugin.on_stop([{"tool": "Read", "target": "auth.py"}], {})

        # Now mention it -- no warning
        context = plugin.on_prompt_post("Fix the bug in auth.py", "", {})
        assert "Referenced but unread" not in context

    # === v2.0 Tests: Compliance Analytics ===

    def test_compliance_analytics_across_sessions(self, plugin):
        """Analytics should persist across sessions."""
        plugin.on_session_start({"session_id": "s1"})

        # Some edits with violations
        plugin.on_stop([{"tool": "Edit", "target": "/a.py"}], {})  # violation
        plugin.on_stop([{"tool": "Read", "target": "/b.py"}], {})
        plugin.on_stop([{"tool": "Edit", "target": "/b.py"}], {})  # no violation

        state = plugin.load_state()
        assert state["analytics"]["total_edits"] == 2
        assert state["analytics"]["total_violations"] == 1

        # Start new session -- analytics preserved
        plugin.on_session_start({"session_id": "s2"})
        state = plugin.load_state()
        assert state["analytics"]["total_edits"] == 2
        assert state["analytics"]["total_violations"] == 1
        assert state["analytics"]["sessions"] == 2

    def test_compliance_rate_calculation(self, plugin):
        """Compliance rate should reflect violation ratio."""
        plugin.on_session_start({})

        # Generate enough edits for rate to be meaningful
        for i in range(5):
            plugin.on_stop([{"tool": "Read", "target": f"/file{i}.py"}], {})
            plugin.on_stop([{"tool": "Edit", "target": f"/file{i}.py"}], {})

        summary = plugin.get_session_summary()
        assert "compliance_rate" in summary
        assert summary["compliance_rate"] == 1.0  # No violations

    def test_compliance_rate_with_violations(self, plugin):
        """Compliance rate should decrease with violations."""
        plugin.on_session_start({})

        # 3 clean edits + 2 violations = 3/5 = 60% compliance
        for i in range(3):
            plugin.on_stop([{"tool": "Read", "target": f"/clean{i}.py"}], {})
            plugin.on_stop([{"tool": "Edit", "target": f"/clean{i}.py"}], {})

        plugin.on_stop([{"tool": "Edit", "target": "/bad1.py"}], {})
        plugin.on_stop([{"tool": "Edit", "target": "/bad2.py"}], {})

        summary = plugin.get_session_summary()
        assert summary["compliance_rate"] == 0.6

    def test_session_start_shows_compliance(self, plugin):
        """Session start message should show compliance if enough data."""
        plugin.on_session_start({})

        # Build up enough data
        for i in range(6):
            plugin.on_stop([{"tool": "Read", "target": f"/f{i}.py"}], {})
            plugin.on_stop([{"tool": "Edit", "target": f"/f{i}.py"}], {})

        # New session should show compliance rate
        result = plugin.on_session_start({"session_id": "s2"})
        assert "compliance:" in result

    # === v2.0 Tests: Rich Tool Calls ===

    def test_rich_tool_call_with_input(self, plugin):
        """Plugin should handle enriched tool calls with input dict."""
        plugin.on_session_start({})

        rich_calls = [{
            "tool": "Edit",
            "target": "/file.py",
            "input": {
                "file_path": "/file.py",
                "old_string": "def old():",
                "new_string": "def new():",
            },
        }]
        warning = plugin.on_stop(rich_calls, {})
        assert warning is not None  # Violation (never read)

        state = plugin.load_state()
        assert state["violations"][0].get("had_old_string") is True

    def test_session_summary_severity_breakdown(self, plugin):
        """Session summary should include severity breakdown."""
        plugin.on_session_start({})

        # Two CRITICAL violations (never read)
        plugin.on_stop([{"tool": "Edit", "target": "/a.py"}], {})
        plugin.on_stop([{"tool": "Edit", "target": "/b.py"}], {})

        summary = plugin.get_session_summary()
        assert summary["critical_violations"] == 2
        assert summary["warning_violations"] == 0
        assert summary["notice_violations"] == 0

    def test_turn_number_increments(self, plugin):
        """Turn number should increment with each on_stop call."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Read", "target": "/a.py"}], {})
        assert plugin.load_state()["turn_number"] == 1

        plugin.on_stop([{"tool": "Read", "target": "/b.py"}], {})
        assert plugin.load_state()["turn_number"] == 2

    def test_read_refreshes_registry(self, plugin):
        """Re-reading a file should update its turn number in the registry."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Read", "target": "/file.py"}], {})
        state = plugin.load_state()
        key = [k for k in state["read_registry"] if "file.py" in k][0]
        assert state["read_registry"][key]["turn"] == 1

        # Advance a few turns
        plugin.on_stop([{"tool": "Read", "target": "/other.py"}], {})
        plugin.on_stop([{"tool": "Read", "target": "/other2.py"}], {})

        # Re-read the file
        plugin.on_stop([{"tool": "Read", "target": "/file.py"}], {})
        state = plugin.load_state()
        assert state["read_registry"][key]["turn"] == 4

    # === v2.1 Tests: Freshness Display ===

    def test_freshness_label_fresh(self, plugin):
        """Recently read files get 'fresh' label."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Read", "target": "/file.py"}], {})

        state = plugin.load_state()
        registry = state["read_registry"]
        turn = state["turn_number"]

        lines = plugin._format_read_registry_summary(registry, turn)
        assert len(lines) == 1
        assert "fresh" in lines[0]
        assert "file.py" in lines[0]

    def test_freshness_label_aging(self, plugin):
        """Files read at half-threshold turns ago get 'aging' label."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Read", "target": "/file.py"}], {})

        # Advance turn counter past half the staleness threshold
        state = plugin.load_state()
        key = [k for k in state["read_registry"] if "file.py" in k][0]
        # Staleness threshold is 10, half is 5
        state["turn_number"] = state["read_registry"][key]["turn"] + 6
        plugin.save_state(state)

        lines = plugin._format_read_registry_summary(
            state["read_registry"], state["turn_number"]
        )
        assert any("aging" in line for line in lines)

    def test_freshness_label_stale(self, plugin):
        """Files read at threshold+ turns ago get 'STALE' label."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Read", "target": "/file.py"}], {})

        state = plugin.load_state()
        key = [k for k in state["read_registry"] if "file.py" in k][0]
        state["turn_number"] = state["read_registry"][key]["turn"] + 15
        plugin.save_state(state)

        lines = plugin._format_read_registry_summary(
            state["read_registry"], state["turn_number"]
        )
        assert any("STALE" in line for line in lines)

    def test_freshness_display_max_files_capped(self, plugin):
        """Registry summary shows at most 8 files."""
        plugin.on_session_start({})

        for i in range(12):
            plugin.on_stop([{"tool": "Read", "target": f"/file{i}.py"}], {})

        state = plugin.load_state()
        lines = plugin._format_read_registry_summary(
            state["read_registry"], state["turn_number"]
        )
        assert len(lines) <= 8

    def test_freshness_display_in_context(self, plugin):
        """Registry summary appears in on_prompt_post context."""
        plugin.on_session_start({})

        for i in range(3):
            plugin.on_stop([{"tool": "Read", "target": f"/f{i}.py"}], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "Verified (3):" in context
        # Should show freshness labels
        assert "fresh" in context

    # === v2.1 Tests: Edit Velocity Tracking ===

    def test_edit_velocity_alert_no_reads(self, plugin):
        """3+ turns of edits-only triggers speculative editing alert."""
        plugin.on_session_start({})

        # 3 turns with only edits, no reads
        for i in range(3):
            plugin.on_stop([{"tool": "Edit", "target": f"/file{i}.py"}], {})

        state = plugin.load_state()
        assert state["speculative_editing_alert"] is True

    def test_edit_velocity_no_alert_with_reads(self, plugin):
        """Turns with reads should not trigger alert."""
        plugin.on_session_start({})

        for i in range(3):
            plugin.on_stop([
                {"tool": "Read", "target": f"/file{i}.py"},
                {"tool": "Edit", "target": f"/file{i}.py"},
            ], {})

        state = plugin.load_state()
        assert state["speculative_editing_alert"] is False

    def test_velocity_resets_on_reads(self, plugin):
        """Alert should clear when reads resume."""
        plugin.on_session_start({})

        # Build up alert
        for i in range(3):
            plugin.on_stop([{"tool": "Edit", "target": f"/bad{i}.py"}], {})

        assert plugin.load_state()["speculative_editing_alert"] is True

        # Add reads to bring ratio down
        for i in range(3):
            plugin.on_stop([
                {"tool": "Read", "target": f"/good{i}.py"},
                {"tool": "Read", "target": f"/good{i}b.py"},
            ], {})

        # With reads, the window should no longer have 3+ edit-only turns
        state = plugin.load_state()
        assert state["speculative_editing_alert"] is False

    def test_velocity_warning_in_context(self, plugin):
        """Velocity alert message appears in on_prompt_post."""
        plugin.on_session_start({})

        for i in range(3):
            plugin.on_stop([{"tool": "Edit", "target": f"/file{i}.py"}], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "High edit velocity" in context

    def test_velocity_window_capped(self, plugin):
        """Velocity window is capped at VELOCITY_WINDOW_SIZE."""
        plugin.on_session_start({})

        for i in range(10):
            plugin.on_stop([{"tool": "Read", "target": f"/f{i}.py"}], {})

        state = plugin.load_state()
        assert len(state["velocity_window"]) <= plugin.VELOCITY_WINDOW_SIZE

    # === v2.1 Tests: Cross-Plugin Compaction Flag ===

    def test_verifyfirst_reads_compaction_flag(self, plugin, tmp_path):
        """VerifyFirst reads and deletes ContextGuard's compaction flag."""
        plugin.on_session_start({})

        # Create the flag file (simulating ContextGuard writing it)
        flag_file = tmp_path / "compaction_occurred.flag"
        flag_file.write_text("2026-02-16T10:00:00", encoding="utf-8")

        context = plugin.on_prompt_post("test", "", {})

        assert "Post-compaction notice" in context
        assert "STALE" in context

        # Flag should be deleted after reading
        assert not flag_file.exists()

    def test_no_compaction_notice_without_flag(self, plugin, tmp_path):
        """No compaction notice when flag file doesn't exist."""
        plugin.on_session_start({})

        context = plugin.on_prompt_post("test", "", {})
        assert "Post-compaction notice" not in context

    def test_session_start_initializes_v21_fields(self, plugin):
        """v2.1 state fields initialized on session start."""
        plugin.on_session_start({})
        state = plugin.load_state()
        assert state["velocity_window"] == []
        assert state["speculative_editing_alert"] is False
