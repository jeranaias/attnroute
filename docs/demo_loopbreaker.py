#!/usr/bin/env python3
"""LoopBreaker demo — stuck, intervention, recovery."""
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

print(f">> fix the JWT validation error in auth.py")
print()
print(f"  Turn 1:  Edit auth.py  ->  {RED}tests fail{RESET}")
print(f"  Turn 2:  Edit auth.py  ->  {RED}tests fail{RESET}")
print(f"  Turn 3:  Edit auth.py  ->  {RED}tests fail{RESET}  {YELLOW}3 attempts...{RESET}")
print(f"  Turn 4:  Edit auth.py  ->  {RED}tests fail{RESET}")
print(f"  Turn 5:  Edit auth.py  ->  {RED}tests fail{RESET}  {RED}{BOLD}5 attempts!{RESET}")
print()
print(f"  {BOLD}{RED}STOP -- same approach failed 5 times.{RESET}")
print(f"  {BOLD}Re-read the file. Check your assumptions.{RESET}")
print()
print(f"  Turn 6:  {GREEN}Read{RESET} auth.py, conftest.py, test_auth.py")
print(f"  Turn 7:  Edit {BOLD}conftest.py{RESET}  ->  {GREEN}{BOLD}tests pass!{RESET}")
print()
print(f"  {GREEN}{BOLD}Loop broken. Different file was the real problem.{RESET}")
