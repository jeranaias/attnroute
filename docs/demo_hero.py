#!/usr/bin/env python3
"""Hero demo: install + plugins list + quick status overview."""
import sys

# ANSI colors
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

def header(text):
    print(f"\n{BOLD}{CYAN}{text}{RESET}")
    print(f"{CYAN}{'─' * 56}{RESET}")

# Scene 1: Install
header("attnroute — Intelligent Context Routing for Claude Code")
print(f"{DIM}$ pip install attnroute[all]{RESET}")
print(f"{GREEN}Successfully installed attnroute-1.0.0{RESET}")
print()
print(f"{DIM}$ attnroute init{RESET}")
print(f"  Scanning projects... found {BOLD}3 projects{RESET}")
print(f"  Registering hooks: {GREEN}UserPromptSubmit{RESET}, {GREEN}SessionStart{RESET}, {GREEN}Stop{RESET}")
print(f"  {BOLD}{GREEN}Done!{RESET} attnroute is active. Start Claude Code normally.")

# Scene 2: Plugins list
header("Installed plugins")
print(f"{DIM}$ attnroute plugins list{RESET}")
plugins = [
    ("verifyfirst",  "v2.1.0", "Read-before-write policy with freshness tracking"),
    ("loopbreaker",  "v3.0.0", "Multi-metric loop detection with git progress"),
    ("burnrate",     "v1.0.0", "Rate limit tracking with budget alerts"),
    ("contextguard", "v1.0.0", "Compaction prevention with prediction"),
]
for name, ver, desc in plugins:
    print(f"  {BOLD}{GREEN}{name}{RESET} {DIM}{ver}{RESET} — {desc}")

# Scene 3: Quick status
header("Live session status")
print(f"{DIM}$ attnroute plugins status burnrate{RESET}")
print(f"  {BOLD}plan_type:{RESET}       max_20x")
print(f"  {BOLD}window_tokens:{RESET}   756,355 / 2,000,000 ({YELLOW}37.8%{RESET})")
print(f"  {BOLD}cost:{RESET}            ${BOLD}20.84{RESET} across 242 API calls")
print(f"  {BOLD}burn_rate:{RESET}       3,740 tokens/min")
print(f"  {BOLD}time_remaining:{RESET}  {GREEN}332 min{RESET}")

# Scene 4: Tagline
header("90%+ Token Reduction  |  <200ms  |  Zero Config")
print(f"  {DIM}Before:{RESET}  Claude hunts for files via tool calls  {DIM}(variable cost){RESET}")
print(f"  {BOLD}{GREEN}After:{RESET}   Pre-selected context injected         {BOLD}{GREEN}(~2K tokens){RESET}")
print()
print(f"  {BOLD}345 tests{RESET} | {BOLD}4 plugins{RESET} | {BOLD}14+ languages{RESET} | {BOLD}MIT License{RESET}")
print()
