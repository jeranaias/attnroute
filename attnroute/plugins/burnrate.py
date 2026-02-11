"""
BurnRate Plugin - Predicts and warns about rate limit consumption.

Addresses GitHub issue #22435: Users report 10x variance in quota
consumption rates, hitting limits unexpectedly.

Tracks token usage patterns and predicts when you'll hit your limit,
giving early warnings to pace your work.
"""
import json
from datetime import datetime
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin


class BurnRatePlugin(AttnroutePlugin):
    """
    Tracks token consumption and predicts rate limit exhaustion.

    Features:
    - Monitors token usage from Claude Code's stats-cache.json
    - Calculates rolling burn rate (tokens per minute)
    - Predicts time until quota exhaustion
    - Injects warnings when approaching limits
    - Provides usage analytics
    """

    name = "burnrate"
    version = "0.1.0"
    description = "Predicts and warns about rate limit consumption"

    # Configuration
    SAMPLE_WINDOW = 20  # Number of samples to keep for rate calculation
    WARNING_THRESHOLD_MINUTES = 30  # Warn when <30 min remaining
    CRITICAL_THRESHOLD_MINUTES = 10  # Critical when <10 min remaining

    # Known plan limits (approximate, tokens per 5-hour window)
    PLAN_LIMITS = {
        "free": 25_000,
        "pro": 150_000,
        "max_5x": 500_000,
        "max_20x": 2_000_000,
        "api": float("inf"),  # API has per-minute limits, not session
    }

    # Stats cache location
    STATS_CACHE = Path.home() / ".claude" / "stats-cache.json"

    def __init__(self):
        super().__init__()
        self._history_file = self._state_dir / "burnrate_history.jsonl"

    def on_session_start(self, session_state: dict) -> str | None:
        """Initialize tracking for new session."""
        # Read current stats to establish baseline
        baseline = self._read_stats_cache()

        self.save_state({
            "session_id": session_state.get("session_id", "unknown"),
            "session_start": datetime.now().isoformat(),
            "baseline_tokens": baseline.get("total_tokens", 0),
            "samples": [],
            "plan_type": self._detect_plan_type(baseline),
            "warnings_issued": 0,
        })

        # Show current status
        plan = self._detect_plan_type(baseline)
        used = baseline.get("session_tokens", 0)
        limit = self.PLAN_LIMITS.get(plan, 150_000)

        if limit == float("inf"):
            return "BurnRate: Active (API mode - per-minute limits)"

        pct = (used / limit * 100) if limit else 0
        return f"BurnRate: Active ({plan} plan, {pct:.0f}% used this window)"

    def on_prompt_pre(self, prompt: str, session_state: dict) -> tuple[str, bool]:
        """Pass through - we don't modify prompts."""
        return prompt, True

    def on_prompt_post(
        self,
        prompt: str,
        context_output: str,
        session_state: dict
    ) -> str:
        """
        Inject burn rate warning if approaching limits.
        """
        state = self.load_state()
        stats = self._read_stats_cache()

        # Record sample
        self._record_sample(state, stats)

        # Calculate burn rate and prediction
        rate_info = self._calculate_burn_rate(state, stats)

        if not rate_info:
            return ""

        minutes_remaining = rate_info.get("minutes_remaining")
        if minutes_remaining is None or minutes_remaining == float("inf"):
            return ""

        # Determine warning level
        if minutes_remaining <= self.CRITICAL_THRESHOLD_MINUTES:
            level = "CRITICAL"
            state["warnings_issued"] = state.get("warnings_issued", 0) + 1
        elif minutes_remaining <= self.WARNING_THRESHOLD_MINUTES:
            level = "WARNING"
            state["warnings_issued"] = state.get("warnings_issued", 0) + 1
        else:
            self.save_state(state)
            return ""  # No warning needed

        self.save_state(state)

        # Build warning context
        lines = [
            "",
            f"## BurnRate {level}",
            f"**Estimated time until rate limit: ~{int(minutes_remaining)} minutes**",
            "",
            f"- Current burn rate: {rate_info['tokens_per_minute']:.0f} tokens/min",
            f"- Tokens used this window: {rate_info['tokens_used']:,}",
            f"- Window limit: {rate_info['limit']:,}",
            "",
        ]

        if level == "CRITICAL":
            lines.extend([
                "**Consider:**",
                "- Pausing for a few minutes to let the window slide",
                "- Switching to a smaller model (Haiku) for simple tasks",
                "- Breaking work into smaller, focused prompts",
                ""
            ])

        return "\n".join(lines)

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """
        Record final token count for this turn.
        """
        state = self.load_state()
        stats = self._read_stats_cache()

        # Record sample
        self._record_sample(state, stats)
        self.save_state(state)

        # Log to history
        self._log_sample(stats)

        return None

    def _read_stats_cache(self) -> dict:
        """Read Claude Code's stats cache."""
        if not self.STATS_CACHE.exists():
            return {}

        try:
            data = json.loads(self.STATS_CACHE.read_text(encoding="utf-8"))
            # Extract relevant fields
            return {
                "total_tokens": data.get("totalTokens", 0),
                "session_tokens": data.get("sessionTokens", 0),
                "input_tokens": data.get("inputTokens", 0),
                "output_tokens": data.get("outputTokens", 0),
                "cost_usd": data.get("costUsd", 0),
                "model": data.get("model", "unknown"),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception:
            return {}

    def _detect_plan_type(self, stats: dict) -> str:
        """Detect plan type from usage patterns."""
        # This is a heuristic - could be improved with actual plan detection
        model = stats.get("model", "").lower()

        if "api" in model or not model:
            return "api"

        # Check for high limits (Max plans)
        session_tokens = stats.get("session_tokens", 0)
        if session_tokens > 300_000:
            return "max_20x"
        elif session_tokens > 100_000:
            return "max_5x"

        # Default to pro
        return "pro"

    def _record_sample(self, state: dict, stats: dict) -> None:
        """Record a token usage sample."""
        samples = state.get("samples", [])

        sample = {
            "timestamp": datetime.now().isoformat(),
            "total_tokens": stats.get("total_tokens", 0),
            "session_tokens": stats.get("session_tokens", 0),
        }

        samples.append(sample)

        # Trim to window size
        samples = samples[-self.SAMPLE_WINDOW:]
        state["samples"] = samples

    def _calculate_burn_rate(self, state: dict, stats: dict) -> dict | None:
        """Calculate current burn rate and predict exhaustion."""
        samples = state.get("samples", [])

        if len(samples) < 2:
            return None

        # Calculate rate from recent samples
        try:
            first = samples[0]
            last = samples[-1]

            first_time = datetime.fromisoformat(first["timestamp"])
            last_time = datetime.fromisoformat(last["timestamp"])

            elapsed_minutes = (last_time - first_time).total_seconds() / 60

            if elapsed_minutes <= 0 or elapsed_minutes < 0.5:
                return None  # Not enough time elapsed (also guards against division by zero)

            tokens_consumed = last["session_tokens"] - first["session_tokens"]

            if tokens_consumed <= 0:
                return None  # No consumption

            tokens_per_minute = tokens_consumed / elapsed_minutes
            if tokens_per_minute <= 0:
                return None  # Guard against edge cases

            # Get limit for plan
            plan = state.get("plan_type", "pro")
            limit = self.PLAN_LIMITS.get(plan, 150_000)

            if limit == float("inf"):
                return {
                    "tokens_per_minute": tokens_per_minute,
                    "tokens_used": stats.get("session_tokens", 0),
                    "limit": limit,
                    "minutes_remaining": float("inf"),
                }

            # Calculate remaining
            tokens_remaining = limit - stats.get("session_tokens", 0)

            if tokens_remaining <= 0:
                minutes_remaining = 0
            else:
                minutes_remaining = tokens_remaining / tokens_per_minute

            return {
                "tokens_per_minute": tokens_per_minute,
                "tokens_used": stats.get("session_tokens", 0),
                "limit": limit,
                "tokens_remaining": tokens_remaining,
                "minutes_remaining": minutes_remaining,
            }

        except Exception:
            return None

    def _log_sample(self, stats: dict) -> None:
        """Append sample to history file."""
        try:
            entry = {
                "timestamp": datetime.now().isoformat(),
                **stats
            }
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def get_session_summary(self) -> dict:
        """Get summary of current session status."""
        state = self.load_state()
        stats = self._read_stats_cache()
        rate_info = self._calculate_burn_rate(state, stats)

        summary = {
            "plan_type": state.get("plan_type", "unknown"),
            "samples_collected": len(state.get("samples", [])),
            "warnings_issued": state.get("warnings_issued", 0),
            "session_tokens": stats.get("session_tokens", 0),
        }

        if rate_info:
            summary["tokens_per_minute"] = round(rate_info.get("tokens_per_minute", 0), 1)
            minutes = rate_info.get("minutes_remaining")
            if minutes and minutes != float("inf"):
                summary["minutes_remaining"] = round(minutes, 1)

        return summary
