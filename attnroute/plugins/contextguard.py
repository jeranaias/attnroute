"""
ContextGuard Plugin — Post-compaction amnesia prevention.

Addresses the #1 community pain point with Claude Code: when the context
window fills up (~95%), Claude Code compacts the conversation, losing all
injected state including file context. This plugin detects compaction events
by monitoring the active file count across turns, and re-injects a compact
recovery block listing the key files that were active before compaction.

Detection: If the number of active files (score >= 0.25) drops by 50%+ in
a single turn, compaction likely occurred. The plugin then re-injects a
summary of the pre-compaction working set.

Guards against false positives:
- Project switches (session_id change) — skipped
- First 2 turns (insufficient baseline) — skipped
- Natural score decay (requires minimum absolute drop of 3 files)

v1.0 Features:
- Compaction prediction based on injection size trend + turn count
- Recovery analytics with timestamps
- CLAUDE.md recovery hint
- Cross-plugin VerifyFirst integration via flag file
"""

import json
import sys
from datetime import datetime

from attnroute.plugins.base import AttnroutePlugin


class ContextGuardPlugin(AttnroutePlugin):
    """Detects context compaction and re-injects key file state."""

    name = "contextguard"
    version = "1.0.0"
    description = "Post-compaction amnesia prevention with prediction"

    # Files with attention score >= this are considered "active"
    ACTIVE_THRESHOLD = 0.25

    # Max files to track in the snapshot
    MAX_SNAPSHOT_FILES = 15

    # Drop ratio that triggers recovery (50% = active count halved)
    DROP_RATIO = 0.50

    # Minimum absolute drop in active files to trigger (prevents decay false positives)
    MIN_ABSOLUTE_DROP = 3

    # Minimum turns before detection activates (need baseline)
    MIN_TURNS_FOR_DETECTION = 2

    # Max recoveries per session (prevent spam)
    MAX_RECOVERIES = 5

    def on_session_start(self, session_state: dict) -> str | None:
        """Initialize tracking state."""
        self.save_state({
            "session_id": session_state.get("session_id", "unknown"),
            "turn_count": 0,
            "last_active_count": 0,
            "last_snapshot": [],
            "current_snapshot": [],
            "recoveries": 0,
            # v1.0: Injection size tracking for compaction prediction
            "injection_sizes": [],
            # v1.0: Recovery analytics
            "recovery_timestamps": [],
        })
        return "ContextGuard v1: Active (compaction detection + prediction)"

    def on_prompt_post(
        self,
        prompt: str,
        context_output: str,
        session_state: dict,
    ) -> str:
        """Check for compaction and re-inject if needed."""
        state = self.load_state()

        # Guard: skip if session changed (project switch resets scores → false positive)
        current_session = session_state.get("session_id", "unknown")
        if state.get("session_id") != current_session:
            state["session_id"] = current_session
            state["turn_count"] = 0
            state["last_active_count"] = 0
            state["last_snapshot"] = []
            state["current_snapshot"] = []
            state["injection_sizes"] = []
            self.save_state(state)
            return ""

        turn_count = state.get("turn_count", 0) + 1
        state["turn_count"] = turn_count

        # v1.0: Track injection size for compaction prediction
        injection_sizes = state.get("injection_sizes", [])
        injection_sizes.append(len(context_output))
        state["injection_sizes"] = injection_sizes[-10:]

        # Read current attention scores
        active_files = self._get_active_files()
        current_count = len(active_files)
        last_count = state.get("last_active_count", 0)

        # Check for compaction with multiple guards against false positives
        recovery_output = ""
        absolute_drop = last_count - current_count
        if (
            turn_count > self.MIN_TURNS_FOR_DETECTION
            and last_count >= 4
            and absolute_drop >= self.MIN_ABSOLUTE_DROP
            and current_count < last_count * (1 - self.DROP_RATIO)
            and state.get("recoveries", 0) < self.MAX_RECOVERIES
        ):
            # Compaction detected — re-inject previous turn's snapshot
            # current_snapshot holds the most recent pre-compaction data
            snapshot = state.get("current_snapshot", [])
            if snapshot:
                recoveries = state.get("recoveries", 0) + 1
                recovery_output = self._format_recovery(snapshot)
                state["recoveries"] = recoveries

                # v1.0: Record recovery timestamp
                timestamps = state.get("recovery_timestamps", [])
                timestamps.append(datetime.now().isoformat())
                state["recovery_timestamps"] = timestamps[-20:]

                # v1.0: Write flag for VerifyFirst cross-plugin integration
                flag_file = self._state_dir / "compaction_occurred.flag"
                try:
                    flag_file.write_text(
                        datetime.now().isoformat(), encoding="utf-8"
                    )
                except OSError:
                    pass

                print(
                    f"[attnroute] ContextGuard: compaction detected "
                    f"({last_count} -> {current_count} active files), "
                    f"recovering {len(snapshot)} files "
                    f"(recovery {recoveries}/{self.MAX_RECOVERIES})",
                    file=sys.stderr,
                )
                if recoveries >= self.MAX_RECOVERIES:
                    print(
                        f"[attnroute] ContextGuard: max recoveries reached "
                        f"({self.MAX_RECOVERIES}), further compactions will "
                        f"not trigger recovery",
                        file=sys.stderr,
                    )

        # v1.0: Compaction prediction
        prediction_output = ""
        risk_level, risk_reason = self._predict_compaction_risk(state)
        if risk_level in ("medium", "high"):
            prediction_output = (
                f"\n## ContextGuard Prediction\n"
                f"**{risk_level.upper()} compaction risk** ({risk_reason}). "
                f"Context window may compact soon. Consider:\n"
                f"- Complete the current subtask before starting new work\n"
                f"- Re-read CLAUDE.md if it reloads after compaction\n\n"
            )

        # Rotate snapshots: current becomes last, build new current
        state["last_snapshot"] = state.get("current_snapshot", [])
        if active_files:
            state["current_snapshot"] = [
                {"path": f, "score": round(s, 3)}
                for f, s in active_files[:self.MAX_SNAPSHOT_FILES]
            ]
        else:
            state["current_snapshot"] = []
        state["last_active_count"] = current_count
        self.save_state(state)

        return recovery_output + prediction_output

    def _get_active_files(self) -> list[tuple[str, float]]:
        """Read attention state and return active files sorted by score."""
        try:
            from attnroute.telemetry_lib import ATTN_STATE_PROJECT
            attn = json.loads(ATTN_STATE_PROJECT.read_text(encoding="utf-8"))
            scores = attn.get("scores", {})
            active = [
                (f, s) for f, s in scores.items()
                if s >= self.ACTIVE_THRESHOLD
            ]
            active.sort(key=lambda x: x[1], reverse=True)
            return active
        except Exception:
            return []

    def _predict_compaction_risk(self, state: dict) -> tuple[str, str]:
        """Predict compaction risk from turn count and injection size trend.

        Returns (risk_level, reason) where risk_level is
        "none" | "low" | "medium" | "high".
        """
        turn_count = state.get("turn_count", 0)
        injection_sizes = state.get("injection_sizes", [])

        if turn_count < 20 or len(injection_sizes) < 3:
            return "none", ""

        current_size = injection_sizes[-1] if injection_sizes else 0

        # Trend: is injection growing?
        growing = False
        if len(injection_sizes) >= 5:
            mid = len(injection_sizes) // 2
            first_avg = sum(injection_sizes[:mid]) / max(1, mid)
            second_avg = sum(injection_sizes[mid:]) / max(
                1, len(injection_sizes) - mid
            )
            growing = second_avg > first_avg * 1.2  # 20% growth

        # Risk matrix
        if turn_count > 60 and current_size > 15_000:
            return "high", f"turn {turn_count}, injection {current_size:,} chars"
        elif turn_count > 40 and current_size > 10_000:
            return "medium", f"turn {turn_count}, injection {current_size:,} chars"
        elif turn_count > 30 and growing and current_size > 8_000:
            return "low", f"growing injection at turn {turn_count}"

        return "none", ""

    def get_session_summary(self) -> dict:
        """Get summary of current session status."""
        state = self.load_state()
        return {
            "session_id": state.get("session_id", "unknown"),
            "turn_count": state.get("turn_count", 0),
            "last_active_count": state.get("last_active_count", 0),
            "snapshot_files": len(state.get("current_snapshot", [])),
            "recoveries": state.get("recoveries", 0),
            "max_recoveries": self.MAX_RECOVERIES,
            "recoveries_remaining": max(
                0, self.MAX_RECOVERIES - state.get("recoveries", 0)
            ),
            "recovery_timestamps": state.get("recovery_timestamps", []),
            "injection_sizes": state.get("injection_sizes", []),
        }

    @staticmethod
    def _format_recovery(snapshot: list[dict]) -> str:
        """Format a compact recovery block for context injection."""
        lines = [
            "",
            "## Context Recovery (post-compaction)",
            "Key files from your working set before context was compacted:",
            "",
        ]
        for entry in snapshot:
            path = entry.get("path", "")
            score = entry.get("score", 0)
            # Show score as a heat indicator
            heat = "hot" if score >= 0.7 else "warm" if score >= 0.4 else "cool"
            lines.append(f"- `{path}` ({heat})")
        lines.append("")
        lines.append(
            "_These files were recently active. Re-read any you need._"
        )
        lines.append(
            "_Also re-check CLAUDE.md and project instructions "
            "— they may need re-reading after compaction._"
        )
        lines.append("")
        return "\n".join(lines)
