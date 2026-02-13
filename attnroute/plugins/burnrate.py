"""
BurnRate Plugin — Real-time rate limit tracker for Claude Code.

Addresses GitHub issue #22435: Users hit rate limits unexpectedly
with no advance warning, despite paying for Pro/Max plans.

Instead of relying on daily aggregate stats, this plugin scans the
actual per-message conversation JSONL files that Claude Code stores
locally. Each API response includes precise token counts — input,
output, cache creation, and cache reads — with millisecond timestamps.

By aggregating these across ALL conversations within a 5-hour sliding
window (matching Claude Code's actual rate limit window), BurnRate
provides:

  - Real-time window utilization (e.g., "73% of 150,000 token limit")
  - Burn rate (tokens per minute) from observed consumption
  - Time-to-exhaustion prediction based on current pace
  - Tiered warnings injected into context when approaching limits
  - Full token breakdown: input, output, cache creation, cache reads
  - Historical usage reports: per-project, per-model, per-day, with cost

This is the rate limit visibility that should be built into Claude Code.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin


class BurnRatePlugin(AttnroutePlugin):
    """
    Real-time rate limit tracker using conversation-level token data.

    Scans Claude Code's conversation JSONL files across all projects
    to build a precise 5-hour sliding window of token consumption,
    then calculates burn rate and predicts time-to-exhaustion.

    Also provides full historical usage reports with per-project,
    per-model, and per-day breakdowns with API-equivalent cost
    estimation.

    Data source: ~/.claude/projects/*/*.jsonl
    Each assistant message contains per-request token usage with
    input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens, and ISO timestamps.
    """

    name = "burnrate"
    version = "0.3.0"
    description = "Real-time rate limit tracker with historical usage reports"

    # --- Configuration ---

    # Claude Code uses 5-hour sliding windows for rate limits
    WINDOW_HOURS = 5

    # Don't re-scan conversations more often than this (seconds)
    SCAN_COOLDOWN = 30

    # Burn rate calculated from last N minutes of activity
    RATE_WINDOW_MINUTES = 15

    # Known plan limits (tokens per 5-hour window, approximate).
    # Anthropic doesn't publish exact numbers; these are community-sourced.
    PLAN_LIMITS = {
        "free": 25_000,
        "pro": 150_000,
        "max_5x": 500_000,
        "max_20x": 2_000_000,
    }

    # Monthly subscription cost per plan (USD)
    PLAN_COSTS = {
        "free": 0,
        "pro": 20,
        "max_5x": 100,
        "max_20x": 200,
    }

    # Warning thresholds (percentage of window limit used)
    WARN_PCT = 60       # Informational notice
    HIGH_PCT = 80       # Warning with suggestions
    CRITICAL_PCT = 95   # Urgent warning

    # Published API pricing per million tokens (USD, as of 2026).
    # Opus 4.5/4.6 have LOWER pricing than Opus 4.0/4.1.
    # Cache write has two tiers: 5-minute (1.25x base) and 1-hour (2x base).
    # Cache reads are 0.1x base regardless of TTL tier.
    # Used for cost estimation — shows users the API-equivalent value
    # of their subscription usage.
    MODEL_PRICING = {
        "opus": {
            "input": 5.00,
            "output": 25.00,
            "cache_create_5m": 6.25,
            "cache_create_1h": 10.00,
            "cache_read": 0.50,
        },
        "opus_legacy": {
            # Opus 4.0 / 4.1 (higher pricing)
            "input": 15.00,
            "output": 75.00,
            "cache_create_5m": 18.75,
            "cache_create_1h": 30.00,
            "cache_read": 1.50,
        },
        "sonnet": {
            "input": 3.00,
            "output": 15.00,
            "cache_create_5m": 3.75,
            "cache_create_1h": 6.00,
            "cache_read": 0.30,
        },
        "haiku": {
            "input": 1.00,
            "output": 5.00,
            "cache_create_5m": 1.25,
            "cache_create_1h": 2.00,
            "cache_read": 0.10,
        },
    }

    # Claude Code data directory (overridable for testing)
    CLAUDE_DIR = Path.home() / ".claude"

    def __init__(self):
        super().__init__()
        self._history_file = self._state_dir / "burnrate_history.jsonl"
        # In-memory cache of last scan results
        self._cached_records: list[dict] = []
        self._cache_time: datetime | None = None

    def on_session_start(self, session_state: dict) -> str | None:
        """Initialize tracking and show current window status."""
        now = datetime.now(timezone.utc)

        # Do initial scan
        records = self._scan_window(now)
        stats = self._compute_window_stats(records)

        # Allow explicit plan_type from session config, fall back to heuristic
        plan = session_state.get("plan_type") or self._detect_plan_type(stats)
        limit = self.PLAN_LIMITS.get(plan, 150_000)
        pct = (stats["window_tokens"] / limit * 100) if limit else 0

        self.save_state({
            "session_id": session_state.get("session_id", "unknown"),
            "session_start": now.isoformat(),
            "plan_type": plan,
            "warnings_issued": 0,
            "last_scan": now.isoformat(),
        })

        return (
            f"BurnRate: Active ({plan} plan, "
            f"{stats['window_tokens']:,} / {limit:,} tokens in 5h window, "
            f"{pct:.0f}% used)"
        )

    def on_prompt_pre(self, prompt: str, session_state: dict) -> tuple[str, bool]:
        """Pass through — we don't modify prompts."""
        return prompt, True

    def on_prompt_post(
        self,
        prompt: str,
        context_output: str,
        session_state: dict,
    ) -> str:
        """Inject burn rate warning if approaching limits."""
        state = self.load_state()
        now = datetime.now(timezone.utc)

        records = self._scan_window(now)
        if not records:
            return ""

        stats = self._compute_window_stats(records)
        plan = state.get("plan_type", "pro")
        limit = self.PLAN_LIMITS.get(plan, 150_000)

        if limit <= 0:
            return ""

        pct = (stats["window_tokens"] / limit) * 100

        if pct < self.WARN_PCT:
            return ""  # No warning needed — don't waste context tokens

        # Calculate burn rate
        burn = self._calculate_burn_rate(records, now)

        # Update warning count
        state["warnings_issued"] = state.get("warnings_issued", 0) + 1
        self.save_state(state)

        return self._format_warning(stats, burn, plan, limit, pct)

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """Log sample to history after each turn."""
        now = datetime.now(timezone.utc)
        records = self._scan_window(now)
        stats = self._compute_window_stats(records)

        try:
            entry = {"timestamp": now.isoformat(), **stats}
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------ #
    #  Core scanning — reads actual conversation JSONL files              #
    # ------------------------------------------------------------------ #

    def _scan_window(self, now: datetime) -> list[dict]:
        """Scan all conversation JSONL files for usage in the 5h window.

        Walks every project under ~/.claude/projects/, reads *.jsonl
        files whose mtime falls within the window, extracts assistant
        messages with token usage, and deduplicates by requestId
        (streaming chunks share the same requestId).

        Returns a sorted list of usage records.
        """
        # Use cached results if fresh enough
        if (
            self._cache_time
            and (now - self._cache_time).total_seconds() < self.SCAN_COOLDOWN
        ):
            return self._cached_records

        window_start = now - timedelta(hours=self.WINDOW_HOURS)
        records: list[dict] = []
        seen_requests: set[str] = set()

        # Walk all project directories
        projects_dir = self.CLAUDE_DIR / "projects"
        if not projects_dir.exists():
            return []

        for project_dir in self._iter_dirs(projects_dir):
            project = self._project_name(project_dir.name)
            for jsonl_file in project_dir.glob("*.jsonl"):
                # Skip files not modified since window start
                try:
                    mtime = datetime.fromtimestamp(
                        jsonl_file.stat().st_mtime, tz=timezone.utc,
                    )
                    if mtime < window_start:
                        continue
                except OSError:
                    continue

                file_records = self._extract_usage_records(
                    jsonl_file, window_start, seen_requests,
                )
                for r in file_records:
                    # Prefer cwd (actual path) over encoded dir name
                    cwd_project = self._project_from_cwd(r.get("cwd", ""))
                    r["project"] = cwd_project or project
                records.extend(file_records)

        # Sort chronologically
        records.sort(key=lambda r: r["timestamp"])

        # Update cache
        self._cached_records = records
        self._cache_time = now

        return records

    def _scan_history(self, days: int = 7) -> list[dict]:
        """Scan all conversation JSONL files for the specified number of days.

        Unlike _scan_window (which uses a 5h window with caching for
        per-prompt injection), this scans a wider time range for
        historical reporting. No caching since it's for on-demand reports.

        Returns records with project attribution.
        """
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days)
        records: list[dict] = []
        seen_requests: set[str] = set()

        projects_dir = self.CLAUDE_DIR / "projects"
        if not projects_dir.exists():
            return []

        for project_dir in self._iter_dirs(projects_dir):
            project = self._project_name(project_dir.name)
            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(
                        jsonl_file.stat().st_mtime, tz=timezone.utc,
                    )
                    if mtime < window_start:
                        continue
                except OSError:
                    continue

                file_records = self._extract_usage_records(
                    jsonl_file, window_start, seen_requests,
                )
                for r in file_records:
                    cwd_project = self._project_from_cwd(r.get("cwd", ""))
                    r["project"] = cwd_project or project
                records.extend(file_records)

        records.sort(key=lambda r: r["timestamp"])
        return records

    @staticmethod
    def _iter_dirs(parent: Path):
        """Yield child directories, tolerating permission errors."""
        try:
            for child in parent.iterdir():
                if child.is_dir():
                    yield child
        except OSError:
            return

    @staticmethod
    def _project_name(dir_name: str) -> str:
        """Extract readable project name from Claude's encoded directory name.

        Claude Code encodes project paths as directory names:
          C:\\Users\\jesse\\attnroute  ->  C--Users-jesse-attnroute
          /home/user/project          ->  -home-user-project

        The encoding replaces ':\\' or ':/' with '--' and remaining
        path separators with '-'. This is lossy (can't distinguish
        hyphens from separators), but we use heuristics to extract
        the meaningful project name.
        """
        if not dir_name:
            return "unknown"

        # Strip drive prefix (everything up to and including '--')
        if "--" in dir_name:
            remainder = dir_name.split("--", 1)[1]
        elif dir_name.startswith("-"):
            # Linux/macOS: /home/user/project -> -home-user-project
            remainder = dir_name.lstrip("-")
        else:
            remainder = dir_name

        # Split into path segments (separator was '-')
        segments = remainder.split("-")

        # Strip common home directory prefixes:
        #   Windows: Users-<username>-...
        #   Linux:   home-<username>-...
        #   macOS:   Users-<username>-...
        if len(segments) >= 2 and segments[0] in ("Users", "home"):
            project_segments = segments[2:]  # Skip Users/home + username
            if project_segments:
                return "-".join(project_segments)
            return "~"

        # No recognized prefix — return everything after drive
        return remainder if remainder else dir_name

    @staticmethod
    def _project_from_cwd(cwd: str) -> str:
        """Extract a readable project name from an actual cwd path.

        Falls back to the last path component, stripping home directory
        prefix for cleaner display.

        Examples:
            C:\\Users\\jesse\\attnroute  ->  attnroute
            /home/user/my-project       ->  my-project
            C:\\Users\\jesse             ->  ~
        """
        if not cwd:
            return ""
        p = Path(cwd)
        home = Path.home()
        try:
            relative = p.relative_to(home)
            parts = relative.parts
            if not parts:
                return "~"
            return parts[0]
        except ValueError:
            # Not under home — just use last component
            return p.name or str(p)

    @staticmethod
    def _get_model_family(model_id: str) -> str:
        """Map a full model ID to its pricing family.

        Model IDs look like 'claude-opus-4-6', 'claude-opus-4-5-20251101',
        'claude-opus-4-20250514', 'claude-sonnet-4-5-20250929'.

        Opus 4.5+ uses lower pricing ($5/M input) vs Opus 4.0/4.1 ($15/M).
        """
        model_lower = model_id.lower()
        if "opus" in model_lower:
            # Opus 4.0 and 4.1 have higher legacy pricing
            # Opus 4.5, 4.6+ have lower pricing
            if "opus-4-0" in model_lower or "opus-4-1" in model_lower:
                return "opus_legacy"
            # Check for plain "opus-4-2025" (no sub-version = 4.0)
            # e.g., claude-opus-4-20250514
            if "opus-4-2025" in model_lower:
                return "opus_legacy"
            return "opus"
        if "haiku" in model_lower:
            return "haiku"
        if "sonnet" in model_lower:
            return "sonnet"
        return "sonnet"  # Default to sonnet pricing

    def _extract_usage_records(
        self,
        filepath: Path,
        window_start: datetime,
        seen_requests: set[str],
    ) -> list[dict]:
        """Extract deduplicated token usage records from a JSONL file.

        Each line is a JSON object. We only care about lines with:
          - type == "assistant"
          - message.usage present
          - timestamp within our window
          - unique requestId (streaming chunks share the same one)
        """
        records: list[dict] = []

        try:
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    # Fast pre-filter — skip lines that can't have usage data
                    if '"usage"' not in line:
                        continue
                    if '"assistant"' not in line:
                        continue

                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("type") != "assistant":
                        continue

                    # Parse timestamp
                    ts = self._parse_timestamp(entry.get("timestamp"))
                    if ts is None or ts < window_start:
                        continue

                    # Deduplicate by requestId
                    request_id = entry.get("requestId", "")
                    if request_id:
                        if request_id in seen_requests:
                            continue
                        seen_requests.add(request_id)

                    # Extract usage from message
                    message = entry.get("message", {})
                    usage = message.get("usage")
                    if not usage:
                        continue

                    # Extract cache creation breakdown by TTL tier
                    # Newer JSONL has: usage.cache_creation.ephemeral_5m/1h
                    cache_detail = usage.get("cache_creation", {})
                    if isinstance(cache_detail, dict):
                        cc_5m = cache_detail.get(
                            "ephemeral_5m_input_tokens", 0,
                        )
                        cc_1h = cache_detail.get(
                            "ephemeral_1h_input_tokens", 0,
                        )
                    else:
                        # Older format or no breakdown — assume all 5m
                        cc_5m = usage.get(
                            "cache_creation_input_tokens", 0,
                        )
                        cc_1h = 0

                    # Total cache creation (for backwards compat)
                    cc_total = usage.get(
                        "cache_creation_input_tokens", 0,
                    )
                    # If breakdown exists but total doesn't, sum them
                    if cc_total == 0 and (cc_5m or cc_1h):
                        cc_total = cc_5m + cc_1h

                    records.append({
                        "timestamp": ts.isoformat(),
                        "model": message.get("model", "unknown"),
                        "session_id": entry.get("sessionId", "unknown"),
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_creation_tokens": cc_total,
                        "cache_creation_5m_tokens": cc_5m,
                        "cache_creation_1h_tokens": cc_1h,
                        "cache_read_tokens": usage.get(
                            "cache_read_input_tokens", 0,
                        ),
                        "cwd": entry.get("cwd", ""),
                        "git_branch": entry.get("gitBranch", ""),
                    })
        except Exception:
            pass

        return records

    @staticmethod
    def _parse_timestamp(ts_str: str | None) -> datetime | None:
        """Parse an ISO timestamp, handling 'Z' suffix and naive datetimes."""
        if not ts_str:
            return None
        try:
            # Python 3.11+ handles "Z" natively, but be safe
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------ #
    #  Computation                                                        #
    # ------------------------------------------------------------------ #

    def _compute_window_stats(self, records: list[dict]) -> dict:
        """Aggregate token usage from all records in the window.

        Returns a dict with:
          - window_tokens: input + output + cache_creation (billable)
          - Separate counts for input, output, cache_creation, cache_read
          - api_calls: number of unique API calls in window
          - models: per-model token breakdown
          - sessions: per-session token breakdown
          - projects: per-project token breakdown
          - primary_model: model with highest usage
        """
        total_input = 0
        total_output = 0
        total_cache_create = 0
        total_cache_read = 0
        models: dict[str, int] = {}
        sessions: dict[str, int] = {}
        projects: dict[str, int] = {}

        for r in records:
            inp = r.get("input_tokens", 0)
            out = r.get("output_tokens", 0)
            cc = r.get("cache_creation_tokens", 0)
            cr = r.get("cache_read_tokens", 0)
            billable = inp + out + cc

            total_input += inp
            total_output += out
            total_cache_create += cc
            total_cache_read += cr

            model = r.get("model", "unknown")
            models[model] = models.get(model, 0) + billable

            session_id = r.get("session_id", "unknown")
            sessions[session_id] = sessions.get(session_id, 0) + billable

            project = r.get("project", "unknown")
            projects[project] = projects.get(project, 0) + billable

        # "Window tokens" = what likely counts toward rate limits.
        # Input + output + cache creation are full-price tokens.
        # Cache reads are heavily discounted (90%) and likely don't
        # count fully against the window, so we exclude them from
        # the primary metric but still report them.
        window_tokens = total_input + total_output + total_cache_create

        return {
            "window_tokens": window_tokens,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_creation_tokens": total_cache_create,
            "cache_read_tokens": total_cache_read,
            "api_calls": len(records),
            "models": models,
            "sessions": sessions,
            "projects": projects,
            "primary_model": (
                max(models, key=models.get) if models else "unknown"
            ),
        }

    def _calculate_burn_rate(
        self,
        records: list[dict],
        now: datetime,
    ) -> dict | None:
        """Calculate tokens/min from recent activity.

        Uses the last RATE_WINDOW_MINUTES of data for responsiveness.
        Falls back to the full window if not enough recent data.
        """
        if len(records) < 2:
            return None

        rate_cutoff = now - timedelta(minutes=self.RATE_WINDOW_MINUTES)

        # Try recent records first
        recent = [
            r for r in records
            if (self._parse_timestamp(r["timestamp"]) or now) >= rate_cutoff
        ]

        # Fall back to full window if not enough recent data
        if len(recent) < 2:
            recent = records

        if len(recent) < 2:
            return None

        first_ts = self._parse_timestamp(recent[0]["timestamp"])
        last_ts = self._parse_timestamp(recent[-1]["timestamp"])

        if not first_ts or not last_ts:
            return None

        elapsed_min = (last_ts - first_ts).total_seconds() / 60
        if elapsed_min < 0.5:
            return None  # Not enough time elapsed

        # Sum billable tokens in rate window
        total = sum(
            r.get("input_tokens", 0)
            + r.get("output_tokens", 0)
            + r.get("cache_creation_tokens", 0)
            for r in recent
        )

        tokens_per_min = total / elapsed_min

        return {
            "tokens_per_minute": tokens_per_min,
            "sample_minutes": round(elapsed_min, 1),
            "sample_count": len(recent),
        }

    def _detect_plan_type(self, stats: dict) -> str:
        """Heuristic plan detection from window usage.

        If you've already consumed more than a plan's limit within
        the window, you must be on a higher-tier plan.

        Can be overridden by setting plan_type in session_state.
        """
        window = stats.get("window_tokens", 0)

        if window > 500_000:
            return "max_20x"
        elif window > 150_000:
            return "max_5x"

        return "pro"

    def _estimate_cost(self, records: list[dict]) -> dict:
        """Estimate API-equivalent cost from token records.

        Maps each record's model to its pricing family (opus/sonnet/haiku)
        and calculates what the usage would cost at published API rates.

        Returns total cost and breakdowns by model and project.
        """
        total_cost = 0.0
        cost_by_model: dict[str, float] = {}
        cost_by_project: dict[str, float] = {}

        for r in records:
            family = self._get_model_family(r.get("model", ""))
            pricing = self.MODEL_PRICING.get(family, self.MODEL_PRICING["sonnet"])

            # Use per-tier cache creation rates when breakdown available
            cc_5m = r.get("cache_creation_5m_tokens", 0)
            cc_1h = r.get("cache_creation_1h_tokens", 0)
            if cc_5m or cc_1h:
                cache_cost = (
                    cc_5m * pricing["cache_create_5m"] / 1_000_000
                    + cc_1h * pricing["cache_create_1h"] / 1_000_000
                )
            else:
                # Fallback: all cache creation at 5m rate
                cache_cost = (
                    r.get("cache_creation_tokens", 0)
                    * pricing["cache_create_5m"] / 1_000_000
                )

            cost = (
                r.get("input_tokens", 0) * pricing["input"] / 1_000_000
                + r.get("output_tokens", 0) * pricing["output"] / 1_000_000
                + cache_cost
                + r.get("cache_read_tokens", 0)
                * pricing["cache_read"] / 1_000_000
            )

            total_cost += cost

            model = r.get("model", "unknown")
            cost_by_model[model] = cost_by_model.get(model, 0) + cost

            project = r.get("project", "unknown")
            cost_by_project[project] = cost_by_project.get(project, 0) + cost

        return {
            "total": round(total_cost, 2),
            "by_model": {k: round(v, 2) for k, v in cost_by_model.items()},
            "by_project": {k: round(v, 2) for k, v in cost_by_project.items()},
        }

    def _compute_daily_breakdown(self, records: list[dict]) -> list[dict]:
        """Group records by date and compute daily stats.

        Returns a list of per-day summaries sorted most-recent first.
        """
        daily: dict[str, dict] = {}

        for r in records:
            ts = self._parse_timestamp(r["timestamp"])
            if not ts:
                continue
            date_key = ts.strftime("%Y-%m-%d")

            if date_key not in daily:
                daily[date_key] = {
                    "date": date_key,
                    "billable_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "api_calls": 0,
                }

            day = daily[date_key]
            inp = r.get("input_tokens", 0)
            out = r.get("output_tokens", 0)
            cc = r.get("cache_creation_tokens", 0)

            day["billable_tokens"] += inp + out + cc
            day["input_tokens"] += inp
            day["output_tokens"] += out
            day["cache_creation_tokens"] += cc
            day["cache_read_tokens"] += r.get("cache_read_tokens", 0)
            day["api_calls"] += 1

        return sorted(daily.values(), key=lambda d: d["date"], reverse=True)

    def _compute_project_breakdown(self, records: list[dict]) -> dict:
        """Group records by project and compute per-project stats.

        Returns a dict mapping project name to stats including
        billable tokens, API calls, and models used.
        """
        projects: dict[str, dict] = {}

        for r in records:
            project = r.get("project", "unknown")
            if project not in projects:
                projects[project] = {
                    "billable_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "api_calls": 0,
                    "models": set(),
                    "sessions": set(),
                }

            p = projects[project]
            inp = r.get("input_tokens", 0)
            out = r.get("output_tokens", 0)
            cc = r.get("cache_creation_tokens", 0)

            p["billable_tokens"] += inp + out + cc
            p["input_tokens"] += inp
            p["output_tokens"] += out
            p["cache_creation_tokens"] += cc
            p["cache_read_tokens"] += r.get("cache_read_tokens", 0)
            p["api_calls"] += 1
            p["models"].add(r.get("model", "unknown"))
            p["sessions"].add(r.get("session_id", "unknown"))

        # Convert sets to lists for JSON serialization
        for p in projects.values():
            p["models"] = sorted(p["models"])
            p["sessions"] = len(p["sessions"])

        return projects

    # ------------------------------------------------------------------ #
    #  Formatting                                                         #
    # ------------------------------------------------------------------ #

    def _format_warning(
        self,
        stats: dict,
        burn: dict | None,
        plan: str,
        limit: int,
        pct: float,
    ) -> str:
        """Format a clear, informative burn rate warning for context injection."""
        lines = [""]

        # Header based on severity
        if pct >= self.CRITICAL_PCT:
            lines.append("## BurnRate CRITICAL")
        elif pct >= self.HIGH_PCT:
            lines.append("## BurnRate WARNING")
        else:
            lines.append("## BurnRate Notice")

        # Progress bar (20 chars wide)
        bar_filled = int(20 * min(pct, 100) / 100)
        bar = "\u2588" * bar_filled + "\u2591" * (20 - bar_filled)
        lines.append(f"`[{bar}]` **{pct:.0f}%**")
        lines.append("")

        # Core stats
        lines.append(
            f"**Window:** {stats['window_tokens']:,} / {limit:,} tokens "
            f"({plan} plan, {self.WINDOW_HOURS}h sliding window)"
        )

        if burn:
            lines.append(
                f"**Burn rate:** {burn['tokens_per_minute']:,.0f} tokens/min "
                f"(last {burn['sample_minutes']}min)"
            )

            # Time remaining prediction
            remaining = limit - stats["window_tokens"]
            if remaining > 0 and burn["tokens_per_minute"] > 0:
                minutes_left = remaining / burn["tokens_per_minute"]
                if minutes_left < 60:
                    lines.append(
                        f"**ETA to limit:** ~{int(minutes_left)} minutes"
                    )
                else:
                    hours_left = minutes_left / 60
                    lines.append(
                        f"**ETA to limit:** ~{hours_left:.1f} hours"
                    )
            elif remaining <= 0:
                lines.append("**At or over limit — expect rate limiting.**")

        # Token breakdown by category
        lines.append("")
        lines.append(
            f"_Input: {stats['input_tokens']:,} | "
            f"Output: {stats['output_tokens']:,} | "
            f"Cache create: {stats['cache_creation_tokens']:,} | "
            f"Cache read: {stats['cache_read_tokens']:,} "
            f"({stats['api_calls']} API calls)_"
        )

        # Per-model attribution (show where tokens are going)
        models = stats.get("models", {})
        if len(models) > 1:
            lines.append("")
            lines.append("**By model:**")
            for model, tokens in sorted(
                models.items(), key=lambda x: x[1], reverse=True,
            ):
                model_pct = (tokens / stats["window_tokens"] * 100
                             if stats["window_tokens"] else 0)
                short_name = model.split("-")[1] if "-" in model else model
                lines.append(f"- {short_name}: {tokens:,} ({model_pct:.0f}%)")

        # Per-project attribution
        projects = stats.get("projects", {})
        if projects and len(projects) > 1:
            lines.append("")
            lines.append("**By project:**")
            for name, tokens in sorted(
                projects.items(), key=lambda x: x[1], reverse=True,
            )[:5]:
                proj_pct = (tokens / stats["window_tokens"] * 100
                            if stats["window_tokens"] else 0)
                lines.append(f"- {name}: {tokens:,} ({proj_pct:.0f}%)")

        # Per-session attribution (which conversations are burning tokens)
        sessions = stats.get("sessions", {})
        if sessions and len(sessions) > 1:
            lines.append("")
            lines.append("**By session:**")
            for sid, tokens in sorted(
                sessions.items(), key=lambda x: x[1], reverse=True,
            )[:5]:  # Top 5 sessions
                sess_pct = (tokens / stats["window_tokens"] * 100
                            if stats["window_tokens"] else 0)
                short_id = sid[:8] if len(sid) > 8 else sid
                lines.append(f"- {short_id}...: {tokens:,} ({sess_pct:.0f}%)")

        # Advice based on severity
        if pct >= self.CRITICAL_PCT:
            lines.extend([
                "",
                "**Slow down immediately:**",
                "- Pause for a few minutes (old usage falls off the window)",
                "- Use shorter, focused prompts",
                "- Avoid large file reads that inflate cache tokens",
            ])
        elif pct >= self.HIGH_PCT:
            lines.extend([
                "",
                "**Consider pacing your work** — keep prompts focused "
                "and avoid unnecessary file reads.",
            ])

        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Usage reports — historical analysis                                #
    # ------------------------------------------------------------------ #

    def _compute_insights(
        self,
        records: list[dict],
        cost: dict,
        daily: list[dict],
    ) -> dict:
        """Derive higher-level insight metrics from raw records.

        Returns:
            peak_hours: list of (hour, token_count) sorted by usage
            sessions: total unique sessions
            avg_tokens_per_session: average billable tokens per session
            avg_cost_per_session: average API-equiv cost per session
            active_days: number of days with any activity
            avg_tokens_per_active_day: average daily billable tokens
            cache_hit_rate: fraction of reads vs total (higher = better)
            busiest_day: (date_str, billable_tokens)
            branches: dict of git_branch -> billable_tokens
        """
        if not records:
            return {}

        # Peak hours
        hours: dict[int, int] = {}
        sessions: set[str] = set()
        branches: dict[str, int] = {}

        for r in records:
            ts = self._parse_timestamp(r["timestamp"])
            if ts:
                # Use local hour (convert from UTC)
                local_hour = (ts.hour - 5) % 24  # EST approximation
                billable = (
                    r.get("input_tokens", 0)
                    + r.get("output_tokens", 0)
                    + r.get("cache_creation_tokens", 0)
                )
                hours[local_hour] = hours.get(local_hour, 0) + billable

            sessions.add(r.get("session_id", "unknown"))

            branch = r.get("git_branch", "")
            if branch:
                billable = (
                    r.get("input_tokens", 0)
                    + r.get("output_tokens", 0)
                    + r.get("cache_creation_tokens", 0)
                )
                branches[branch] = branches.get(branch, 0) + billable

        # Sort peak hours descending
        peak_hours = sorted(hours.items(), key=lambda x: x[1], reverse=True)

        # Session stats
        n_sessions = len(sessions)
        total_billable = sum(
            r.get("input_tokens", 0) + r.get("output_tokens", 0)
            + r.get("cache_creation_tokens", 0)
            for r in records
        )
        total_cache_read = sum(r.get("cache_read_tokens", 0) for r in records)

        # Cache hit rate: what fraction of all input was served from cache
        total_input_all = total_billable + total_cache_read
        cache_hit_rate = (
            total_cache_read / total_input_all if total_input_all else 0
        )

        # Busiest day
        busiest = max(daily, key=lambda d: d["billable_tokens"]) if daily else None

        # Active days
        active_days = len(daily)

        return {
            "peak_hours": peak_hours[:5],  # Top 5 hours
            "sessions": n_sessions,
            "avg_tokens_per_session": (
                round(total_billable / n_sessions) if n_sessions else 0
            ),
            "avg_cost_per_session": (
                round(cost.get("total", 0) / n_sessions, 2)
                if n_sessions else 0
            ),
            "active_days": active_days,
            "avg_tokens_per_active_day": (
                round(total_billable / active_days) if active_days else 0
            ),
            "cache_hit_rate": round(cache_hit_rate, 3),
            "busiest_day": (
                (busiest["date"], busiest["billable_tokens"])
                if busiest else None
            ),
            "branches": dict(
                sorted(branches.items(), key=lambda x: x[1], reverse=True)[:10]
            ),
        }

    def get_usage_report(self, days: int = 7) -> dict:
        """Comprehensive usage report for the specified time period.

        Scans all conversation JSONL files going back `days` days and
        returns a structured report with:
          - Total billable tokens, API calls
          - API-equivalent cost estimation
          - Per-day breakdown with daily totals
          - Per-project breakdown with token attribution
          - Per-model breakdown
          - Current rate limit status (5h window)

        This goes far beyond the 5h rate limit window — it's a full
        historical view of where tokens are going.
        """
        records = self._scan_history(days)

        if not records:
            return {
                "days": days,
                "total_records": 0,
                "billable_tokens": 0,
                "message": "No usage data found.",
            }

        stats = self._compute_window_stats(records)
        cost = self._estimate_cost(records)
        daily = self._compute_daily_breakdown(records)
        projects = self._compute_project_breakdown(records)

        # Current rate limit status (5h window)
        now = datetime.now(timezone.utc)
        window_records = self._scan_window(now)
        window_stats = self._compute_window_stats(window_records)
        # Re-detect plan from actual usage (stale state may be wrong)
        plan = self._detect_plan_type(window_stats)
        limit = self.PLAN_LIMITS.get(plan, 150_000)
        burn = self._calculate_burn_rate(window_records, now)

        # --- Insight metrics ---
        insights = self._compute_insights(records, cost, daily)

        return {
            "days": days,
            "total_records": len(records),
            "billable_tokens": stats["window_tokens"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "cache_creation_tokens": stats["cache_creation_tokens"],
            "cache_read_tokens": stats["cache_read_tokens"],
            "api_calls": stats["api_calls"],
            "cost": cost,
            "daily": daily,
            "projects": projects,
            "models": stats["models"],
            "insights": insights,
            "rate_limit": {
                "plan": plan,
                "limit": limit,
                "used": window_stats["window_tokens"],
                "pct": (
                    round(window_stats["window_tokens"] / limit * 100, 1)
                    if limit else 0
                ),
                "burn": burn,
            },
        }

    def format_usage_report(self, report: dict) -> str:
        """Format a usage report as beautiful human-readable text.

        Produces a complete terminal-friendly report with:
          - Summary totals with API-equivalent cost
          - Token breakdown by category
          - Daily usage chart with visual bars
          - Per-project attribution with costs
          - Per-model attribution with costs
          - Current rate limit status with progress bar
        """
        if report.get("total_records", 0) == 0:
            msg = report.get("message", "No usage data found.")
            return f"\n  {msg}\n"

        days = report["days"]
        lines = [
            "",
            "\u2550" * 55,
            f"  CLAUDE CODE USAGE REPORT \u2014 Last {days} "
            f"Day{'s' if days != 1 else ''}",
            "\u2550" * 55,
            "",
        ]

        # --- TOTALS ---
        cost = report.get("cost", {})
        plan = report.get("rate_limit", {}).get("plan", "pro")
        plan_monthly = self.PLAN_COSTS.get(plan, 0)

        total_cost = cost.get("total", 0)
        lines.extend([
            "  TOTALS",
            "  " + "\u2500" * 51,
            f"  Billable tokens:  {report['billable_tokens']:>12,}",
            f"  API calls:        {report['api_calls']:>12,}",
            f"  API-equiv cost:     ${total_cost:>10.2f}"
            f"  ({plan} @ ${plan_monthly}/mo)",
        ])

        # Show value multiplier if plan costs something
        if plan_monthly > 0 and total_cost > plan_monthly:
            multiplier = total_cost / plan_monthly
            lines.append(f"  Value:              {multiplier:>10.1f}x"
                         f"  your subscription")

        lines.append("")

        # --- TOKEN BREAKDOWN ---
        total = report["billable_tokens"] or 1
        inp_pct = report["input_tokens"] / total * 100
        out_pct = report["output_tokens"] / total * 100
        cc_pct = report["cache_creation_tokens"] / total * 100

        lines.extend([
            "  TOKEN BREAKDOWN",
            "  " + "\u2500" * 51,
            f"  Input:            {report['input_tokens']:>12,}"
            f"  ({inp_pct:4.0f}%)",
            f"  Output:           {report['output_tokens']:>12,}"
            f"  ({out_pct:4.0f}%)",
            f"  Cache creation:   {report['cache_creation_tokens']:>12,}"
            f"  ({cc_pct:4.0f}%)",
            f"  Cache reads:      {report['cache_read_tokens']:>12,}"
            f"  (not billed)",
            "",
        ])

        # --- DAILY USAGE ---
        daily = report.get("daily", [])
        if daily:
            max_daily = max(d["billable_tokens"] for d in daily) or 1
            lines.extend([
                "  DAILY USAGE",
                "  " + "\u2500" * 51,
            ])
            for d in daily[:14]:  # Show up to 14 days
                bar_len = int(20 * d["billable_tokens"] / max_daily)
                bar = "\u2588" * bar_len + "\u2591" * (20 - bar_len)
                # Parse date for display
                try:
                    date_obj = datetime.strptime(d["date"], "%Y-%m-%d")
                    date_str = date_obj.strftime("%b %d")
                except ValueError:
                    date_str = d["date"][:6]
                lines.append(
                    f"  {date_str}  {bar}  {d['billable_tokens']:>10,}"
                )
            lines.append("")

        # --- BY PROJECT ---
        projects = report.get("projects", {})
        if projects:
            proj_total = sum(
                p["billable_tokens"] for p in projects.values()
            ) or 1
            # Dynamic column width — cap at 24 to keep numbers aligned
            max_name = max(len(n) for n in projects) if projects else 10
            col_w = min(max(max_name + 2, 14), 26)  # 14–26 chars wide
            lines.extend([
                "  BY PROJECT",
                "  " + "\u2500" * 51,
            ])
            for name, data in sorted(
                projects.items(),
                key=lambda x: x[1]["billable_tokens"],
                reverse=True,
            ):
                pct = data["billable_tokens"] / proj_total * 100
                proj_cost = cost.get("by_project", {}).get(name, 0)
                # Truncate long names to fit the column
                display_name = (
                    name[:col_w - 3] + ".." if len(name) > col_w - 1
                    else name
                )
                lines.append(
                    f"  {display_name:<{col_w}} {data['billable_tokens']:>10,}"
                    f"  ({pct:4.0f}%)  ~${proj_cost:.2f}"
                )
            lines.append("")

        # --- BY MODEL (grouped by family) ---
        models = report.get("models", {})
        if models:
            # Group by family (opus/sonnet/haiku) and sum tokens + cost
            family_tokens: dict[str, int] = {}
            family_cost: dict[str, float] = {}
            family_versions: dict[str, list[str]] = {}
            for model, tokens in models.items():
                family = self._get_model_family(model)
                family_tokens[family] = family_tokens.get(family, 0) + tokens
                model_c = cost.get("by_model", {}).get(model, 0)
                family_cost[family] = family_cost.get(family, 0) + model_c
                if family not in family_versions:
                    family_versions[family] = []
                family_versions[family].append(model)

            model_total = sum(family_tokens.values()) or 1
            lines.extend([
                "  BY MODEL",
                "  " + "\u2500" * 51,
            ])
            for family, tokens in sorted(
                family_tokens.items(), key=lambda x: x[1], reverse=True,
            ):
                if tokens == 0:
                    continue
                pct = tokens / model_total * 100
                fc = family_cost.get(family, 0)
                # Display name: "opus_legacy" -> "opus (legacy)"
                display = family.replace("_legacy", " (legacy)")
                label = display
                # Show version count if multiple versions of same family
                versions = family_versions.get(family, [])
                if len(versions) > 1:
                    label = f"{display} ({len(versions)} versions)"
                lines.append(
                    f"  {label:<24} {tokens:>10,}"
                    f"  ({pct:4.0f}%)  ~${fc:.2f}"
                )
            lines.append("")

        # --- INSIGHTS ---
        insights = report.get("insights", {})
        if insights:
            lines.extend([
                "  INSIGHTS",
                "  " + "\u2500" * 51,
            ])

            # Session stats
            n_sessions = insights.get("sessions", 0)
            if n_sessions:
                avg_tok = insights.get("avg_tokens_per_session", 0)
                avg_cost = insights.get("avg_cost_per_session", 0)
                lines.append(
                    f"  Sessions:         {n_sessions:>12,}    "
                    f"avg {avg_tok:,} tok  (~${avg_cost:.2f})"
                )

            # Active days + avg
            active = insights.get("active_days", 0)
            if active:
                avg_day = insights.get("avg_tokens_per_active_day", 0)
                lines.append(
                    f"  Active days:      {active:>12}    "
                    f"avg {avg_day:,} tok/day"
                )

            # Cache hit rate
            cache_rate = insights.get("cache_hit_rate", 0)
            if cache_rate > 0:
                pct_str = f"{cache_rate * 100:.1f}%"
                lines.append(
                    f"  Cache hit rate:   {pct_str:>12}    "
                    f"{'excellent' if cache_rate > 0.9 else 'good' if cache_rate > 0.7 else 'low'}"
                )

            # Busiest day
            busiest = insights.get("busiest_day")
            if busiest:
                try:
                    bd = datetime.strptime(busiest[0], "%Y-%m-%d")
                    date_str = bd.strftime("%b %d")
                except ValueError:
                    date_str = busiest[0]
                lines.append(
                    f"  Busiest day:      {date_str:>12}    "
                    f"{busiest[1]:,} tokens"
                )

            # Peak hours
            peak = insights.get("peak_hours", [])
            if peak:
                top3 = [f"{h}:00" for h, _ in peak[:3]]
                lines.append(
                    f"  Peak hours:         {', '.join(top3):>10}"
                )

            # Git branches (if any)
            branches = insights.get("branches", {})
            if branches and len(branches) > 1:
                lines.append("")
                lines.append("  Top branches:")
                total_b = sum(branches.values()) or 1
                for br, tok in list(branches.items())[:5]:
                    br_pct = tok / total_b * 100
                    lines.append(
                        f"    {br:<20} {tok:>10,}  ({br_pct:4.0f}%)"
                    )

            lines.append("")

        # --- RATE LIMIT STATUS ---
        rl = report.get("rate_limit", {})
        if rl:
            used = rl.get("used", 0)
            limit = rl.get("limit", 150_000)
            pct = rl.get("pct", 0)
            bar_filled = int(20 * min(pct, 100) / 100)
            bar = "\u2588" * bar_filled + "\u2591" * (20 - bar_filled)

            lines.extend([
                "  RATE LIMIT STATUS (5h window)",
                "  " + "\u2500" * 51,
                f"  [{bar}] {pct:.0f}%",
                f"  {used:,} / {limit:,} tokens ({rl.get('plan', 'pro')} plan)",
            ])

            burn = rl.get("burn")
            if burn:
                lines.append(
                    f"  Burn rate: "
                    f"{burn['tokens_per_minute']:,.0f} tokens/min"
                )
                remaining = limit - used
                if remaining > 0 and burn["tokens_per_minute"] > 0:
                    mins = remaining / burn["tokens_per_minute"]
                    if mins < 60:
                        lines.append(f"  ETA to limit: ~{int(mins)} minutes")
                    else:
                        lines.append(
                            f"  ETA to limit: ~{mins / 60:.1f} hours"
                        )
            lines.append("")

        lines.append("\u2550" * 55)
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Billing audit — cross-reference your dashboard charges             #
    # ------------------------------------------------------------------ #

    def get_billing_audit(self, days: int = 14) -> dict:
        """Audit token usage for cross-referencing against billing dashboard.

        Scans JSONL files and computes what published API rates SHOULD
        charge for each token type. Users can compare this against their
        actual billing dashboard charges to verify billing accuracy.

        Returns a structured dict with token totals, published-rate cost,
        per-day breakdown, per-model breakdown, and diagnostic rates.
        """
        records = self._scan_history(days)

        if not records:
            return {
                "days": days,
                "total_records": 0,
                "message": "No usage data found.",
            }

        # Aggregate totals
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0,
            "cache_read_tokens": 0,
        }
        for r in records:
            totals["input_tokens"] += r.get("input_tokens", 0)
            totals["output_tokens"] += r.get("output_tokens", 0)
            totals["cache_creation_tokens"] += r.get("cache_creation_tokens", 0)
            totals["cache_creation_5m_tokens"] += r.get(
                "cache_creation_5m_tokens", 0,
            )
            totals["cache_creation_1h_tokens"] += r.get(
                "cache_creation_1h_tokens", 0,
            )
            totals["cache_read_tokens"] += r.get("cache_read_tokens", 0)

        all_tokens = (
            totals["input_tokens"] + totals["output_tokens"]
            + totals["cache_creation_tokens"] + totals["cache_read_tokens"]
        )

        # Cost at published rates (per-model, per-cache-tier)
        cost_breakdown = {
            "input": 0.0,
            "output": 0.0,
            "cache_creation_5m": 0.0,
            "cache_creation_1h": 0.0,
            "cache_read": 0.0,
        }
        for r in records:
            family = self._get_model_family(r.get("model", ""))
            pricing = self.MODEL_PRICING.get(family, self.MODEL_PRICING["sonnet"])
            cost_breakdown["input"] += (
                r.get("input_tokens", 0) * pricing["input"] / 1_000_000
            )
            cost_breakdown["output"] += (
                r.get("output_tokens", 0) * pricing["output"] / 1_000_000
            )
            # Use per-tier rates when breakdown is available
            cc_5m = r.get("cache_creation_5m_tokens", 0)
            cc_1h = r.get("cache_creation_1h_tokens", 0)
            if cc_5m or cc_1h:
                cost_breakdown["cache_creation_5m"] += (
                    cc_5m * pricing["cache_create_5m"] / 1_000_000
                )
                cost_breakdown["cache_creation_1h"] += (
                    cc_1h * pricing["cache_create_1h"] / 1_000_000
                )
            else:
                # No breakdown — assume all 5m
                cost_breakdown["cache_creation_5m"] += (
                    r.get("cache_creation_tokens", 0)
                    * pricing["cache_create_5m"] / 1_000_000
                )
            cost_breakdown["cache_read"] += (
                r.get("cache_read_tokens", 0)
                * pricing["cache_read"] / 1_000_000
            )

        published_total = sum(cost_breakdown.values())

        # "Flat rate" scenario: all input-type tokens at cache creation rate
        primary_family = "sonnet"
        if records:
            # Use the most common model family
            family_counts: dict[str, int] = {}
            for r in records:
                fam = self._get_model_family(r.get("model", ""))
                family_counts[fam] = family_counts.get(fam, 0) + 1
            primary_family = max(family_counts, key=family_counts.get)

        pricing = self.MODEL_PRICING.get(primary_family, self.MODEL_PRICING["sonnet"])
        flat_rate = pricing["cache_create_5m"]
        all_input_tokens = (
            totals["input_tokens"]
            + totals["cache_creation_tokens"]
            + totals["cache_read_tokens"]
        )
        flat_cost = (
            all_input_tokens * flat_rate / 1_000_000
            + totals["output_tokens"] * pricing["output"] / 1_000_000
        )

        # Per-day breakdown
        daily = self._compute_daily_breakdown(records)

        # Per-model usage
        model_usage: dict[str, dict] = {}
        for r in records:
            model = r.get("model", "unknown")
            if model not in model_usage:
                model_usage[model] = {
                    "requests": 0, "input": 0, "output": 0,
                    "cache_create": 0, "cache_read": 0,
                }
            m = model_usage[model]
            m["requests"] += 1
            m["input"] += r.get("input_tokens", 0)
            m["output"] += r.get("output_tokens", 0)
            m["cache_create"] += r.get("cache_creation_tokens", 0)
            m["cache_read"] += r.get("cache_read_tokens", 0)

        return {
            "days": days,
            "total_records": len(records),
            "totals": totals,
            "all_tokens": all_tokens,
            "cost_at_published_rates": {
                "breakdown": {k: round(v, 2) for k, v in cost_breakdown.items()},
                "total": round(published_total, 2),
            },
            "flat_rate_scenario": {
                "description": (
                    f"All input-type tokens at cache creation rate "
                    f"(${flat_rate}/M)"
                ),
                "total": round(flat_cost, 2),
            },
            "cache_read_pct": round(
                totals["cache_read_tokens"] / all_tokens * 100, 2
            ) if all_tokens else 0,
            "daily": daily,
            "models": model_usage,
            "primary_family": primary_family,
            "pricing_used": pricing,
        }

    def format_billing_audit(self, audit: dict) -> str:
        """Format a billing audit as human-readable text.

        Designed for users to compare against their Anthropic billing
        dashboard to verify that cache read tokens are being billed
        at the correct rate.
        """
        if audit.get("total_records", 0) == 0:
            msg = audit.get("message", "No usage data found.")
            return f"\n  {msg}\n"

        days = audit["days"]
        totals = audit["totals"]
        all_tokens = audit["all_tokens"]
        published = audit["cost_at_published_rates"]
        flat = audit["flat_rate_scenario"]

        lines = [
            "",
            "\u2550" * 60,
            f"  BILLING AUDIT \u2014 Last {days} Days",
            f"  Compare this against your Anthropic billing dashboard",
            "\u2550" * 60,
            "",
        ]

        # Token breakdown
        lines.extend([
            "  TOKEN USAGE",
            "  " + "\u2500" * 56,
        ])
        for label, key in [
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_creation_input_tokens", "cache_creation_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
        ]:
            count = totals[key]
            pct = count / all_tokens * 100 if all_tokens else 0
            lines.append(f"  {label + ':':<38} {count:>12,}  ({pct:5.2f}%)")

        # Show cache creation tier breakdown if available
        cc_5m = totals.get("cache_creation_5m_tokens", 0)
        cc_1h = totals.get("cache_creation_1h_tokens", 0)
        if cc_5m and cc_1h:
            cc_total = totals["cache_creation_tokens"] or 1
            lines.append(
                f"    {'-> 5-min cache writes:':<36} {cc_5m:>12,}"
                f"  ({cc_5m / cc_total * 100:5.1f}%)"
            )
            lines.append(
                f"    {'-> 1-hr cache writes:':<36} {cc_1h:>12,}"
                f"  ({cc_1h / cc_total * 100:5.1f}%)"
            )

        lines.append(f"  {'TOTAL:':<38} {all_tokens:>12,}")
        lines.append("")

        # Cost at published rates
        bd = published["breakdown"]
        family = audit.get("primary_family", "sonnet")
        pricing = audit.get("pricing_used", self.MODEL_PRICING["sonnet"])
        cc_5m_cost = bd.get("cache_creation_5m", 0)
        cc_1h_cost = bd.get("cache_creation_1h", 0)
        cc_total_cost = cc_5m_cost + cc_1h_cost

        lines.extend([
            f"  COST AT PUBLISHED API RATES ({family})",
            "  " + "\u2500" * 56,
            f"  Input          (${pricing['input']:.2f}/M):"
            f"    ${bd['input']:>10.2f}",
            f"  Output         (${pricing['output']:.2f}/M):"
            f"    ${bd['output']:>10.2f}",
        ])

        if cc_5m_cost and cc_1h_cost:
            lines.extend([
                f"  Cache write 5m (${pricing['cache_create_5m']:.2f}/M):"
                f"    ${cc_5m_cost:>10.2f}",
                f"  Cache write 1h (${pricing['cache_create_1h']:.2f}/M):"
                f"    ${cc_1h_cost:>10.2f}",
            ])
        else:
            lines.append(
                f"  Cache creation (${pricing['cache_create_5m']:.2f}/M):"
                f"    ${cc_total_cost:>10.2f}"
            )

        lines.extend([
            f"  Cache read     (${pricing['cache_read']:.2f}/M):"
            f"    ${bd['cache_read']:>10.2f}",
            f"  {'TOTAL:':<38}  ${published['total']:>10.2f}",
            "",
        ])

        # Flat-rate scenario
        lines.extend([
            "  SCENARIO: CACHE READS AT CREATION RATE",
            "  " + "\u2500" * 56,
            f"  {flat['description']}",
            f"  {'Estimated cost:':<38}  ${flat['total']:>10.2f}",
            "",
        ])

        # Comparison
        lines.extend([
            "  HOW TO INTERPRET",
            "  " + "\u2500" * 56,
            f"  Your billing dashboard shows:     $___________",
            f"  Published API rates predict:       ${published['total']:>10.2f}",
            f"  Flat-rate scenario predicts:        ${flat['total']:>10.2f}",
            "",
            f"  If your dashboard is close to ${published['total']:.0f},"
            f" billing is correct.",
            f"  If your dashboard is close to ${flat['total']:.0f},"
            f" cache reads may not be",
            f"  discounted. Cache reads are {audit['cache_read_pct']:.1f}%"
            f" of your tokens.",
            "",
        ])

        # Daily breakdown
        daily = audit.get("daily", [])
        if daily:
            lines.extend([
                "  DAILY BREAKDOWN",
                "  " + "\u2500" * 56,
                f"  {'Date':<12} {'Calls':>6} {'All Tokens':>14}"
                f" {'Cache Reads':>14} {'Cache %':>8}",
            ])
            for d in daily[:14]:
                day_all = (
                    d["input_tokens"] + d["output_tokens"]
                    + d["cache_creation_tokens"] + d["cache_read_tokens"]
                )
                cr_pct = (
                    d["cache_read_tokens"] / day_all * 100 if day_all else 0
                )
                try:
                    date_obj = datetime.strptime(d["date"], "%Y-%m-%d")
                    date_str = date_obj.strftime("%b %d")
                except ValueError:
                    date_str = d["date"][:6]
                lines.append(
                    f"  {date_str:<12} {d['api_calls']:>6}"
                    f" {day_all:>14,} {d['cache_read_tokens']:>14,}"
                    f" {cr_pct:>7.1f}%"
                )
            lines.append("")

        # Models
        model_usage = audit.get("models", {})
        if model_usage:
            lines.extend([
                "  MODELS",
                "  " + "\u2500" * 56,
            ])
            for model, m in sorted(
                model_usage.items(), key=lambda x: x[1]["requests"], reverse=True,
            ):
                lines.append(
                    f"  {model}: {m['requests']:,} requests"
                )
            lines.append("")

        lines.extend([
            "\u2550" * 60,
            "  Generated by attnroute BurnRate plugin",
            "  https://github.com/jeranaias/attnroute",
            "",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Session summary                                                    #
    # ------------------------------------------------------------------ #

    def get_session_summary(self) -> dict:
        """Comprehensive session status for diagnostics."""
        state = self.load_state()
        now = datetime.now(timezone.utc)

        records = self._scan_window(now)
        stats = self._compute_window_stats(records)
        burn = self._calculate_burn_rate(records, now)
        cost = self._estimate_cost(records)

        plan = state.get("plan_type", "pro")
        limit = self.PLAN_LIMITS.get(plan, 150_000)

        summary = {
            "plan_type": plan,
            "window_tokens": stats["window_tokens"],
            "window_limit": limit,
            "window_pct": (
                round(stats["window_tokens"] / limit * 100, 1) if limit else 0
            ),
            "api_calls_in_window": stats["api_calls"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "cache_creation_tokens": stats["cache_creation_tokens"],
            "cache_read_tokens": stats["cache_read_tokens"],
            "primary_model": stats.get("primary_model", "unknown"),
            "models": stats.get("models", {}),
            "projects": stats.get("projects", {}),
            "cost": cost,
            "warnings_issued": state.get("warnings_issued", 0),
        }

        if burn:
            summary["tokens_per_minute"] = round(
                burn["tokens_per_minute"], 1,
            )
            remaining = limit - stats["window_tokens"]
            if remaining > 0 and burn["tokens_per_minute"] > 0:
                summary["minutes_remaining"] = round(
                    remaining / burn["tokens_per_minute"], 1,
                )

        return summary
