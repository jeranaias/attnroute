#!/usr/bin/env python3
"""BurnRate demo — progress bar filling up to warning."""
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

print(f"$ claude")
print(f">> refactor the auth module to use JWT")
print()
print(f"  {GREEN}[###########-------------------]{RESET}  38%   {GREEN}332 min left{RESET}")
print(f"  {DIM}756K / 2M tokens  |  3,740 tok/min{RESET}")
print()
print(f"  {DIM}... 40 turns later ...{RESET}")
print()
print(f"  {RED}{BOLD}[############################--]{RESET}  93%   {RED}{BOLD}~36 min left{RESET}")
print(f"  {DIM}1.85M / 2M tokens  |  4,200 tok/min{RESET}")
print()
print(f"  {BOLD}{RED}WARNING: Approaching rate limit{RESET}")
print(f"  {DIM}Consider pacing prompts or switching to Sonnet for simple tasks{RESET}")
print()
print(f"  {GREEN}{BOLD}You always know where you stand.{RESET}")
