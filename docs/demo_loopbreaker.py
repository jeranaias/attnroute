#!/usr/bin/env python3
"""LoopBreaker demo — without vs with."""
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
print(f"  {RED}{BOLD}Without LoopBreaker:{RESET}")
print(f"  Edit auth.py  {RED}tests fail{RESET}")
print(f"  Edit auth.py  {RED}tests fail{RESET}")
print(f"  Edit auth.py  {RED}tests fail{RESET}")
print(f"  Edit auth.py  {RED}tests fail{RESET}")
print(f"  Edit auth.py  {RED}tests fail{RESET}")
print(f"  Edit auth.py  {RED}tests fail...{RESET}")
print(f"  {RED}{BOLD}Same file. Same approach. Burns your tokens forever.{RESET}")
print()
print(f"  {GREEN}{BOLD}With LoopBreaker:{RESET}")
print(f"  {BOLD}5 attempts detected.{RESET} Stop and re-read.")
print(f"  Read auth.py, conftest.py, test_auth.py")
print(f"  Edit {BOLD}conftest.py{RESET}  {GREEN}{BOLD}tests pass!{RESET}")
print(f"  {GREEN}{BOLD}Different file was the real problem.{RESET}")
