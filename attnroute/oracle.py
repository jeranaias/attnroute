#!/usr/bin/env python3
"""
attnroute.oracle — Per-Task Cost Oracle

Classifies sessions into task types and predicts cost with confidence intervals.
Uses historical data to estimate token usage and costs for different task types.

Task types: refactor, bug_fix, feature, review, exploration, config
Classification: keyword heuristics on prompts + file types touched

Storage: learned_state.json["task_costs"]
CLI: attnroute-oracle
"""

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from attnroute.telemetry_lib import (
        LEARNED_STATE_FILE,
        ensure_telemetry_dir,
        load_stats_cache,
        load_turns,
        windows_utf8_io,
    )
    windows_utf8_io()
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from telemetry_lib import (
            LEARNED_STATE_FILE,
            ensure_telemetry_dir,
            load_stats_cache,
            load_turns,
            windows_utf8_io,
        )
        windows_utf8_io()
    except ImportError:
        LEARNED_STATE_FILE = Path.home() / ".claude" / "telemetry" / "learned_state.json"
        def ensure_telemetry_dir(): LEARNED_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        def load_turns(n=25, project=None): return []
        def load_stats_cache(): return {}


# ============================================================================
# TASK CLASSIFICATION
# ============================================================================

# Keywords for task type classification
TASK_KEYWORDS = {
    "refactor": ["refactor", "rename", "reorganize", "restructure", "cleanup", "simplify", "extract", "move"],
    "bug_fix": ["fix", "bug", "error", "broken", "crash", "issue", "wrong", "fail", "problem"],
    "feature": ["add", "implement", "create", "new", "feature", "build", "develop"],
    "review": ["review", "check", "look at", "examine", "audit", "analyze"],
    "exploration": ["find", "search", "where", "how does", "what is", "explain", "show", "explore"],
    "config": ["config", "setting", "environment", "setup", "install", "deploy"],
}

# Pricing (approximate, per 1M tokens)
# Claude Opus: $15/M input, $75/M output (blended ~$30/M average)
# Claude Sonnet: $3/M input, $15/M output (blended ~$6/M average)
PRICE_PER_M_TOKENS = 6.0  # Assuming Sonnet, blended rate


# ============================================================================
# COST ORACLE
# ============================================================================

