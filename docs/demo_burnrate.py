#!/usr/bin/env python3
"""BurnRate demo — without vs with."""
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

print(f">> refactor the auth module to use JWT")
print()
print(f"  {RED}{BOLD}Without BurnRate:{RESET}")
print(f"  Turn 40... Turn 50... Turn 60...")
print(f"  {RED}{BOLD}Rate limit hit. Session dead.{RESET}")
print(f"  {RED}No warning. Mid-task. Work lost.{RESET}")
print()
print(f"  {GREEN}{BOLD}With BurnRate:{RESET}")
print(f"  {GREEN}[###########-------------------]{RESET}  38%  {GREEN}332 min left{RESET}")
print(f"  {DIM}... 40 turns later ...{RESET}")
print(f"  {RED}{BOLD}[############################--]{RESET}  93%  {RED}{BOLD}~36 min left{RESET}")
print(f"  {BOLD}{YELLOW}Approaching limit. Wrap up or switch models.{RESET}")
print(f"  {GREEN}{BOLD}You always see it coming.{RESET}")
