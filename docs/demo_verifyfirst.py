#!/usr/bin/env python3
"""VerifyFirst demo: Read-before-write policy in a real Claude Code session."""
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

# ── Scene 1: Turn 3 -- Happy Path ────────────────────────────────────────────

header("Turn 3 -- Claude reads files, then edits")

print(f"{DIM}>> fix the session expiry logic in auth.py{RESET}")
print()
print(f"  {DIM}Claude calls: Read(auth.py), Read(session.py), Edit(auth.py){RESET}")
print()
print(f"{DIM}# VerifyFirst injects this into Claude's context:{RESET}")
print()
print(f"  {BOLD}## VerifyFirst Policy{RESET}")
print(f"  You MUST read a file before editing it.")
print(f"  {BOLD}Verified (3):{RESET}")
print(f"  - `auth.py` (turn 3, {GREEN}fresh{RESET})")
print(f"  - `session.py` (turn 2, {GREEN}fresh{RESET})")
print(f"  - `config.py` (turn 1, {YELLOW}aging{RESET})")
print(f"  For any new file, use Read first.")
print()
print(f"  {GREEN}No violations. Clean session.{RESET}")

# ── Scene 2: Turn 7 -- Violation ─────────────────────────────────────────────

header("Turn 7 -- Claude edits a file it never read")

print(f"{DIM}>> also update the rate limiting in middleware.py{RESET}")
print()
print(f"  {DIM}Claude calls: Edit(middleware.py)  -- without reading it first!{RESET}")
print()
print(f"{DIM}# on_stop fires after Claude's tool calls:{RESET}")
print()
print(f"  {BOLD}{RED}[VerifyFirst] CRITICAL -- never read: middleware.py{RESET}")
print()
print(f"{DIM}# Next turn, Claude's context now includes:{RESET}")
print()
print(f"  {BOLD}## VerifyFirst Policy{RESET}")
print(f"  You MUST read a file before editing it.")
print(f"  {BOLD}Verified (3):{RESET}")
print(f"  - `auth.py` (turn 3, {GREEN}fresh{RESET})")
print(f"  - `session.py` (turn 2, {GREEN}fresh{RESET})")
print(f"  - `config.py` (turn 1, {YELLOW}aging{RESET})")
print(f"  {BOLD}Referenced but unread:{RESET} `middleware.py` -- read before editing.")
print(f"  For any new file, use Read first.")

# ── Scene 3: Turn 14 -- Staleness ────────────────────────────────────────────

header("Turn 14 -- Reads go stale over time")

print(f"{DIM}>> now refactor the config loading{RESET}")
print()
print(f"{DIM}# config.py was read at turn 1 -- that's 13 turns ago (threshold: 10){RESET}")
print()
print(f"  {BOLD}## VerifyFirst Policy{RESET}")
print(f"  You MUST read a file before editing it.")
print(f"  {BOLD}Verified (4):{RESET}")
print(f"  - `middleware.py` (turn 8, {GREEN}fresh{RESET})")
print(f"  - `auth.py` (turn 3, {YELLOW}aging{RESET})")
print(f"  - `session.py` (turn 2, {RED}{BOLD}STALE{RESET})")
print(f"  - `config.py` (turn 1, {RED}{BOLD}STALE{RESET})")
print(f"  {BOLD}Stale reads:{RESET} `session.py`, `config.py` (read 10+ turns ago, re-read before editing)")
print(f"  For any new file, use Read first.")

# ── Scene 4: Turn 18 -- Edit Velocity Alert ──────────────────────────────────

header("Turn 18 -- Edit velocity alert")

print(f"{DIM}>> keep going, fix the remaining issues{RESET}")
print()
print(f"{DIM}# Claude has been editing for 4 straight turns with zero reads{RESET}")
print(f"{DIM}# Turn 15: Edit(routes.py)  Turn 16: Edit(views.py){RESET}")
print(f"{DIM}# Turn 17: Edit(models.py)  Turn 18: Edit(forms.py){RESET}")
print()
print(f"  {BOLD}## VerifyFirst Policy{RESET}")
print(f"  You MUST read a file before editing it.")
print(f"  {BOLD}{YELLOW}High edit velocity:{RESET} 4 consecutive turns with")
print(f"  more edits than reads -- verify root cause before editing further.")
print(f"  {BOLD}Verified (4):{RESET}")
print(f"  - `middleware.py` (turn 8, {YELLOW}aging{RESET})")
print(f"  - `auth.py` (turn 3, {RED}{BOLD}STALE{RESET})")
print(f"  - `session.py` (turn 2, {RED}{BOLD}STALE{RESET})")
print(f"  - `config.py` (turn 1, {RED}{BOLD}STALE{RESET})")
print(f"  {BOLD}Stale reads:{RESET} `auth.py`, `session.py`, `config.py` (read 10+ turns ago, re-read before editing)")
print(f"  For any new file, use Read first.")
print()
