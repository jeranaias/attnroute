"""Tests for LoopBreaker plugin.

Tool calls from telemetry contain only {tool, target}:
  - Edit/Write: target = file_path
  - Bash: target = command text (first 100 chars)
  - Read/Grep/Glob: target = file_path or pattern
"""

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
            {"tool": "Edit", "target": "/path/to/file.py"},
        ]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is None
        state = plugin.load_state()
        assert state["active_loop"] is None

    def test_loop_detection_identical_attempts(self, plugin):
        """Four edits to the same file triggers loop detection (threshold=4)."""
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py"},
        ]

        # First three — no loop yet
        for _ in range(3):
            plugin.on_stop(tool_calls, {})
        state = plugin.load_state()
        assert state["active_loop"] is None

        # Fourth attempt — triggers loop
        warning = plugin.on_stop(tool_calls, {})
        assert warning is not None
        assert "LoopBreaker" in warning

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["loops_detected"] == 1

    def test_loop_breaking_on_different_file(self, plugin):
        """Working on a different file breaks the loop."""
        plugin.on_session_start({})

        same_edit = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(same_edit, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

        # Different file — should break loop
        different_edit = [{"tool": "Edit", "target": "/path/to/other.py"}]
        plugin.on_stop(different_edit, {})

        state = plugin.load_state()
        assert state["active_loop"] is None
        assert state["loops_broken"] == 1

    def test_loop_context_injection(self, plugin):
        """Active loop injects warning context into prompt."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(tool_calls, {})

        context = plugin.on_prompt_post("test prompt", "existing context", {})

        assert "LoopBreaker Alert" in context
        assert "file.py" in context
        assert "reconsider" in context.lower()

    def test_no_context_when_no_loop(self, plugin):
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        plugin.on_stop(tool_calls, {})

        context = plugin.on_prompt_post("test prompt", "existing context", {})
        assert context == ""

    def test_read_tools_dont_count(self, plugin):
        """Read tools should not contribute to loop detection."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Read", "target": "/path/to/file.py"}]
        for _ in range(5):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is None
        assert len(state["recent_attempts"]) == 0

    def test_read_between_edits_keeps_loop(self, plugin):
        """Read-only turns should NOT clear an active loop."""
        plugin.on_session_start({})

        edit = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(edit, {})

        assert plugin.load_state()["active_loop"] is not None

        # Read-only turn — loop should persist
        read = [{"tool": "Read", "target": "/path/to/file.py"}]
        plugin.on_stop(read, {})

        assert plugin.load_state()["active_loop"] is not None

    def test_test_commands_exempt(self, plugin):
        """Test/build commands should NOT trigger loop detection."""
        plugin.on_session_start({})

        # pytest is in TEST_COMMANDS — running it 5x is legitimate
        tool_calls = [{"tool": "Bash", "target": "pytest tests/ -v"}]
        for _ in range(5):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is None
        assert len(state["recent_attempts"]) == 0

    def test_npm_test_exempt(self, plugin):
        """npm commands (test/build/install) are exempt."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Bash", "target": "npm test"}]
        for _ in range(5):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is None

    def test_non_test_bash_tracked(self, plugin):
        """Non-test Bash commands should still be tracked for loops."""
        plugin.on_session_start({})

        # curl is not in TEST_COMMANDS
        tool_calls = [{"tool": "Bash", "target": "curl http://localhost:3000/api/health"}]
        for _ in range(4):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

    def test_session_summary(self, plugin):
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(tool_calls, {})

        summary = plugin.get_session_summary()
        assert summary["loops_detected"] == 1
        assert summary["active_loop"] == "/path/to/file.py"

    def test_empty_tool_calls_require_two_idle_turns(self, plugin):
        """Empty tool calls need 2 consecutive idle turns to clear loop."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(tool_calls, {})

        assert plugin.load_state()["active_loop"] is not None

        # First empty turn — loop persists
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is not None

        # Second empty turn — loop clears
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is None

    def test_history_size_limit(self, plugin):
        """Recent attempts should be limited to HISTORY_SIZE."""
        plugin.on_session_start({})

        for i in range(30):
            tool_calls = [{"tool": "Edit", "target": f"/path/to/file{i}.py"}]
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert len(state["recent_attempts"]) <= plugin.HISTORY_SIZE

    # --- Signature similarity tests ---

    def test_signature_similarity_identical(self, plugin):
        """Identical signatures have similarity 1.0."""
        sig = "Bash|curl|api:curl:health:http:localhost|"
        assert plugin._signature_similarity(sig, sig) == 1.0

    def test_signature_similarity_different_file(self, plugin):
        """Different file paths have similarity 0.0."""
        sig1 = "Edit|/path/to/auth.py||"
        sig2 = "Edit|/path/to/other.py||"
        assert plugin._signature_similarity(sig1, sig2) == 0.0

    def test_signature_similarity_different_tool(self, plugin):
        """Different tools have similarity 0.0."""
        sig1 = "Edit|/path/to/auth.py||"
        sig2 = "Bash|/path/to/auth.py||"
        assert plugin._signature_similarity(sig1, sig2) == 0.0

    def test_signature_similarity_partial_overlap(self, plugin):
        """Overlapping Bash identifiers produce partial similarity."""
        sig1 = "Bash|python|auth:manage:py:python:test:login|"
        sig2 = "Bash|python|auth:manage:py:python:test:signup|"
        sim = plugin._signature_similarity(sig1, sig2)
        # 5 shared (auth,manage,py,python,test) out of 7 total = 0.714
        assert 0.7 <= sim < 1.0

    def test_signature_similarity_different_bash_command(self, plugin):
        """Different base commands have similarity 0.0."""
        sig1 = "Bash|curl|curl:health|"
        sig2 = "Bash|wget|health:wget|"
        assert plugin._signature_similarity(sig1, sig2) == 0.0

    def test_signature_edit_same_file_always_matches(self, plugin):
        """Edit signatures for same file are always identical (path-only)."""
        sig1 = "Edit|/path/to/file.py||"
        sig2 = "Edit|/path/to/file.py||"
        assert plugin._signature_similarity(sig1, sig2) == 1.0

    # --- Fuzzy loop detection ---

    def test_fuzzy_loop_detection_bash(self, plugin):
        """Similar Bash commands should trigger fuzzy loop detection."""
        plugin.on_session_start({})

        # Four similar commands testing the same subsystem (python not in TEST_COMMANDS)
        tc1 = [{"tool": "Bash", "target": "python manage.py test auth login"}]
        tc2 = [{"tool": "Bash", "target": "python manage.py test auth signup"}]
        tc3 = [{"tool": "Bash", "target": "python manage.py test auth logout"}]
        tc4 = [{"tool": "Bash", "target": "python manage.py test auth reset"}]

        plugin.on_stop(tc1, {})
        plugin.on_stop(tc2, {})
        plugin.on_stop(tc3, {})
        plugin.on_stop(tc4, {})

        state = plugin.load_state()
        # All share python, manage, py, test, auth (5/7 >= 0.7)
        assert state["active_loop"] is not None

    def test_no_fuzzy_match_different_subsystems(self, plugin):
        """Bash commands targeting different subsystems should NOT loop."""
        plugin.on_session_start({})

        tc1 = [{"tool": "Bash", "target": "python manage.py test auth"}]
        tc2 = [{"tool": "Bash", "target": "python manage.py migrate database"}]
        tc3 = [{"tool": "Bash", "target": "python manage.py collectstatic"}]
        tc4 = [{"tool": "Bash", "target": "python setup.py develop"}]

        plugin.on_stop(tc1, {})
        plugin.on_stop(tc2, {})
        plugin.on_stop(tc3, {})
        plugin.on_stop(tc4, {})

        state = plugin.load_state()
        # These are different operations, low Jaccard similarity
        assert state["active_loop"] is None

    def test_write_tool_tracked_as_work(self, plugin):
        """Write tool calls should be tracked for loop detection."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Write", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

    def test_idle_counter_resets_on_work(self, plugin):
        """Work tool call resets the idle counter."""
        plugin.on_session_start({})

        edit = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(4):
            plugin.on_stop(edit, {})

        assert plugin.load_state()["active_loop"] is not None

        # One idle turn
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is not None

        # Work on same file resets idle counter
        plugin.on_stop(edit, {})
        assert plugin.load_state()["active_loop"] is not None

        # One idle turn again — loop persists (counter reset)
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is not None
