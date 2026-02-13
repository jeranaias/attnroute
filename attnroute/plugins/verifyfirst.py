"""
VerifyFirst Plugin - Ensures files are read before being edited.

Tracks files read during the session and injects policy reminders
to prevent editing unread files. This addresses GitHub issue #23833
where Claude implements speculative fixes without verifying root cause.
"""
import fnmatch
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

    # Tools that read/search files (counts as "having seen" the file)
    READ_TOOLS = {"Read", "read", "Grep", "grep", "Glob", "glob"}

    # Tools that modify files
    WRITE_TOOLS = {"Edit", "Write", "edit", "write", "MultiEdit"}

    # Only Edit tools require read-before-write (Write creates new files)
    EDIT_TOOLS = {"Edit", "edit", "MultiEdit"}

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
            "read_patterns": [],
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

        # Build compact policy context (avoid bloating every prompt)
        policy_lines = [
            "",
            "## VerifyFirst Policy",
            "You MUST read a file before editing it.",
        ]

        if files_read:
            count = len(files_read)
            if count <= 5:
                names = ", ".join(f"`{Path(f).name}`" for f in files_read)
                policy_lines.append(f"**Verified ({count}):** {names}")
            else:
                policy_lines.append(f"**Verified:** {count} files read this session.")
            policy_lines.append("For any new file, use Read first.")
        else:
            policy_lines.append("No files read yet. Use Read before Edit.")

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
        read_patterns: set[str] = set(state.get("read_patterns", []))
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
                # If target looks like a glob pattern, store separately
                if "*" in target or "?" in target:
                    read_patterns.add(target_normalized)
                else:
                    files_read.add(target_normalized)

            elif tool in self.WRITE_TOOLS:
                files_written.add(target_normalized)

                # Only Edit tools require read-before-write
                # Write tool is used for new file creation and doesn't require prior read
                if tool in self.EDIT_TOOLS and not self._was_read(
                    target_normalized, files_read, read_patterns
                ):
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
        state["read_patterns"] = list(read_patterns)
        state["files_written"] = list(files_written)
        state["violations"] = violations
        self.save_state(state)

        # Return warning if violations occurred
        if new_violations:
            files = [v["file"] for v in new_violations]
            return f"[VerifyFirst] VIOLATION: Edited without reading first: {', '.join(files)}"

        return None

    def _was_read(self, path: str, files_read: set, read_patterns: set) -> bool:
        """Check if a file was explicitly read or matches a read pattern."""
        if path in files_read:
            return True
        # Check against glob patterns from Grep/Glob
        for pattern in read_patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
            # Also match just the filename against the pattern
            if fnmatch.fnmatch(Path(path).name, Path(pattern).name):
                return True
        return False

    def _normalize_path(self, path: str) -> str:
        """Normalize path for comparison, resolving relative paths."""
        # Try to resolve to absolute path for consistent comparison
        try:
            resolved = str(Path(path).resolve())
        except (OSError, ValueError):
            resolved = path

        # Convert to forward slashes, lowercase on Windows
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

    def get_session_summary(self) -> dict:
        """Get summary of current session status."""
        state = self.load_state()
        return {
            "files_read": len(state.get("files_read", [])),
            "files_written": len(state.get("files_written", [])),
            "violations": len(state.get("violations", [])),
            "session_start": state.get("session_start"),
        }
