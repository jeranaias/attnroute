#!/usr/bin/env python3
"""
attnroute.advisor — Auto-Evolving CLAUDE.md Suggestions

Detects patterns that suggest CLAUDE.md improvements. Never auto-modifies —
only suggests additions, removals, and optimizations.

Detection rules:
  1. File HOT in >80% of last 20 turns -> suggest adding key content to CLAUDE.md
  2. Same 3+ word phrase in 3+ prompts -> suggest adding as instruction
  3. File with waste>0.9 over 10+ turns -> suggest removing from keywords.json

Storage: learned_state.json["suggestions"]
CLI: attnroute-suggest (list / --apply N / --dismiss N)
"""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    from attnroute.telemetry_lib import (
        LEARNED_STATE_FILE,
        ensure_telemetry_dir,
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
            load_turns,
            windows_utf8_io,
        )
        windows_utf8_io()
    except ImportError:
        LEARNED_STATE_FILE = Path.home() / ".claude" / "telemetry" / "learned_state.json"
        def ensure_telemetry_dir(): LEARNED_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        def load_turns(n=25, project=None): return []


# ============================================================================
# SUGGESTION TYPES
# ============================================================================

SUGGESTION_TYPES = {
    "always_hot": "File is HOT in most turns — consider adding key content to CLAUDE.md",
    "repeated_phrase": "Phrase appears frequently in prompts — consider adding as instruction",
    "high_waste": "File rarely used when injected — consider removing from keywords.json",
}


# ============================================================================
# CLAUDE.MD ADVISOR
# ============================================================================

