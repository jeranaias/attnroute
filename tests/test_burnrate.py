"""Tests for BurnRate plugin — real-time rate limit tracker.

The BurnRate v0.3.0 scans conversation JSONL files (the same ones
Claude Code writes per-session) to build a 5-hour sliding window of
actual token usage, with full historical reporting capabilities.

These tests create mock project directories with realistic JSONL data
to verify scanning, deduplication, burn rate calculation, warning
formatting, cost estimation, daily breakdowns, project attribution,
and report formatting.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ------------------------------------------------------------------ #
#  Helpers — build realistic JSONL entries                            #
# ------------------------------------------------------------------ #

def _ts(minutes_ago: float = 0) -> str:
    """Return an ISO timestamp N minutes in the past (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat()


def _assistant_entry(
    minutes_ago: float = 0,
    request_id: str = "req_001",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 200,
    cache_read: int = 1000,
    model: str = "claude-opus-4-6",
    session_id: str = "test-session",
) -> str:
    """Build one JSONL line that looks like a real Claude Code assistant message."""
    return json.dumps({
        "type": "assistant",
        "timestamp": _ts(minutes_ago),
        "requestId": request_id,
        "sessionId": session_id,
        "message": {
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    })


def _user_entry(minutes_ago: float = 0) -> str:
    """Build a user message line (should be skipped by scanner)."""
    return json.dumps({
        "type": "user",
        "timestamp": _ts(minutes_ago),
        "message": {"role": "user", "content": "hello"},
    })


def _progress_entry(minutes_ago: float = 0) -> str:
    """Build a progress message line (should be skipped by scanner)."""
    return json.dumps({
        "type": "progress",
        "timestamp": _ts(minutes_ago),
        "data": {"type": "bash_progress", "output": ""},
    })


def _write_jsonl(path: Path, lines: list[str]) -> None:
    """Write multiple JSONL lines to a file."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ #
#  Fixtures                                                           #
# ------------------------------------------------------------------ #

@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """Create a BurnRatePlugin pointing at a mock .claude directory."""
    from attnroute.plugins.base import AttnroutePlugin
    from attnroute.plugins.burnrate import BurnRatePlugin

    monkeypatch.setattr(AttnroutePlugin, "_state_dir", tmp_path)

    plugin = BurnRatePlugin()
    # Point CLAUDE_DIR at our tmp directory
    monkeypatch.setattr(plugin, "CLAUDE_DIR", tmp_path)
    # Disable scan cooldown for testing
    monkeypatch.setattr(plugin, "SCAN_COOLDOWN", 0)

    return plugin


@pytest.fixture
def mock_project(tmp_path):
    """Create a mock project directory with JSONL files."""
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    return project_dir


# ------------------------------------------------------------------ #
#  Scanning tests                                                     #
# ------------------------------------------------------------------ #

class TestJSONLScanning:
    """Test that BurnRate correctly scans conversation JSONL files."""

    def test_scans_assistant_messages(self, plugin, mock_project):
        """Assistant messages with usage data are extracted."""
        _write_jsonl(mock_project / "session1.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_001",
                             input_tokens=100, output_tokens=50),
            _assistant_entry(minutes_ago=5, request_id="req_002",
                             input_tokens=200, output_tokens=100),
        ])

        now = datetime.now(timezone.utc)
        records = plugin._scan_window(now)

        assert len(records) == 2
        assert records[0]["input_tokens"] == 100
        assert records[1]["input_tokens"] == 200

    def test_skips_user_and_progress_messages(self, plugin, mock_project):
        """Non-assistant messages are ignored."""
        _write_jsonl(mock_project / "session1.jsonl", [
            _user_entry(minutes_ago=10),
            _progress_entry(minutes_ago=8),
            _assistant_entry(minutes_ago=5, request_id="req_001"),
            _user_entry(minutes_ago=3),
        ])

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 1

    def test_deduplicates_by_request_id(self, plugin, mock_project):
        """Streaming chunks with same requestId are counted once."""
        _write_jsonl(mock_project / "session1.jsonl", [
            # Three chunks from the same streaming response
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=50),
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=50),
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=50),
        ])

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 1  # Not 3

    def test_deduplicates_across_files(self, plugin, mock_project):
        """Same requestId in different files is still deduped."""
        _write_jsonl(mock_project / "session1.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])
        _write_jsonl(mock_project / "session2.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 1

    def test_filters_by_window(self, plugin, mock_project):
        """Messages older than WINDOW_HOURS are excluded."""
        _write_jsonl(mock_project / "session1.jsonl", [
            # 6 hours ago — outside 5h window
            _assistant_entry(minutes_ago=360, request_id="req_old"),
            # 2 hours ago — inside window
            _assistant_entry(minutes_ago=120, request_id="req_new"),
        ])

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 1
        assert records[0]["input_tokens"] == 100

    def test_scans_across_multiple_projects(self, plugin, tmp_path):
        """Scans all project directories (rate limits are account-wide)."""
        proj_a = tmp_path / "projects" / "project-a"
        proj_b = tmp_path / "projects" / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)

        _write_jsonl(proj_a / "session.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_a",
                             input_tokens=100, output_tokens=50),
        ])
        _write_jsonl(proj_b / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_b",
                             input_tokens=200, output_tokens=100),
        ])

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 2
        total_input = sum(r["input_tokens"] for r in records)
        assert total_input == 300

    def test_skips_stale_files(self, plugin, tmp_path, monkeypatch):
        """Files with mtime before window start are skipped entirely."""
        import os

        proj = tmp_path / "projects" / "test"
        proj.mkdir(parents=True)

        jsonl = proj / "old_session.jsonl"
        _write_jsonl(jsonl, [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        # Set file mtime to 6 hours ago (outside 5h window)
        old_time = (datetime.now() - timedelta(hours=6)).timestamp()
        os.utime(jsonl, (old_time, old_time))

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 0  # File skipped due to stale mtime

    def test_handles_missing_projects_dir(self, plugin, tmp_path):
        """Gracefully returns empty if projects/ doesn't exist."""
        # Don't create projects dir
        records = plugin._scan_window(datetime.now(timezone.utc))
        assert records == []

    def test_handles_malformed_jsonl(self, plugin, mock_project):
        """Malformed JSON lines are skipped without crashing."""
        jsonl = mock_project / "broken.jsonl"
        jsonl.write_text(
            '{"type": "assistant", "this is broken\n'
            + _assistant_entry(minutes_ago=5, request_id="req_ok") + "\n",
            encoding="utf-8",
        )

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 1  # Only the valid line

    def test_records_include_project_name(self, plugin, tmp_path):
        """Scanned records include decoded project name."""
        proj = tmp_path / "projects" / "C--Users-jesse-attnroute"
        proj.mkdir(parents=True)

        _write_jsonl(proj / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        records = plugin._scan_window(datetime.now(timezone.utc))
        assert len(records) == 1
        assert records[0]["project"] == "attnroute"


# ------------------------------------------------------------------ #
#  Window stats computation                                           #
# ------------------------------------------------------------------ #

class TestWindowStats:
    """Test token aggregation within the sliding window."""

    def test_sums_billable_tokens(self, plugin):
        """window_tokens = input + output + cache_creation (not cache_read)."""
        records = [
            {"timestamp": _ts(5), "model": "opus", "input_tokens": 100,
             "output_tokens": 50, "cache_creation_tokens": 200,
             "cache_read_tokens": 10000},
            {"timestamp": _ts(3), "model": "opus", "input_tokens": 150,
             "output_tokens": 75, "cache_creation_tokens": 300,
             "cache_read_tokens": 15000},
        ]

        stats = plugin._compute_window_stats(records)

        # Billable: (100+50+200) + (150+75+300) = 875
        assert stats["window_tokens"] == 875
        assert stats["input_tokens"] == 250
        assert stats["output_tokens"] == 125
        assert stats["cache_creation_tokens"] == 500
        # Cache reads tracked but NOT in window_tokens
        assert stats["cache_read_tokens"] == 25000
        assert stats["api_calls"] == 2

    def test_tracks_models(self, plugin):
        """Per-model breakdown and primary model detection."""
        records = [
            {"timestamp": _ts(5), "model": "opus", "input_tokens": 100,
             "output_tokens": 50, "cache_creation_tokens": 200,
             "cache_read_tokens": 0},
            {"timestamp": _ts(3), "model": "sonnet", "input_tokens": 500,
             "output_tokens": 200, "cache_creation_tokens": 100,
             "cache_read_tokens": 0},
        ]

        stats = plugin._compute_window_stats(records)
        assert stats["primary_model"] == "sonnet"  # Higher billable total
        assert stats["models"]["opus"] == 350       # 100+50+200
        assert stats["models"]["sonnet"] == 800     # 500+200+100

    def test_tracks_sessions(self, plugin):
        """Per-session breakdown for token attribution."""
        records = [
            {"timestamp": _ts(5), "model": "opus", "session_id": "sess-aaa",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 200, "cache_read_tokens": 0},
            {"timestamp": _ts(3), "model": "opus", "session_id": "sess-bbb",
             "input_tokens": 500, "output_tokens": 200,
             "cache_creation_tokens": 100, "cache_read_tokens": 0},
            {"timestamp": _ts(1), "model": "opus", "session_id": "sess-aaa",
             "input_tokens": 50, "output_tokens": 25,
             "cache_creation_tokens": 25, "cache_read_tokens": 0},
        ]

        stats = plugin._compute_window_stats(records)
        assert stats["sessions"]["sess-aaa"] == 450   # 350 + 100
        assert stats["sessions"]["sess-bbb"] == 800   # 500+200+100

    def test_tracks_projects(self, plugin):
        """Per-project breakdown for token attribution."""
        records = [
            {"timestamp": _ts(5), "model": "opus", "project": "attnroute",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 200, "cache_read_tokens": 0},
            {"timestamp": _ts(3), "model": "opus", "project": "tanaghum",
             "input_tokens": 500, "output_tokens": 200,
             "cache_creation_tokens": 100, "cache_read_tokens": 0},
            {"timestamp": _ts(1), "model": "opus", "project": "attnroute",
             "input_tokens": 50, "output_tokens": 25,
             "cache_creation_tokens": 25, "cache_read_tokens": 0},
        ]

        stats = plugin._compute_window_stats(records)
        assert stats["projects"]["attnroute"] == 450
        assert stats["projects"]["tanaghum"] == 800

    def test_empty_records(self, plugin):
        """Empty records produce zero stats."""
        stats = plugin._compute_window_stats([])
        assert stats["window_tokens"] == 0
        assert stats["api_calls"] == 0
        assert stats["primary_model"] == "unknown"


# ------------------------------------------------------------------ #
#  Burn rate calculation                                              #
# ------------------------------------------------------------------ #

class TestBurnRate:
    """Test burn rate and time-to-exhaustion calculations."""

    def test_calculates_rate_from_records(self, plugin):
        """Burn rate = total billable tokens / elapsed minutes."""
        now = datetime.now(timezone.utc)
        records = [
            {"timestamp": (now - timedelta(minutes=10)).isoformat(),
             "model": "opus", "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 50, "cache_read_tokens": 0},
            {"timestamp": now.isoformat(),
             "model": "opus", "input_tokens": 200, "output_tokens": 100,
             "cache_creation_tokens": 100, "cache_read_tokens": 0},
        ]

        burn = plugin._calculate_burn_rate(records, now)

        assert burn is not None
        # Total billable: (100+50+50) + (200+100+100) = 600
        # Over 10 minutes = 60 tokens/min
        assert abs(burn["tokens_per_minute"] - 60.0) < 1.0
        assert burn["sample_count"] == 2

    def test_returns_none_with_single_record(self, plugin):
        """Need at least 2 records for rate calculation."""
        now = datetime.now(timezone.utc)
        records = [
            {"timestamp": now.isoformat(), "model": "opus",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
        ]

        assert plugin._calculate_burn_rate(records, now) is None

    def test_returns_none_with_no_records(self, plugin):
        assert plugin._calculate_burn_rate([], datetime.now(timezone.utc)) is None

    def test_uses_recent_window_for_responsiveness(self, plugin):
        """Burn rate uses last RATE_WINDOW_MINUTES, not full 5h."""
        now = datetime.now(timezone.utc)

        # Old records (4 hours ago) — low rate
        old_records = [
            {"timestamp": (now - timedelta(hours=4)).isoformat(),
             "model": "opus", "input_tokens": 10, "output_tokens": 5,
             "cache_creation_tokens": 5, "cache_read_tokens": 0},
            {"timestamp": (now - timedelta(hours=3, minutes=50)).isoformat(),
             "model": "opus", "input_tokens": 10, "output_tokens": 5,
             "cache_creation_tokens": 5, "cache_read_tokens": 0},
        ]

        # Recent records (last 10 min) — high rate
        recent_records = [
            {"timestamp": (now - timedelta(minutes=10)).isoformat(),
             "model": "opus", "input_tokens": 500, "output_tokens": 250,
             "cache_creation_tokens": 250, "cache_read_tokens": 0},
            {"timestamp": now.isoformat(),
             "model": "opus", "input_tokens": 500, "output_tokens": 250,
             "cache_creation_tokens": 250, "cache_read_tokens": 0},
        ]

        all_records = old_records + recent_records
        burn = plugin._calculate_burn_rate(all_records, now)

        assert burn is not None
        # Should reflect recent high rate (~200 tok/min), not diluted average
        assert burn["tokens_per_minute"] > 100


# ------------------------------------------------------------------ #
#  Plan detection                                                     #
# ------------------------------------------------------------------ #

class TestPlanDetection:
    """Test heuristic plan type detection."""

    def test_pro_plan(self, plugin):
        assert plugin._detect_plan_type({"window_tokens": 50_000}) == "pro"

    def test_max_5x_plan(self, plugin):
        assert plugin._detect_plan_type({"window_tokens": 200_000}) == "max_5x"

    def test_max_20x_plan(self, plugin):
        assert plugin._detect_plan_type({"window_tokens": 600_000}) == "max_20x"

    def test_explicit_plan_overrides_heuristic(self, plugin, mock_project):
        """session_state plan_type is used when provided."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=50),
        ])

        result = plugin.on_session_start({"plan_type": "max_20x"})
        assert "max_20x" in result

        state = plugin.load_state()
        assert state["plan_type"] == "max_20x"


# ------------------------------------------------------------------ #
#  Warning thresholds and formatting                                  #
# ------------------------------------------------------------------ #

class TestWarnings:
    """Test warning injection at various usage levels."""

    def _make_records(self, total_billable: int, minutes_span: float = 60):
        """Create records that sum to total_billable tokens over minutes_span."""
        now = datetime.now(timezone.utc)
        count = max(2, int(minutes_span / 5))
        per_call = total_billable // count
        records = []
        for i in range(count):
            mins_ago = minutes_span - (i * minutes_span / count)
            records.append({
                "timestamp": (now - timedelta(minutes=mins_ago)).isoformat(),
                "model": "opus",
                "input_tokens": per_call // 3,
                "output_tokens": per_call // 3,
                "cache_creation_tokens": per_call - 2 * (per_call // 3),
                "cache_read_tokens": 0,
            })
        return records

    def test_no_warning_below_threshold(self, plugin):
        """Below WARN_PCT — no context injected."""
        # 50% of 150k pro limit = 75k
        records = self._make_records(75_000)
        stats = plugin._compute_window_stats(records)
        pct = (stats["window_tokens"] / 150_000) * 100

        assert pct < plugin.WARN_PCT

    def test_notice_at_60_percent(self, plugin):
        """At WARN_PCT — informational notice."""
        stats = {
            "window_tokens": 95_000, "input_tokens": 30_000,
            "output_tokens": 15_000, "cache_creation_tokens": 50_000,
            "cache_read_tokens": 100_000, "api_calls": 50,
        }
        pct = (95_000 / 150_000) * 100  # ~63%
        burn = {"tokens_per_minute": 500, "sample_minutes": 10.0, "sample_count": 5}

        warning = plugin._format_warning(stats, burn, "pro", 150_000, pct)
        assert "Notice" in warning
        assert "BurnRate" in warning

    def test_warning_at_80_percent(self, plugin):
        """At HIGH_PCT — warning with pacing advice."""
        stats = {
            "window_tokens": 125_000, "input_tokens": 40_000,
            "output_tokens": 20_000, "cache_creation_tokens": 65_000,
            "cache_read_tokens": 200_000, "api_calls": 80,
        }
        pct = (125_000 / 150_000) * 100  # ~83%
        burn = {"tokens_per_minute": 800, "sample_minutes": 10.0, "sample_count": 5}

        warning = plugin._format_warning(stats, burn, "pro", 150_000, pct)
        assert "WARNING" in warning
        assert "pacing" in warning.lower()

    def test_critical_at_95_percent(self, plugin):
        """At CRITICAL_PCT — urgent warning with advice."""
        stats = {
            "window_tokens": 145_000, "input_tokens": 50_000,
            "output_tokens": 25_000, "cache_creation_tokens": 70_000,
            "cache_read_tokens": 300_000, "api_calls": 100,
        }
        pct = (145_000 / 150_000) * 100  # ~96%
        burn = {"tokens_per_minute": 1000, "sample_minutes": 10.0, "sample_count": 5}

        warning = plugin._format_warning(stats, burn, "pro", 150_000, pct)
        assert "CRITICAL" in warning
        assert "Slow down" in warning
        assert "pause" in warning.lower()

    def test_warning_includes_progress_bar(self, plugin):
        """Warning output includes visual progress bar."""
        stats = {
            "window_tokens": 120_000, "input_tokens": 40_000,
            "output_tokens": 20_000, "cache_creation_tokens": 60_000,
            "cache_read_tokens": 0, "api_calls": 50,
        }
        pct = 80.0
        warning = plugin._format_warning(stats, None, "pro", 150_000, pct)
        assert "\u2588" in warning  # Filled block
        assert "\u2591" in warning  # Empty block
        assert "80%" in warning

    def test_warning_includes_token_breakdown(self, plugin):
        """Warning shows input/output/cache breakdown."""
        stats = {
            "window_tokens": 120_000, "input_tokens": 40_000,
            "output_tokens": 20_000, "cache_creation_tokens": 60_000,
            "cache_read_tokens": 150_000, "api_calls": 50,
        }
        warning = plugin._format_warning(stats, None, "pro", 150_000, 80.0)
        assert "40,000" in warning
        assert "20,000" in warning
        assert "60,000" in warning
        assert "150,000" in warning
        assert "50 API calls" in warning

    def test_warning_includes_eta(self, plugin):
        """When burn rate available, shows ETA to limit."""
        stats = {
            "window_tokens": 100_000, "input_tokens": 30_000,
            "output_tokens": 20_000, "cache_creation_tokens": 50_000,
            "cache_read_tokens": 0, "api_calls": 40,
        }
        burn = {"tokens_per_minute": 1000, "sample_minutes": 10.0, "sample_count": 5}
        # 50k remaining / 1000 per min = 50 min
        warning = plugin._format_warning(stats, burn, "pro", 150_000, 66.7)
        assert "50 minutes" in warning

    def test_over_limit_message(self, plugin):
        """When over limit, shows appropriate message."""
        stats = {
            "window_tokens": 160_000, "input_tokens": 50_000,
            "output_tokens": 30_000, "cache_creation_tokens": 80_000,
            "cache_read_tokens": 0, "api_calls": 100,
        }
        burn = {"tokens_per_minute": 500, "sample_minutes": 10.0, "sample_count": 5}
        warning = plugin._format_warning(stats, burn, "pro", 150_000, 106.7)
        assert "over limit" in warning.lower()

    def test_warning_includes_project_attribution(self, plugin):
        """Warning shows per-project breakdown when multiple projects."""
        stats = {
            "window_tokens": 120_000, "input_tokens": 40_000,
            "output_tokens": 20_000, "cache_creation_tokens": 60_000,
            "cache_read_tokens": 0, "api_calls": 50,
            "projects": {"attnroute": 80_000, "tanaghum": 40_000},
        }
        warning = plugin._format_warning(stats, None, "pro", 150_000, 80.0)
        assert "By project" in warning
        assert "attnroute" in warning
        assert "tanaghum" in warning


# ------------------------------------------------------------------ #
#  End-to-end plugin lifecycle                                        #
# ------------------------------------------------------------------ #

class TestPluginLifecycle:
    """Test the full plugin lifecycle with mock data."""

    def test_session_start_shows_window_status(self, plugin, mock_project):
        """on_session_start reports current window usage."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=30, request_id="req_001",
                             input_tokens=1000, output_tokens=500,
                             cache_creation=2000),
            _assistant_entry(minutes_ago=15, request_id="req_002",
                             input_tokens=1000, output_tokens=500,
                             cache_creation=2000),
        ])

        result = plugin.on_session_start({})

        assert "BurnRate" in result
        assert "Active" in result
        assert "7,000" in result  # 2 * (1000+500+2000) = 7000

    def test_no_warning_when_low_usage(self, plugin, mock_project):
        """Low usage produces no context injection."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=30, request_id="req_001",
                             input_tokens=100, output_tokens=50,
                             cache_creation=100),
        ])

        plugin.on_session_start({})
        context = plugin.on_prompt_post("test", "", {})
        assert context == ""

    def test_warning_when_high_usage(self, plugin, mock_project, monkeypatch):
        """High usage triggers warning injection."""
        # Create enough usage to exceed WARN_PCT of pro limit (60% of 150k = 90k)
        lines = []
        for i in range(100):
            lines.append(_assistant_entry(
                minutes_ago=60 - (i * 0.5),
                request_id=f"req_{i:04d}",
                input_tokens=300,
                output_tokens=200,
                cache_creation=500,
            ))

        _write_jsonl(mock_project / "session.jsonl", lines)

        plugin.on_session_start({})
        context = plugin.on_prompt_post("test", "", {})

        # 100 * (300+200+500) = 100,000 > 90k threshold
        assert "BurnRate" in context
        assert "%" in context

    def test_on_stop_logs_to_history(self, plugin, mock_project):
        """on_stop writes sample to history file."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        plugin.on_session_start({})
        plugin.on_stop([], {})

        assert plugin._history_file.exists()
        data = json.loads(plugin._history_file.read_text().strip().split("\n")[-1])
        assert "window_tokens" in data
        assert "api_calls" in data

    def test_session_summary(self, plugin, mock_project):
        """get_session_summary returns comprehensive stats."""
        lines = []
        for i in range(10):
            lines.append(_assistant_entry(
                minutes_ago=60 - (i * 5),
                request_id=f"req_{i:03d}",
                input_tokens=100, output_tokens=50, cache_creation=100,
            ))

        _write_jsonl(mock_project / "session.jsonl", lines)

        plugin.on_session_start({})
        summary = plugin.get_session_summary()

        assert summary["plan_type"] == "pro"
        assert summary["window_tokens"] == 2500  # 10 * (100+50+100)
        assert summary["api_calls_in_window"] == 10
        assert "tokens_per_minute" in summary
        assert summary["warnings_issued"] == 0

    def test_session_summary_includes_cost(self, plugin, mock_project):
        """get_session_summary includes cost estimation."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=1000, output_tokens=500,
                             model="claude-opus-4-6"),
        ])

        plugin.on_session_start({})
        summary = plugin.get_session_summary()

        assert "cost" in summary
        assert summary["cost"]["total"] > 0

    def test_session_summary_includes_projects(self, plugin, tmp_path):
        """get_session_summary includes per-project attribution."""
        proj_a = tmp_path / "projects" / "C--Users-jesse-attnroute"
        proj_b = tmp_path / "projects" / "C--Users-jesse-tanaghum"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)

        _write_jsonl(proj_a / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_a",
                             input_tokens=1000, output_tokens=500),
        ])
        _write_jsonl(proj_b / "session.jsonl", [
            _assistant_entry(minutes_ago=3, request_id="req_b",
                             input_tokens=500, output_tokens=200),
        ])

        plugin.on_session_start({})
        summary = plugin.get_session_summary()

        assert "projects" in summary
        assert "attnroute" in summary["projects"]
        assert "tanaghum" in summary["projects"]

    def test_scan_cooldown(self, plugin, mock_project, monkeypatch):
        """Cached results are reused within SCAN_COOLDOWN."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100),
        ])

        # Enable cooldown
        monkeypatch.setattr(plugin, "SCAN_COOLDOWN", 60)

        now = datetime.now(timezone.utc)
        records_1 = plugin._scan_window(now)
        assert len(records_1) == 1

        # Add more data
        _write_jsonl(mock_project / "session2.jsonl", [
            _assistant_entry(minutes_ago=3, request_id="req_002",
                             input_tokens=200),
        ])

        # Same timestamp — should use cache
        records_2 = plugin._scan_window(now)
        assert len(records_2) == 1  # Still cached

        # Force cache expiry
        plugin._cache_time = now - timedelta(seconds=61)
        records_3 = plugin._scan_window(now)
        assert len(records_3) == 2  # Fresh scan picks up new file


