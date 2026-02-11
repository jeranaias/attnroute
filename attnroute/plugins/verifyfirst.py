"""
VerifyFirst Plugin - Ensures files are read before being edited.

Tracks files read during the session and injects policy reminders
to prevent editing unread files. This addresses GitHub issue #23833
where Claude implements speculative fixes without verifying root cause.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin


class VerifyFirstPlugin(AttnroutePlugin):
    """
    Enforces read-before-write policy for Claude Code sessions.

    Features:
    - Tracks all files read during the session
    - Injects policy reminder into context
    - Logs violations when edits occur on unread files
    - Provides session summary of verification status
    """

    name = "verifyfirst"
    version = "0.1.0"
    description = "Ensures files are read before being edited"

    # Tools that read files
    READ_TOOLS = {"Read", "read"}

    # Tools that modify files
    WRITE_TOOLS = {"Edit", "Write", "edit", "write", "MultiEdit"}

    # Max files to show in policy reminder
    MAX_DISPLAY_FILES = 30

    def __init__(self):
        super().__init__()
        self._violations_file = self._state_dir / "verifyfirst_violations.jsonl"

    def on_session_start(self, session_state: dict) -> str | None:
        """Reset tracking for new session."""
        session_id = session_state.get("session_id", "unknown")

        # Initialize fresh state for this session
        self.save_state({
            "session_id": session_id,
            "session_start": datetime.now().isoformat(),
            "files_read": [],
            "files_written": [],
            "violations": [],
        })

        return "VerifyFirst: Active (read-before-write policy)"

    def on_prompt_pre(self, prompt: str, session_state: dict) -> tuple[str, bool]:
        """
        Check prompt for explicit file references.

        If user explicitly asks to edit a file, note it for context.
        """
        # Pass through unchanged - we track via tool calls
        return prompt, True

    def on_prompt_post(
        self,
        prompt: str,
        context_output: str,
        session_state: dict
    ) -> str:
        """
        Inject VerifyFirst policy into context.

        This instructs Claude to read files before editing them.
        """
        state = self.load_state()
        files_read = state.get("files_read", [])

        # Build policy context
        policy_lines = [
            "",
            "## VerifyFirst Policy",
            "You MUST read a file before editing it. This ensures you understand the full context.",
            ""
        ]

        if files_read:
            display_files = files_read[:self.MAX_DISPLAY_FILES]
            policy_lines.append("**Files verified (safe to edit):**")
            for f in display_files:
                # Show just filename for brevity
                name = Path(f).name if "/" in f or "\\" in f else f
                policy_lines.append(f"- `{name}`")

            if len(files_read) > self.MAX_DISPLAY_FILES:
                policy_lines.append(f"- ... and {len(files_read) - self.MAX_DISPLAY_FILES} more")

            policy_lines.append("")
            policy_lines.append("**IMPORTANT:** For any file NOT in this list, you MUST use Read first.")
        else:
            policy_lines.append("**No files have been read yet this session.**")
            policy_lines.append("You MUST Read any file before attempting to Edit or Write it.")

        policy_lines.append("")

        return "\n".join(policy_lines)

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """
        Process tool calls to track reads and detect violations.
        """
        if not tool_calls:
            return None

        state = self.load_state()
        files_read: set[str] = set(state.get("files_read", []))
        files_written: set[str] = set(state.get("files_written", []))
        violations: list[dict] = state.get("violations", [])

        new_violations = []

        for tc in tool_calls:
            tool = tc.get("tool", "")
            target = tc.get("target", "")

            if not target:
                continue

            # Normalize path
            target_normalized = self._normalize_path(target)

            if tool in self.READ_TOOLS:
                files_read.add(target_normalized)

            elif tool in self.WRITE_TOOLS:
                files_written.add(target_normalized)

                # Check for violation
                if target_normalized not in files_read:
                    violation = {
                        "timestamp": datetime.now().isoformat(),
                        "tool": tool,
                        "file": target,
                        "session_id": state.get("session_id", "unknown"),
                    }
                    violations.append(violation)
                    new_violations.append(violation)

                    # Log to violations file
                    self._log_violation(violation)

        # Update state
        state["files_read"] = list(files_read)
        state["files_written"] = list(files_written)
        state["violations"] = violations
        self.save_state(state)

        # Return warning if violations occurred
        if new_violations:
            files = [v["file"] for v in new_violations]
            return f"[VerifyFirst] VIOLATION: Edited without reading first: {', '.join(files)}"

        return None

    def _normalize_path(self, path: str) -> str:
        """Normalize path for comparison."""
        # Convert to forward slashes, lowercase on Windows
        normalized = path.replace("\\", "/")
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

    def get_session_summary(self) -> dict:
        """Get summary of current session status."""
        state = self.load_state()
        return {
            "files_read": len(state.get("files_read", [])),
            "files_written": len(state.get("files_written", [])),
            "violations": len(state.get("violations", [])),
            "session_start": state.get("session_start"),
        }