class ClaudeMdAdvisor:
    """
    Detects patterns that suggest CLAUDE.md improvements.

    Analyzes turn history to find:
      - Files that are always HOT (should be in CLAUDE.md directly)
      - Repeated phrases in prompts (could be standard instructions)
      - High-waste files (should be demoted or removed)
    """

    def __init__(self):
        self.suggestions = self._load_suggestions()

    def _load_suggestions(self) -> list[dict]:
        """Load suggestions from learned_state.json."""
        if LEARNED_STATE_FILE.exists():
            try:
                state = json.loads(LEARNED_STATE_FILE.read_text(encoding="utf-8"))
                return state.get("suggestions", [])
            except Exception:
                pass
        return []

    def _save_suggestions(self):
        """Save suggestions to learned_state.json."""
        ensure_telemetry_dir()
        try:
            if LEARNED_STATE_FILE.exists():
                state = json.loads(LEARNED_STATE_FILE.read_text(encoding="utf-8"))
            else:
                state = {}

            state["suggestions"] = self.suggestions
            LEARNED_STATE_FILE.write_text(
                json.dumps(state, indent=2, default=str),
                encoding="utf-8"
            )
        except Exception:
            pass

    def analyze(self, turns: list[dict] = None) -> list[dict]:
        """
        Analyze turns and generate suggestions.

        Args:
            turns: Turn history to analyze (loads from telemetry if not provided)

        Returns:
            List of new suggestions found
        """
        if turns is None:
            turns = load_turns(n=50)

        if len(turns) < 10:
            return []

        new_suggestions = []

        # Rule 1: Always-HOT files
        hot_suggestions = self._find_always_hot(turns)
        new_suggestions.extend(hot_suggestions)

        # Rule 2: Repeated phrases
        phrase_suggestions = self._find_repeated_phrases(turns)
        new_suggestions.extend(phrase_suggestions)

        # Rule 3: High-waste files
        waste_suggestions = self._find_high_waste(turns)
        new_suggestions.extend(waste_suggestions)

        # Deduplicate against existing suggestions
        existing_ids = {s.get("id") for s in self.suggestions}
        for s in new_suggestions:
            if s["id"] not in existing_ids:
                s["created"] = datetime.now().isoformat()
                s["status"] = "pending"
                self.suggestions.append(s)

        self._save_suggestions()
        return new_suggestions

    def _find_always_hot(self, turns: list[dict]) -> list[dict]:
        """Find files that are HOT in >80% of turns."""
        recent = turns[-20:]  # Last 20 turns
        if len(recent) < 10:
            return []

        hot_counts = Counter()
        for turn in recent:
            # Files in "hot" list or files_injected with high tier
            hot_files = turn.get("hot", [])
            for f in hot_files:
                hot_counts[f] += 1

        suggestions = []
        threshold = len(recent) * 0.8

        for file, count in hot_counts.items():
            if count >= threshold:
                suggestions.append({
                    "id": f"always_hot_{file}",
                    "type": "always_hot",
                    "file": file,
                    "reason": f"HOT in {count}/{len(recent)} turns ({count/len(recent):.0%})",
                    "action": f"Consider moving key content from {file} into CLAUDE.md",
                })

        return suggestions

    def _find_repeated_phrases(self, turns: list[dict]) -> list[dict]:
        """Find phrases that appear frequently in prompts."""
        # Extract 3-5 word phrases from prompts
        phrase_counts = Counter()

        for turn in turns:
            keywords = turn.get("prompt_keywords", [])
            if len(keywords) >= 3:
                # Create overlapping 3-word phrases
                for i in range(len(keywords) - 2):
                    phrase = " ".join(keywords[i:i+3])
                    phrase_counts[phrase] += 1

        suggestions = []
        for phrase, count in phrase_counts.most_common(5):
            if count >= 3:
                suggestions.append({
                    "id": f"repeated_phrase_{hash(phrase) % 10000}",
                    "type": "repeated_phrase",
                    "phrase": phrase,
                    "reason": f"Appeared in {count} prompts",
                    "action": f"Consider adding instruction about '{phrase}' to CLAUDE.md",
                })

        return suggestions

    def _find_high_waste(self, turns: list[dict]) -> list[dict]:
        """Find files with high waste ratio over multiple turns."""
        file_stats = {}  # file -> {injected: n, used: n}

        for turn in turns:
            injected = turn.get("files_injected", [])
            used = turn.get("files_used", [])

            for f in injected:
                if f not in file_stats:
                    file_stats[f] = {"injected": 0, "used": 0}
                file_stats[f]["injected"] += 1

            for f in used:
                if f in file_stats:
                    file_stats[f]["used"] += 1

        suggestions = []
        for file, stats in file_stats.items():
            if stats["injected"] >= 10:  # Enough data
                waste_ratio = 1.0 - (stats["used"] / stats["injected"])
                if waste_ratio >= 0.9:
                    suggestions.append({
                        "id": f"high_waste_{file}",
                        "type": "high_waste",
                        "file": file,
                        "reason": f"Used {stats['used']}/{stats['injected']} times ({1-waste_ratio:.0%} usefulness)",
                        "action": f"Consider removing {file} from keywords.json",
                    })

        return suggestions

    def list_suggestions(self, status: str = None) -> list[dict]:
        """List suggestions, optionally filtered by status."""
        if status:
            return [s for s in self.suggestions if s.get("status") == status]
        return self.suggestions

    def apply_suggestion(self, suggestion_id: str) -> bool:
        """
        Apply a suggestion (mark as applied, perform action if possible).

        Returns True if successful.
        """
        for s in self.suggestions:
            if s.get("id") == suggestion_id:
                if s["type"] == "always_hot":
                    # Append a note to CLAUDE.md
                    return self._append_to_claude_md(s)
                elif s["type"] == "high_waste":
                    # Add to demoted files
                    return self._add_to_demoted(s)
                else:
                    # Just mark as applied
                    s["status"] = "applied"
                    s["applied_at"] = datetime.now().isoformat()
                    self._save_suggestions()
                    return True
        return False

    def dismiss_suggestion(self, suggestion_id: str) -> bool:
        """Dismiss a suggestion (mark as dismissed, won't be suggested again)."""
        for s in self.suggestions:
            if s.get("id") == suggestion_id:
                s["status"] = "dismissed"
                s["dismissed_at"] = datetime.now().isoformat()
                self._save_suggestions()
                return True
        return False

    def _append_to_claude_md(self, suggestion: dict) -> bool:
        """Append suggested content to CLAUDE.md."""
        claude_md = Path.cwd() / "CLAUDE.md"
        if not claude_md.exists():
            claude_md = Path.cwd() / ".claude" / "CLAUDE.md"

        try:
            note = f"\n\n<!-- attnroute suggestion: {suggestion['file']} is frequently accessed -->\n"
            note += f"## {Path(suggestion['file']).stem} (auto-suggested)\n"
            note += "This file is accessed in most sessions. Key content should be added here.\n"

            if claude_md.exists():
                content = claude_md.read_text(encoding="utf-8")
                claude_md.write_text(content + note, encoding="utf-8")
            else:
                claude_md.parent.mkdir(parents=True, exist_ok=True)
                claude_md.write_text(f"# Project CLAUDE.md\n{note}", encoding="utf-8")

            suggestion["status"] = "applied"
            suggestion["applied_at"] = datetime.now().isoformat()
            self._save_suggestions()
            return True

        except Exception as e:
            print(f"Error applying suggestion: {e}", file=sys.stderr)
            return False

    def _add_to_demoted(self, suggestion: dict) -> bool:
        """Add file to demoted_files in router_overrides.json."""
        overrides_file = Path.home() / ".claude" / "telemetry" / "router_overrides.json"

        try:
            if overrides_file.exists():
                data = json.loads(overrides_file.read_text(encoding="utf-8"))
            else:
                data = {}

            demoted = data.get("demoted_files", [])
            file = suggestion.get("file", "")
            if file and file not in demoted:
                demoted.append(file)
                data["demoted_files"] = demoted

                overrides_file.parent.mkdir(parents=True, exist_ok=True)
                overrides_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

            suggestion["status"] = "applied"
            suggestion["applied_at"] = datetime.now().isoformat()
            self._save_suggestions()
            return True

        except Exception as e:
            print(f"Error applying suggestion: {e}", file=sys.stderr)
            return False

    def summary(self) -> str:
        """One-line summary for dashboard."""
        pending = len([s for s in self.suggestions if s.get("status") == "pending"])
        if pending > 0:
            return f"Suggestions: {pending} pending (run attnroute-suggest)"
        return ""


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for managing CLAUDE.md suggestions."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute CLAUDE.md advisor")
    parser.add_argument("command", nargs="?", default="list",
                        choices=["list", "analyze", "apply", "dismiss"],
                        help="Command to run")
    parser.add_argument("--id", "-i", type=str, help="Suggestion ID for apply/dismiss")
    parser.add_argument("--status", "-s", type=str, choices=["pending", "applied", "dismissed"],
                        help="Filter by status")
    args = parser.parse_args()

    advisor = ClaudeMdAdvisor()

    if args.command == "list":
        suggestions = advisor.list_suggestions(args.status)
        print()
        print("CLAUDE.md Suggestions")
        print("=" * 60)

        if not suggestions:
            print("  No suggestions.")
        else:
            for s in suggestions:
                status_icon = {"pending": "○", "applied": "✓", "dismissed": "✗"}.get(s.get("status", "?"), "?")
                print(f"\n  [{status_icon}] {s.get('id', 'unknown')}")
                print(f"      Type: {s.get('type', 'unknown')}")
                print(f"      Reason: {s.get('reason', 'N/A')}")
                print(f"      Action: {s.get('action', 'N/A')}")

        print()

    elif args.command == "analyze":
        print("Analyzing turn history...")
        new_suggestions = advisor.analyze()
        print(f"Found {len(new_suggestions)} new suggestions.")
        if new_suggestions:
            for s in new_suggestions:
                print(f"  - {s.get('id')}: {s.get('reason')}")

    elif args.command == "apply":
        if not args.id:
            print("Error: --id required for apply command")
            sys.exit(1)
        if advisor.apply_suggestion(args.id):
            print(f"Applied suggestion: {args.id}")
        else:
            print(f"Failed to apply suggestion: {args.id}")

    elif args.command == "dismiss":
        if not args.id:
            print("Error: --id required for dismiss command")
            sys.exit(1)
        if advisor.dismiss_suggestion(args.id):
            print(f"Dismissed suggestion: {args.id}")
        else:
            print(f"Failed to dismiss suggestion: {args.id}")


if __name__ == "__main__":
    main()
