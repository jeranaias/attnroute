"""Tests for ContextGuard plugin — post-compaction amnesia prevention."""

import json
from unittest.mock import patch

import pytest


class TestContextGuard:
    """Test ContextGuard plugin functionality."""

    @pytest.fixture
    def plugin(self, tmp_path, monkeypatch):
        from attnroute.plugins.base import AttnroutePlugin
        from attnroute.plugins.contextguard import ContextGuardPlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)
        return ContextGuardPlugin()

    @pytest.fixture
    def attn_file(self, tmp_path):
        """Create a mock attention state file."""
        f = tmp_path / "attn_state.json"
        return f

    def _write_attn(self, attn_file, scores: dict):
        """Write scores to attention state file."""
        attn_file.write_text(json.dumps({
            "scores": scores,
            "turn_count": 5,
            "consecutive_turns": {},
        }), encoding="utf-8")

    def _patch_attn(self, attn_file):
        """Return a patch context manager for ATTN_STATE_PROJECT."""
        return patch("attnroute.plugins.contextguard.ContextGuardPlugin._get_active_files")

    def test_session_start_initializes_state(self, plugin):
        result = plugin.on_session_start({"session_id": "test123"})
        assert "ContextGuard" in result

        state = plugin.load_state()
        assert state["session_id"] == "test123"
        assert state["turn_count"] == 0
        assert state["last_active_count"] == 0
        assert state["last_snapshot"] == []
        assert state["current_snapshot"] == []
        assert state["recoveries"] == 0

    def test_no_detection_on_first_turns(self, plugin):
        """Should not trigger on first 2 turns (MIN_TURNS_FOR_DETECTION)."""
        plugin.on_session_start({"session_id": "s1"})

        # Simulate 10 active files on turn 1
        with patch.object(plugin, "_get_active_files", return_value=[
            (f"file{i}.py", 0.9 - i * 0.05) for i in range(10)
        ]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert result == ""

        # Turn 2: drop to 0 — should NOT trigger (too early)
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert result == ""

    def test_compaction_detected_after_baseline(self, plugin):
        """After enough turns, a 50%+ drop triggers recovery."""
        plugin.on_session_start({"session_id": "s1"})

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]

        # Build baseline over 3 turns
        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_10):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Turn 4: sudden drop to 2 files (compaction)
        with patch.object(plugin, "_get_active_files", return_value=[
            ("file0.py", 0.9), ("file1.py", 0.85)
        ]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert "Context Recovery" in result
            assert "file0.py" in result

        state = plugin.load_state()
        assert state["recoveries"] == 1

    def test_no_false_positive_on_project_switch(self, plugin):
        """Project switch changes session_id — should reset, not trigger."""
        plugin.on_session_start({"session_id": "s1"})

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]

        # Build baseline
        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_10):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # New session (project switch) with 0 files — should NOT trigger
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s2"})
            assert result == ""

        state = plugin.load_state()
        assert state["session_id"] == "s2"
        assert state["turn_count"] == 0
        assert state["recoveries"] == 0

    def test_no_false_positive_on_small_drop(self, plugin):
        """A drop from 10 to 8 (20%) should NOT trigger."""
        plugin.on_session_start({"session_id": "s1"})

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]
        active_8 = active_10[:8]

        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_10):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        with patch.object(plugin, "_get_active_files", return_value=active_8):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert result == ""

    def test_min_absolute_drop_prevents_decay_false_positive(self, plugin):
        """Drop from 4 to 1 (75%) but only 3 absolute — borderline."""
        plugin.on_session_start({"session_id": "s1"})

        active_4 = [(f"file{i}.py", 0.5 - i * 0.05) for i in range(4)]

        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_4):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Drop to 1 file: absolute drop = 3, passes MIN_ABSOLUTE_DROP
        with patch.object(plugin, "_get_active_files", return_value=[("file0.py", 0.5)]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert "Context Recovery" in result

    def test_snapshot_uses_last_turn_not_current(self, plugin):
        """Recovery should use last_snapshot (turn N-1), not current_snapshot."""
        plugin.on_session_start({"session_id": "s1"})

        turn1_files = [(f"a{i}.py", 0.9) for i in range(10)]
        turn2_files = [(f"b{i}.py", 0.9) for i in range(10)]

        # Turn 1: snapshot has a*.py files
        with patch.object(plugin, "_get_active_files", return_value=turn1_files):
            plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Turn 2: snapshot has b*.py files
        with patch.object(plugin, "_get_active_files", return_value=turn2_files):
            plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Turn 3: another set
        turn3_files = [(f"c{i}.py", 0.9) for i in range(10)]
        with patch.object(plugin, "_get_active_files", return_value=turn3_files):
            plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Turn 4: compaction — drop to 0
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            # Should recover turn 3's files (current_snapshot = most recent pre-compaction)
            assert "c0.py" in result
            # Should NOT have turn 1's files
            assert "a0.py" not in result

    def test_max_recoveries_prevents_spam(self, plugin):
        """After MAX_RECOVERIES, stop injecting."""
        plugin.on_session_start({"session_id": "s1"})
        plugin.MAX_RECOVERIES = 2

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]

        for recovery_num in range(3):
            # Build up
            for _ in range(3):
                with patch.object(plugin, "_get_active_files", return_value=active_10):
                    plugin.on_prompt_post("test", "", {"session_id": "s1"})

            # Compaction
            with patch.object(plugin, "_get_active_files", return_value=[]):
                result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
                if recovery_num < 2:
                    assert "Context Recovery" in result
                else:
                    assert result == ""  # Capped

    def test_empty_state_no_crash(self, plugin):
        """Plugin handles missing/empty state gracefully."""
        # Don't call on_session_start — simulate corrupt/missing state
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert result == ""

    def test_format_recovery_output(self, plugin):
        """Recovery block has correct format with heat indicators."""
        snapshot = [
            {"path": "src/auth.py", "score": 0.9},
            {"path": "src/session.py", "score": 0.5},
            {"path": "src/utils.py", "score": 0.3},
        ]
        result = plugin._format_recovery(snapshot)
        assert "## Context Recovery" in result
        assert "`src/auth.py`" in result
        assert "`src/session.py`" in result
        assert "`src/utils.py`" in result
        assert "(hot)" in result      # 0.9 >= 0.7
        assert "(warm)" in result     # 0.5 >= 0.4
        assert "(cool)" in result     # 0.3 < 0.4
        assert "Re-read any you need" in result

    def test_session_summary(self, plugin):
        """get_session_summary returns correct diagnostics."""
        plugin.on_session_start({"session_id": "s1"})

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]

        # Build baseline over 3 turns
        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_10):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        summary = plugin.get_session_summary()
        assert summary["session_id"] == "s1"
        assert summary["turn_count"] == 3
        assert summary["last_active_count"] == 10
        assert summary["snapshot_files"] == 10
        assert summary["recoveries"] == 0
        assert summary["max_recoveries"] == 5
        assert summary["recoveries_remaining"] == 5

    def test_fluctuating_counts_no_false_positive(self, plugin):
        """Counts go 10→8→12→5 — only the 12→5 drop matters."""
        plugin.on_session_start({"session_id": "s1"})

        files_10 = [(f"file{i}.py", 0.9) for i in range(10)]
        files_8 = files_10[:8]
        files_12 = [(f"file{i}.py", 0.9) for i in range(12)]
        files_5 = files_12[:5]

        # Turn 1: 10 files
        with patch.object(plugin, "_get_active_files", return_value=files_10):
            plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Turn 2: 8 files (small drop, no trigger)
        with patch.object(plugin, "_get_active_files", return_value=files_8):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert result == ""

        # Turn 3: 12 files (growth)
        with patch.object(plugin, "_get_active_files", return_value=files_12):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert result == ""

        # Turn 4: 5 files (12→5 = 58% drop, absolute 7) — TRIGGERS
        with patch.object(plugin, "_get_active_files", return_value=files_5):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert "Context Recovery" in result

    def test_double_compaction(self, plugin):
        """Two compactions in a row — second uses the post-first snapshot."""
        plugin.on_session_start({"session_id": "s1"})

        files_a = [(f"a{i}.py", 0.9) for i in range(10)]
        files_b = [(f"b{i}.py", 0.9) for i in range(8)]

        # Build baseline with a-files
        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=files_a):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # First compaction: drop to 0
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result1 = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert "a0.py" in result1

        # Rebuild with b-files
        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=files_b):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Second compaction: drop to 0
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result2 = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert "b0.py" in result2
            assert "a0.py" not in result2

        state = plugin.load_state()
        assert state["recoveries"] == 2

    def test_recovery_includes_heat_indicators(self, plugin):
        """Recovery output uses heat indicators based on score."""
        plugin.on_session_start({"session_id": "s1"})

        # Build baseline with mixed scores
        mixed_files = [
            ("hot_file.py", 0.9),
            ("warm_file.py", 0.5),
            ("cool_file.py", 0.3),
        ] + [(f"pad{i}.py", 0.9) for i in range(7)]

        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=mixed_files):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        # Compaction
        with patch.object(plugin, "_get_active_files", return_value=[]):
            result = plugin.on_prompt_post("test", "", {"session_id": "s1"})
            assert "(hot)" in result
            assert "(warm)" in result
            assert "(cool)" in result

    # === v1.0 Tests: Injection Size Tracking ===

    def test_injection_size_tracked(self, plugin):
        """Each turn's context_output length is tracked."""
        plugin.on_session_start({"session_id": "s1"})

        with patch.object(plugin, "_get_active_files", return_value=[]):
            plugin.on_prompt_post("test", "x" * 5000, {"session_id": "s1"})
            plugin.on_prompt_post("test", "y" * 8000, {"session_id": "s1"})

        state = plugin.load_state()
        assert state["injection_sizes"] == [5000, 8000]

    def test_injection_sizes_capped(self, plugin):
        """injection_sizes is capped at last 10 entries."""
        plugin.on_session_start({"session_id": "s1"})

        with patch.object(plugin, "_get_active_files", return_value=[]):
            for i in range(15):
                plugin.on_prompt_post("test", "x" * (i * 100), {"session_id": "s1"})

        state = plugin.load_state()
        assert len(state["injection_sizes"]) == 10

    # === v1.0 Tests: Compaction Prediction ===

    def test_predict_risk_none_early(self, plugin):
        """Prediction returns 'none' for early turns."""
        plugin.on_session_start({"session_id": "s1"})
        state = plugin.load_state()
        state["turn_count"] = 5
        state["injection_sizes"] = [1000, 1000, 1000]

        risk, _ = plugin._predict_compaction_risk(state)
        assert risk == "none"

    def test_predict_risk_none_few_samples(self, plugin):
        """Prediction returns 'none' with too few injection samples."""
        plugin.on_session_start({"session_id": "s1"})
        state = plugin.load_state()
        state["turn_count"] = 50
        state["injection_sizes"] = [5000, 5000]

        risk, _ = plugin._predict_compaction_risk(state)
        assert risk == "none"

    def test_predict_risk_medium(self, plugin):
        """Prediction returns 'medium' at turn > 40 with injection > 10k."""
        plugin.on_session_start({"session_id": "s1"})
        state = plugin.load_state()
        state["turn_count"] = 45
        state["injection_sizes"] = [11_000] * 5

        risk, reason = plugin._predict_compaction_risk(state)
        assert risk == "medium"
        assert "turn 45" in reason

    def test_predict_risk_high(self, plugin):
        """Prediction returns 'high' at turn > 60 with injection > 15k."""
        plugin.on_session_start({"session_id": "s1"})
        state = plugin.load_state()
        state["turn_count"] = 65
        state["injection_sizes"] = [16_000] * 5

        risk, reason = plugin._predict_compaction_risk(state)
        assert risk == "high"
        assert "turn 65" in reason

    def test_predict_growing_injection(self, plugin):
        """Prediction detects growing injection trend."""
        plugin.on_session_start({"session_id": "s1"})
        state = plugin.load_state()
        state["turn_count"] = 35
        # Growing: first half avg ~5k, second half avg ~9k (80% growth > 20%)
        state["injection_sizes"] = [4000, 5000, 5500, 6000, 8500, 9000, 9500, 10000]

        risk, reason = plugin._predict_compaction_risk(state)
        # turn > 30, growing, last > 8000
        assert risk == "low"
        assert "growing" in reason

    def test_prediction_warning_injected(self, plugin):
        """Medium/high risk injects prediction warning into context."""
        plugin.on_session_start({"session_id": "s1"})

        # Set up state for medium risk
        state = plugin.load_state()
        state["turn_count"] = 44  # Will become 45 on next call
        state["injection_sizes"] = [11_000] * 5
        plugin.save_state(state)

        with patch.object(plugin, "_get_active_files", return_value=[]):
            result = plugin.on_prompt_post("test", "x" * 11000, {"session_id": "s1"})

        assert "ContextGuard Prediction" in result
        assert "compaction risk" in result

    # === v1.0 Tests: Recovery Analytics ===

    def test_recovery_timestamp_stored(self, plugin):
        """Recovery stores ISO timestamp."""
        plugin.on_session_start({"session_id": "s1"})

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]

        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_10):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        with patch.object(plugin, "_get_active_files", return_value=[]):
            plugin.on_prompt_post("test", "", {"session_id": "s1"})

        state = plugin.load_state()
        assert len(state["recovery_timestamps"]) == 1
        # Verify it's a valid ISO timestamp
        from datetime import datetime
        datetime.fromisoformat(state["recovery_timestamps"][0])

    # === v1.0 Tests: CLAUDE.md Hint ===

    def test_claude_md_hint_in_recovery(self, plugin):
        """Recovery block includes CLAUDE.md re-read hint."""
        snapshot = [
            {"path": "src/main.py", "score": 0.9},
        ]
        result = plugin._format_recovery(snapshot)
        assert "CLAUDE.md" in result
        assert "re-reading after compaction" in result

    # === v1.0 Tests: Cross-Plugin Flag ===

    def test_compaction_flag_written(self, plugin, tmp_path):
        """Compaction detection writes flag file for VerifyFirst."""
        plugin.on_session_start({"session_id": "s1"})

        active_10 = [(f"file{i}.py", 0.9 - i * 0.05) for i in range(10)]

        for _ in range(3):
            with patch.object(plugin, "_get_active_files", return_value=active_10):
                plugin.on_prompt_post("test", "", {"session_id": "s1"})

        with patch.object(plugin, "_get_active_files", return_value=[]):
            plugin.on_prompt_post("test", "", {"session_id": "s1"})

        flag_file = tmp_path / "compaction_occurred.flag"
        assert flag_file.exists()

    def test_injection_sizes_reset_on_session_change(self, plugin):
        """injection_sizes resets when session_id changes."""
        plugin.on_session_start({"session_id": "s1"})

        with patch.object(plugin, "_get_active_files", return_value=[]):
            plugin.on_prompt_post("test", "x" * 5000, {"session_id": "s1"})

        state = plugin.load_state()
        assert len(state["injection_sizes"]) == 1

        # Session change — resets
        with patch.object(plugin, "_get_active_files", return_value=[]):
            plugin.on_prompt_post("test", "y" * 3000, {"session_id": "s2"})

        state = plugin.load_state()
        assert state["injection_sizes"] == []  # Reset on session change
