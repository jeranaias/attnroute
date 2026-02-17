#!/usr/bin/env python3
"""BurnRate demo: Real-time token monitoring in a Claude Code session."""
import sys
if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ANSI codes
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

def progress_bar(pct, width=20):
    filled = int(width * min(pct, 100) / 100)
    bar = "#" * filled + "-" * (width - filled)
    if pct >= 80:
        color = RED
    elif pct >= 50:
        color = YELLOW
    else:
        color = GREEN
    return f"{color}[{bar}]{RESET} {pct:.0f}%"

# ── Scene 1: Normal Session Monitoring ────────────────────────────────────────

header("Session start -- BurnRate shows current window")

print(f"{DIM}$ claude{RESET}")
print(f"  {DIM}attnroute: 4 plugins loaded{RESET}")
print(f"  BurnRate: Active (max_20x plan, 756,355 / 2,000,000 tokens in 5h window, 38% used)")
print()
print(f"{DIM}>> refactor the auth module to use JWT{RESET}")
print()
print(f"{DIM}# BurnRate monitors every API call from ~/.claude/projects/*/*.jsonl{RESET}")
print(f"{DIM}# Status at any time:{RESET}")
print()
print(f"  {BOLD}Plan:{RESET}         max_20x (2,000,000 tokens / 5h window)")
print(f"  {BOLD}Window:{RESET}       756,355 / 2,000,000 tokens")
print(f"  {BOLD}Usage:{RESET}        {progress_bar(37.8)}")
print(f"  {BOLD}Burn rate:{RESET}    3,740 tokens/min {DIM}(last 15.0min){RESET}")
print(f"  {BOLD}Remaining:{RESET}    {GREEN}332 minutes{RESET}")

# ── Scene 2: Budget Configuration ────────────────────────────────────────────

header("Budget alerts -- daily and weekly limits")

print(f"{DIM}# Optional budget config in ~/.claude/plugins/config.json:{RESET}")
print(f'{DIM}  {{"burnrate": {{"daily_budget": 800000, "weekly_budget": 3000000}}}}{RESET}')
print()
print(f"{DIM}# When approaching budgets, BurnRate injects:{RESET}")
print()
print(f"  {BOLD}{YELLOW}## BurnRate Budget{RESET}")
print(f"  Daily:  607,268 / 800,000 tokens  {progress_bar(75.9)}")
print(f"  Weekly: 2,408,161 / 3,000,000 tokens  {progress_bar(80.3)}")

# ── Scene 3: Per-Model Cost Breakdown ─────────────────────────────────────────

header("Cost tracking -- per-model breakdown")

print(f"{DIM}$ attnroute plugins status burnrate{RESET}")
print()
print(f"  {BOLD}{'Model':<26}{'Tokens':>12}{'Cost':>10}{RESET}")
print(f"  {'-' * 48}")
print(f"  claude-opus-4-6           {'746,185':>12}{'$20.84':>10}")
print(f"  claude-sonnet-4-5         {'124,300':>12} {'$1.24':>10}")
print(f"  {'-' * 48}")
print(f"  {BOLD}{'Total':<26}{'870,485':>12}{'$22.08':>10}{RESET}")
print()
print(f"  {DIM}Cost is API-equivalent pricing (what you'd pay without a subscription){RESET}")
print(f"  {DIM}Opus: $5/M input, $25/M output | Sonnet: $3/M input, $15/M output{RESET}")

# ── Scene 4: Critical Rate Limit Warning ─────────────────────────────────────

header("Turn 47 -- approaching rate limit")

print(f"{DIM}>> keep going, implement the remaining endpoints{RESET}")
print()
print(f"{DIM}# BurnRate detects 92.5% window usage, injects warning:{RESET}")
print()
print(f"  {BOLD}{RED}## BurnRate WARNING{RESET}")
print(f"  `[##################--]` {BOLD}93%{RESET}")
print()
print(f"  {BOLD}Window:{RESET} 1,850,000 / 2,000,000 tokens (max_20x plan, 5h sliding window)")
print(f"  {BOLD}Burn rate:{RESET} 4,200 tokens/min (last 15.0min)")
print(f"  {BOLD}ETA to limit:{RESET} ~36 minutes")
print()
print(f"  {DIM}Input: 1,204,000 | Output: 412,000 | Cache create: 234,000 | Cache read: 891,000 (242 API calls){RESET}")
print()
print(f"  {BOLD}Consider pacing your work{RESET} -- keep prompts focused")
print(f"  and avoid unnecessary file reads.")
print()
