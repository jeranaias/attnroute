"""Tests for BurnRate plugin."""
import json
from datetime import datetime, timedelta

import pytest


class TestBurnRate:
    """Test BurnRate plugin functionality."""

    @pytest.fixture
    def plugin(self, tmp_path, monkeypatch):
        from attnroute.plugins.base import AttnroutePlugin
        from attnroute.plugins.burnrate import BurnRatePlugin

        monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)

        # Create mock stats cache
        stats_cache = tmp_path / "stats-cache.json"
        stats_cache.write_text(json.dumps({
            "totalTokens": 50000,
            "sessionTokens": 10000,
            "inputTokens": 8000,
            "outputTokens": 2000,
            "costUsd": 0.15,
            "model": "claude-sonnet",
        }))

        # Point plugin to mock stats cache
        plugin = BurnRatePlugin()
        monkeypatch.setattr(plugin, "STATS_CACHE", stats_cache)

        return plugin

    @pytest.fixture
    def stats_cache(self, tmp_path):
        return tmp_path / "stats-cache.json"

    def test_session_start_shows_status(self, plugin):
        result = plugin.on_session_start({"session_id": "test123"})

        assert "BurnRate" in result
        assert "Active" in result

        state = plugin.load_state()
        assert state["session_id"] == "test123"
        assert state["samples"] == []

    def test_sample_collection(self, plugin, tmp_path, monkeypatch):
        plugin.on_session_start({})

        # Simulate multiple turns with increasing tokens
        stats_cache = tmp_path / "stats-cache.json"

        for i in range(5):
            stats_cache.write_text(json.dumps({
                "totalTokens": 50000 + (i * 1000),
                "sessionTokens": 10000 + (i * 1000),
                "model": "claude-sonnet",
            }))
            plugin.on_stop([], {})

        state = plugin.load_state()
        assert len(state["samples"]) == 5

    def test_no_warning_when_plenty_remaining(self, plugin, tmp_path):
        plugin.on_session_start({})

        # Low usage, plenty remaining
        stats_cache = plugin.STATS_CACHE
        stats_cache.write_text(json.dumps({
            "totalTokens": 10000,
            "sessionTokens": 5000,  # Low usage
            "model": "claude-sonnet",
        }))

        # Add samples with low burn rate
        state = plugin.load_state()
        now = datetime.now()
        state["samples"] = [
            {"timestamp": (now - timedelta(minutes=10)).isoformat(), "total_tokens": 9000, "session_tokens": 4000},
            {"timestamp": now.isoformat(), "total_tokens": 10000, "session_tokens": 5000},
        ]
        plugin.save_state(state)

        context = plugin.on_prompt_post("test", "", {})
        assert context == ""  # No warning

    def test_warning_when_approaching_limit(self, plugin, tmp_path):
        plugin.on_session_start({})

        # High usage, approaching limit
        stats_cache = plugin.STATS_CACHE
        stats_cache.write_text(json.dumps({
            "totalTokens": 140000,
            "sessionTokens": 140000,  # Close to 150k pro limit
            "model": "claude-sonnet",
        }))

        # Add samples with high burn rate
        state = plugin.load_state()
        state["plan_type"] = "pro"
        now = datetime.now()
        state["samples"] = [
            {"timestamp": (now - timedelta(minutes=5)).isoformat(), "total_tokens": 130000, "session_tokens": 130000},
            {"timestamp": now.isoformat(), "total_tokens": 140000, "session_tokens": 140000},
        ]
        plugin.save_state(state)

        context = plugin.on_prompt_post("test", "", {})
        # With 10k remaining and 2k/min rate, that's only 5 minutes - should trigger warning
        assert "BurnRate" in context
        assert "minutes" in context.lower()

    def test_critical_warning(self, plugin, tmp_path):
        plugin.on_session_start({})

        # Very high usage, critical
        stats_cache = plugin.STATS_CACHE
        stats_cache.write_text(json.dumps({
            "totalTokens": 148000,
            "sessionTokens": 148000,  # Only 2k remaining
            "model": "claude-sonnet",
        }))

        # Add samples with high burn rate
        state = plugin.load_state()
        state["plan_type"] = "pro"
        now = datetime.now()
        state["samples"] = [
            {"timestamp": (now - timedelta(minutes=2)).isoformat(), "total_tokens": 146000, "session_tokens": 146000},
            {"timestamp": now.isoformat(), "total_tokens": 148000, "session_tokens": 148000},
        ]
        plugin.save_state(state)

        context = plugin.on_prompt_post("test", "", {})
        assert "CRITICAL" in context
        assert "Consider" in context

    def test_api_mode_no_warning(self, plugin, tmp_path):
        """API mode should not warn (different rate limiting)."""
        stats_cache = plugin.STATS_CACHE
        stats_cache.write_text(json.dumps({
            "totalTokens": 1000000,
            "sessionTokens": 1000000,
            "model": "api",
        }))

        result = plugin.on_session_start({})
        assert "API mode" in result

        state = plugin.load_state()
        assert state["plan_type"] == "api"

    def test_session_summary(self, plugin, tmp_path):
        plugin.on_session_start({})

        # Add some samples
        state = plugin.load_state()
        now = datetime.now()
        state["samples"] = [
            {"timestamp": (now - timedelta(minutes=5)).isoformat(), "total_tokens": 5000, "session_tokens": 5000},
            {"timestamp": now.isoformat(), "total_tokens": 10000, "session_tokens": 10000},
        ]
        state["warnings_issued"] = 2
        plugin.save_state(state)

        summary = plugin.get_session_summary()
        assert "plan_type" in summary
        assert summary["samples_collected"] == 2
        assert summary["warnings_issued"] == 2
        assert "tokens_per_minute" in summary

    def test_sample_window_limit(self, plugin, tmp_path):
        """Samples should be limited to SAMPLE_WINDOW size."""
        plugin.on_session_start({})

        stats_cache = plugin.STATS_CACHE

        # Add many samples
        for i in range(30):
            stats_cache.write_text(json.dumps({
                "totalTokens": 10000 + (i * 100),
                "sessionTokens": 5000 + (i * 100),
                "model": "claude-sonnet",
            }))
            plugin.on_stop([], {})

        state = plugin.load_state()
        assert len(state["samples"]) <= plugin.SAMPLE_WINDOW

    def test_missing_stats_cache(self, plugin, tmp_path, monkeypatch):
        """Should handle missing stats cache gracefully."""
        # Point to non-existent file
        monkeypatch.setattr(plugin, "STATS_CACHE", tmp_path / "nonexistent.json")

        result = plugin.on_session_start({})
        assert "BurnRate" in result

        # Should not crash
        context = plugin.on_prompt_post("test", "", {})
        assert context == ""

    def test_plan_detection_max(self, plugin, tmp_path):
        """Should detect Max plans from high token counts."""
        stats_cache = plugin.STATS_CACHE
        stats_cache.write_text(json.dumps({
            "totalTokens": 400000,
            "sessionTokens": 400000,
            "model": "claude-opus",
        }))

        plugin.on_session_start({})
        state = plugin.load_state()

        # High usage suggests Max plan
        assert state["plan_type"] in ["max_5x", "max_20x"]