class CostOracle:
    """
    Predicts session costs based on task type classification.

    Learns from historical session data to estimate:
      - Token usage per task type
      - Cost distribution (mean, std, percentiles)
    """

    def __init__(self):
        self.task_costs = self._load_costs()

    def _load_costs(self) -> dict:
        """Load task costs from learned_state.json (TOCTOU-safe)."""
        try:
            state = json.loads(LEARNED_STATE_FILE.read_text(encoding="utf-8"))
            # Type validation: ensure we get dicts
            if not isinstance(state, dict):
                return {}
            costs = state.get("task_costs", {})
            return costs if isinstance(costs, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_costs(self):
        """Save task costs to learned_state.json (TOCTOU-safe)."""
        ensure_telemetry_dir()
        try:
            # Use try/except instead of exists() to avoid race condition
            try:
                state = json.loads(LEARNED_STATE_FILE.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                state = {}

            state["task_costs"] = self.task_costs

            # Use atomic write if available
            try:
                from attnroute.compat import safe_atomic_write
                content = json.dumps(state, indent=2, default=str)
                if not safe_atomic_write(LEARNED_STATE_FILE, content):
                    # Fallback with flush
                    with open(LEARNED_STATE_FILE, "w", encoding="utf-8") as f:
                        f.write(content)
                        f.flush()
            except ImportError:
                # Fallback with flush as best-effort
                with open(LEARNED_STATE_FILE, "w", encoding="utf-8") as f:
                    f.write(json.dumps(state, indent=2, default=str))
                    f.flush()
        except Exception:
            pass

    def classify_task(self, prompts: list[str], files_touched: list[str] = None) -> str:
        """
        Classify task type from prompts and file patterns.

        Returns: task type string
        """
        prompt_text = " ".join(prompts).lower()
        files_touched = files_touched or []

        # Score each task type
        scores = defaultdict(int)

        # Use word boundary matching to avoid substring false positives
        import re
        for task_type, keywords in TASK_KEYWORDS.items():
            for kw in keywords:
                # Match whole word to avoid "fix" matching "suffix"
                if re.search(r'\b' + re.escape(kw) + r'\b', prompt_text):
                    scores[task_type] += 1

        # File-based heuristics
        file_extensions = [Path(f).suffix for f in files_touched]
        if ".json" in file_extensions or ".yaml" in file_extensions or ".yml" in file_extensions:
            scores["config"] += 2
        # Check for test files properly (avoid substring false positives)
        if any(".test." in Path(f).name or "_test." in Path(f).name or f.endswith("_test.py") for f in files_touched):
            scores["review"] += 1

        # Return highest scoring type, or "exploration" as default
        if not scores:
            return "exploration"

        return max(scores.items(), key=lambda x: x[1])[0]

    def record_session(self, task_type: str, token_count: int, cost: float = None):
        """
        Record a completed session's cost data.

        Args:
            task_type: Classified task type
            token_count: Total tokens used
            cost: Actual cost in dollars (estimated if not provided)
        """
        if task_type not in self.task_costs:
            self.task_costs[task_type] = {
                "samples": [],
                "token_samples": [],
                "last_updated": None
            }

        # Calculate cost if not provided
        if cost is None:
            cost = (token_count / 1_000_000) * PRICE_PER_M_TOKENS

        # Add samples (keep last 50)
        self.task_costs[task_type]["samples"].append(round(cost, 2))
        self.task_costs[task_type]["token_samples"].append(token_count)
        self.task_costs[task_type]["samples"] = self.task_costs[task_type]["samples"][-50:]
        self.task_costs[task_type]["token_samples"] = self.task_costs[task_type]["token_samples"][-50:]
        self.task_costs[task_type]["last_updated"] = datetime.now().isoformat()

        self._save_costs()

    def predict(self, task_type: str):
        """
        Predict cost for a task type.

        Returns:
            {
                "mean": float,
                "std": float,
                "p25": float,
                "p50": float,
                "p75": float,
                "p95": float,
                "samples": int,
                "confidence": str  # "low", "medium", "high"
            }
            or None if insufficient data
        """
        if task_type not in self.task_costs:
            return None

        samples = self.task_costs[task_type].get("samples", [])
        if len(samples) < 3:
            return None

        sorted_samples = sorted(samples)
        n = len(samples)

        mean = sum(samples) / n
        variance = sum((x - mean) ** 2 for x in samples) / n
        std = math.sqrt(variance)

        # Percentiles (with bounds checking)
        def percentile(data, p):
            if not data:
                return 0.0
            k = (len(data) - 1) * p / 100
            f = int(math.floor(k))
            c = int(math.ceil(k))
            # Ensure indices are within bounds
            f = max(0, min(f, len(data) - 1))
            c = max(0, min(c, len(data) - 1))
            if f == c:
                return data[f]
            return data[f] * (c - k) + data[c] * (k - f)

        # Confidence based on sample size
        if n >= 20:
            confidence = "high"
        elif n >= 10:
            confidence = "medium"
        else:
            confidence = "low"

        return {
            "mean": round(mean, 2),
            "std": round(std, 2),
            "p25": round(percentile(sorted_samples, 25), 2),
            "p50": round(percentile(sorted_samples, 50), 2),
            "p75": round(percentile(sorted_samples, 75), 2),
            "p95": round(percentile(sorted_samples, 95), 2),
            "samples": n,
            "confidence": confidence,
        }

    def predict_range(self, task_type: str):
        """
        Get predicted cost range (P25-P75) for a task type.

        Returns: (low, high) tuple or None
        """
        pred = self.predict(task_type)
        if not pred:
            return None
        return (pred["p25"], pred["p75"])

    def learn_from_stats(self, stats_cache: dict = None):
        """
        Learn from stats-cache.json token usage data.

        Correlates daily token usage with session patterns to build cost models.
        """
        if stats_cache is None:
            stats_cache = load_stats_cache()

        if not stats_cache:
            return

        daily_tokens = stats_cache.get("dailyModelTokens", [])
        if not daily_tokens:
            return

        # Sum tokens by day and estimate costs
        for day_data in daily_tokens[-30:]:  # Last 30 days
            tokens_by_model = day_data.get("tokensByModel", {})
            total_tokens = sum(tokens_by_model.values())

            if total_tokens > 0:
                # Estimate cost
                cost = (total_tokens / 1_000_000) * PRICE_PER_M_TOKENS

                # Classify based on average session size
                # (rough heuristic - small sessions = exploration, large = feature)
                if total_tokens < 50000:
                    task_type = "exploration"
                elif total_tokens < 200000:
                    task_type = "bug_fix"
                else:
                    task_type = "feature"

                # Don't record if we already have data for this day
                date = day_data.get("date", "")
                if date and task_type in self.task_costs:
                    last_updated = self.task_costs[task_type].get("last_updated", "")
                    if date in last_updated:
                        continue

                self.record_session(task_type, total_tokens, cost)

    def summary(self) -> str:
        """One-line summary for dashboard."""
        if not self.task_costs:
            return ""

        # Find most common task type
        task_samples = [(t, len(d.get("samples", []))) for t, d in self.task_costs.items()]
        if not task_samples:
            return ""

        top_task = max(task_samples, key=lambda x: x[1])[0]
        pred = self.predict(top_task)

        if pred:
            return f"Projected cost: ${pred['p25']:.2f}-${pred['p75']:.2f} ({top_task} task, {pred['samples']} samples)"
        return ""

    def get_all_predictions(self) -> dict:
        """Get predictions for all task types."""
        return {
            task_type: self.predict(task_type)
            for task_type in self.task_costs
            if self.predict(task_type)
        }


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for cost oracle."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute cost oracle")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "predict", "learn", "record"],
                        help="Command to run")
    parser.add_argument("--task", "-t", type=str, help="Task type for prediction")
    parser.add_argument("--tokens", type=int, help="Token count for recording")
    parser.add_argument("--cost", type=float, help="Actual cost for recording")
    args = parser.parse_args()

    oracle = CostOracle()

    if args.command == "status":
        print()
        print("Cost Oracle Status")
        print("=" * 60)

        predictions = oracle.get_all_predictions()
        if not predictions:
            print("  No data yet. Run 'attnroute-oracle learn' to build models.")
        else:
            for task_type, pred in sorted(predictions.items()):
                print(f"\n  {task_type.upper()} ({pred['samples']} samples, {pred['confidence']} confidence)")
                print(f"    Range: ${pred['p25']:.2f} - ${pred['p75']:.2f}")
                print(f"    Mean: ${pred['mean']:.2f} (±${pred['std']:.2f})")
                print(f"    P95: ${pred['p95']:.2f}")

        print()

    elif args.command == "predict":
        if not args.task:
            print("Error: --task required for predict command")
            print("Available types: refactor, bug_fix, feature, review, exploration, config")
            sys.exit(1)

        pred = oracle.predict(args.task)
        if pred:
            print()
            print(f"Prediction for: {args.task}")
            print("=" * 40)
            print(f"  Cost range: ${pred['p25']:.2f} - ${pred['p75']:.2f}")
            print(f"  Mean: ${pred['mean']:.2f} (±${pred['std']:.2f})")
            print(f"  P95 (worst case): ${pred['p95']:.2f}")
            print(f"  Confidence: {pred['confidence']} ({pred['samples']} samples)")
            print()
        else:
            print(f"Insufficient data for task type: {args.task}")

    elif args.command == "learn":
        print("Learning from stats-cache.json...")
        oracle.learn_from_stats()
        predictions = oracle.get_all_predictions()
        print(f"Built models for {len(predictions)} task types:")
        for task_type in predictions:
            print(f"  - {task_type}")

    elif args.command == "record":
        if not args.task or not args.tokens:
            print("Error: --task and --tokens required for record command")
            sys.exit(1)

        oracle.record_session(args.task, args.tokens, args.cost)
        print(f"Recorded: {args.task} with {args.tokens} tokens")


if __name__ == "__main__":
    main()
