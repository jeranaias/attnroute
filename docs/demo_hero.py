#!/usr/bin/env python3
"""Hero demo — one continuous terminal session."""
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

print(f"$ pip install attnroute[all] && attnroute init")
print(f"  {GREEN}Ready!{RESET} 4 plugins active. No restart needed.")
print()
print(f"$ claude")
print(f">> fix the login timeout bug in auth.py")
print()
print(f"  {DIM}attnroute injected 3 files in 43ms:{RESET}")
print(f"  {BOLD}auth.py{RESET}      {RED}HOT{RESET}   full source")
print(f"  {BOLD}session.py{RESET}   {RED}HOT{RESET}   full source")
print(f"  {BOLD}config.py{RESET}    {YELLOW}WARM{RESET}  function signatures only")
print()
print(f"  {GREEN}{BOLD}2,847 tokens injected  (99% reduction from 356K){RESET}")
print()
print(f"  {GREEN}VerifyFirst{RESET}    Stops edits on unread files")
print(f"  {GREEN}LoopBreaker{RESET}    Breaks repetitive failure loops")
print(f"  {GREEN}BurnRate{RESET}       Tracks token spend + rate limits")
print(f"  {GREEN}ContextGuard{RESET}   Recovers context after compaction")
