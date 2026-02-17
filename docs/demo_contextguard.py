#!/usr/bin/env python3
"""ContextGuard demo — without vs with."""
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

print(f">> wire up the WebSocket handlers")
print(f"  {DIM}Turn 65  |  16 active files{RESET}")
print()
print(f"  {RED}{BOLD}Without ContextGuard:{RESET}")
print(f"  {DIM}... 2 turns later ...{RESET}")
print(f"  {RED}{BOLD}Context compacted.{RESET}")
print(f"  Active files:  16  {RED}->  3{RESET}")
print(f"  {RED}Claude forgot what it was doing. Starts over.{RESET}")
print()
print(f"  {GREEN}{BOLD}With ContextGuard:{RESET}")
print(f"  {BOLD}{YELLOW}HIGH compaction risk{RESET} {DIM}-- finish current subtask{RESET}")
print(f"  {DIM}Compaction hits...{RESET}")
print(f"  {GREEN}{BOLD}12 files recovered.{RESET} Claude picks up where it left off.")
