#!/usr/bin/env python3
"""VerifyFirst demo — violation, correction, freshness."""
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

print(f">> update the rate limiting in middleware.py")
print(f"  Claude calls: {BOLD}Edit(middleware.py){RESET}")
print(f"  {BOLD}{RED}BLOCKED{RESET} {RED}-- middleware.py was never read{RESET}")
print()
print(f">> try again")
print(f"  Claude calls: {BOLD}Read(middleware.py){RESET}")
print(f"  Claude calls: {BOLD}Edit(middleware.py){RESET}")
print(f"  {GREEN}{BOLD}ALLOWED{RESET} {GREEN}-- file verified{RESET}")
print()
print(f"  {DIM}Freshness degrades over time:{RESET}")
print(f"  auth.py        {GREEN}fresh{RESET}     {DIM}read 2 turns ago{RESET}")
print(f"  session.py     {YELLOW}aging{RESET}     {DIM}read 8 turns ago{RESET}")
print(f"  config.py      {RED}{BOLD}STALE{RESET}     {DIM}read 15 turns ago -- re-read required{RESET}")
print()
print(f"  {GREEN}{BOLD}Claude reads first. Every time.{RESET}")
