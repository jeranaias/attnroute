# Hands-off attnroute: ranked by money and quality per unit of work

The bar, from the owner (2026-10-13, relayed): *"a brainless install and a brainless use, full
hands-off setup that saves big"* — saving money **and** protecting quality, with nothing for him
to do.

This ranks the work by what it returns per unit of effort, states the arithmetic behind each
money claim, and names what each item cannot do. It is a plan, not a promise: every figure
below is either measured and cited, or marked as derived and what it rests on.

---

## The money, as measured

| | |
|---|---|
| bill composition, cost-weighted, 3 sessions | cache_read **79%**, cache_creation 12.5%, output 8% |
| halving the window (replay, 73 windows, 38,315 turns) | **−33.5% of the bill** |
| re-derivation after a compaction, 1M window, 74 compactions | **8.4** repeats per compaction |

Two consequences that set the ranking:

1. **The largest saving is already banked and is a settings value.** `autoCompactWindow=400000`
   is what delivers the −33.5%, and it is currently true on two machines because somebody typed
   it. Nothing re-asserts it. A reinstall, a new machine, or a settings edit silently reverts the
   single biggest number in this programme — which makes the installer a *money* item, not
   housekeeping.
2. **Compacting at 80% rather than 100% is itself a saving**, because the bill follows the mean
   context, not the window:

   ```
   mean context ≈ (post-compact floor + trigger point) / 2
   floor ≈ 44k        derived: observed mean 522k at a 1,000,000 window ⇒ 2×522 − 1000
   at 400k trigger    mean ≈ (44 + 400)/2 = 222k
   at 320k trigger    mean ≈ (44 + 320)/2 = 182k      −18% of mean context
                                                       ≈ −14% of the whole bill
   ```

   ⚠ **The floor is derived from the 1M era and must be re-measured under 400k before this
   number is quoted to anyone.** A shorter window may not shrink the floor proportionally — the
   handback, the system prompt and the tool schemas are fixed costs — and if the floor is a
   larger share of a smaller window, this saving is smaller than 14%.

---

## Ranked

### 1. Command-shape guards — tiny work, immediate quality, nothing for anyone to do

Both are decidable from the Bash command string alone, at `PreToolUse`, with no network and no
state:

* **a merge chained after its own tests in one command.** `pytest … && gh pr merge …` merges on
  the exit status of a command whose output nobody has read. The tests passing is not the same
  fact as the tests being *looked at*.
* **tests run with `PYTHONIOENCODING` set.** It changes what the suite can print, so a green run
  under it is green under a different configuration than CI's.

Why first: hours of work, no latency cost (a regex on a string already in the payload), no
owner action, and both fire on patterns that have actually happened here.

### 2. Push to an already-merged branch — small work, proven defect

I did this twice in one week (#1678 after #1674, #1855 after #1852). The information is almost
always **already in the window**: a `gh pr view … --json state` printing `MERGED`, then a later
`git push` to that same branch. Detecting it from the transcript costs nothing and needs no
network, which a hook cannot use (`no_egress.lock_down()` is called on every hook event, and the
p95 budget is 63 ms — a round trip does not fit).

### 3. Compaction that the session takes at a break — the biggest unbanked money, if it works

The 80% alert already exists and already fires (it fired on the architect seat at 354,877 of
400,000 while this document was being written). Two changes:

* the alert asks the session to **finish its step and request `/compact`**, rather than only
  reporting the number. Claude Code cannot compact on command, so the ask goes to the session
  and the human is not in the loop;
* **measure compliance**: a compaction within N turns of an alert is the treated case, an
  auto-compact at the window is the control. This is readable from the transcript — the alert is
  already a telemetry row, and `compact_boundary` is already a transcript marker.

⚠ The saving is contingent on sessions acting, and the honest version of this item is the
measurement, not the nudge. If compliance is low the number is zero, and the report should say
so rather than quoting the −14% as achieved.

### 4. One installer per machine, that verifies and updates itself — protects the −33.5%

`attnroute/installer.py` exists. What it must become:

* one command per machine: global hooks (never per-worktree — they merge, and identical local
  entries double-fire every hook), the pinned venv, the settings **including
  `autoCompactWindow`**, then `attnroute validate` run against its own work;
* **self-update to a pinned release on a schedule**, verified **by content** rather than by
  version. `pip install --upgrade` is a no-op on this package because the version string stays
  `1.0.2`; it needs `--force-reinstall --no-deps`, and the check that it worked is
  `hasattr(session_state, "ledger_summary")`, not a version number;
* registering the schedule is a machine-level change, so the installer *offers* it and the owner
  runs the installer once. That is one command, which meets the bar; a package that registers
  scheduled tasks behind a `pip install` does not.

### 5. Attribution with no command — inferred, but never silently

`attnroute/team.py` refuses to read a team out of a directory name, and the reason stands: a
renamed worktree files one team's work under another's name and nothing looks wrong. The
hands-off requirement and that refusal reconcile on one condition — **infer only when the
evidence inside the session is unanimous**:

* gather every worktree path the session's own tool calls touch (the `cd` targets already
  recorded in `facts`), map each through the existing 128-prefix mapping;
* **one team ⇒ record it with `team_source="inferred"`**. Two teams, or none ⇒ record `None`,
  with the reason. A split session is reported as unknown rather than allocated to whichever
  path came first.

That keeps the failure visible, which is the actual principle behind the original refusal, and
`attnroute team set` (#35) remains the override — now rarely needed rather than mandatory.

### 6. Verify that a merge reached main — needs git, so it is not a hook

The strand detector jesse-b3 described (a PR reported `MERGED` whose head's tree changes are not
on `main`; he missed #1883 exactly that way) cannot live in a hook: it needs to compare trees,
and hooks have no network. It belongs in the scheduled report (item 7), which runs outside the
hook path and may use git freely.

A weaker hook-side version is honest and cheap in the meantime: **after `gh pr merge`, flag that
the session did not verify by content in the same window.** That is a property of the transcript,
not of the repository, and it is the habit that catches the strand case.

### 7. The weekly savings-and-quality report, produced automatically

The container for everything above, and the owner's standing requirement ("permanent logs", a
report each phase). Contents: the savings table from `usage` only, treated vs held-out; the
re-derivation ledger per session against **its own earlier windows** (never seat-to-seat — the
spread is 2.4 to 18.7); the guard findings from items 1, 2 and 6; and an explicit list of what
could not be measured.

---

## What this plan does not claim

* **No quality gate blocks anything.** Every guard above reports. A gate on a blunt number gets
  switched off within a week and takes the measurement with it.
* **The quality half remains a proxy.** Re-derivation is countable; a contradicted ruling needs a
  judgement about prose, and this package's rule is that a fact handed forward is something
  already written down. A classifier guessing at "contradicted ruling" would find nothing in most
  windows and report quality fine — a check that cannot fire, wearing the name of the one that
  matters. Those stay with a human, via `attnroute note add --kind correction`.
* **"Hands-off" is not "unobserved".** The first week of any of this is observe-only with the
  holdouts already in place (50/50 today), and the acting flags stay off until the report shows
  no harm.
