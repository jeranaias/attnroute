#!/usr/bin/env python3
"""
attnroute.integrations — Integration Adapters

Provides compatibility layers for:
- Claude-Mem: Use attnroute's attention routing with Claude-Mem's compression
- Continuous-Claude: Export/import learned state in ledger format
- Generic hooks: Easy integration with other context management tools

Architecture:
  attnroute ←→ Adapter ←→ External Tool
             ↑
        Bidirectional sync of:
        - Attention scores
        - Learned associations
        - Observation storage
"""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Try to import attnroute modules
try:
    from attnroute.telemetry_lib import TELEMETRY_DIR, windows_utf8_io
    windows_utf8_io()
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from telemetry_lib import TELEMETRY_DIR, windows_utf8_io
        windows_utf8_io()
    except ImportError:
        TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"


# ============================================================================
# CLAUDE-MEM ADAPTER
# ============================================================================

@dataclass
class ClaudeMemObservation:
    """Claude-Mem compatible observation format."""
    id: str
    timestamp: str
    observation_type: str
    content: str
    related_files: list[str]
    concepts: list[str]


class ClaudeMemAdapter:
    """
    Adapter for Claude-Mem integration.

    Use cases:
    1. Filter Claude-Mem observations using attnroute's attention scores
    2. Use Claude-Mem's compression with attnroute's routing
    3. Sync learned associations between systems

    Example:
        adapter = ClaudeMemAdapter()
        relevant_obs = adapter.filter_by_attention(observations, current_scores)
    """

    def __init__(self):
        self.hot_threshold = 0.8
        self.warm_threshold = 0.25

    def filter_by_attention(self, observations: list[ClaudeMemObservation],
                           attention_scores: dict[str, float]) -> list[ClaudeMemObservation]:
        """
        Filter Claude-Mem observations based on attnroute attention scores.

        Only returns observations related to HOT or WARM files.
        """
        hot_files = {f for f, score in attention_scores.items()
                    if score >= self.hot_threshold}
        warm_files = {f for f, score in attention_scores.items()
                     if self.warm_threshold <= score < self.hot_threshold}

        active_files = hot_files | warm_files

        filtered = []
        for obs in observations:
            # Check if observation relates to any active file
            for related_file in obs.related_files:
                # Match by stem or full path
                stem = Path(related_file).stem
                if (related_file in active_files or
                    any(stem in f or f.endswith(related_file) for f in active_files)):
                    filtered.append(obs)
                    break

        return filtered

    def convert_from_claude_mem(self, claude_mem_data: dict) -> dict:
        """
        Convert Claude-Mem observation format to attnroute format.
        """
        return {
            "id": claude_mem_data.get("id", ""),
            "session_id": claude_mem_data.get("session_id", "unknown"),
            "timestamp": claude_mem_data.get("timestamp", datetime.now().isoformat()),
            "tool_name": claude_mem_data.get("tool", "unknown"),
            "observation_type": claude_mem_data.get("type", "info"),
            "concepts": claude_mem_data.get("concepts", []),
            "raw_tokens": claude_mem_data.get("raw_tokens", 0),
            "compressed_tokens": claude_mem_data.get("compressed_tokens", 0),
            "semantic_summary": claude_mem_data.get("summary", ""),
            "key_facts": claude_mem_data.get("facts", []),
            "related_files": claude_mem_data.get("files", []),
        }

    def convert_to_claude_mem(self, attnroute_data: dict) -> dict:
        """
        Convert attnroute observation format to Claude-Mem format.
        """
        return {
            "id": attnroute_data.get("id", ""),
            "session_id": attnroute_data.get("session_id", "unknown"),
            "timestamp": attnroute_data.get("timestamp", datetime.now().isoformat()),
            "tool": attnroute_data.get("tool_name", "unknown"),
            "type": attnroute_data.get("observation_type", "info"),
            "concepts": attnroute_data.get("concepts", []),
            "raw_tokens": attnroute_data.get("raw_tokens", 0),
            "compressed_tokens": attnroute_data.get("compressed_tokens", 0),
            "summary": attnroute_data.get("semantic_summary", ""),
            "facts": attnroute_data.get("key_facts", []),
            "files": attnroute_data.get("related_files", []),
        }

    def merge_learned_state(self, attnroute_state: dict,
                           claude_mem_state: dict) -> dict:
        """
        Merge learned state from both systems.

        Combines:
        - Prompt-file affinities
        - Co-activation patterns
        - Concept associations
        """
        merged = dict(attnroute_state)

        # Merge prompt-file affinities
        attn_affinity = attnroute_state.get("prompt_file_affinity", {})
        cm_affinity = claude_mem_state.get("prompt_file_affinity", {})

        for word, files in cm_affinity.items():
            if word not in attn_affinity:
                attn_affinity[word] = {}
            for file, score in files.items():
                existing = attn_affinity[word].get(file, 0)
                attn_affinity[word][file] = max(existing, score)

        merged["prompt_file_affinity"] = attn_affinity

        return merged


