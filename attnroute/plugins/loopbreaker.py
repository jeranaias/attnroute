"""
LoopBreaker Plugin - Detects and breaks repetitive failure loops.

Addresses GitHub issue #21431: Claude gets stuck making "multiple broken
attempts instead of thinking through problems."

v2.0 Features:
- Content-aware Edit signatures (hash of old_string + new_string)
- Escalating interventions (NOTICE at 3, WARNING at 5, CRITICAL at 7)
- Multi-file bounce detection (A→B→A→B pattern)
- Error pattern tracking for Bash commands

v3.0 Features:
- Git-based progress detection (zero-change detection via git diff)
- Multi-metric severity scoring (signature + failures + git progress)
- State machine stages (detected → escalated → cooling)
"""
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from attnroute.plugins.base import AttnroutePlugin

# Intervention levels
NOTICE = "NOTICE"
WARNING = "WARNING"
CRITICAL = "CRITICAL"


class LoopBreakerPlugin(AttnroutePlugin):
    """
    Detects repetitive failure patterns and forces strategy changes.

    Features:
    - Content-aware Edit signatures (different edits to same file ≠ loop)
    - Escalating interventions: NOTICE → WARNING → CRITICAL
    - Multi-file bounce detection (ping-pong patterns)
    - Error pattern tracking for repeated Bash failures
    - Cross-session analytics
    """

    name = "loopbreaker"
    version = "3.0.0"
    description = "Detects and breaks repetitive failure loops (multi-metric)"

    # Escalation thresholds
    NOTICE_THRESHOLD = 3    # Gentle reminder
    WARNING_THRESHOLD = 5   # Stronger warning + suggest alternatives
    CRITICAL_THRESHOLD = 7  # Explicit STOP + suggest asking user

    HISTORY_SIZE = 20       # Number of recent attempts to track
    SIMILARITY_THRESHOLD = 0.7  # How similar attempts must be
    BOUNCE_THRESHOLD = 3    # File-level bounces before detection (3 = A→B→A→B→A→B)

    # Tools that indicate active work (not just reading)
    WORK_TOOLS = {"Edit", "Write", "edit", "write", "MultiEdit", "Bash", "bash"}

    # Test/build/lint commands that are legitimate to run repeatedly
    TEST_COMMANDS = {
        "pytest", "test", "jest", "mocha", "vitest", "cargo", "go", "make",
        "npm", "npx", "yarn", "pnpm", "bun", "deno",
        "tox", "gradle", "mvn", "rspec", "rake", "dotnet", "php",
        "./gradlew", "./mvnw",
        "ruff", "black", "prettier", "eslint", "mypy", "pylint", "flake8",
        "mix", "zig", "swift", "javac", "scalac", "kotlinc",
    }

    def __init__(self):
        super().__init__()
        self._loops_file = self._state_dir / "loopbreaker_events.jsonl"

    def on_session_start(self, session_state: dict) -> str | None:
        """Reset tracking for new session."""
        # Preserve cross-session analytics
        old_state = self.load_state()
        analytics = old_state.get("analytics", {
            "total_loops": 0,
            "total_broken": 0,
            "sessions": 0,
        })
        analytics["sessions"] = analytics.get("sessions", 0) + 1

        self.save_state({
            "session_id": session_state.get("session_id", "unknown"),
            "session_start": datetime.now().isoformat(),
            "recent_attempts": [],
            "loops_detected": 0,
            "loops_broken": 0,
            "active_loop": None,
            # v2.0: File-level history for bounce detection
            "file_history": [],
            # v2.0: Error tracking
            "error_patterns": {},
            # v2.0: Cross-session analytics
            "analytics": analytics,
            # v3.0: Git progress detection
            "git_diff_hash": None,
            "zero_progress_turns": 0,
            "turn_number": 0,
        })
        return "LoopBreaker v3: Active (multi-metric loop detection)"

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
        Inject loop-breaking context with escalating severity.
        """
        state = self.load_state()
        active_loop = state.get("active_loop")

        if not active_loop:
            return ""

        file_name = Path(active_loop.get("file", "unknown")).name
        attempt_count = active_loop.get("count", 0)
        pattern = active_loop.get("pattern_desc", "similar approach")
        severity = active_loop.get("severity", NOTICE)
        stage = active_loop.get("stage", "detected")

        lines = ["", "## LoopBreaker Alert"]

        # v3.0: Stage-aware messaging
        if stage == "cooling":
            lines.extend([
                f"**Recovering:** Previous loop on `{file_name}` appears to be resolving.",
                "Continue with your new approach.",
            ])
            lines.append("")
            return "\n".join(lines)

        if severity == CRITICAL:
            lines.extend([
                f"**CRITICAL:** You've attempted `{file_name}` {attempt_count} times with {pattern}.",
                "",
                "**STOP IMMEDIATELY.** Your approach is not working.",
                "1. Do NOT attempt another similar fix",
                "2. Re-read the file completely from scratch",
                "3. Reconsider what the ACTUAL problem is",
                "4. **Ask the user for help** — they may have context you're missing",
                "",
                "If you make the same attempt again, you are wasting tokens.",
            ])
        elif severity == WARNING:
            lines.extend([
                f"**WARNING:** You've attempted `{file_name}` {attempt_count} times with {pattern}.",
                "",
                "**Your approach is not working. Try something different:**",
                "1. Re-read the file to verify your understanding",
                "2. Check if you're solving the RIGHT problem",
                "3. Consider a completely different approach",
                "4. If stuck, ask the user for clarification",
            ])
        else:  # NOTICE
            lines.extend([
                f"**NOTICE:** You've made {attempt_count} similar attempts on `{file_name}`.",
                "",
                "Consider verifying your approach:",
                "1. Re-read the relevant file(s)",
                "2. Make sure you understand the root cause",
            ])

        # v3.0: Git zero-progress warning
        zero_turns = state.get("zero_progress_turns", 0)
        if zero_turns >= 2:
            lines.append("")
            lines.append(
                f"**No git changes detected for {zero_turns} turns** "
                f"— your edits may not be persisting."
            )

        # Add error context if available
        error_patterns = state.get("error_patterns", {})
        if error_patterns:
            top_errors = sorted(error_patterns.items(), key=lambda x: x[1]["count"], reverse=True)[:2]
            if top_errors:
                lines.append("")
                lines.append("**Recent errors:**")
                for cmd, info in top_errors:
                    lines.append(f"- `{cmd}` failed {info['count']}x")

        # Add bounce detection warning
        bounce = self._detect_bounce(state.get("file_history", []))
        if bounce:
            lines.append("")
            names = " ↔ ".join(f"`{Path(f).name}`" for f in bounce)
            lines.append(f"**Bounce detected:** You're ping-ponging between {names}. Step back and plan holistically.")

        lines.append("")
        return "\n".join(lines)

    def on_stop(self, tool_calls: list[dict], session_state: dict) -> str | None:
        """
        Analyze tool calls for repetitive patterns with content awareness.
        """
        state = self.load_state()
        state["turn_number"] = state.get("turn_number", 0) + 1
        recent_attempts: list[dict] = state.get("recent_attempts", [])

        if not tool_calls:
            if state.get("active_loop"):
                idle = state.get("idle_turns", 0) + 1
                state["idle_turns"] = idle
                if idle >= 1:
                    # v3.0: transition to cooling before clearing
                    active = state["active_loop"]
                    if active.get("stage") == "cooling" or idle >= 2:
                        state["active_loop"] = None
                        state["idle_turns"] = 0
                        state["loops_broken"] = state.get("loops_broken", 0) + 1
                        analytics = state.get("analytics", {})
                        analytics["total_broken"] = analytics.get("total_broken", 0) + 1
                        state["analytics"] = analytics
                    else:
                        active["stage"] = "cooling"
                self.save_state(state)
            return None

        # Reset idle counter on any tool call
        state["idle_turns"] = 0

        # Track errors from Bash commands
        self._track_errors(tool_calls, state)

        # Extract work attempts from this turn
        work_attempts = self._extract_work_attempts(tool_calls)

        if not work_attempts:
            self.save_state(state)
            return None

        # Update file-level history for bounce detection
        file_history = state.get("file_history", [])
        for attempt in work_attempts:
            file_history.append(attempt["file"])
        file_history = file_history[-30:]  # Keep last 30
        state["file_history"] = file_history

        # v3.0: Git progress detection
        self._check_git_progress(state, had_work=bool(work_attempts))

        # Check if current work is on a different file than active loop
        active_loop = state.get("active_loop")
        if active_loop:
            current_files = set(a["file"] for a in work_attempts)
            loop_file = active_loop.get("file")
            if loop_file and loop_file not in current_files:
                state["active_loop"] = None
                state["loops_broken"] = state.get("loops_broken", 0) + 1
                analytics = state.get("analytics", {})
                analytics["total_broken"] = analytics.get("total_broken", 0) + 1
                state["analytics"] = analytics
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
            })

        recent_attempts = recent_attempts[-self.HISTORY_SIZE:]
        state["recent_attempts"] = recent_attempts

        # Check for loops
        loop_info = self._detect_loop(recent_attempts)

        if loop_info:
            count = loop_info["count"]
            # Determine severity based on count
            if count >= self.CRITICAL_THRESHOLD:
                severity = CRITICAL
            elif count >= self.WARNING_THRESHOLD:
                severity = WARNING
            else:
                severity = NOTICE

            loop_info["severity"] = severity

            # v3.0: State machine stage
            prev_loop = state.get("active_loop")
            multi_score = self._compute_severity_score(state)
            if prev_loop and prev_loop.get("file") == loop_info["file"]:
                prev_severity = prev_loop.get("severity", NOTICE)
                if severity != prev_severity or multi_score >= 0.7:
                    loop_info["stage"] = "escalated"
                else:
                    prev_stage = prev_loop.get("stage", "detected")
                    # If loop was cooling but work continues, reset to detected
                    loop_info["stage"] = "detected" if prev_stage == "cooling" else prev_stage
            else:
                loop_info["stage"] = "detected"
            loop_info["multi_score"] = round(multi_score, 2)

            if not state.get("active_loop") or state["active_loop"].get("file") != loop_info["file"]:
                state["loops_detected"] = state.get("loops_detected", 0) + 1
                analytics = state.get("analytics", {})
                analytics["total_loops"] = analytics.get("total_loops", 0) + 1
                state["analytics"] = analytics
                self._log_loop_event({
                    "type": "loop_detected",
                    "file": loop_info["file"],
                    "count": count,
                    "severity": severity,
                    "timestamp": datetime.now().isoformat(),
                })

            state["active_loop"] = loop_info
            self.save_state(state)

            severity_tag = f" [{severity}]" if severity != NOTICE else ""
            return f"[LoopBreaker]{severity_tag} Detected {count} similar attempts on {Path(loop_info['file']).name}"
        else:
            if state.get("active_loop"):
                state["loops_broken"] = state.get("loops_broken", 0) + 1
                analytics = state.get("analytics", {})
                analytics["total_broken"] = analytics.get("total_broken", 0) + 1
                state["analytics"] = analytics
                self._log_loop_event({
                    "type": "loop_broken",
                    "file": state["active_loop"].get("file"),
                    "timestamp": datetime.now().isoformat(),
                })
            state["active_loop"] = None
            self.save_state(state)
            return None

    def _extract_work_attempts(self, tool_calls: list[dict]) -> list[dict]:
        """Extract work tool calls with content-aware signatures."""
        attempts = []

        for tc in tool_calls:
            tool = tc.get("tool", "")
            if tool not in self.WORK_TOOLS:
                continue

            target = tc.get("target", "")
            if not target:
                continue

            if tool in {"Bash", "bash"}:
                parts = target.split()
                base_cmd = parts[0] if parts else ""
                if base_cmd in self.TEST_COMMANDS:
                    continue

            signature = self._create_signature(tc)

            if tool in {"Bash", "bash"}:
                parts = target.split()
                file_key = parts[0] if parts else target
            else:
                file_key = target

            attempts.append({
                "file": file_key,
                "tool": tool,
                "signature": signature,
            })

        return attempts

    def _create_signature(self, tool_call: dict) -> str:
        """
        Create a content-aware signature for loop detection.

        For Edit/Write with enriched tool calls (containing input dict),
        includes a hash of old_string + new_string so different edits to
        the same file produce different signatures.
        """
        tool = tool_call.get("tool", "")
        target = tool_call.get("target", "")
        tool_input = tool_call.get("input", {})

        if tool in {"Bash", "bash"}:
            parts = target.split()
            cmd = parts[0] if parts else ""
            identifiers = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', target)
            key_ids = sorted(set(identifiers))
            return f"{tool}|{cmd}|{':'.join(key_ids)}|"

        # For Edit/Write: use normalized file path + content hash
        if sys.platform == "win32":
            normalized = target.replace("\\", "/").lower()
        else:
            normalized = target.replace("\\", "/")

        # Content-aware: hash old_string + new_string if available
        content_hash = ""
        if tool_input:
            old_str = tool_input.get("old_string", "")
            new_str = tool_input.get("new_string", "")
            if old_str or new_str:
                raw = f"{old_str}|||{new_str}"
                content_hash = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:8]

        return f"{tool}|{normalized}|{content_hash}|"

    def _signature_similarity(self, sig1: str, sig2: str) -> float:
        """
        Compute similarity between two signatures using Jaccard similarity.
        """
        parts1 = sig1.split("|")
        parts2 = sig2.split("|")

        if len(parts1) < 4 or len(parts2) < 4:
            return 1.0 if sig1 == sig2 else 0.0

        if parts1[0] != parts2[0]:
            return 0.0

        tool = parts1[0]

        # For Edit/Write: path + content hash comparison
        if tool not in {"Bash", "bash"}:
            if parts1[1] != parts2[1]:
                return 0.0
            # If both have content hashes, compare them
            if parts1[2] and parts2[2]:
                return 1.0 if parts1[2] == parts2[2] else 0.3
            # If neither has content hash (legacy), match on path alone
            if not parts1[2] and not parts2[2]:
                return 1.0
            # Mixed: one has hash, one doesn't — partial match
            return 0.5

        # For Bash: command base must match
        if parts1[1] != parts2[1]:
            return 0.0

        ids1 = set(parts1[2].split(":")) if parts1[2] else set()
        ids2 = set(parts2[2].split(":")) if parts2[2] else set()

        if not ids1 and not ids2:
            return 1.0

        if not ids1 or not ids2:
            return 0.0

        intersection = ids1 & ids2
        union = ids1 | ids2
        return len(intersection) / len(union) if union else 0.0

    def _detect_loop(self, recent_attempts: list[dict]) -> dict | None:
        """
        Detect if recent attempts form a repetitive loop.
        Uses escalating thresholds for severity.
        """
        if len(recent_attempts) < self.NOTICE_THRESHOLD:
            return None

        by_file: dict[str, list[dict]] = {}
        for attempt in recent_attempts:
            file = attempt.get("file", "")
            if file:
                by_file.setdefault(file, []).append(attempt)

        for file, attempts in by_file.items():
            if len(attempts) < self.NOTICE_THRESHOLD:
                continue

            # Check the most recent N attempts
            check_count = min(len(attempts), self.CRITICAL_THRESHOLD + 2)
            recent = attempts[-check_count:]
            signatures = [a.get("signature", "") for a in recent]

            # Exact identical signatures
            if len(set(signatures[-self.NOTICE_THRESHOLD:])) == 1:
                return {
                    "file": file,
                    "count": len(recent),
                    "pattern_desc": "identical approach",
                    "signatures": signatures,
                }

            # Count exact signature matches
            sig_counts: dict[str, int] = {}
            for sig in signatures:
                sig_counts[sig] = sig_counts.get(sig, 0) + 1

            max_count = max(sig_counts.values())
            if max_count >= self.NOTICE_THRESHOLD:
                return {
                    "file": file,
                    "count": max_count,
                    "pattern_desc": "repeated approach",
                    "signatures": signatures,
                }

            # Fuzzy similarity check
            reference_sig = signatures[-1]
            similar_count = 1
            for sig in signatures[:-1]:
                if self._signature_similarity(reference_sig, sig) >= self.SIMILARITY_THRESHOLD:
                    similar_count += 1

            if similar_count >= self.NOTICE_THRESHOLD:
                return {
                    "file": file,
                    "count": similar_count,
                    "pattern_desc": "similar approach (fuzzy match)",
                    "signatures": signatures,
                }

        return None

    def _detect_bounce(self, file_history: list[str]) -> list[str] | None:
        """
        Detect A→B→A→B ping-pong patterns in file history.
        Returns the bouncing file pair, or None.
        """
        if len(file_history) < self.BOUNCE_THRESHOLD * 2:
            return None

        recent = file_history[-self.BOUNCE_THRESHOLD * 2:]

        # Check for alternating pattern between 2 files
        unique = list(set(recent))
        if len(unique) != 2:
            return None

        # Check if the pattern alternates
        alternating = True
        for i in range(1, len(recent)):
            if recent[i] == recent[i - 1]:
                alternating = False
                break

        if alternating:
            return unique

        return None

    def _track_errors(self, tool_calls: list[dict], state: dict) -> None:
        """Track tool failures for loop detection.

        Consecutive identical failures are the strongest loop signal.
        3+ identical failures = definitive loop (escalate to WARNING).
        """
        error_patterns = state.get("error_patterns", {})
        consecutive_failures = state.get("consecutive_failures", [])
        had_success = False

        for tc in tool_calls:
            tool = tc.get("tool", "")
            target = tc.get("target", "")
            tool_input = tc.get("input", {})

            if tool in {"Bash", "bash"}:
                command = tool_input.get("command", target) if tool_input else target
                if not command:
                    continue
                cmd_key = command[:60]

                # Detect failure indicators
                is_failure = False
                if tool_input:
                    exit_code = tool_input.get("exit_code")
                    if exit_code is not None and exit_code != 0:
                        is_failure = True
                    # Also check for common error patterns in output
                    stdout = tool_input.get("stdout", "")
                    stderr = tool_input.get("stderr", "")
                    output = (stdout or "") + (stderr or "")
                    output_lower = output.lower()
                    if any(pat in output_lower for pat in [
                        "error:", "traceback", "exception",
                        "command not found", "permission denied",
                    ]) or re.search(r'\bfailed\b', output_lower):
                        is_failure = True

                if is_failure:
                    consecutive_failures.append(cmd_key)
                else:
                    had_success = True
            elif tool in {"Edit", "edit", "Write", "write", "MultiEdit"}:
                # Edit failures manifest as repeated identical attempts
                # (tracked by main loop detector via signatures)
                had_success = True
                continue
            else:
                continue

            # Track frequency
            if cmd_key in error_patterns:
                error_patterns[cmd_key]["count"] += 1
                error_patterns[cmd_key]["last_seen"] = datetime.now().isoformat()
            else:
                error_patterns[cmd_key] = {
                    "count": 1,
                    "first_seen": datetime.now().isoformat(),
                    "last_seen": datetime.now().isoformat(),
                }

        # Reset consecutive failures on any success
        if had_success:
            consecutive_failures = []

        # Cap to last 10 entries
        consecutive_failures = consecutive_failures[-10:]

        # 3+ identical consecutive failures = definitive loop
        if len(consecutive_failures) >= 3:
            last_3 = consecutive_failures[-3:]
            if len(set(last_3)) == 1:
                failure_count = sum(1 for f in consecutive_failures if f == last_3[0])
                state["active_loop"] = {
                    "file": last_3[0],
                    "count": failure_count,
                    "pattern_desc": "identical consecutive failures",
                    "severity": WARNING if failure_count < 5 else CRITICAL,
                }

        # Cap error patterns to prevent unbounded growth
        if len(error_patterns) > 50:
            sorted_patterns = sorted(
                error_patterns.items(), key=lambda x: x[1]["last_seen"], reverse=True
            )
            error_patterns = dict(sorted_patterns[:30])

        state["error_patterns"] = error_patterns
        state["consecutive_failures"] = consecutive_failures

    def _git_diff_hash(self) -> str | None:
        """Run git diff --stat HEAD and return hash of output.

        Returns None if git is unavailable or not in a git repo.
        Uses the same subprocess pattern as warmup.py.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                capture_output=True, text=True,
                cwd=str(Path.cwd()), timeout=2,
            )
            if result.returncode == 0:
                return hashlib.sha256(
                    result.stdout.encode("utf-8", errors="replace")
                ).hexdigest()[:16]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def _check_git_progress(self, state: dict, had_work: bool) -> None:
        """Check if git diff changed since last turn (zero-progress detection).

        Only runs when work tool calls were made this turn. If the git diff
        hash hasn't changed across multiple work turns, the agent is likely
        spinning without making real progress.
        """
        if not had_work:
            return

        current_hash = self._git_diff_hash()
        if current_hash is None:
            return  # Not in a git repo or git unavailable

        last_hash = state.get("git_diff_hash")
        if last_hash is not None and current_hash == last_hash:
            state["zero_progress_turns"] = state.get("zero_progress_turns", 0) + 1
        else:
            state["zero_progress_turns"] = 0

        state["git_diff_hash"] = current_hash

    def _compute_severity_score(self, state: dict) -> float:
        """Combine 3 independent signals into a 0.0–1.0 composite score.

        Signals:
        - Signature repetition (weight 0.5): primary signal from loop detection
        - Consecutive failures (weight 0.25): Bash error pattern
        - Zero git progress (weight 0.25): no file changes across work turns
        """
        # Signal 1: signature repetition
        loop = state.get("active_loop") or {}
        count = loop.get("count", 0)
        sig_score = min(count / self.CRITICAL_THRESHOLD, 1.0)

        # Signal 2: consecutive failures
        failures = state.get("consecutive_failures", [])
        fail_score = min(len(failures) / 5.0, 1.0)

        # Signal 3: zero git progress
        zero_turns = state.get("zero_progress_turns", 0)
        git_score = min(zero_turns / 4.0, 1.0)

        return 0.5 * sig_score + 0.25 * fail_score + 0.25 * git_score

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
        analytics = state.get("analytics", {})

        summary = {
            "recent_attempts": len(state.get("recent_attempts", [])),
            "loops_detected": state.get("loops_detected", 0),
            "loops_broken": state.get("loops_broken", 0),
            "active_loop": state.get("active_loop", {}).get("file") if state.get("active_loop") else None,
        }

        if state.get("active_loop"):
            summary["active_severity"] = state["active_loop"].get("severity", NOTICE)
            summary["active_stage"] = state["active_loop"].get("stage", "detected")
            summary["multi_score"] = state["active_loop"].get("multi_score", 0)

        # v3.0 metrics
        summary["zero_progress_turns"] = state.get("zero_progress_turns", 0)

        # Bounce detection
        bounce = self._detect_bounce(state.get("file_history", []))
        if bounce:
            summary["bounce_detected"] = bounce

        # Error patterns
        error_patterns = state.get("error_patterns", {})
        repeated_errors = {k: v for k, v in error_patterns.items() if v["count"] >= 3}
        if repeated_errors:
            summary["repeated_errors"] = len(repeated_errors)

        # Cross-session analytics
        summary["total_loops_all_time"] = analytics.get("total_loops", 0)
        summary["total_broken_all_time"] = analytics.get("total_broken", 0)
        summary["sessions"] = analytics.get("sessions", 0)

        return summary
