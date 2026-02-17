#!/usr/bin/env python3
"""BurnRate demo: tracking → warning → action in 4 frames."""
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

def bar(pct, width=30):
    filled = int(width * pct / 100)
    b = "#" * filled + "-" * (width - filled)
    color = RED if pct >= 80 else YELLOW if pct >= 50 else GREEN
    return f"{color}[{b}]{RESET} {pct:.0f}%"

# Frame 1: Healthy
header("Monitoring your token window")
print(f"  {bar(38)}")
print(f"  756K / 2M tokens  |  3,740 tok/min  |  {GREEN}332 min left{RESET}")

# Frame 2: Getting warm
header("Usage climbing")
print(f"  {bar(65)}")
print(f"  1.3M / 2M tokens  |  4,100 tok/min  |  {YELLOW}140 min left{RESET}")

# Frame 3: Danger
header("Approaching rate limit")
print(f"  {bar(93)}")
print(f"  1.85M / 2M tokens  |  4,200 tok/min  |  {RED}{BOLD}~36 min left{RESET}")
print()
print(f"  {BOLD}{RED}WARNING: Consider pacing your prompts{RESET}")

# Frame 4: What you get
header("Result")
print(f"  No more surprise rate limits")
print(f"  Daily/weekly budget alerts")
print(f"  Per-model cost breakdown ($22.08 across 242 calls)")
print()
print(f"  {GREEN}You always know where you stand.{RESET}")
print()
