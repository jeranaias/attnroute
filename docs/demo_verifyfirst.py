#!/usr/bin/env python3
"""VerifyFirst demo — the real problem, then the fix."""
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

# The problem: Claude edits from memory, gets it wrong
print(f">> fix the token refresh bug in auth.py")
print()
print(f"  {DIM}Claude thinks it remembers auth.py...{RESET}")
print(f"  Edit auth.py:  {RED}wrong function name{RESET}")
print(f"  Edit auth.py:  {RED}import doesn't exist{RESET}")
print(f"  Edit auth.py:  {RED}stale variable name{RESET}")
print(f"  {RED}{BOLD}3 turns wasted. auth.py was refactored 10 turns ago.{RESET}")
print()

# The fix: VerifyFirst catches it
print(f"  {DIM}With VerifyFirst:{RESET}")
print(f"  auth.py  {RED}{BOLD}STALE{RESET}  {DIM}read 12 turns ago{RESET}")
print(f"  {YELLOW}Re-read before editing.{RESET}")
print()
print(f"  Read auth.py  {GREEN}fresh{RESET}")
print(f"  Edit auth.py  {GREEN}{BOLD}correct on first try{RESET}")
