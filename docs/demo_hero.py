#!/usr/bin/env python3
"""Hero demo: what attnroute actually does in 4 frames."""
import sys
if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 56}{RESET}")

# Frame 1: Install
header("Setup (30 seconds)")
print(f"  {DIM}${RESET} pip install attnroute[all] && attnroute init")
print(f"  {GREEN}Ready!{RESET} 4 plugins active. Start Claude Code normally.")

# Frame 2: Before
header("Before attnroute")
print(f"  {DIM}${RESET} claude")
print(f"  {DIM}>>{RESET} fix the login bug in auth.py")
print()
print(f"  Claude reads 14 files looking for context...")
print(f"  {RED}~45,000 tokens burned on exploration{RESET}")

# Frame 3: After
header("After attnroute")
print(f"  {DIM}${RESET} claude")
print(f"  {DIM}>>{RESET} fix the login bug in auth.py")
print()
print(f"  attnroute injects: auth.py, session.py, config.py")
print(f"  {GREEN}2,847 tokens | 99% reduction | 43ms{RESET}")

# Frame 4: What you get
header("What's included")
print(f"  {GREEN}VerifyFirst{RESET}   Stops edits on unread files")
print(f"  {GREEN}LoopBreaker{RESET}   Breaks repetitive failure loops")
print(f"  {GREEN}BurnRate{RESET}      Tracks token spend + rate limits")
print(f"  {GREEN}ContextGuard{RESET}  Recovers context after compaction")
print()
