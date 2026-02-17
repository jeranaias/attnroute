#!/usr/bin/env python3
"""Hero demo: Overview GIF for the top of the README."""
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

# ── Scene 1: Install + Init ──────────────────────────────────────────────────

header("Install & Initialize")

print(f"{DIM}${RESET} pip install attnroute[all]")
print(f"  Collecting attnroute[all]")
print(f"  Downloading attnroute-1.0.0-py3-none-any.whl (84 kB)")
print(f"  Installing collected packages: attnroute")
print(f"  {GREEN}Successfully installed attnroute-1.0.0{RESET}")
print()
print(f"{DIM}${RESET} attnroute init")
print(f"  Scanning projects... found {BOLD}3 projects{RESET}")
print(f"  Registering hooks: {GREEN}UserPromptSubmit{RESET}, {GREEN}SessionStart{RESET}, {GREEN}Stop{RESET}")
print(f"  Building search index... {GREEN}done{RESET} (247 files, 1.2s)")
print(f"  {BOLD}{GREEN}Ready!{RESET} attnroute is active. Start Claude Code normally.")

# ── Scene 2: Plugins List ────────────────────────────────────────────────────

header("Installed Plugins")

print(f"{DIM}${RESET} attnroute plugins list")
print()
plugins = [
    ("verifyfirst",  "v2.1.0", "Read-before-write policy with freshness tracking"),
    ("loopbreaker",  "v3.0.0", "Multi-metric loop detection with git progress"),
    ("burnrate",     "v1.0.0", "Rate limit tracking with budget alerts"),
    ("contextguard", "v1.0.0", "Compaction prevention with prediction"),
]
for name, ver, desc in plugins:
    print(f"  {GREEN}{BOLD}{name:<14}{RESET} {DIM}{ver}{RESET}  {desc}")

# ── Scene 3: Context Injection in a Real Prompt ──────────────────────────────

header("What Claude Sees (injected context)")

print(f"{DIM}>> fix the login timeout in auth.py{RESET}")
print()
print(f"{DIM}# attnroute injects this into Claude's system prompt:{RESET}")
print()
print(f"  {BOLD}## attnroute context{RESET}")
print(f"  {BOLD}HOT{RESET}  src/auth.py         {RED}(HOT){RESET}  -- full file injected")
print(f"  {BOLD}HOT{RESET}  src/session.py      {RED}(HOT){RESET}  -- full file injected")
print(f"  {YELLOW}WARM{RESET} src/middleware.py   {YELLOW}(WARM){RESET} -- compressed outline")
print(f"  {YELLOW}WARM{RESET} src/config.py       {YELLOW}(WARM){RESET} -- compressed outline")
print(f"  {DIM}COLD tests/test_auth.py  (evicted){RESET}")
print()
print(f"  {BOLD}## VerifyFirst Policy{RESET}")
print(f"  You MUST read a file before editing it.")
print(f"  {BOLD}Verified (2):{RESET}")
print(f"  - `auth.py` (turn 3, {GREEN}fresh{RESET})")
print(f"  - `session.py` (turn 2, {GREEN}fresh{RESET})")
print(f"  For any new file, use Read first.")

# ── Scene 4: Stats + Tagline ─────────────────────────────────────────────────

header("Result")

print(f"  {BOLD}Context:{RESET} 2,847 tokens injected  {GREEN}(99.2% reduction from 356K source){RESET}")
print(f"  {BOLD}Latency:{RESET} 43ms total  {DIM}(indexing 12ms + routing 18ms + plugins 13ms){RESET}")
print(f"  {BOLD}Plugins:{RESET} 4 active  {DIM}(verifyfirst, loopbreaker, burnrate, contextguard){RESET}")
print()
print(f"  {DIM}Before:{RESET}  Claude hunts for files via tool calls   {DIM}(variable cost){RESET}")
print(f"  {BOLD}{GREEN}After:{RESET}   Pre-selected context injected          {BOLD}{GREEN}(~2K tokens){RESET}")
print()
print(f"  {BOLD}345 tests{RESET} | {BOLD}4 plugins{RESET} | {BOLD}14+ languages{RESET} | {BOLD}MIT License{RESET}")
print()
