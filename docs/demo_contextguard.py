#!/usr/bin/env python3
"""ContextGuard demo: Compaction prediction and recovery in a Claude Code session."""
import sys
if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ANSI codes
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 56}{RESET}")

# ── Scene 1: Early Session -- No Risk ────────────────────────────────────────

header("Turn 15 -- early session, low risk")

print(f"{DIM}>> add input validation to the user endpoints{RESET}")
print()
print(f"{DIM}# ContextGuard monitors injection size and turn count:{RESET}")
print()
print(f"  Turn:           15")
print(f"  Injection size: 5,000 chars")
print(f"  Active files:   8")
print(f"  Compaction risk: {GREEN}none{RESET}")
print()
print(f"  {DIM}(no warning injected -- session is healthy){RESET}")

# ── Scene 2: Mid Session -- Medium Risk ──────────────────────────────────────

header("Turn 45 -- growing injection, medium risk")

print(f"{DIM}>> now let's refactor the middleware layer too{RESET}")
print()
print(f"{DIM}# Injection size has been growing as more files become active:{RESET}")
print()
print(f"  Turn:           45")
print(f"  Injection size: 12,000 chars {YELLOW}(growing){RESET}")
print(f"  Active files:   14")
print(f"  Compaction risk: {YELLOW}medium{RESET}")
print()
print(f"{DIM}# ContextGuard injects a prediction warning:{RESET}")
print()
print(f"  {BOLD}{YELLOW}## ContextGuard Prediction{RESET}")
print(f"  {BOLD}MEDIUM compaction risk{RESET} (turn 45, injection 12,000 chars).")
print(f"  Context window may compact soon. Consider:")
print(f"  - Complete the current subtask before starting new work")
print(f"  - Re-read CLAUDE.md if it reloads after compaction")

# ── Scene 3: Long Session -- High Risk ───────────────────────────────────────

header("Turn 65 -- large injection, high risk")

print(f"{DIM}>> also wire up the WebSocket handlers{RESET}")
print()
print(f"  Turn:           65")
print(f"  Injection size: 18,000 chars {RED}(high){RESET}")
print(f"  Active files:   16")
print(f"  Compaction risk: {RED}{BOLD}HIGH{RESET}")
print()
print(f"{DIM}# ContextGuard injects an urgent warning:{RESET}")
print()
print(f"  {BOLD}{RED}## ContextGuard Prediction{RESET}")
print(f"  {BOLD}HIGH compaction risk{RESET} (turn 65, injection 18,000 chars).")
print(f"  Context window may compact soon. Consider:")
print(f"  - Complete the current subtask before starting new work")
print(f"  - Re-read CLAUDE.md if it reloads after compaction")

# ── Scene 4: Compaction Happens ──────────────────────────────────────────────

header("Turn 67 -- compaction detected!")

print(f"{DIM}# Active files dropped from 16 -> 3 in one turn{RESET}")
print(f"{DIM}# ContextGuard detects the 81% drop and fires recovery:{RESET}")
print()
print(f"  {DIM}[attnroute] ContextGuard: compaction detected (16 -> 3 active files),{RESET}")
print(f"  {DIM}            recovering 12 files (recovery 1/5){RESET}")
print()
print(f"{DIM}# ContextGuard injects a recovery block into Claude's context:{RESET}")
print()
print(f"  {BOLD}{RED}## Context Recovery (post-compaction){RESET}")
print(f"  Key files from your working set before context was compacted:")
print()
print(f"  - `auth.py` (hot)")
print(f"  - `session.py` (hot)")
print(f"  - `middleware.py` (hot)")
print(f"  - `routes.py` (warm)")
print(f"  - `config.py` (warm)")
print(f"  - `models.py` (warm)")
print(f"  - `views.py` (warm)")
print(f"  - `forms.py` (warm)")
print(f"  - `utils.py` (cool)")
print(f"  - `validators.py` (cool)")
print(f"  - `serializers.py` (cool)")
print(f"  - `permissions.py` (cool)")
print()
print(f"  {DIM}These files were recently active. Re-read any you need.{RESET}")
print(f"  {DIM}Also re-check CLAUDE.md and project instructions{RESET}")
print(f"  {DIM}-- they may need re-reading after compaction.{RESET}")

# ── Scene 5: Cross-Plugin -- VerifyFirst Integration ─────────────────────────

header("Cross-plugin: ContextGuard -> VerifyFirst")

print(f"{DIM}# ContextGuard writes a flag file for VerifyFirst:{RESET}")
print()
print(f"  ContextGuard writes: {MAGENTA}~/.claude/plugins/compaction_occurred.flag{RESET}")
print(f"  VerifyFirst reads the flag on next prompt and injects:")
print()
print(f"  {BOLD}## VerifyFirst Policy{RESET}")
print(f"  You MUST read a file before editing it.")
print(f"  {BOLD}{YELLOW}Post-compaction notice:{RESET} All previously-verified reads")
print(f"  are {BOLD}{RED}STALE{RESET}. Re-read before editing.")
print(f"  No files read yet. Use Read before Edit.")
print()
print(f"  {DIM}Flag consumed (one-shot) -- won't repeat next turn.{RESET}")
print()
