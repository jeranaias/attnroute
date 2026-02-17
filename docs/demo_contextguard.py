#!/usr/bin/env python3
"""ContextGuard demo: compaction prediction + recovery + cross-plugin."""
import sys

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
    print(f"{CYAN}{'─' * 50}{RESET}")

# Scene 1: Early session — no risk
header("Early session (turn 15)")

print(f"  Injection size: 5,000 chars")
print(f"  Compaction risk: {GREEN}none{RESET}")
print(f"  {DIM}(no warning injected){RESET}")

# Scene 2: Mid session — medium risk
header("Mid session (turn 45)")

print(f"  Injection size: 12,000 chars {YELLOW}(growing){RESET}")
print(f"  Compaction risk: {YELLOW}medium{RESET}")
print()
print(f"  {BOLD}{YELLOW}## ContextGuard Prediction{RESET}")
print(f"  {BOLD}MEDIUM compaction risk{RESET} (turn 45, injection 12,000 chars)")
print(f"  Consider completing current subtask before starting new work.")

# Scene 3: High risk
header("Long session (turn 65)")

print(f"  Injection size: 18,000 chars {RED}(high){RESET}")
print(f"  Compaction risk: {RED}{BOLD}HIGH{RESET}")
print()
print(f"  {BOLD}{RED}## ContextGuard Prediction{RESET}")
print(f"  {BOLD}HIGH compaction risk{RESET} (turn 65, injection 18,000 chars)")
print(f"  Context window may compact soon. Consider:")
print(f"  - Complete the current subtask before starting new work")
print(f"  - Re-read CLAUDE.md if it reloads after compaction")

# Scene 4: Compaction happens!
header("Compaction detected!")

print(f"  {DIM}Active files dropped from 12 -> 3 in one turn...{RESET}")
print()
print(f"  {BOLD}{RED}## Context Recovery (post-compaction){RESET}")
print(f"  Key files from your working set before compaction:")
print(f"  - `auth.py`")
print(f"  - `session.py`")
print(f"  - `middleware.py`")
print(f"  - `config.py`")
print(f"  - `routes.py`")
print(f"  {DIM}These files were recently active. Re-read any you need.{RESET}")
print(f"  Also re-check CLAUDE.md and project instructions.")

# Scene 5: Cross-plugin integration
header("Cross-plugin: ContextGuard -> VerifyFirst")

print(f"  ContextGuard writes: {MAGENTA}compaction_occurred.flag{RESET}")
print(f"  VerifyFirst reads it and injects:")
print()
print(f"  {BOLD}{YELLOW}Post-compaction notice:{RESET} All previously-verified reads")
print(f"  are {BOLD}{RED}STALE{RESET}. Re-read before editing.")
print()
print(f"  {DIM}Flag consumed (one-shot) -- won't repeat next turn.{RESET}")
print()