# ============================================================================
# CONTINUOUS-CLAUDE ADAPTER
# ============================================================================

@dataclass
class LedgerEntry:
    """Continuous-Claude ledger entry format."""
    timestamp: str
    type: str  # handoff, observation, decision
    content: str
    metadata: dict[str, Any]


class ContinuousClaudeAdapter:
    """
    Adapter for Continuous-Claude integration.

    Use cases:
    1. Export attnroute learned state to ledger format
    2. Import from Continuous-Claude handoffs
    3. Sync session memory across systems

    Example:
        adapter = ContinuousClaudeAdapter()
        ledger = adapter.export_to_ledger(learned_state)
        adapter.save_ledger(ledger, "session_handoff.json")
    """

    def __init__(self):
        self.ledger_version = "1.0"

    def export_to_ledger(self, learned_state: dict) -> list[LedgerEntry]:
        """
        Export attnroute learned state to Continuous-Claude ledger format.
        """
        entries = []

        # Export prompt-file affinities as observations
        affinities = learned_state.get("prompt_file_affinity", {})
        for word, files in affinities.items():
            for file, score in files.items():
                if score > 0.5:  # Only export strong associations
                    entries.append(LedgerEntry(
                        timestamp=datetime.now().isoformat(),
                        type="observation",
                        content=f"Learned association: '{word}' → {file} (score: {score:.2f})",
                        metadata={
                            "source": "attnroute",
                            "word": word,
                            "file": file,
                            "score": score,
                        }
                    ))

        # Export co-activation patterns
        coactivation = learned_state.get("coactivation_learned", {})
        for file, related in coactivation.items():
            for related_file, score in related.items():
                if score > 0.3:
                    entries.append(LedgerEntry(
                        timestamp=datetime.now().isoformat(),
                        type="observation",
                        content=f"Co-activation pattern: {file} ↔ {related_file} (score: {score:.2f})",
                        metadata={
                            "source": "attnroute",
                            "file1": file,
                            "file2": related_file,
                            "score": score,
                        }
                    ))

        # Export discoveries
        discoveries = learned_state.get("discoveries", [])
        for discovery in discoveries:
            entries.append(LedgerEntry(
                timestamp=discovery.get("timestamp", datetime.now().isoformat()),
                type="discovery",
                content=discovery.get("description", ""),
                metadata={
                    "source": "attnroute",
                    "file": discovery.get("file", ""),
                }
            ))

        return entries

    def import_from_ledger(self, entries: list[LedgerEntry]) -> dict:
        """
        Import Continuous-Claude ledger entries to attnroute state format.
        """
        state = {
            "prompt_file_affinity": {},
            "coactivation_learned": {},
            "discoveries": [],
            "imported_from": "continuous-claude",
            "import_timestamp": datetime.now().isoformat(),
        }

        for entry in entries:
            meta = entry.metadata or {}

            if entry.type == "observation" and "word" in meta and "file" in meta:
                word = meta["word"]
                file = meta["file"]
                score = meta.get("score", 0.5)

                if word not in state["prompt_file_affinity"]:
                    state["prompt_file_affinity"][word] = {}
                state["prompt_file_affinity"][word][file] = score

            elif entry.type == "observation" and "file1" in meta and "file2" in meta:
                file1 = meta["file1"]
                file2 = meta["file2"]
                score = meta.get("score", 0.3)

                if file1 not in state["coactivation_learned"]:
                    state["coactivation_learned"][file1] = {}
                state["coactivation_learned"][file1][file2] = score

            elif entry.type == "discovery":
                state["discoveries"].append({
                    "timestamp": entry.timestamp,
                    "description": entry.content,
                    "file": meta.get("file", ""),
                })

        return state

    def save_ledger(self, entries: list[LedgerEntry], filepath: Path):
        """Save ledger entries to JSON file."""
        data = {
            "version": self.ledger_version,
            "generated_at": datetime.now().isoformat(),
            "source": "attnroute",
            "entries": [
                {
                    "timestamp": e.timestamp,
                    "type": e.type,
                    "content": e.content,
                    "metadata": e.metadata,
                }
                for e in entries
            ]
        }

        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_ledger(self, filepath: Path) -> list[LedgerEntry]:
        """Load ledger entries from JSON file."""
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))

        return [
            LedgerEntry(
                timestamp=e.get("timestamp", ""),
                type=e.get("type", ""),
                content=e.get("content", ""),
                metadata=e.get("metadata", {}),
            )
            for e in data.get("entries", [])
        ]

    def create_handoff(self, session_summary: str, key_decisions: list[str],
                       next_steps: list[str]) -> LedgerEntry:
        """
        Create a session handoff entry for Continuous-Claude.
        """
        return LedgerEntry(
            timestamp=datetime.now().isoformat(),
            type="handoff",
            content=session_summary,
            metadata={
                "source": "attnroute",
                "decisions": key_decisions,
                "next_steps": next_steps,
            }
        )


