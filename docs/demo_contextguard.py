#!/usr/bin/env python3
"""ContextGuard demo: prediction → compaction → recovery in 4 frames."""
import sys
if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 56}{RESET}")

# Frame 1: Warning
header("Long session — compaction risk rising")
print(f"  Turn 65  |  18,000 char injection  |  16 active files")
print()
print(f"  {BOLD}{YELLOW}HIGH compaction risk{RESET}")
print(f"  {DIM}Finish your current subtask before starting new work{RESET}")

# Frame 2: Compaction strikes
header("Context compacted")
print(f"  Active files: {BOLD}16{RESET}  ->  {RED}{BOLD}3{RESET}")
print(f"  Claude just forgot what it was working on.")
print()
print(f"  {DIM}Without ContextGuard, you'd start over.{RESET}")

# Frame 3: Recovery
header("ContextGuard recovers your files")
print(f"  Restoring: auth.py, session.py, middleware.py,")
print(f"             config.py, routes.py, models.py + 6 more")
print()
print(f"  {GREEN}12 files recovered. Claude knows where it was.{RESET}")

# Frame 4: Result
header("Result")
print(f"  Predicts compaction before it happens")
print(f"  Recovers your working set after it happens")
print(f"  Marks all stale reads via VerifyFirst")
print()
print(f"  {GREEN}Long sessions don't lose context anymore.{RESET}")
print()
