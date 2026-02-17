"""
VerifyFirst Plugin - Ensures files are read before being edited.

Tracks files read during the session and injects policy reminders
to prevent editing unread files. This addresses GitHub issue #23833
where Claude implements speculative fixes without verifying root cause.

v2.0 Features:
- Read freshness registry with turn/timestamp tracking
- Severity escalation (NOTICE/WARNING/CRITICAL)
- Proactive context injection with file reference extraction
- Compliance analytics with cross-session persistence

v2.1 Features:
- Read registry summary with freshness labels (fresh/aging/STALE)
- Edit velocity tracking with speculative editing alerts
- Cross-plugin ContextGuard compaction flag integration
"""
import fnmatch
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin

# Severity levels for violations
NOTICE = "NOTICE"
WARNING = "WARNING"
CRITICAL = "CRITICAL"


class VerifyFirstPlugin(AttnroutePlugin):
    """
    Enforces read-before-write policy for Claude Code sessions.

    Features:
    - Tracks all files read with turn numbers and timestamps
    - Detects stale reads (file read many turns ago)
    - Escalating severity: NOTICE → WARNING → CRITICAL
    - Proactive file reference extraction from prompts
    - Compliance analytics with cross-session trending
    """

    name = "verifyfirst"
    version = "2.1.0"
    description = "Ensures files are read before being edited (with freshness tracking and velocity alerts)"

    # Tools that read/search files (counts as "having seen" the file)
    READ_TOOLS = {"Read", "read", "Grep", "grep", "Glob", "glob"}

    # Tools that modify files
    WRITE_TOOLS = {"Edit", "Write", "edit", "write", "MultiEdit"}

    # Only Edit tools require read-before-write (Write creates new files)
    EDIT_TOOLS = {"Edit", "edit", "MultiEdit"}

    # Configuration
    STALENESS_THRESHOLD = 10  # Turns before a read is considered stale
    ANALYTICS_WINDOW = 100    # Rolling window for compliance rate
    VELOCITY_WINDOW_SIZE = 5  # Rolling window for edit velocity tracking

    def __init__(self):
        super().__init__()
        self._violations_file = self._state_dir / "verifyfirst_violations.jsonl"

    def on_session_start(self, session_state: dict) -> str | None:
        """Reset tracking for new session."""
        session_id = session_state.get("session_id", "unknown")

        # Load cross-session analytics before resetting
        old_state = self.load_state()
        analytics = old_state.get("analytics", {
            "total_edits": 0,
            "total_violations": 0,
            "sessions": 0,
        })
        analytics["sessions"] = analytics.get("sessions", 0) + 1

        self.save_state({
            "session_id": session_id,
            "session_start": datetime.now().isoformat(),
            "files_read": [],
            "read_patterns": [],
            "files_written": [],
            "violations": [],
            # v2.0: Read freshness registry
            "read_registry": {},
            "turn_number": 0,
            # v2.0: Cross-session analytics
            "analytics": analytics,
            # v2.1: Edit velocity tracking
            "velocity_window": [],
            "speculative_editing_alert": False,
        })

        compliance = self._get_compliance_rate(analytics)
        if compliance is not None:
            return f"VerifyFirst v2: Active (compliance: {compliance:.0%})"
        return "VerifyFirst v2: Active (read-before-write policy)"

    def on_prompt_pre(self, prompt: str, session_state: dict) -> tuple[str, bool]:
        """
        Extract file references from the prompt for proactive checking.
        """
        return prompt, True

    def on_prompt_post(
        self,
        prompt: str,
        context_output: str,
        session_state: dict
    ) -> str:
        """
        Inject VerifyFirst policy into context with proactive file warnings.
        """
        state = self.load_state()
        files_read = state.get("files_read", [])
        read_registry = state.get("read_registry", {})
        turn_number = state.get("turn_number", 0)
        speculative_alert = state.get("speculative_editing_alert", False)

        lines = ["", "## VerifyFirst Policy"]
        lines.append("You MUST read a file before editing it.")

        # v2.1: Cross-plugin compaction flag from ContextGuard
        compaction_flag = self._state_dir / "compaction_occurred.flag"
        if compaction_flag.exists():
            lines.append(
                "**Post-compaction notice:** All previously-verified reads "
                "are STALE. Re-read before editing."
            )
            compaction_flag.unlink(missing_ok=True)

        # v2.1: Velocity alert for speculative editing
        if speculative_alert:
            velocity_window = state.get("velocity_window", [])
            consecutive = 0
            for entry in reversed(velocity_window):
                if entry.get("edits", 0) > 0 and entry.get("reads", 0) == 0:
                    consecutive += 1
                else:
                    break
            lines.append(
                f"**High edit velocity:** {consecutive} consecutive turns with "
                "more edits than reads \u2014 verify root cause before editing further."
            )

        if files_read:
            count = len(files_read)
            # v2.1: Use registry summary when registry is non-empty and <= 8 files
            if read_registry and count <= 8:
                registry_lines = self._format_read_registry_summary(
                    read_registry, turn_number
                )
                lines.append(f"**Verified ({count}):**")
                lines.extend(registry_lines)
            elif count <= 5:
                names = ", ".join(f"`{Path(f).name}`" for f in files_read)
                lines.append(f"**Verified ({count}):** {names}")
            else:
                lines.append(f"**Verified:** {count} files read this session.")

            # Flag stale reads
            stale = self._get_stale_reads(read_registry, turn_number)
            if stale:
                stale_names = ", ".join(f"`{Path(f).name}`" for f in list(stale)[:3])
                lines.append(f"**Stale reads:** {stale_names} (read {self.STALENESS_THRESHOLD}+ turns ago, re-read before editing)")

            lines.append("For any new file, use Read first.")
        else:
            lines.append("No files read yet. Use Read before Edit.")

        # Proactive: extract file references from the user prompt
        referenced = self._extract_file_references(prompt)
        if referenced:
            unread = [f for f in referenced if not self._was_read(
                self._normalize_path(f),
                set(files_read),
                set(state.get("read_patterns", []))
            )]
            if unread:
                unread_names = ", ".join(f"`{f}`" for f in unread[:5])
                lines.append(f"**Referenced but unread:** {unread_names} — read before editing.")

        lines.append("")
        return "\n".join(lines)

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """
        Process tool calls to track reads and detect violations with severity.
        """
        if not tool_calls:
            return None

        state = self.load_state()
        files_read: set[str] = set(state.get("files_read", []))
        read_patterns: set[str] = set(state.get("read_patterns", []))
        files_written: set[str] = set(state.get("files_written", []))
        violations: list[dict] = state.get("violations", [])
        read_registry: dict = state.get("read_registry", {})
        turn_number: int = state.get("turn_number", 0) + 1
        analytics: dict = state.get("analytics", {
            "total_edits": 0,
            "total_violations": 0,
            "sessions": 0,
        })
        velocity_window: list[dict] = state.get("velocity_window", [])

        new_violations = []
        turn_edits = 0
        turn_reads = 0

        for tc in tool_calls:
            tool = tc.get("tool", "")
            target = tc.get("target", "")

            if not target:
                continue

            target_normalized = self._normalize_path(target)

            if tool in self.READ_TOOLS:
                turn_reads += 1
                if "*" in target or "?" in target:
                    read_patterns.add(target_normalized)
                else:
                    files_read.add(target_normalized)
                    # Update freshness registry
                    read_registry[target_normalized] = {
                        "turn": turn_number,
                        "timestamp": time.time(),
                        "method": tool,
                    }

            elif tool in self.WRITE_TOOLS:
                files_written.add(target_normalized)
                turn_edits += 1

                if tool in self.EDIT_TOOLS:
                    analytics["total_edits"] = analytics.get("total_edits", 0) + 1

                    if not self._was_read(target_normalized, files_read, read_patterns):
                        # Determine severity
                        severity = self._classify_severity(
                            target_normalized, read_registry, turn_number
                        )

                        violation = {
                            "timestamp": datetime.now().isoformat(),
                            "tool": tool,
                            "file": target,
                            "severity": severity,
                            "turn": turn_number,
                            "session_id": state.get("session_id", "unknown"),
                        }

                        # Include edit content info if available (rich tool calls)
                        tool_input = tc.get("input", {})
                        if tool_input.get("old_string"):
                            violation["had_old_string"] = True

                        violations.append(violation)
                        new_violations.append(violation)
                        analytics["total_violations"] = analytics.get("total_violations", 0) + 1

                        self._log_violation(violation)

        # v2.1: Update velocity window (rolling 5-turn window)
        velocity_window.append({"edits": turn_edits, "reads": turn_reads})
        if len(velocity_window) > self.VELOCITY_WINDOW_SIZE:
            velocity_window = velocity_window[-self.VELOCITY_WINDOW_SIZE:]

        # v2.1: Check for speculative editing pattern
        speculative_alert = False
        edits_without_reads = sum(
            1 for entry in velocity_window
            if entry.get("edits", 0) > 0 and entry.get("reads", 0) == 0
        )
        total_edits_window = sum(entry.get("edits", 0) for entry in velocity_window)
        total_reads_window = sum(entry.get("reads", 0) for entry in velocity_window)

        if edits_without_reads >= 3:
            speculative_alert = True
        elif total_reads_window > 0 and (total_edits_window / total_reads_window) > 3.0:
            speculative_alert = True

        # Update state
        state["files_read"] = list(files_read)
        state["read_patterns"] = list(read_patterns)
        state["files_written"] = list(files_written)
        state["violations"] = violations
        state["read_registry"] = read_registry
        state["turn_number"] = turn_number
        state["analytics"] = analytics
        state["velocity_window"] = velocity_window
        state["speculative_editing_alert"] = speculative_alert
        self.save_state(state)

        if new_violations:
            # Group by severity for the warning message
            crits = [v for v in new_violations if v["severity"] == CRITICAL]
            warns = [v for v in new_violations if v["severity"] == WARNING]
            notices = [v for v in new_violations if v["severity"] == NOTICE]

            parts = []
            if crits:
                files = [v["file"] for v in crits]
                parts.append(f"CRITICAL — never read: {', '.join(files)}")
            if warns:
                files = [v["file"] for v in warns]
                parts.append(f"WARNING — stale read: {', '.join(files)}")
            if notices:
                files = [v["file"] for v in notices]
                parts.append(f"NOTICE — {', '.join(files)}")

            return f"[VerifyFirst] {'; '.join(parts)}"

        return None

    def _format_read_registry_summary(
        self, read_registry: dict, current_turn: int
    ) -> list[str]:
        """
        Format a per-file freshness summary from the read registry.

        Returns a list of lines like:
            - `file.py` (turn 12, fresh)

        Labels:
            fresh  — read less than half the staleness threshold ago
            aging  — read at or beyond half the threshold
            STALE  — read at or beyond the full threshold
        """
        if not read_registry:
            return []

        half_threshold = self.STALENESS_THRESHOLD / 2

        # Sort by turn, most recent first
        sorted_entries = sorted(
            read_registry.items(),
            key=lambda item: item[1].get("turn", 0),
            reverse=True,
        )

        lines = []
        for path, info in sorted_entries[:8]:
            turn = info.get("turn", 0)
            turns_since = current_turn - turn

            if turns_since >= self.STALENESS_THRESHOLD:
                label = "STALE"
            elif turns_since >= half_threshold:
                label = "aging"
            else:
                label = "fresh"

            filename = Path(path).name
            lines.append(f"- `{filename}` (turn {turn}, {label})")

        return lines

    def _classify_severity(
        self, path: str, read_registry: dict, current_turn: int
    ) -> str:
        """Classify violation severity based on read freshness."""
        reg = read_registry.get(path)
        if reg is None:
            # Never read in this session
            return CRITICAL

        turns_since = current_turn - reg.get("turn", 0)
        if turns_since >= self.STALENESS_THRESHOLD:
            return WARNING

        return NOTICE

    def _get_stale_reads(self, read_registry: dict, current_turn: int) -> list[str]:
        """Get files whose reads are stale (many turns ago)."""
        stale = []
        for path, info in read_registry.items():
            turns_since = current_turn - info.get("turn", 0)
            if turns_since >= self.STALENESS_THRESHOLD:
                stale.append(path)
        return stale

    def _extract_file_references(self, prompt: str) -> list[str]:
        """Extract file paths and module references from user prompt text."""
        refs = []

        # Match explicit file paths (with extensions)
        path_pattern = r'(?:[\w./\\-]+/)?[\w.-]+\.(?:py|js|ts|tsx|jsx|rs|go|java|rb|cpp|c|h|hpp|css|html|md|json|yaml|yml|toml|sql|txt|sh|bat|cfg|ini|xml|env|lock|conf|vue|svelte|swift|kt|scala|zig|ex|exs|erl|lua|r|jl|dart|proto|graphql|prisma|tf|hcl)'
        for match in re.finditer(path_pattern, prompt):
            refs.append(match.group())

        # Match Windows-style paths
        win_pattern = r'[A-Za-z]:\\(?:[\w.-]+\\)*[\w.-]+\.\w+'
        for match in re.finditer(win_pattern, prompt):
            refs.append(match.group())

        return list(set(refs))

    def _was_read(self, path: str, files_read: set, read_patterns: set) -> bool:
        """Check if a file was explicitly read or matches a read pattern."""
        if path in files_read:
            return True
        for pattern in read_patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
            if fnmatch.fnmatch(Path(path).name, Path(pattern).name):
                return True
        return False

    def _normalize_path(self, path: str) -> str:
        """Normalize path for comparison, resolving relative paths."""
        try:
            resolved = str(Path(path).resolve())
        except (OSError, ValueError):
            resolved = path
        normalized = resolved.replace("\\", "/")
        if sys.platform == "win32":
            normalized = normalized.lower()
        return normalized

    def _log_violation(self, violation: dict) -> None:
        """Append violation to log file."""
        try:
            with open(self._violations_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(violation) + "\n")
        except Exception:
            pass

    def _get_compliance_rate(self, analytics: dict) -> float | None:
        """Calculate compliance rate from analytics."""
        total = analytics.get("total_edits", 0)
        violations = analytics.get("total_violations", 0)
        if total < 5:
            return None
        return 1.0 - (violations / total)

    def get_session_summary(self) -> dict:
        """Get summary of current session status."""
        state = self.load_state()
        analytics = state.get("analytics", {})
        compliance = self._get_compliance_rate(analytics)

        summary = {
            "files_read": len(state.get("files_read", [])),
            "files_written": len(state.get("files_written", [])),
            "violations": len(state.get("violations", [])),
            "session_start": state.get("session_start"),
            "turn_number": state.get("turn_number", 0),
        }

        # Violation breakdown by severity
        violations = state.get("violations", [])
        summary["critical_violations"] = len([v for v in violations if v.get("severity") == CRITICAL])
        summary["warning_violations"] = len([v for v in violations if v.get("severity") == WARNING])
        summary["notice_violations"] = len([v for v in violations if v.get("severity") == NOTICE])

        if compliance is not None:
            summary["compliance_rate"] = round(compliance, 3)

        # Analytics
        summary["total_edits_all_time"] = analytics.get("total_edits", 0)
        summary["total_violations_all_time"] = analytics.get("total_violations", 0)
        summary["sessions"] = analytics.get("sessions", 0)

        return summary