# ============================================================================
# GENERIC HOOK ADAPTER
# ============================================================================

class HookAdapter:
    """
    Generic adapter for integrating attnroute with custom hooks.

    Provides callbacks for key events:
    - on_file_activated: When a file becomes HOT
    - on_file_decayed: When a file drops to COLD
    - on_context_built: When context is generated
    - on_turn_complete: After each turn

    Example:
        adapter = HookAdapter()

        @adapter.on_file_activated
        def notify_activated(file, score):
            print(f"File activated: {file} ({score:.2f})")

        # Use in context router
        adapter.trigger("file_activated", "auth.md", 0.95)
    """

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = {
            "file_activated": [],
            "file_decayed": [],
            "context_built": [],
            "turn_complete": [],
            "error": [],
        }

    def on_file_activated(self, func: Callable):
        """Decorator for file activation hook."""
        self._hooks["file_activated"].append(func)
        return func

    def on_file_decayed(self, func: Callable):
        """Decorator for file decay hook."""
        self._hooks["file_decayed"].append(func)
        return func

    def on_context_built(self, func: Callable):
        """Decorator for context build hook."""
        self._hooks["context_built"].append(func)
        return func

    def on_turn_complete(self, func: Callable):
        """Decorator for turn complete hook."""
        self._hooks["turn_complete"].append(func)
        return func

    def on_error(self, func: Callable):
        """Decorator for error hook."""
        self._hooks["error"].append(func)
        return func

    def trigger(self, event: str, *args, **kwargs):
        """Trigger all registered hooks for an event."""
        for hook in self._hooks.get(event, []):
            try:
                hook(*args, **kwargs)
            except Exception as e:
                # Trigger error hooks
                for error_hook in self._hooks.get("error", []):
                    try:
                        error_hook(event, e)
                    except Exception:
                        pass

    def register(self, event: str, func: Callable):
        """Register a hook function for an event."""
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(func)

    def unregister(self, event: str, func: Callable):
        """Unregister a hook function."""
        if event in self._hooks and func in self._hooks[event]:
            self._hooks[event].remove(func)


# ============================================================================
# INTEGRATION UTILITIES
# ============================================================================

def get_attention_scores() -> dict[str, float]:
    """Get current attention scores from attnroute state."""
    try:
        from attnroute.context_router import get_state_file, load_state
        state_file = get_state_file()
        state = load_state(state_file)
        return state.get("scores", {})
    except Exception:
        return {}


def get_learned_state() -> dict:
    """Get current learned state from attnroute learner."""
    try:
        from attnroute.learner import Learner
        learner = Learner()
        return learner.state
    except Exception:
        return {}


