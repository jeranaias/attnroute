#!/usr/bin/env python3
"""VerifyFirst demo — without vs with."""
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

print(f">> fix the token refresh bug in auth.py")
print()
print(f"  {RED}{BOLD}Without VerifyFirst:{RESET}")
print(f"  Claude {DIM}\"remembers\"{RESET} auth.py from 14 turns ago...")
print(f"  Edit auth.py  {RED}wrong function name{RESET}")
print(f"  Edit auth.py  {RED}import doesn't exist{RESET}")
print(f"  Edit auth.py  {RED}stale variable name{RESET}")
print(f"  {RED}{BOLD}3 turns wasted. The file was refactored since.{RESET}")
print()
print(f"  {GREEN}{BOLD}With VerifyFirst:{RESET}")
print(f"  auth.py  {RED}{BOLD}STALE{RESET}  {DIM}read 14 turns ago{RESET}")
print(f"  {YELLOW}Re-read required before editing.{RESET}")
print(f"  Read auth.py  {GREEN}fresh{RESET}")
print(f"  Edit auth.py  {GREEN}{BOLD}correct on first try.{RESET}")