# ------------------------------------------------------------------ #
#  Timestamp parsing                                                  #
# ------------------------------------------------------------------ #

class TestTimestampParsing:
    """Test ISO timestamp handling."""

    def test_z_suffix(self, plugin):
        ts = plugin._parse_timestamp("2026-02-12T10:30:00Z")
        assert ts is not None
        assert ts.tzinfo is not None

    def test_offset_suffix(self, plugin):
        ts = plugin._parse_timestamp("2026-02-12T10:30:00+00:00")
        assert ts is not None

    def test_naive_timestamp(self, plugin):
        ts = plugin._parse_timestamp("2026-02-12T10:30:00")
        assert ts is not None
        assert ts.tzinfo is not None  # Should be set to UTC

    def test_none_input(self, plugin):
        assert plugin._parse_timestamp(None) is None

    def test_empty_string(self, plugin):
        assert plugin._parse_timestamp("") is None

    def test_invalid_format(self, plugin):
        assert plugin._parse_timestamp("not-a-date") is None


# ------------------------------------------------------------------ #
#  Project name decoding                                              #
# ------------------------------------------------------------------ #

class TestProjectNameDecoding:
    """Test decoding Claude's encoded project directory names."""

    def test_windows_home_directory(self, plugin):
        """C--Users-jesse decodes to ~."""
        assert plugin._project_name("C--Users-jesse") == "~"

    def test_windows_project(self, plugin):
        """C--Users-jesse-attnroute decodes to attnroute."""
        assert plugin._project_name("C--Users-jesse-attnroute") == "attnroute"

    def test_windows_nested_project(self, plugin):
        """C--Users-jesse-Downloads decodes to Downloads."""
        assert plugin._project_name("C--Users-jesse-Downloads") == "Downloads"

    def test_linux_home(self, plugin):
        """-home-user decodes to ~."""
        # Linux paths may start with -home-username
        assert plugin._project_name("-home-user") == "~"

    def test_linux_project(self, plugin):
        """-home-user-myproject decodes to myproject."""
        assert plugin._project_name("-home-user-myproject") == "myproject"

    def test_drive_root_project(self, plugin):
        """C--ExpertDrivenDevelopment preserves the full name."""
        name = plugin._project_name("C--ExpertDrivenDevelopment")
        assert name == "ExpertDrivenDevelopment"

    def test_lowercase_drive(self, plugin):
        """c--mardetpomtutorapp handles lowercase drive."""
        name = plugin._project_name("c--mardetpomtutorapp")
        assert name == "mardetpomtutorapp"

    def test_project_with_hyphens(self, plugin):
        """C--Users-jesse-my-cool-project preserves hyphens after username."""
        # Lossy encoding: can't distinguish hyphens from path separators
        # But anything after Users-<username>- is the project path
        name = plugin._project_name("C--Users-jesse-my-cool-project")
        assert name == "my-cool-project"

    def test_empty_name(self, plugin):
        assert plugin._project_name("") == "unknown"

    def test_no_separator(self, plugin):
        """Plain directory name returned as-is."""
        assert plugin._project_name("standalone") == "standalone"

    def test_deep_nested_path(self, plugin):
        """C--dev-arabic-nlp-mutawazin-mutawazin-app returns full sub-path."""
        name = plugin._project_name(
            "C--dev-arabic-nlp-mutawazin-mutawazin-app"
        )
        # After C--, no Users/home prefix, so returns everything
        assert name == "dev-arabic-nlp-mutawazin-mutawazin-app"


