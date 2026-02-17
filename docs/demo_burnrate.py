#!/usr/bin/env python3
"""BurnRate demo: real-time tracking + budget alerts + cost breakdown."""
import sys

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 50}{RESET}")

def progress_bar(pct, width=20):
    filled = int(width * pct / 100)
    bar = "#" * filled + "-" * (width - filled)
    if pct >= 80:
        color = RED
    elif pct >= 50:
        color = YELLOW
    else:
        color = GREEN
    return f"{color}[{bar}]{RESET} {pct:.0f}%"

# Scene 1: Normal monitoring
header("Real-time rate limit tracking")

print(f"  {BOLD}Plan:{RESET}         Max 20x")
print(f"  {BOLD}Window:{RESET}       756,355 / 2,000,000 tokens")
print(f"  {BOLD}Usage:{RESET}        {progress_bar(37.8)}")
print(f"  {BOLD}Burn rate:{RESET}    3,740 tokens/min")
print(f"  {BOLD}Remaining:{RESET}    {GREEN}332 minutes{RESET}")

# Scene 2: Budget configuration
header("Budget alerts")

print(f"{DIM}~/.claude/plugins/config.json:{RESET}")
print(f'  {DIM}{{"burnrate": {{"daily_budget": 800000, "weekly_budget": 3000000}}}}{RESET}')
print()
print(f"{BOLD}{YELLOW}## BurnRate Budget{RESET}")
print(f"  Daily:  607,268 / 800,000  {progress_bar(75.9)}")
print(f"  Weekly: 2,408,161 / 3,000,000  {progress_bar(80.3)}")

# Scene 3: Cost breakdown
header("Cost tracking")

print(f"  {BOLD}{'Model':<25} {'Tokens':>12} {'Cost':>10}{RESET}")
print(f"  {'─' * 48}")
print(f"  claude-opus-4-6          {'746,185':>12} {'$20.84':>10}")
print(f"  claude-sonnet-4-5        {'124,300':>12}  {'$1.24':>10}")
print(f"  {'─' * 48}")
print(f"  {BOLD}{'Total':>25} {'870,485':>12} {'$22.08':>10}{RESET}")

# Scene 4: Export
header("Usage export")

print(f"{DIM}>>> burnrate.export_usage(format='csv', days=7){RESET}")
print(f"  timestamp,model,project,input_tokens,output_tokens,...")
print(f"  2026-02-16T02:47:20,claude-opus-4-6,~,1,3839,4701,80432")
print(f"  2026-02-16T02:47:25,claude-opus-4-6,~,5,1204,0,95841")
print(f"  {DIM}... 1,083 more records{RESET}")

# Scene 5: Critical warning
header("Rate limit warning (injected into Claude's context)")

print(f"  {BOLD}{RED}## BurnRate WARNING{RESET}")
print(f"  {BOLD}Estimated time until rate limit: ~12 minutes{RESET}")
print(f"  Burn rate: 4,200 tokens/min | Used: {RED}1,850,000{RESET} / 2,000,000")
print()
print(f"  {BOLD}Consider:{RESET}")
print(f"  - Pausing for a few minutes to let the window slide")
print(f"  - Switching to Sonnet for simple tasks")
print(f"  - Breaking work into smaller prompts")
print()
