# I used Claude Code to build a free hook system that fixes Claude Code's worst habits

I've been using Claude Code daily for months — and I used it to build this entire project. 345 tests, 4 plugins, thousands of lines of Python, all written in Claude Code sessions. The irony is that the tool I built fixes the exact problems I kept hitting while building it.

**Free and open source.** MIT licensed, zero dependencies for the base install, free on PyPI.

**Built specifically for Claude Code** — plugs into its native hook system.

---

## The problems (you've probably hit all of these)

- **Editing files from memory** instead of re-reading them. The file was refactored 10 turns ago. Claude doesn't know. 3 turns wasted.
- **Getting stuck in a loop.** Same file, same approach, tests fail, repeat. 8 times. While your token budget evaporates.
- **Hitting the rate limit with zero warning.** Mid-task. Work gone.
- **Context compaction wipes Claude's memory.** It forgets what it was working on. Starts over from scratch.

I built **[attnroute](https://github.com/jeranaias/attnroute)** to fix all four.

```bash
pip install attnroute        # core + all 4 plugins, zero dependencies
pip install attnroute[all]   # + BM25, PageRank, tree-sitter (Python 3.10-3.13)
attnroute init
```

Works on the next prompt. No restart needed.

**[See it in action (GIF)](https://imgur.com/a/lroty8t)**

---

## How Claude helped me build it

Every feature came from a real frustration during development. I'd be deep in a Claude Code session and Claude would edit a file it hadn't read in 20 turns — wrong function names, broken imports. Or it would loop on the same failing test 7 times in a row instead of stepping back. I'd think "there has to be a way to catch this" and then ask Claude to help me build the fix.

The plugin architecture, the co-activation learner, the hook integration — all pair-programmed with Claude Code. It's genuinely good at building tools to fix its own problems when you point them out.

---

## How it works

attnroute plugs into Claude Code's **native hook system**. It has two parts: a context router that pre-loads relevant files into every prompt, and four plugins that fix specific behavioral problems.

The **context router** tracks which files you use together and learns co-activation patterns — files that get touched together get loaded together. It ranks files using PageRank on your import/dependency graph and serves them in tiers:

| Tier | What Claude sees |
|------|-----------------|
| **HOT** | Full file source |
| **WARM** | Function signatures + class definitions only |
| **COLD** | Not injected |

First session uses heuristics (git recency, file timestamps, import graphs). The co-activation learner activates after ~25 prompts and gets smarter from there.

---

## The plugins

### VerifyFirst v2.1 — *Stop editing from stale memory*

Tracks every file Claude reads and assigns freshness labels:

| Label | Meaning |
|-------|---------|
| `fresh` | Read recently, safe to edit |
| `aging` | Getting old, proceed with caution |
| `STALE` | Read 10+ turns ago — re-read before editing |

When Claude tries to edit a file it read 15 turns ago (or never read at all), it gets flagged and told to re-read first. Also monitors **edit velocity** — if Claude is hammering out edits without reading anything, it catches the speculative editing pattern and intervenes.

This alone saves me 2-3 wasted turns per session.

---

### LoopBreaker v3.0 — *Break the infinite retry loop*

Detects when Claude is stuck, but smart about it:

- **Content-aware signatures** — different edits to the same file don't trigger it
- **Git-based progress detection** — checks if `git diff` actually changed between attempts
- **Multi-metric severity scoring** — combines signature repetition + failure count + git progress
- **Multi-file bounce detection** — catches A→B→A→B ping-pong patterns

Escalation:

| Attempts | Response |
|----------|----------|
| 3 | Gentle nudge — "consider a different approach" |
| 5 | Strong warning — "stop and re-read the files" |
| 7 | Full stop — "ask the user for guidance" |

When it fires, Claude steps back, re-reads surrounding files, and usually finds the real bug was somewhere else entirely.

---

### BurnRate v1.0 — *See your rate limit coming — and catch billing errors*

Scans your actual conversation JSONL files across **all projects** and builds a real-time 5-hour sliding window of token consumption — matching Claude Code's actual rate limit window.

**Live dashboard:**
- Burn rate (tokens/min)
- Time-to-exhaustion prediction
- Tiered warnings as you approach the limit

**Full billing audit:**
- Per-project, per-model, per-day cost attribution
- Cache tier awareness (cache creation vs cache reads are billed differently)
- CSV and JSON export
- Configurable daily/weekly budget alerts

**This plugin caught that Anthropic was overcharging me.** I ran BurnRate's billing audit and found that 94.8% of my tokens were cache reads — which should be billed at $0.50/M — but my effective rate was $6.21/M. Over ~2.5 months, that added up to roughly **$6,000 in overcharges** across 591 line items. I've had an open support ticket for 3 weeks with no resolution. BurnRate gave me the per-model, per-day cost breakdown I needed to build an airtight case.

This is the rate limit and cost visibility that should be built into Claude Code natively. You literally cannot see your own token usage or costs without parsing JSONL files yourself — or using something like this.

---

### ContextGuard v1.0 — *Survive context compaction*

Doesn't just recover from compaction — it **predicts** it.

Tracks injection size trends and turn count to warn you when compaction risk is HIGH, so you can wrap up your current subtask first.

When compaction hits (active files drop from 16 to 3):
1. Re-injects your working file set
2. Claude picks up where it left off
3. Flags VerifyFirst that all previously-read files are now stale

No more "what were we working on?" after compaction.

---

## What I'm NOT claiming

- The plugins **don't hard-block anything**. They inject instructions into Claude's context that it follows. It's policy guidance, not a firewall.
- The benchmarked **97-99% context reduction** is measured against all source files concatenated — not against how Claude natively works. Your real-world mileage depends on your workflow. Methodology is [documented in the repo](https://github.com/jeranaias/attnroute#benchmarks).
- **First session won't be magic.** Heuristics work immediately, but the learning engine needs ~25 prompts to start contributing. It gets noticeably better over the first week.

---

## The numbers

| | |
|---|---|
| **Tests** | 345 passing |
| **Python** | 3.10 — 3.14 |
| **License** | MIT |
| **Cost** | Free |
| **Base install** | Zero dependencies |
| **Setup time** | Two commands |

---

**GitHub:** [github.com/jeranaias/attnroute](https://github.com/jeranaias/attnroute)
**PyPI:** [pypi.org/project/attnroute](https://pypi.org/project/attnroute/)

I built this to scratch my own itch using Claude Code itself. Curious if others hit the same pain points — and if you find edge cases, I want to hear about them.
