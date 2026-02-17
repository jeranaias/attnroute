#!/usr/bin/env python3
"""VerifyFirst demo: violation → correction → success in 4 frames."""
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

# Frame 1: Claude edits without reading
header("Claude tries to edit a file it never read")
print(f"  {DIM}>>{RESET} update the rate limiting in middleware.py")
print(f"  Claude calls: {BOLD}Edit(middleware.py){RESET}")
print()
print(f"  {BOLD}{RED}BLOCKED — middleware.py was never read{RESET}")

# Frame 2: Claude corrects
header("Claude reads first, then edits")
print(f"  Claude calls: {BOLD}Read(middleware.py){RESET}")
print(f"  Claude calls: {BOLD}Edit(middleware.py){RESET}")
print()
print(f"  {GREEN}Allowed — file verified{RESET}")

# Frame 3: Freshness tracking
header("Reads go stale over time")
print(f"  auth.py        {GREEN}fresh{RESET}   {DIM}(read 2 turns ago){RESET}")
print(f"  session.py     {YELLOW}aging{RESET}   {DIM}(read 8 turns ago){RESET}")
print(f"  config.py      {RED}{BOLD}STALE{RESET}   {DIM}(read 15 turns ago){RESET}")
print()
print(f"  {DIM}Stale files must be re-read before editing{RESET}")

# Frame 4: Result
header("Result")
print(f"  No more edits based on stale assumptions")
print(f"  No more broken code from speculative changes")
print()
print(f"  {GREEN}Claude reads first. Every time.{RESET}")
print()