# ------------------------------------------------------------------ #
#  Model family detection                                             #
# ------------------------------------------------------------------ #

class TestModelFamily:
    """Test mapping model IDs to pricing families."""

    def test_opus_4_6(self, plugin):
        assert plugin._get_model_family("claude-opus-4-6") == "opus"

    def test_opus_4_5(self, plugin):
        assert plugin._get_model_family("claude-opus-4-5-20251101") == "opus"

    def test_opus_4_legacy(self, plugin):
        """Opus 4.0 (no sub-version) maps to legacy pricing."""
        assert plugin._get_model_family("claude-opus-4-20250514") == "opus_legacy"

    def test_opus_4_1_legacy(self, plugin):
        """Opus 4.1 maps to legacy pricing."""
        assert plugin._get_model_family("claude-opus-4-1-20250801") == "opus_legacy"

    def test_sonnet_model(self, plugin):
        assert plugin._get_model_family("claude-sonnet-4-5-20250929") == "sonnet"

    def test_haiku_model(self, plugin):
        assert plugin._get_model_family("claude-haiku-4-5-20251001") == "haiku"

    def test_opus_case_insensitive(self, plugin):
        assert plugin._get_model_family("Claude-Opus-4-6") == "opus"

    def test_unknown_defaults_to_sonnet(self, plugin):
        assert plugin._get_model_family("some-unknown-model") == "sonnet"

    def test_empty_string(self, plugin):
        assert plugin._get_model_family("") == "sonnet"


