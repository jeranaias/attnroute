#!/usr/bin/env python3
"""LoopBreaker demo: stuck → intervention → recovery in 4 frames."""
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

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 56}{RESET}")

# Frame 1: Claude is stuck
header("Claude is stuck")
print(f"  Turn 1:  Edit auth.py  ->  {RED}tests fail{RESET}")
print(f"  Turn 2:  Edit auth.py  ->  {RED}tests fail{RESET}")
print(f"  Turn 3:  Edit auth.py  ->  {RED}tests fail{RESET}  {YELLOW}(3 attempts...){RESET}")
print(f"  Turn 4:  Edit auth.py  ->  {RED}tests fail{RESET}")
print(f"  Turn 5:  Edit auth.py  ->  {RED}tests fail{RESET}  {RED}{BOLD}(5 attempts!){RESET}")

# Frame 2: LoopBreaker intervenes
header("LoopBreaker intervenes")
print(f"  {BOLD}{RED}STOP — same approach failed 5 times{RESET}")
print()
print(f"  Try something different:")
print(f"  Re-read the file. Check your assumptions.")
print(f"  Ask the user if you're stuck.")

# Frame 3: Claude changes approach
header("Claude changes approach")
print(f"  Turn 6:  {GREEN}Read{RESET} auth.py, conftest.py, test_auth.py")
print(f"  Turn 7:  Edit {BOLD}conftest.py{RESET}  ->  {GREEN}tests pass!{RESET}")
print()
print(f"  {GREEN}Loop broken. Different file was the problem.{RESET}")

# Frame 4: Result
header("Result")
print(f"  Without LoopBreaker: Claude retries forever")
print(f"  With LoopBreaker:    Forced to reconsider at turn 5")
print()
print(f"  {GREEN}Saves tokens. Saves time. Solves the bug.{RESET}")
print()
