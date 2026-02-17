#!/usr/bin/env python3
"""LoopBreaker demo: Loop detection and intervention in a real Claude Code session."""
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

def progress_bar(value, max_val, width=10):
    filled = int(width * value / max_val)
    return "#" * filled + "-" * (width - filled)

# ── Scene 1: Claude Gets Stuck ───────────────────────────────────────────────

header("Turns 1-5 -- Claude gets stuck on auth.py")

print(f"{DIM}>> fix the JWT validation error in auth.py{RESET}")
print()

turns = [
    (1, "Edit auth.py -> pytest -> FAILED (2 errors)",    GREEN, " "),
    (2, "Edit auth.py -> pytest -> FAILED (2 errors)",    GREEN, " "),
    (3, "Edit auth.py -> pytest -> FAILED (2 errors)",    YELLOW, " NOTICE: 3 similar attempts on auth.py"),
    (4, "Edit auth.py -> pytest -> FAILED (2 errors)",    YELLOW, " "),
    (5, "Edit auth.py -> pytest -> FAILED (2 errors)",    RED,    " [WARNING] 5 similar attempts on auth.py"),
]

for turn, action, color, note in turns:
    line = f"  {DIM}Turn {turn}:{RESET} {action}"
    if note.strip():
        line += f"  {color}{note}{RESET}"
    print(line)

# ── Scene 2: LoopBreaker Intervenes ──────────────────────────────────────────

header("Turn 6 -- LoopBreaker injects intervention")

print(f"{DIM}>> keep trying to fix it{RESET}")
print()
print(f"{DIM}# LoopBreaker injects this into Claude's context:{RESET}")
print()
print(f"  {BOLD}{RED}## LoopBreaker Alert{RESET}")
print(f"  {BOLD}{YELLOW}WARNING:{RESET} You've attempted `auth.py` {BOLD}5 times{RESET} with repeated approach.")
print()
print(f"  {BOLD}Your approach is not working. Try something different:{RESET}")
print(f"  1. Re-read the file to verify your understanding")
print(f"  2. Check if you're solving the RIGHT problem")
print(f"  3. Consider a completely different approach")
print(f"  4. If stuck, ask the user for clarification")
print()
print(f"  {BOLD}No git changes detected for 5 turns{RESET}")
print(f"  -- your edits may not be persisting.")
print()
print(f"  {BOLD}Recent errors:{RESET}")
print(f"  - `pytest tests/test_auth.py` failed 5x")

# ── Scene 3: Multi-Metric Severity Score ─────────────────────────────────────

header("Under the hood -- multi-metric severity scoring")

print(f"{DIM}# Three independent signals weighted into a composite score:{RESET}")
print()

# Signature repetition: 5 out of 7 (CRITICAL threshold) = 0.71
sig_score = 5 / 7
sig_bar = progress_bar(sig_score, 1.0)
print(f"  Signature repetition:  {YELLOW}[{sig_bar}]{RESET}  {sig_score:.2f}  {DIM}(weight 0.50){RESET}")

# Consecutive failures: 5 out of 5 max = 1.0
fail_score = 5 / 5
fail_bar = progress_bar(fail_score, 1.0)
print(f"  Consecutive failures:  {RED}[{fail_bar}]{RESET}  {fail_score:.2f}  {DIM}(weight 0.25){RESET}")

# Git zero-progress: 5 out of 4 max = 1.0
git_score = min(5 / 4, 1.0)
git_bar = progress_bar(git_score, 1.0)
print(f"  Git zero-progress:     {RED}[{git_bar}]{RESET}  {git_score:.2f}  {DIM}(weight 0.25){RESET}")

composite = 0.50 * sig_score + 0.25 * fail_score + 0.25 * git_score
print(f"  {'- ' * 26}")
print(f"  {BOLD}Composite score:         {RED}{composite:.2f}{RESET} / 1.00  ->  {BOLD}{YELLOW}WARNING{RESET}")
print()
print(f"  {DIM}Thresholds: NOTICE at 3 attempts | WARNING at 5 | CRITICAL at 7{RESET}")

# ── Scene 4: Recovery ────────────────────────────────────────────────────────

header("Turn 7 -- Claude changes approach, loop clears")

print(f"{DIM}# Claude reads different files instead of editing auth.py again:{RESET}")
print()
print(f"  {DIM}Turn 7:{RESET} Read(auth.py), Read(tests/test_auth.py), Read(conftest.py)")
print(f"  {DIM}        No edits this turn.{RESET}")
print()
print(f"  Stage: {RED}escalated{RESET} -> {YELLOW}cooling{RESET}")
print()
print(f"{DIM}# LoopBreaker injects a softer message:{RESET}")
print()
print(f"  {BOLD}## LoopBreaker Alert{RESET}")
print(f"  {BOLD}Recovering:{RESET} Previous loop on `auth.py` appears to be resolving.")
print(f"  Continue with your new approach.")
print()
print(f"  {DIM}Turn 8:{RESET} Edit(conftest.py) -> pytest -> {GREEN}PASSED{RESET}")
print(f"  Stage: {YELLOW}cooling{RESET} -> {GREEN}cleared{RESET}")
print()
print(f"  {GREEN}Loop resolved. Back to normal.{RESET}")
print()