def sync_with_external(external_state: dict, merge_strategy: str = "max") -> dict:
    """
    Sync attnroute state with external system state.

    Merge strategies:
    - "max": Take maximum score from either system
    - "avg": Average scores from both systems
    - "attnroute": Prefer attnroute scores
    - "external": Prefer external scores
    """
    attn_state = get_learned_state()

    if merge_strategy == "max":
        return _merge_max(attn_state, external_state)
    elif merge_strategy == "avg":
        return _merge_avg(attn_state, external_state)
    elif merge_strategy == "attnroute":
        return _merge_prefer(attn_state, external_state)
    elif merge_strategy == "external":
        return _merge_prefer(external_state, attn_state)
    else:
        return attn_state


def _merge_max(state1: dict, state2: dict) -> dict:
    """Merge taking maximum values."""
    merged = dict(state1)

    for key in ["prompt_file_affinity", "coactivation_learned"]:
        s1 = state1.get(key, {})
        s2 = state2.get(key, {})

        merged_key = dict(s1)
        for k, v in s2.items():
            if isinstance(v, dict):
                if k not in merged_key:
                    merged_key[k] = {}
                for inner_k, inner_v in v.items():
                    existing = merged_key[k].get(inner_k, 0)
                    merged_key[k][inner_k] = max(existing, inner_v)
            else:
                existing = merged_key.get(k, 0)
                merged_key[k] = max(existing, v)

        merged[key] = merged_key

    return merged


def _merge_avg(state1: dict, state2: dict) -> dict:
    """Merge taking average values."""
    merged = dict(state1)

    for key in ["prompt_file_affinity", "coactivation_learned"]:
        s1 = state1.get(key, {})
        s2 = state2.get(key, {})

        merged_key = dict(s1)
        for k, v in s2.items():
            if isinstance(v, dict):
                if k not in merged_key:
                    merged_key[k] = {}
                for inner_k, inner_v in v.items():
                    existing = merged_key[k].get(inner_k, 0)
                    merged_key[k][inner_k] = (existing + inner_v) / 2
            else:
                existing = merged_key.get(k, 0)
                merged_key[k] = (existing + v) / 2

        merged[key] = merged_key

    return merged


def _merge_prefer(preferred: dict, fallback: dict) -> dict:
    """Merge preferring one state over another."""
    merged = dict(fallback)
    merged.update(preferred)
    return merged


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for integration utilities."""
    import argparse

    parser = argparse.ArgumentParser(description="attnroute integration utilities")
    subparsers = parser.add_subparsers(dest="command")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export to ledger format")
    export_parser.add_argument("--output", type=Path, required=True, help="Output file")

    # Import command
    import_parser = subparsers.add_parser("import", help="Import from ledger")
    import_parser.add_argument("--input", type=Path, required=True, help="Input file")

    # Status command
    subparsers.add_parser("status", help="Show integration status")

    args = parser.parse_args()

    if args.command == "export":
        adapter = ContinuousClaudeAdapter()
        state = get_learned_state()
        entries = adapter.export_to_ledger(state)
        adapter.save_ledger(entries, args.output)
        print(f"Exported {len(entries)} entries to {args.output}")

    elif args.command == "import":
        adapter = ContinuousClaudeAdapter()
        entries = adapter.load_ledger(args.input)
        state = adapter.import_from_ledger(entries)
        print(f"Imported {len(entries)} entries")
        print(f"  Affinities: {len(state.get('prompt_file_affinity', {}))}")
        print(f"  Co-activations: {len(state.get('coactivation_learned', {}))}")
        print(f"  Discoveries: {len(state.get('discoveries', []))}")

    elif args.command == "status":
        scores = get_attention_scores()
        state = get_learned_state()
        print("Integration Status")
        print("=" * 50)
        print(f"Active files (HOT): {sum(1 for s in scores.values() if s >= 0.8)}")
        print(f"Background files (WARM): {sum(1 for s in scores.values() if 0.25 <= s < 0.8)}")
        print(f"Learned affinities: {len(state.get('prompt_file_affinity', {}))}")
        print(f"Learned co-activations: {len(state.get('coactivation_learned', {}))}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
