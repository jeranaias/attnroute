#!/usr/bin/env python3
"""VerifyFirst demo: freshness tracking + violation detection."""
import sys

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 50}{RESET}")

# Scene 1: Normal flow — read then edit
header("Scene 1: Read-before-write (happy path)")

print(f"{DIM}Claude reads auth.py, then edits it...{RESET}")
print()
print(f"{BOLD}## VerifyFirst Policy{RESET}")
print(f"You MUST read a file before editing it.")
print(f"{BOLD}Verified (3):{RESET}")
print(f"  - `auth.py` (turn 3, {GREEN}fresh{RESET})")
print(f"  - `session.py` (turn 2, {GREEN}fresh{RESET})")
print(f"  - `config.py` (turn 1, {YELLOW}aging{RESET})")
print(f"For any new file, use Read first.")

# Scene 2: Violation — edit without reading
header("Scene 2: Edit without reading (violation!)")

print(f"{DIM}Claude tries to edit middleware.py without reading it...{RESET}")
print()
print(f"{BOLD}{RED}[VerifyFirst] CRITICAL — never read: middleware.py{RESET}")

# Scene 3: Stale reads
header("Scene 3: Stale read detection")

print(f"{DIM}10 turns later, early reads are now stale...{RESET}")
print()
print(f"{BOLD}Verified (3):{RESET}")
print(f"  - `auth.py` (turn 12, {GREEN}fresh{RESET})")
print(f"  - `session.py` (turn 5, {YELLOW}aging{RESET})")
print(f"  - `config.py` (turn 1, {RED}{BOLD}STALE{RESET})")
print(f"{BOLD}Stale reads:{RESET} `config.py` (read 10+ turns ago, re-read before editing)")

# Scene 4: Velocity alert
header("Scene 4: Edit velocity alert")

print(f"{DIM}4 turns of edits with zero reads...{RESET}")
print()
print(f"{BOLD}{YELLOW}High edit velocity:{RESET} 4 consecutive turns with")
print(f"more edits than reads — verify root cause before editing further.")
print()
print(f"{DIM}VerifyFirst: keeping Claude honest, one read at a time.{RESET}")
print()