# ------------------------------------------------------------------ #
#  Cost estimation                                                    #
# ------------------------------------------------------------------ #

class TestCostEstimation:
    """Test API-equivalent cost calculations."""

    def test_opus_cost_calculation(self, plugin):
        """Opus 4.5+ tokens priced at $5/$25 per million."""
        records = [{
            "model": "claude-opus-4-6",
            "project": "test",
            "input_tokens": 1_000_000,   # $5.00
            "output_tokens": 100_000,    # $2.50
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }]

        cost = plugin._estimate_cost(records)
        assert cost["total"] == 7.50

    def test_sonnet_cost_calculation(self, plugin):
        """Sonnet tokens priced at $3/$15 per million."""
        records = [{
            "model": "claude-sonnet-4-5",
            "project": "test",
            "input_tokens": 1_000_000,   # $3.00
            "output_tokens": 100_000,    # $1.50
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }]

        cost = plugin._estimate_cost(records)
        assert cost["total"] == 4.50

    def test_haiku_cost_calculation(self, plugin):
        """Haiku 4.5 tokens priced at $1/$5 per million."""
        records = [{
            "model": "claude-haiku-4-5",
            "project": "test",
            "input_tokens": 1_000_000,   # $1.00
            "output_tokens": 1_000_000,  # $5.00
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }]

        cost = plugin._estimate_cost(records)
        assert cost["total"] == 6.00

    def test_cache_tokens_costed(self, plugin):
        """Cache creation and read tokens have their own pricing."""
        records = [{
            "model": "claude-opus-4-6",
            "project": "test",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 1_000_000,  # $6.25
            "cache_read_tokens": 1_000_000,       # $0.50
        }]

        cost = plugin._estimate_cost(records)
        assert cost["total"] == 6.75

    def test_cost_breakdown_by_model(self, plugin):
        """Cost broken down per model."""
        records = [
            {"model": "claude-opus-4-6", "project": "test",
             "input_tokens": 1_000_000, "output_tokens": 0,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
            {"model": "claude-sonnet-4-5", "project": "test",
             "input_tokens": 1_000_000, "output_tokens": 0,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
        ]

        cost = plugin._estimate_cost(records)
        assert cost["by_model"]["claude-opus-4-6"] == 5.00
        assert cost["by_model"]["claude-sonnet-4-5"] == 3.00
        assert cost["total"] == 8.00

    def test_cost_breakdown_by_project(self, plugin):
        """Cost broken down per project."""
        records = [
            {"model": "claude-opus-4-6", "project": "attnroute",
             "input_tokens": 1_000_000, "output_tokens": 0,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
            {"model": "claude-opus-4-6", "project": "tanaghum",
             "input_tokens": 500_000, "output_tokens": 0,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
        ]

        cost = plugin._estimate_cost(records)
        assert cost["by_project"]["attnroute"] == 5.00
        assert cost["by_project"]["tanaghum"] == 2.50

    def test_empty_records(self, plugin):
        cost = plugin._estimate_cost([])
        assert cost["total"] == 0
        assert cost["by_model"] == {}
        assert cost["by_project"] == {}


# ------------------------------------------------------------------ #
#  Daily breakdown                                                    #
# ------------------------------------------------------------------ #

class TestDailyBreakdown:
    """Test per-day aggregation of token usage."""

    def test_groups_by_date(self, plugin):
        """Records on different dates are grouped separately."""
        now = datetime.now(timezone.utc)
        records = [
            {"timestamp": now.isoformat(),
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 200, "cache_read_tokens": 0},
            {"timestamp": (now - timedelta(days=1)).isoformat(),
             "input_tokens": 300, "output_tokens": 100,
             "cache_creation_tokens": 400, "cache_read_tokens": 0},
            {"timestamp": now.isoformat(),
             "input_tokens": 50, "output_tokens": 25,
             "cache_creation_tokens": 25, "cache_read_tokens": 0},
        ]

        daily = plugin._compute_daily_breakdown(records)

        assert len(daily) == 2
        # Sorted most recent first
        assert daily[0]["billable_tokens"] == 450  # (100+50+200) + (50+25+25)
        assert daily[1]["billable_tokens"] == 800  # 300+100+400
        assert daily[0]["api_calls"] == 2
        assert daily[1]["api_calls"] == 1

    def test_includes_token_breakdown(self, plugin):
        """Each day has full token category breakdown."""
        now = datetime.now(timezone.utc)
        records = [
            {"timestamp": now.isoformat(),
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 200, "cache_read_tokens": 5000},
        ]

        daily = plugin._compute_daily_breakdown(records)
        day = daily[0]

        assert day["input_tokens"] == 100
        assert day["output_tokens"] == 50
        assert day["cache_creation_tokens"] == 200
        assert day["cache_read_tokens"] == 5000

    def test_empty_records(self, plugin):
        assert plugin._compute_daily_breakdown([]) == []


# ------------------------------------------------------------------ #
#  Project breakdown                                                  #
# ------------------------------------------------------------------ #

class TestProjectBreakdown:
    """Test per-project aggregation of token usage."""

    def test_groups_by_project(self, plugin):
        """Records from different projects grouped separately."""
        records = [
            {"project": "attnroute", "model": "opus",
             "session_id": "s1",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 200, "cache_read_tokens": 0},
            {"project": "tanaghum", "model": "sonnet",
             "session_id": "s2",
             "input_tokens": 500, "output_tokens": 200,
             "cache_creation_tokens": 100, "cache_read_tokens": 0},
            {"project": "attnroute", "model": "opus",
             "session_id": "s3",
             "input_tokens": 50, "output_tokens": 25,
             "cache_creation_tokens": 25, "cache_read_tokens": 0},
        ]

        projects = plugin._compute_project_breakdown(records)

        assert len(projects) == 2
        assert projects["attnroute"]["billable_tokens"] == 450
        assert projects["tanaghum"]["billable_tokens"] == 800
        assert projects["attnroute"]["api_calls"] == 2
        assert projects["tanaghum"]["api_calls"] == 1

    def test_tracks_models_per_project(self, plugin):
        """Each project tracks which models were used."""
        records = [
            {"project": "attnroute", "model": "opus",
             "session_id": "s1",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
            {"project": "attnroute", "model": "sonnet",
             "session_id": "s2",
             "input_tokens": 200, "output_tokens": 100,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
        ]

        projects = plugin._compute_project_breakdown(records)
        assert sorted(projects["attnroute"]["models"]) == ["opus", "sonnet"]

    def test_counts_unique_sessions(self, plugin):
        """Each project tracks number of unique sessions."""
        records = [
            {"project": "attnroute", "model": "opus",
             "session_id": "s1",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
            {"project": "attnroute", "model": "opus",
             "session_id": "s1",
             "input_tokens": 100, "output_tokens": 50,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
            {"project": "attnroute", "model": "opus",
             "session_id": "s2",
             "input_tokens": 200, "output_tokens": 100,
             "cache_creation_tokens": 0, "cache_read_tokens": 0},
        ]

        projects = plugin._compute_project_breakdown(records)
        assert projects["attnroute"]["sessions"] == 2  # s1, s2

    def test_empty_records(self, plugin):
        assert plugin._compute_project_breakdown([]) == {}


# ------------------------------------------------------------------ #
#  Historical scanning                                                #
# ------------------------------------------------------------------ #

class TestHistoricalScanning:
    """Test _scan_history for wider time ranges."""

    def test_scans_wider_than_5h(self, plugin, tmp_path):
        """_scan_history can reach days into the past."""
        proj = tmp_path / "projects" / "C--Users-jesse-attnroute"
        proj.mkdir(parents=True)

        # 3 days ago (outside 5h window but inside 7-day scan)
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(days=3)).isoformat()
        old_entry = json.dumps({
            "type": "assistant",
            "timestamp": ts_old,
            "requestId": "req_old",
            "sessionId": "session-old",
            "message": {
                "model": "claude-opus-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 100,
                },
            },
        })

        _write_jsonl(proj / "session.jsonl", [old_entry])

        # 5h window should NOT find it
        window_records = plugin._scan_window(now)
        assert len(window_records) == 0

        # Historical scan for 7 days SHOULD find it
        history_records = plugin._scan_history(days=7)
        assert len(history_records) == 1
        assert history_records[0]["project"] == "attnroute"
        assert history_records[0]["input_tokens"] == 1000

    def test_returns_empty_when_no_projects(self, plugin):
        """Gracefully returns empty when projects dir doesn't exist."""
        assert plugin._scan_history(days=7) == []

    def test_deduplicates(self, plugin, tmp_path):
        """Historical scan deduplicates by requestId."""
        proj = tmp_path / "projects" / "test-project"
        proj.mkdir(parents=True)

        _write_jsonl(proj / "s1.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_same"),
        ])
        _write_jsonl(proj / "s2.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_same"),
        ])

        records = plugin._scan_history(days=1)
        assert len(records) == 1


# ------------------------------------------------------------------ #
#  Usage report                                                       #
# ------------------------------------------------------------------ #

class TestUsageReport:
    """Test get_usage_report comprehensive reporting."""

    def test_report_structure(self, plugin, mock_project):
        """Report contains all expected top-level keys."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_001",
                             input_tokens=1000, output_tokens=500),
        ])

        plugin.on_session_start({})
        report = plugin.get_usage_report(days=1)

        assert report["days"] == 1
        assert report["total_records"] == 1
        assert report["billable_tokens"] > 0
        assert "cost" in report
        assert "daily" in report
        assert "projects" in report
        assert "models" in report
        assert "rate_limit" in report

    def test_report_empty_when_no_data(self, plugin):
        """Report handles no data gracefully."""
        plugin.on_session_start({})
        report = plugin.get_usage_report(days=1)

        assert report["total_records"] == 0
        assert report["billable_tokens"] == 0
        assert "message" in report

    def test_report_includes_cost(self, plugin, mock_project):
        """Report calculates API-equivalent cost."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=1_000_000, output_tokens=0,
                             cache_creation=0, cache_read=0,
                             model="claude-opus-4-6"),
        ])

        plugin.on_session_start({})
        report = plugin.get_usage_report(days=1)

        # 1M opus 4.6 input tokens = $5.00
        assert report["cost"]["total"] == 5.00

    def test_report_includes_daily_breakdown(self, plugin, mock_project):
        """Report includes per-day stats."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        plugin.on_session_start({})
        report = plugin.get_usage_report(days=1)

        assert len(report["daily"]) >= 1
        assert report["daily"][0]["billable_tokens"] > 0

    def test_report_includes_rate_limit(self, plugin, mock_project):
        """Report includes current 5h rate limit status."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        plugin.on_session_start({})
        report = plugin.get_usage_report(days=1)

        rl = report["rate_limit"]
        assert "plan" in rl
        assert "limit" in rl
        assert "used" in rl
        assert "pct" in rl


