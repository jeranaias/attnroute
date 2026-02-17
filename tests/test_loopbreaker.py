"""Tests for LoopBreaker plugin v2.0.

Tool calls can be in legacy format {tool, target} or enriched format
{tool, target, input: {...}} from the rich transcript extractor.
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
        assert state["file_history"] == []
        assert state["error_patterns"] == {}

    def test_no_loop_on_single_attempt(self, plugin):
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py"},
        ]
        warning = plugin.on_stop(tool_calls, {})

        assert warning is None
        state = plugin.load_state()
        assert state["active_loop"] is None

    def test_loop_detection_at_notice_threshold(self, plugin):
        """Three edits to same file triggers NOTICE (threshold=3)."""
        plugin.on_session_start({})

        tool_calls = [
            {"tool": "Edit", "target": "/path/to/file.py"},
        ]

        # First two -- no loop yet
        for _ in range(2):
            plugin.on_stop(tool_calls, {})
        state = plugin.load_state()
        assert state["active_loop"] is None

        # Third attempt -- triggers NOTICE
        warning = plugin.on_stop(tool_calls, {})
        assert warning is not None
        assert "LoopBreaker" in warning

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["active_loop"]["severity"] == "NOTICE"
        assert state["loops_detected"] == 1

    def test_loop_breaking_on_different_file(self, plugin):
        """Working on a different file breaks the loop."""
        plugin.on_session_start({})

        same_edit = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(3):
            plugin.on_stop(same_edit, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

        # Different file -- should break loop
        different_edit = [{"tool": "Edit", "target": "/path/to/other.py"}]
        plugin.on_stop(different_edit, {})

        state = plugin.load_state()
        assert state["active_loop"] is None
        assert state["loops_broken"] == 1

    def test_loop_context_injection_notice(self, plugin):
        """NOTICE severity injects gentle reminder."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        context = plugin.on_prompt_post("test prompt", "existing context", {})

        assert "LoopBreaker Alert" in context
        assert "file.py" in context
        assert "NOTICE" in context
        assert "verifying" in context.lower()

    def test_loop_context_injection_warning(self, plugin):
        """WARNING severity at 5+ repetitions."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(5):
            plugin.on_stop(tool_calls, {})

        context = plugin.on_prompt_post("test prompt", "existing context", {})
        assert "WARNING" in context
        assert "different" in context.lower()

    def test_loop_context_injection_critical(self, plugin):
        """CRITICAL severity at 7+ repetitions."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(7):
            plugin.on_stop(tool_calls, {})

        context = plugin.on_prompt_post("test prompt", "existing context", {})
        assert "CRITICAL" in context
        assert "STOP" in context
        assert "user" in context.lower()

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
        for _ in range(3):
            plugin.on_stop(edit, {})

        assert plugin.load_state()["active_loop"] is not None

        # Read-only turn -- loop should persist
        read = [{"tool": "Read", "target": "/path/to/file.py"}]
        plugin.on_stop(read, {})

        assert plugin.load_state()["active_loop"] is not None

    def test_test_commands_exempt(self, plugin):
        """Test/build commands should NOT trigger loop detection."""
        plugin.on_session_start({})

        # pytest is in TEST_COMMANDS -- running it 5x is legitimate
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
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

    def test_session_summary(self, plugin):
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        summary = plugin.get_session_summary()
        assert summary["loops_detected"] == 1
        assert summary["active_loop"] == "/path/to/file.py"
        assert summary["active_severity"] == "NOTICE"

    def test_empty_tool_calls_require_two_idle_turns(self, plugin):
        """Empty tool calls need 2 consecutive idle turns to clear loop."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        assert plugin.load_state()["active_loop"] is not None

        # First empty turn -- loop persists
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is not None

        # Second empty turn -- loop clears
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

    def test_signature_edit_same_file_no_content_matches(self, plugin):
        """Edit signatures for same file without content hash are identical (legacy)."""
        sig1 = "Edit|/path/to/file.py||"
        sig2 = "Edit|/path/to/file.py||"
        assert plugin._signature_similarity(sig1, sig2) == 1.0

    def test_fuzzy_loop_detection_bash(self, plugin):
        """Similar Bash commands should trigger fuzzy loop detection."""
        plugin.on_session_start({})

        # Three similar commands (threshold=3)
        tc1 = [{"tool": "Bash", "target": "python manage.py test auth login"}]
        tc2 = [{"tool": "Bash", "target": "python manage.py test auth signup"}]
        tc3 = [{"tool": "Bash", "target": "python manage.py test auth logout"}]

        plugin.on_stop(tc1, {})
        plugin.on_stop(tc2, {})
        plugin.on_stop(tc3, {})

        state = plugin.load_state()
        # All share python, manage, py, test, auth (5/7 >= 0.7)
        assert state["active_loop"] is not None

    def test_no_fuzzy_match_different_subsystems(self, plugin):
        """Bash commands targeting different subsystems should NOT loop."""
        plugin.on_session_start({})

        tc1 = [{"tool": "Bash", "target": "python manage.py test auth"}]
        tc2 = [{"tool": "Bash", "target": "python manage.py migrate database"}]
        tc3 = [{"tool": "Bash", "target": "python manage.py collectstatic"}]

        plugin.on_stop(tc1, {})
        plugin.on_stop(tc2, {})
        plugin.on_stop(tc3, {})

        state = plugin.load_state()
        # These are different operations, low Jaccard similarity
        assert state["active_loop"] is None

    def test_write_tool_tracked_as_work(self, plugin):
        """Write tool calls should be tracked for loop detection."""
        plugin.on_session_start({})

        tool_calls = [{"tool": "Write", "target": "/path/to/file.py"}]
        for _ in range(3):
            plugin.on_stop(tool_calls, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

    def test_idle_counter_resets_on_work(self, plugin):
        """Work tool call resets the idle counter."""
        plugin.on_session_start({})

        edit = [{"tool": "Edit", "target": "/path/to/file.py"}]
        for _ in range(3):
            plugin.on_stop(edit, {})

        assert plugin.load_state()["active_loop"] is not None

        # One idle turn
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is not None

        # Work on same file resets idle counter
        plugin.on_stop(edit, {})
        assert plugin.load_state()["active_loop"] is not None

        # One idle turn again -- loop persists (counter reset)
        plugin.on_stop([], {})
        assert plugin.load_state()["active_loop"] is not None

    # === v2.0 Tests: Content-Aware Signatures ===

    def test_content_aware_edit_different_content_no_loop(self, plugin):
        """Different edits to the same file should NOT trigger loop."""
        plugin.on_session_start({})

        # Three different edits to the same file
        tc1 = [{"tool": "Edit", "target": "/file.py", "input": {
            "old_string": "def foo():", "new_string": "def bar():"
        }}]
        tc2 = [{"tool": "Edit", "target": "/file.py", "input": {
            "old_string": "x = 1", "new_string": "x = 2"
        }}]
        tc3 = [{"tool": "Edit", "target": "/file.py", "input": {
            "old_string": "import os", "new_string": "import sys"
        }}]

        plugin.on_stop(tc1, {})
        plugin.on_stop(tc2, {})
        plugin.on_stop(tc3, {})

        state = plugin.load_state()
        # Different content = different signatures = no loop
        assert state["active_loop"] is None

    def test_content_aware_edit_same_content_loops(self, plugin):
        """Identical edits to the same file SHOULD trigger loop."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py", "input": {
            "old_string": "def foo():", "new_string": "def bar():"
        }}]

        for _ in range(3):
            plugin.on_stop(tc, {})

        state = plugin.load_state()
        assert state["active_loop"] is not None

    def test_signature_with_content_hash(self, plugin):
        """Signatures with content should include hash."""
        tc = {
            "tool": "Edit",
            "target": "/file.py",
            "input": {"old_string": "a", "new_string": "b"},
        }
        sig = plugin._create_signature(tc)
        parts = sig.split("|")
        assert parts[0] == "Edit"
        assert parts[2] != ""  # Should have a content hash

    def test_signature_without_content_no_hash(self, plugin):
        """Legacy signatures without input should have empty hash."""
        tc = {"tool": "Edit", "target": "/file.py"}
        sig = plugin._create_signature(tc)
        parts = sig.split("|")
        assert parts[2] == ""  # No content hash

    def test_content_hash_similarity_same(self, plugin):
        """Same content hash = 1.0 similarity."""
        sig1 = "Edit|/file.py|abc12345|"
        sig2 = "Edit|/file.py|abc12345|"
        assert plugin._signature_similarity(sig1, sig2) == 1.0

    def test_content_hash_similarity_different(self, plugin):
        """Different content hashes = 0.3 similarity (same file, different edit)."""
        sig1 = "Edit|/file.py|abc12345|"
        sig2 = "Edit|/file.py|xyz98765|"
        assert plugin._signature_similarity(sig1, sig2) == 0.3

    def test_content_hash_mixed_legacy(self, plugin):
        """One with hash, one without = 0.5 similarity."""
        sig1 = "Edit|/file.py|abc12345|"
        sig2 = "Edit|/file.py||"
        assert plugin._signature_similarity(sig1, sig2) == 0.5

    # === v2.0 Tests: Escalating Severity ===

    def test_severity_escalation(self, plugin):
        """Severity should escalate as repetitions increase."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]

        # 3 reps: NOTICE
        for _ in range(3):
            plugin.on_stop(tc, {})
        assert plugin.load_state()["active_loop"]["severity"] == "NOTICE"

        # 5 reps: WARNING
        for _ in range(2):
            plugin.on_stop(tc, {})
        assert plugin.load_state()["active_loop"]["severity"] == "WARNING"

        # 7 reps: CRITICAL
        for _ in range(2):
            plugin.on_stop(tc, {})
        assert plugin.load_state()["active_loop"]["severity"] == "CRITICAL"

    def test_severity_in_warning_message(self, plugin):
        """Warning message should include severity tag for WARNING/CRITICAL."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]

        # Build up to WARNING
        for _ in range(5):
            warning = plugin.on_stop(tc, {})

        assert "[WARNING]" in warning

    # === v2.0 Tests: Multi-File Bounce Detection ===

    def test_bounce_detection(self, plugin):
        """Alternating between two files should be detected."""
        plugin.on_session_start({})

        tc_a = [{"tool": "Edit", "target": "/a.py"}]
        tc_b = [{"tool": "Edit", "target": "/b.py"}]

        # Alternate: A B A B A B
        for _ in range(3):
            plugin.on_stop(tc_a, {})
            plugin.on_stop(tc_b, {})

        summary = plugin.get_session_summary()
        assert "bounce_detected" in summary

    def test_no_bounce_without_alternation(self, plugin):
        """Non-alternating patterns should not trigger bounce."""
        plugin.on_session_start({})

        # A A B B A A -- not alternating
        for _ in range(2):
            plugin.on_stop([{"tool": "Edit", "target": "/a.py"}], {})
        for _ in range(2):
            plugin.on_stop([{"tool": "Edit", "target": "/b.py"}], {})
        for _ in range(2):
            plugin.on_stop([{"tool": "Edit", "target": "/a.py"}], {})

        summary = plugin.get_session_summary()
        assert "bounce_detected" not in summary

    def test_bounce_in_context(self, plugin):
        """Bounce warning should appear in prompt context."""
        plugin.on_session_start({})

        tc_a = [{"tool": "Edit", "target": "/a.py"}]
        tc_b = [{"tool": "Edit", "target": "/b.py"}]

        # Alternate to trigger bounce + enough for loop detection
        for _ in range(4):
            plugin.on_stop(tc_a, {})
            plugin.on_stop(tc_b, {})

        # Force active loop for context injection
        state = plugin.load_state()
        state["active_loop"] = {"file": "/a.py", "count": 5, "severity": "WARNING", "pattern_desc": "test"}
        plugin.save_state(state)

        context = plugin.on_prompt_post("test", "", {})
        assert "Bounce detected" in context

    # === v2.0 Tests: Error Tracking ===

    def test_error_pattern_tracking(self, plugin):
        """Repeated Bash commands should be tracked."""
        plugin.on_session_start({})

        tc = [{"tool": "Bash", "target": "curl http://failing-api.com/endpoint"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        state = plugin.load_state()
        assert len(state["error_patterns"]) > 0

    def test_error_patterns_capped(self, plugin):
        """Error patterns should be capped to prevent unbounded growth."""
        plugin.on_session_start({})

        # Generate many different commands
        for i in range(60):
            tc = [{"tool": "Bash", "target": f"cmd_{i} --flag value"}]
            plugin.on_stop(tc, {})

        state = plugin.load_state()
        assert len(state["error_patterns"]) <= 50

    def test_error_context_in_prompt(self, plugin):
        """Repeated errors should show in prompt context."""
        plugin.on_session_start({})

        tc = [{"tool": "Bash", "target": "failing_command --arg"}]
        for _ in range(4):
            plugin.on_stop(tc, {})

        # Need active loop for context injection
        state = plugin.load_state()
        state["active_loop"] = {"file": "failing_command", "count": 4, "severity": "WARNING", "pattern_desc": "test"}
        plugin.save_state(state)

        context = plugin.on_prompt_post("test", "", {})
        assert "Recent errors" in context

    # === v2.0 Tests: Cross-Session Analytics ===

    def test_analytics_preserved_across_sessions(self, plugin):
        """Loop analytics should persist across sessions."""
        plugin.on_session_start({"session_id": "s1"})

        tc = [{"tool": "Edit", "target": "/file.py"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        state = plugin.load_state()
        assert state["loops_detected"] == 1

        # New session
        plugin.on_session_start({"session_id": "s2"})
        state = plugin.load_state()
        assert state["loops_detected"] == 0  # Session counter reset
        assert state["analytics"]["total_loops"] == 1  # Cross-session preserved
        assert state["analytics"]["sessions"] == 2

    def test_session_summary_includes_analytics(self, plugin):
        """Session summary should include cross-session analytics."""
        plugin.on_session_start({})

        summary = plugin.get_session_summary()
        assert "total_loops_all_time" in summary
        assert "sessions" in summary

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
        plugin.on_stop(rich_calls, {})

        state = plugin.load_state()
        # Should have a content-aware signature
        sig = state["recent_attempts"][0]["signature"]
        parts = sig.split("|")
        assert parts[2] != ""  # Has content hash

    def test_file_history_capped(self, plugin):
        """File history should be capped to prevent unbounded growth."""
        plugin.on_session_start({})

        for i in range(40):
            plugin.on_stop([{"tool": "Edit", "target": f"/file{i}.py"}], {})

        state = plugin.load_state()
        assert len(state["file_history"]) <= 30

    # ---- Consecutive failure tracking tests ----

    def test_consecutive_failures_detected(self, plugin):
        """3+ identical Bash failures should trigger active_loop."""
        plugin.on_session_start({})

        # 3 identical failing commands
        for i in range(3):
            plugin.on_stop([{
                "tool": "Bash",
                "target": "pytest tests/",
                "input": {
                    "command": "pytest tests/",
                    "exit_code": 1,
                    "stderr": "FAILED error: test_foo",
                },
            }], {})

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["active_loop"]["pattern_desc"] == "identical consecutive failures"

    def test_consecutive_failures_reset_on_success(self, plugin):
        """A successful command should reset consecutive failure tracking."""
        plugin.on_session_start({})

        # 2 failures
        for _ in range(2):
            plugin.on_stop([{
                "tool": "Bash",
                "target": "pytest tests/",
                "input": {
                    "command": "pytest tests/",
                    "exit_code": 1,
                    "stderr": "FAILED error: test_foo",
                },
            }], {})

        # 1 success (edit counts as success)
        plugin.on_stop([{
            "tool": "Edit",
            "target": "/fix.py",
        }], {})

        state = plugin.load_state()
        # Consecutive failures should be cleared
        assert state.get("consecutive_failures", []) == []

    def test_consecutive_failures_escalation(self, plugin):
        """5+ identical failures should escalate to CRITICAL."""
        plugin.on_session_start({})

        for _ in range(5):
            plugin.on_stop([{
                "tool": "Bash",
                "target": "npm test",
                "input": {
                    "command": "npm test",
                    "exit_code": 1,
                    "stderr": "error: Cannot find module",
                },
            }], {})

        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["active_loop"]["severity"] == "CRITICAL"

    def test_consecutive_failures_capped(self, plugin):
        """Consecutive failures list should not grow unbounded."""
        plugin.on_session_start({})

        for i in range(15):
            plugin.on_stop([{
                "tool": "Bash",
                "target": f"cmd{i}",
                "input": {
                    "command": f"cmd{i}",
                    "exit_code": 1,
                    "stderr": "error: failed",
                },
            }], {})

        state = plugin.load_state()
        assert len(state.get("consecutive_failures", [])) <= 10

    def test_error_output_patterns_detected(self, plugin):
        """Error patterns in stdout/stderr should mark as failure."""
        plugin.on_session_start({})

        # Command with exit_code=0 but error pattern in output
        for _ in range(3):
            plugin.on_stop([{
                "tool": "Bash",
                "target": "python script.py",
                "input": {
                    "command": "python script.py",
                    "stdout": "Traceback (most recent call last):\n  File ...",
                },
            }], {})

        state = plugin.load_state()
        # Should detect via "traceback" pattern
        failures = state.get("consecutive_failures", [])
        assert len(failures) >= 3

    # === v3.0 Tests: Git Progress Detection ===

    def test_git_diff_hash_timeout(self, plugin, monkeypatch):
        """_git_diff_hash returns None on timeout."""
        import subprocess

        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=2)

        monkeypatch.setattr("subprocess.run", mock_run)
        assert plugin._git_diff_hash() is None

    def test_git_diff_hash_not_git_repo(self, plugin, monkeypatch):
        """_git_diff_hash returns None when git not available."""
        def mock_run(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr("subprocess.run", mock_run)
        assert plugin._git_diff_hash() is None

    def test_git_diff_hash_returns_hash(self, plugin, monkeypatch):
        """_git_diff_hash returns truncated SHA256 on success."""
        import subprocess

        def mock_run(*args, **kwargs):
            result = subprocess.CompletedProcess(args=args[0], returncode=0, stdout="file.py | 2 +-\n")
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        h = plugin._git_diff_hash()
        assert h is not None
        assert len(h) == 16

    def test_zero_progress_increments(self, plugin, monkeypatch):
        """zero_progress_turns increments when git diff unchanged."""
        plugin.on_session_start({})

        # Mock _git_diff_hash to return same hash
        monkeypatch.setattr(plugin, "_git_diff_hash", lambda: "aaaa1111bbbb2222")

        state = plugin.load_state()
        state["git_diff_hash"] = "aaaa1111bbbb2222"  # Same as mock
        plugin.save_state(state)

        plugin._check_git_progress(state, had_work=True)
        assert state["zero_progress_turns"] == 1

        plugin._check_git_progress(state, had_work=True)
        assert state["zero_progress_turns"] == 2

    def test_zero_progress_resets_on_change(self, plugin, monkeypatch):
        """zero_progress_turns resets when git diff changes."""
        plugin.on_session_start({})

        state = plugin.load_state()
        state["git_diff_hash"] = "aaaa1111bbbb2222"
        state["zero_progress_turns"] = 3
        plugin.save_state(state)

        # Different hash = progress
        monkeypatch.setattr(plugin, "_git_diff_hash", lambda: "cccc3333dddd4444")

        plugin._check_git_progress(state, had_work=True)
        assert state["zero_progress_turns"] == 0

    def test_zero_progress_skips_no_work(self, plugin, monkeypatch):
        """_check_git_progress skips when no work tools used."""
        plugin.on_session_start({})

        called = []
        monkeypatch.setattr(plugin, "_git_diff_hash", lambda: (called.append(1), "hash")[1])

        state = plugin.load_state()
        plugin._check_git_progress(state, had_work=False)
        assert len(called) == 0  # Should not call _git_diff_hash

    # === v3.0 Tests: Multi-Metric Severity Score ===

    def test_severity_score_zero_clean(self, plugin):
        """Clean state should produce score 0.0."""
        plugin.on_session_start({})
        state = plugin.load_state()
        score = plugin._compute_severity_score(state)
        assert score == 0.0

    def test_severity_score_max(self, plugin):
        """Maximum signals should produce score close to 1.0."""
        plugin.on_session_start({})
        state = plugin.load_state()
        state["active_loop"] = {"count": 7}  # At CRITICAL_THRESHOLD
        state["consecutive_failures"] = ["cmd"] * 5
        state["zero_progress_turns"] = 4
        plugin.save_state(state)

        score = plugin._compute_severity_score(state)
        assert score == 1.0

    def test_severity_score_combines_signals(self, plugin):
        """Partial signals combine proportionally."""
        plugin.on_session_start({})
        state = plugin.load_state()

        # Only signature repetition: 3/7 * 0.5 ≈ 0.214
        state["active_loop"] = {"count": 3}
        state["consecutive_failures"] = []
        state["zero_progress_turns"] = 0
        plugin.save_state(state)

        score = plugin._compute_severity_score(state)
        expected = 0.5 * (3 / 7)
        assert abs(score - expected) < 0.01

    # === v3.0 Tests: State Machine Stages ===

    def test_stage_detected_on_new_loop(self, plugin):
        """New loop detection starts in 'detected' stage."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        state = plugin.load_state()
        assert state["active_loop"]["stage"] == "detected"

    def test_stage_escalated_on_severity_change(self, plugin):
        """Stage transitions to 'escalated' when severity changes."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]

        # 3 reps: NOTICE + detected
        for _ in range(3):
            plugin.on_stop(tc, {})
        assert plugin.load_state()["active_loop"]["stage"] == "detected"

        # 5 reps: WARNING → escalated (severity changed)
        for _ in range(2):
            plugin.on_stop(tc, {})
        assert plugin.load_state()["active_loop"]["stage"] == "escalated"

    def test_stage_cooling_on_idle(self, plugin):
        """Stage transitions to 'cooling' after 1 idle turn."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        assert plugin.load_state()["active_loop"] is not None

        # 1 idle turn → cooling
        plugin.on_stop([], {})
        state = plugin.load_state()
        assert state["active_loop"] is not None
        assert state["active_loop"]["stage"] == "cooling"

    def test_cooling_stage_context_message(self, plugin):
        """Cooling stage shows gentle recovery message."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        # Transition to cooling
        plugin.on_stop([], {})

        context = plugin.on_prompt_post("test", "", {})
        assert "Recovering" in context
        assert "STOP" not in context

    def test_git_no_progress_message_in_context(self, plugin):
        """Zero progress warning appears in context when active loop + no git changes."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        # Set zero progress state
        state = plugin.load_state()
        state["zero_progress_turns"] = 3
        plugin.save_state(state)

        context = plugin.on_prompt_post("test", "", {})
        assert "No git changes detected" in context
        assert "3 turns" in context

    def test_session_start_initializes_v3_fields(self, plugin):
        """v3.0 state fields initialized on session start."""
        plugin.on_session_start({})
        state = plugin.load_state()
        assert state["git_diff_hash"] is None
        assert state["zero_progress_turns"] == 0
        assert state["turn_number"] == 0

    def test_turn_number_increments(self, plugin):
        """turn_number increments with each on_stop call."""
        plugin.on_session_start({})

        plugin.on_stop([{"tool": "Edit", "target": "/a.py"}], {})
        assert plugin.load_state()["turn_number"] == 1

        plugin.on_stop([{"tool": "Edit", "target": "/b.py"}], {})
        assert plugin.load_state()["turn_number"] == 2

    def test_multi_score_in_session_summary(self, plugin):
        """Session summary includes multi_score when loop active."""
        plugin.on_session_start({})

        tc = [{"tool": "Edit", "target": "/file.py"}]
        for _ in range(3):
            plugin.on_stop(tc, {})

        summary = plugin.get_session_summary()
        assert "multi_score" in summary
        assert summary["multi_score"] >= 0

    def test_zero_progress_in_session_summary(self, plugin):
        """Session summary includes zero_progress_turns."""
        plugin.on_session_start({})
        summary = plugin.get_session_summary()
        assert "zero_progress_turns" in summary
        assert summary["zero_progress_turns"] == 0
