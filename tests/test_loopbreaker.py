"""Tests for LoopBreaker plugin."""

import pytest


class TestLoopBreaker:
    """Test LoopBreaker plugin functionality."""

    @pytest.fixture
    def plugin(self, tmp_path, monkeypatch):
        from attnroute.plugins.base import AttnroutePlugin
        from attnroute.plugins.loopbreaker import LoopBreakerPlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)
        return LoopBreakerPlugin()

    def test_session_start_resets_state(self, plugin):
        result = plugin.on_session_start({"session_id": "test123"})
        assert "Active" in result

        state = plugin.load_state()
        assert state["recent_attempts"] == []
        assert state["loops_detected"] == 0
        assert state["active_loop"] is None

    def test_no_loop_on_single_attempt(self, plugin):
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo", "new_string": "bar"},
        ]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is None
        state = plugin.load_state()
        assert state["active_loop"] is None

    def test_loop_detection_identical_attempts(self, plugin):
        plugin.on_session_start({})

        # Same edit 3 times
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo = 1", "new_string": "foo = 2"},
        ]

        # First attempt
        plugin.on_stop(tool_calls, {})
        state = plugin.load_state()
        assert state["active_loop"] is None

        # Second attempt
        plugin.on_stop(tool_calls, {})
        state = plugin.load_state()
        assert state["active_loop"] is None

        # Third attempt - should trigger loop
        warning = plugin.on_stop(tool_calls, {})
        assert warning is not None
        assert "LoopBreaker" in warning

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["loops_detected"] == 1

    def test_loop_breaking_on_different_approach(self, plugin):
        plugin.on_session_start({})

        # Same edit 3 times to trigger loop
        same_edit = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo = 1", "new_string": "foo = 2"},
        ]
        for _ in range(3):
            plugin.on_stop(same_edit, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["loops_detected"] == 1

        # Different approach - should break loop
        different_edit = [
            {"tool": "Edit", "target": "/path/to/other.py", "old_string": "bar = 1", "new_string": "bar = 2"},
        ]
        plugin.on_stop(different_edit, {})

        state = plugin.load_state()
        assert state["active_loop"] is None
        assert state["loops_broken"] == 1

    def test_loop_context_injection(self, plugin):
        plugin.on_session_start({})

        # Trigger a loop
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo", "new_string": "bar"},
        ]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        # Check context injection
        context = plugin.on_prompt_post("test prompt", "existing context", {})

        assert "LoopBreaker Alert" in context
        assert "file.py" in context
        assert "reconsider" in context.lower()

    def test_no_context_when_no_loop(self, plugin):
        plugin.on_session_start({})

        # Single attempt, no loop
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo", "new_string": "bar"},
        ]
        plugin.on_stop(tool_calls, {})

        context = plugin.on_prompt_post("test prompt", "existing context", {})
        assert context == ""

    def test_read_tools_dont_count(self, plugin):
        """Read tools should not contribute to loop detection."""
        plugin.on_session_start({})

        # Same read 5 times
        tool_calls = [
            {"tool": "Read", "target": "/path/to/file.py"},
        ]
        for _ in range(5):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is None
        assert len(state["recent_attempts"]) == 0

    def test_bash_commands_tracked(self, plugin):
        plugin.on_session_start({})

        # Same bash command 3 times
        tool_calls = [
            {"tool": "Bash", "target": "/path/to/dir", "command": "npm test"},
        ]

        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert "loops_detected" in state

    def test_session_summary(self, plugin):
        plugin.on_session_start({})

        # Trigger a loop
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo", "new_string": "bar"},
        ]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        summary = plugin.get_session_summary()
        assert summary["loops_detected"] == 1
        assert summary["active_loop"] == "/path/to/file.py"

    def test_empty_tool_calls(self, plugin):
        """Empty tool calls should clear active loop."""
        plugin.on_session_start({})

        # Trigger a loop first
        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py", "old_string": "foo", "new_string": "bar"},
        ]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        assert plugin.load_state()["active_loop"] is not None

        # Empty tool calls should break the loop
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is None

    def test_history_size_limit(self, plugin):
        """Recent attempts should be limited to HISTORY_SIZE."""
        plugin.on_session_start({})

        # Add many different attempts
        for i in range(30):
            tool_calls = [
                {"tool": "Edit", "target": f"/path/to/file{i}.py", "old_string": f"v{i}", "new_string": f"v{i+1}"},
            ]
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert len(state["recent_attempts"]) <= plugin.HISTORY_SIZE