# ------------------------------------------------------------------ #
#  Report formatting                                                  #
# ------------------------------------------------------------------ #

class TestReportFormatting:
    """Test format_usage_report output."""

    def _sample_report(self):
        """Build a realistic report dict for formatting tests."""
        return {
            "days": 7,
            "total_records": 150,
            "billable_tokens": 500_000,
            "input_tokens": 200_000,
            "output_tokens": 100_000,
            "cache_creation_tokens": 200_000,
            "cache_read_tokens": 800_000,
            "api_calls": 150,
            "cost": {
                "total": 42.50,
                "by_model": {
                    "claude-opus-4-6": 35.00,
                    "claude-sonnet-4-5": 7.50,
                },
                "by_project": {
                    "attnroute": 30.00,
                    "tanaghum": 12.50,
                },
            },
            "daily": [
                {"date": "2026-02-12", "billable_tokens": 80_000,
                 "input_tokens": 30_000, "output_tokens": 20_000,
                 "cache_creation_tokens": 30_000, "cache_read_tokens": 100_000,
                 "api_calls": 25},
                {"date": "2026-02-11", "billable_tokens": 120_000,
                 "input_tokens": 50_000, "output_tokens": 30_000,
                 "cache_creation_tokens": 40_000, "cache_read_tokens": 200_000,
                 "api_calls": 35},
            ],
            "projects": {
                "attnroute": {
                    "billable_tokens": 350_000,
                    "input_tokens": 140_000,
                    "output_tokens": 70_000,
                    "cache_creation_tokens": 140_000,
                    "cache_read_tokens": 500_000,
                    "api_calls": 100,
                    "models": ["claude-opus-4-6", "claude-sonnet-4-5"],
                    "sessions": 5,
                },
                "tanaghum": {
                    "billable_tokens": 150_000,
                    "input_tokens": 60_000,
                    "output_tokens": 30_000,
                    "cache_creation_tokens": 60_000,
                    "cache_read_tokens": 300_000,
                    "api_calls": 50,
                    "models": ["claude-sonnet-4-5"],
                    "sessions": 3,
                },
            },
            "models": {
                "claude-opus-4-6": 350_000,
                "claude-sonnet-4-5": 150_000,
            },
            "rate_limit": {
                "plan": "pro",
                "limit": 150_000,
                "used": 45_000,
                "pct": 30.0,
                "burn": {
                    "tokens_per_minute": 500,
                    "sample_minutes": 10.0,
                    "sample_count": 5,
                },
            },
        }

    def test_includes_header(self, plugin):
        """Report starts with header showing time range."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "USAGE REPORT" in text
        assert "7 Days" in text

    def test_includes_totals(self, plugin):
        """Report shows total tokens and cost."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "500,000" in text
        assert "42.50" in text

    def test_includes_token_breakdown(self, plugin):
        """Report shows per-category token breakdown."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "200,000" in text  # Input
        assert "100,000" in text  # Output
        assert "not billed" in text  # Cache read note

    def test_includes_daily_bars(self, plugin):
        """Report shows daily usage with bar chart."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "Feb 12" in text
        assert "Feb 11" in text
        assert "\u2588" in text  # Has bars

    def test_includes_project_attribution(self, plugin):
        """Report shows per-project breakdown with costs."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "BY PROJECT" in text
        assert "attnroute" in text
        assert "tanaghum" in text
        assert "$30.00" in text

    def test_includes_model_attribution(self, plugin):
        """Report shows per-model breakdown with costs."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "BY MODEL" in text
        assert "opus" in text
        assert "sonnet" in text

    def test_includes_rate_limit_status(self, plugin):
        """Report shows current rate limit status."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "RATE LIMIT STATUS" in text
        assert "30%" in text
        assert "500 tokens/min" in text

    def test_empty_report(self, plugin):
        """Empty report shows meaningful message."""
        report = {"total_records": 0, "message": "No usage data found."}
        text = plugin.format_usage_report(report)
        assert "No usage data" in text

    def test_single_day_grammar(self, plugin):
        """Single day uses 'Day' not 'Days'."""
        report = self._sample_report()
        report["days"] = 1
        text = plugin.format_usage_report(report)
        assert "1 Day" in text
        assert "1 Days" not in text

    def test_plan_cost_shown(self, plugin):
        """Report shows plan monthly cost for comparison."""
        report = self._sample_report()
        text = plugin.format_usage_report(report)
        assert "$20/mo" in text  # Pro plan cost

    def test_model_grouping_multiple_versions(self, plugin):
        """Multiple versions of same family are grouped together."""
        report = self._sample_report()
        # Two opus versions, one sonnet
        report["models"] = {
            "claude-opus-4-5-20251101": 200_000,
            "claude-opus-4-6": 150_000,
            "claude-sonnet-4-5": 50_000,
        }
        report["cost"]["by_model"] = {
            "claude-opus-4-5-20251101": 20.00,
            "claude-opus-4-6": 15.00,
            "claude-sonnet-4-5": 5.00,
        }
        text = plugin.format_usage_report(report)
        # Should show "opus (2 versions)" with combined tokens
        assert "2 versions" in text
        assert "350,000" in text  # 200k + 150k combined


# ------------------------------------------------------------------ #
#  Billing audit                                                      #
# ------------------------------------------------------------------ #

class TestBillingAudit:
    """Test the billing audit feature for cross-referencing dashboard charges."""

    def test_audit_structure(self, plugin, mock_project):
        """Audit returns all expected top-level keys."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_001",
                             input_tokens=1000, output_tokens=500,
                             cache_creation=200, cache_read=5000),
        ])

        audit = plugin.get_billing_audit(days=1)

        assert audit["days"] == 1
        assert audit["total_records"] == 1
        assert "totals" in audit
        assert "all_tokens" in audit
        assert "cost_at_published_rates" in audit
        assert "flat_rate_scenario" in audit
        assert "cache_read_pct" in audit
        assert "daily" in audit
        assert "models" in audit

    def test_audit_token_totals(self, plugin, mock_project):
        """Audit correctly sums all token types."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_001",
                             input_tokens=100, output_tokens=50,
                             cache_creation=200, cache_read=1000),
            _assistant_entry(minutes_ago=5, request_id="req_002",
                             input_tokens=300, output_tokens=150,
                             cache_creation=400, cache_read=3000),
        ])

        audit = plugin.get_billing_audit(days=1)
        totals = audit["totals"]

        assert totals["input_tokens"] == 400
        assert totals["output_tokens"] == 200
        assert totals["cache_creation_tokens"] == 600
        assert totals["cache_read_tokens"] == 4000
        assert audit["all_tokens"] == 5200

    def test_audit_published_cost_opus(self, plugin, mock_project):
        """Audit calculates correct cost at published Opus 4.5/4.6 rates."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=1_000_000,   # $5.00
                             output_tokens=100_000,    # $2.50
                             cache_creation=1_000_000,  # $6.25
                             cache_read=10_000_000,     # $5.00
                             model="claude-opus-4-6"),
        ])

        audit = plugin.get_billing_audit(days=1)
        cost = audit["cost_at_published_rates"]

        assert cost["breakdown"]["input"] == 5.00
        assert cost["breakdown"]["output"] == 2.50
        assert cost["breakdown"]["cache_creation"] == 6.25
        assert cost["breakdown"]["cache_read"] == 5.00
        assert cost["total"] == 18.75

    def test_audit_flat_rate_scenario(self, plugin, mock_project):
        """Flat-rate scenario bills all input-type tokens at cache creation rate."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=1_000_000,
                             output_tokens=100_000,
                             cache_creation=1_000_000,
                             cache_read=10_000_000,
                             model="claude-opus-4-6"),
        ])

        audit = plugin.get_billing_audit(days=1)
        flat = audit["flat_rate_scenario"]

        # All input-type = (1M + 1M + 10M) * $6.25/M = $75.00
        # Output = 100K * $25/M = $2.50
        assert flat["total"] == 77.50

    def test_audit_cache_read_percentage(self, plugin, mock_project):
        """Audit correctly calculates cache read percentage."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=100,
                             cache_creation=100, cache_read=9700),
        ])

        audit = plugin.get_billing_audit(days=1)
        assert audit["cache_read_pct"] == 97.0

    def test_audit_empty(self, plugin):
        """Audit handles no data gracefully."""
        audit = plugin.get_billing_audit(days=1)
        assert audit["total_records"] == 0
        assert "message" in audit

    def test_audit_format_includes_header(self, plugin, mock_project):
        """Formatted audit includes header."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        audit = plugin.get_billing_audit(days=1)
        text = plugin.format_billing_audit(audit)

        assert "BILLING AUDIT" in text
        assert "1 Days" in text

    def test_audit_format_includes_token_breakdown(self, plugin, mock_project):
        """Formatted audit shows all four token types."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=50,
                             cache_creation=200, cache_read=1000),
        ])

        audit = plugin.get_billing_audit(days=1)
        text = plugin.format_billing_audit(audit)

        assert "input_tokens:" in text
        assert "output_tokens:" in text
        assert "cache_creation_input_tokens:" in text
        assert "cache_read_input_tokens:" in text

    def test_audit_format_includes_comparison(self, plugin, mock_project):
        """Formatted audit shows how-to-interpret section."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        audit = plugin.get_billing_audit(days=1)
        text = plugin.format_billing_audit(audit)

        assert "HOW TO INTERPRET" in text
        assert "billing dashboard" in text

    def test_audit_format_includes_attnroute_link(self, plugin, mock_project):
        """Formatted audit includes attnroute attribution."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001"),
        ])

        audit = plugin.get_billing_audit(days=1)
        text = plugin.format_billing_audit(audit)

        assert "attnroute" in text

    def test_audit_format_empty(self, plugin):
        """Empty audit shows meaningful message."""
        audit = {"total_records": 0, "message": "No usage data found."}
        text = plugin.format_billing_audit(audit)
        assert "No usage data" in text

    def test_audit_format_includes_daily(self, plugin, mock_project):
        """Formatted audit includes daily breakdown with cache %."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=5, request_id="req_001",
                             input_tokens=100, output_tokens=50,
                             cache_creation=200, cache_read=5000),
        ])

        audit = plugin.get_billing_audit(days=1)
        text = plugin.format_billing_audit(audit)

        assert "DAILY BREAKDOWN" in text
        assert "Cache %" in text

    def test_audit_model_usage_tracked(self, plugin, mock_project):
        """Audit tracks per-model request counts."""
        _write_jsonl(mock_project / "session.jsonl", [
            _assistant_entry(minutes_ago=10, request_id="req_001",
                             model="claude-opus-4-6"),
            _assistant_entry(minutes_ago=5, request_id="req_002",
                             model="claude-opus-4-5-20251101"),
        ])

        audit = plugin.get_billing_audit(days=1)

        assert "claude-opus-4-6" in audit["models"]
        assert "claude-opus-4-5-20251101" in audit["models"]
        assert audit["models"]["claude-opus-4-6"]["requests"] == 1
        assert audit["models"]["claude-opus-4-5-20251101"]["requests"] == 1
