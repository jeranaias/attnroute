"""
LoopBreaker Plugin - Detects and breaks repetitive failure loops.

Addresses GitHub issue #21431: Claude gets stuck making "multiple broken
attempts instead of thinking through problems."

Tracks tool call patterns to detect when Claude is repeating the same
failing approach, then injects context forcing a different strategy.
"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin


class LoopBreakerPlugin(AttnroutePlugin):
    """
    Detects repetitive failure patterns and forces strategy changes.

    Features:
    - Tracks recent tool calls and their outcomes
    - Detects 3+ similar consecutive attempts on same file
    - Injects "stop and reconsider" context when loops detected
    - Provides analytics on loops detected/broken
    """

    name = "loopbreaker"
    version = "0.1.0"
    description = "Detects and breaks repetitive failure loops"

    # Configuration
    LOOP_THRESHOLD = 3  # Number of similar attempts before triggering
    HISTORY_SIZE = 20   # Number of recent attempts to track
    SIMILARITY_THRESHOLD = 0.7  # How similar attempts must be to count as loop

    # Tools that indicate active work (not just reading)
    WORK_TOOLS = {"Edit", "Write", "edit", "write", "MultiEdit", "Bash", "bash"}

    def __init__(self):
        super().__init__()
        self._loops_file = self._state_dir / "loopbreaker_events.jsonl"

    def on_session_start(self, session_state: dict) -> str | None:
        """Reset tracking for new session."""
        self.save_state({
            "session_id": session_state.get("session_id", "unknown"),
            "session_start": datetime.now().isoformat(),
            "recent_attempts": [],
            "loops_detected": 0,
            "loops_broken": 0,
            "active_loop": None,
        })
        return "LoopBreaker: Active (repetitive failure detection)"

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
        Inject loop-breaking context if we're in an active loop.
        """
        state = self.load_state()
        active_loop = state.get("active_loop")

        if not active_loop:
            return ""

        # Build intervention context
        file_name = Path(active_loop.get("file", "unknown")).name
        attempt_count = active_loop.get("count", 0)
        pattern = active_loop.get("pattern_desc", "similar approach")

        lines = [
            "",
            "## LoopBreaker Alert",
            f"**WARNING:** You've attempted to modify `{file_name}` {attempt_count} times with {pattern}.",
            "",
            "**STOP and reconsider your approach:**",
            "1. Re-read the file to verify your understanding",
            "2. Check if you're solving the RIGHT problem",
            "3. Consider a completely different approach",
            "4. If stuck, ask the user for clarification",
            "",
            "**Do NOT repeat the same fix.** Try something fundamentally different.",
            ""
        ]

        return "\n".join(lines)

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """
        Analyze tool calls for repetitive patterns.
        """
        state = self.load_state()
        recent_attempts: list[dict] = state.get("recent_attempts", [])

        if not tool_calls:
            # No tool calls - clear any active loop (user is doing something else)
            if state.get("active_loop"):
                state["active_loop"] = None
                state["loops_broken"] = state.get("loops_broken", 0) + 1
                self.save_state(state)
            return None

        # Extract work attempts from this turn
        work_attempts = self._extract_work_attempts(tool_calls)

        if not work_attempts:
            # No work tools used (only reads, etc.) - clear any active loop
            if state.get("active_loop"):
                state["active_loop"] = None
                state["loops_broken"] = state.get("loops_broken", 0) + 1
                self.save_state(state)
            return None

        # Check if current work is on a different file than active loop
        active_loop = state.get("active_loop")
        if active_loop:
            current_files = set(a["file"] for a in work_attempts)
            loop_file = active_loop.get("file")
            if loop_file and loop_file not in current_files:
                # Working on different file - break the loop
                state["active_loop"] = None
                state["loops_broken"] = state.get("loops_broken", 0) + 1
                self._log_loop_event({
                    "type": "loop_broken",
                    "file": loop_file,
                    "reason": "different_file",
                    "timestamp": datetime.now().isoformat(),
                })
                self.save_state(state)
                return None

        # Add new attempts to history
        for attempt in work_attempts:
            recent_attempts.append({
                "timestamp": datetime.now().isoformat(),
                "file": attempt["file"],
                "tool": attempt["tool"],
                "signature": attempt["signature"],
                "content_hash": attempt.get("content_hash", ""),
            })

        # Trim to history size
        recent_attempts = recent_attempts[-self.HISTORY_SIZE:]
        state["recent_attempts"] = recent_attempts

        # Check for loops
        loop_info = self._detect_loop(recent_attempts)

        if loop_info:
            # Loop detected!
            if not state.get("active_loop") or state["active_loop"].get("file") != loop_info["file"]:
                # New loop
                state["loops_detected"] = state.get("loops_detected", 0) + 1
                self._log_loop_event({
                    "type": "loop_detected",
                    "file": loop_info["file"],
                    "count": loop_info["count"],
                    "timestamp": datetime.now().isoformat(),
                })

            state["active_loop"] = loop_info
            self.save_state(state)

            return f"[LoopBreaker] Detected {loop_info['count']} similar attempts on {Path(loop_info['file']).name}"
        else:
            # No loop - clear active loop if we had one
            if state.get("active_loop"):
                state["loops_broken"] = state.get("loops_broken", 0) + 1
                self._log_loop_event({
                    "type": "loop_broken",
                    "file": state["active_loop"].get("file"),
                    "timestamp": datetime.now().isoformat(),
                })
            state["active_loop"] = None
            self.save_state(state)
            return None

    def _extract_work_attempts(self, tool_calls: list[dict]) -> list[dict]:
        """Extract work tool calls with signatures for comparison."""
        attempts = []

        for tc in tool_calls:
            tool = tc.get("tool", "")
            if tool not in self.WORK_TOOLS:
                continue

            target = tc.get("target", "")
            if not target:
                continue

            # Create a signature for this attempt
            signature = self._create_signature(tc)

            attempts.append({
                "file": target,
                "tool": tool,
                "signature": signature,
                "content_hash": self._hash_content(tc.get("content", "")),
            })

        return attempts

    def _create_signature(self, tool_call: dict) -> str:
        """
        Create a signature that captures the 'shape' of an attempt.
        Similar attempts should have similar signatures.
        """
        tool = tool_call.get("tool", "")
        target = tool_call.get("target", "")

        # For Edit/Write: hash the old_string pattern (what we're replacing)
        old_string = tool_call.get("old_string", "")
        if old_string:
            # Extract key identifiers from old_string
            identifiers = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', old_string)
            key_ids = sorted(set(identifiers))[:5]  # Top 5 identifiers
            old_sig = ":".join(key_ids)
        else:
            old_sig = ""

        # For Bash: extract command pattern
        command = tool_call.get("command", "")
        if command:
            # Extract first word (the command) and key flags
            parts = command.split()
            cmd_sig = parts[0] if parts else ""
        else:
            cmd_sig = ""

        # Use full normalized path to avoid false positives for same-named files in different dirs
        normalized_target = target.replace("\\", "/").lower() if sys.platform == "win32" else target.replace("\\", "/")
        return f"{tool}|{normalized_target}|{old_sig}|{cmd_sig}"

    def _hash_content(self, content: str) -> str:
        """Hash content for quick comparison."""
        if not content:
            return ""
        return hashlib.md5(content.encode()).hexdigest()[:8]

    def _detect_loop(self, recent_attempts: list[dict]) -> dict | None:
        """
        Detect if recent attempts form a repetitive loop.
        Returns loop info if detected, None otherwise.
        """
        if len(recent_attempts) < self.LOOP_THRESHOLD:
            return None

        # Group by file
        by_file: dict[str, list[dict]] = {}
        for attempt in recent_attempts:
            file = attempt.get("file", "")
            if file:
                by_file.setdefault(file, []).append(attempt)

        # Check each file for loops
        for file, attempts in by_file.items():
            if len(attempts) < self.LOOP_THRESHOLD:
                continue

            # Check recent attempts for similarity
            recent = attempts[-self.LOOP_THRESHOLD:]
            signatures = [a.get("signature", "") for a in recent]

            # Count similar signatures
            if len(set(signatures)) == 1:
                # All identical - definite loop
                return {
                    "file": file,
                    "count": len(recent),
                    "pattern_desc": "identical approach",
                    "signatures": signatures,
                }

            # Check for partial similarity
            sig_counts: dict[str, int] = {}
            for sig in signatures:
                sig_counts[sig] = sig_counts.get(sig, 0) + 1

            max_count = max(sig_counts.values())
            if max_count >= self.LOOP_THRESHOLD:
                return {
                    "file": file,
                    "count": max_count,
                    "pattern_desc": "similar approach",
                    "signatures": signatures,
                }

        return None

    def _log_loop_event(self, event: dict) -> None:
        """Append loop event to log file."""
        try:
            with open(self._loops_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass

    def get_session_summary(self) -> dict:
        """Get summary of current session status."""
        state = self.load_state()
        return {
            "recent_attempts": len(state.get("recent_attempts", [])),
            "loops_detected": state.get("loops_detected", 0),
            "loops_broken": state.get("loops_broken", 0),
            "active_loop": state.get("active_loop", {}).get("file") if state.get("active_loop") else None,
        }
