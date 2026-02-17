#!/usr/bin/env python3
"""ContextGuard demo — prediction, compaction, recovery."""
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

print(f">> also wire up the WebSocket handlers")
print()
print(f"  {DIM}Turn 65  |  16 active files  |  18K char injection{RESET}")
print(f"  {BOLD}{YELLOW}HIGH compaction risk -- finish current subtask{RESET}")
print()
print(f"  {DIM}... 2 turns later ...{RESET}")
print()
print(f"  Active files:  {BOLD}16{RESET}  ->  {RED}{BOLD}3{RESET}")
print(f"  {RED}Context compacted. Claude forgot what it was working on.{RESET}")
print()
print(f"  {DIM}ContextGuard recovers:{RESET}")
print(f"  auth.py, session.py, middleware.py, config.py,")
print(f"  routes.py, models.py + 6 more")
print()
print(f"  {GREEN}{BOLD}12 files recovered. Claude picks up where it left off.{RESET}")
