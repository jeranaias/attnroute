#!/usr/bin/env python3
"""LoopBreaker demo: loop detection + git progress + intervention."""
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

# Scene 1: Claude gets stuck
header("Claude gets stuck editing auth.py...")

attempts = [
    ("Turn 1", "Edit auth.py -> pytest fails", "NOTICE"),
    ("Turn 2", "Edit auth.py -> pytest fails", "NOTICE"),
    ("Turn 3", "Edit auth.py -> pytest fails", "detected"),
    ("Turn 4", "Edit auth.py -> pytest fails", "WARNING"),
    ("Turn 5", "Edit auth.py -> pytest fails", "escalated"),
]

for turn, action, severity in attempts:
    color = GREEN if severity == "NOTICE" else YELLOW if severity in ("detected", "WARNING") else RED
    icon = "." if severity == "NOTICE" else "!" if severity == "WARNING" else "!!"
    print(f"  {DIM}{turn}:{RESET} {action}  {color}{icon}{RESET}")

# Scene 2: LoopBreaker intervention
header("LoopBreaker intervenes")

print(f"{BOLD}{RED}## LoopBreaker Alert{RESET}")
print(f"{BOLD}{YELLOW}WARNING:{RESET} You've attempted `auth.py` {BOLD}5 times{RESET} with identical approach.")
print()
print(f"{BOLD}Your approach is not working. Try something different:{RESET}")
print(f"  1. Re-read the file to verify your understanding")
print(f"  2. Check if you're solving the RIGHT problem")
print(f"  3. Consider a completely different approach")
print(f"  4. If stuck, ask the user for clarification")

# Scene 3: Git progress detection
header("Git progress detection")

print(f"  {DIM}git diff --stat HEAD -> same hash for 5 turns{RESET}")
print()
print(f"{BOLD}{RED}No git changes detected for 5 turns{RESET}")
print(f"  -- your edits may not be persisting.")

# Scene 4: Multi-metric severity
header("Multi-metric severity score")

bar_sig = "========.."
bar_fail = "=====....."
bar_git = "=======..."
print(f"  Signature repetition: {YELLOW}{bar_sig}{RESET}  0.42 {DIM}(weight 0.5){RESET}")
print(f"  Consecutive failures: {YELLOW}{bar_fail}{RESET}  0.25 {DIM}(weight 0.25){RESET}")
print(f"  Git zero-progress:    {RED}{bar_git}{RESET}    0.38 {DIM}(weight 0.25){RESET}")
print(f"  {'─' * 42}")
print(f"  {BOLD}Composite score:       {RED}0.54{RESET} / 1.00  ->  {BOLD}{YELLOW}WARNING{RESET}")

# Scene 5: Auto-recovery
header("Auto-recovery after idle turn")
print(f"  {DIM}Turn 6: Claude reads different files (no edits)...{RESET}")
print(f"  Stage: {YELLOW}escalated{RESET} -> {GREEN}cooling{RESET} -> {GREEN}cleared{RESET}")
print(f"  {GREEN}Loop resolved. Back to normal.{RESET}")
print()
