# Going active: what has to be true, and what the canary can and cannot tell us

Status: drafted for review after the T5 canary was installed in observe-only mode.

Everything below is measured from the 22 transcripts in `~/.claude/projects/C--Users-jesse`
(71 windows with real tool use, 38,315 turns deduplicated by message id), not assumed.

---

## 1. The uncomfortable finding: the arm contrast cannot be the gate

The headline in `attnroute savings` is the within-session arm contrast, and it is the right
headline — it is the only comparison with no confound in it. But it is a comparison of means
between a treated arm and a **10% control arm**, so the control arm is the binding constraint,
and it grows at one tenth of the rate at which the lever gets decisions.

How many decisions does each lever actually get?

| lever | what it can act on | per window | windows for 50 control decisions |
|---|---|---|---|
| read ledger | a repeat Read, same window, unchanged | mean **1.9**, median 0, p90 8 | **~257** |
| output cap | a Bash result over 6,000 chars | mean **2.9**, median 2, p90 6 | **~171** |

At ~272 turns per window, 171 windows is roughly 46,000 turns. The canary would run for months
before either lever had a control group worth a confidence interval.

This is not a reason to abandon them. It is a reason not to pretend the gate is statistical
when it cannot be. Two things follow.

### 1a. Raise the holdout for the trial, then lower it

A 10% holdout is right for steady state and wrong for a trial: equal allocation maximises
power. Setting both holdouts to **50% for the canary** makes the control arm grow five times
faster — ~34 windows for the cap, ~53 for the ledger — at the cost of forgoing half of a
combined ~2.6% of the bill while the trial runs. That is a cheap price for being able to see
harm at all.

### 1b. Size the saving exactly; use the contrast only for harm

The saving from a suppressed read is **not an estimate**. It is the token count of the read
that did not enter the context, and the ledger knows it exactly, because it recorded that
file's size when it was read the first time. Same for the cap: the characters not returned are
counted, not guessed.

What is genuinely uncertain is whether the model **compensated** — re-read the file, re-ran the
command, or went round the houses. That is what the arm contrast is for, and it is a harm
measurement, not a savings measurement. So:

- report the saving from the exact suppressed-token sum, labelled as what it is;
- report harm from the arm contrast, and when the interval is too wide to say anything, print
  that instead of a number.

---

## 2. What the canary in observe-only mode can and cannot produce

**It cannot produce an arm contrast at all.** `arm_contrast` counts only records with
`acting=true`, and that is correct: while observing, both arms are untouched and identical by
construction. Any observe-only number labelled "saving" is a would-have-saved estimate.

What observe-only genuinely establishes:

| | what it proves |
|---|---|
| stream records appear for all four components | the wiring is real, not notionally installed |
| hook latency p95 | the cost per event the user actually pays |
| handback tokens and dropped count | the budget holds on a real session, not a fixture |
| `repeats/compaction` | the **baseline** for the quality gate |
| would-have-saved totals | the size of the prize, as an estimate |

---

## 3. Gates to flip `ATTNROUTE_LEDGER_ACT` and `ATTNROUTE_CAP_ACT`

All of these are checkable from `attnroute savings` plus the stream.

**Must hold (any failure blocks):**

1. **The stream is flowing from every component.** `read_ledger`, `output_cap`,
   `session_state` and the handback all appear, with `torn` and `other-version` skips at zero.
   A silent gap means the measurement is not measuring.
2. **No stranded read, ever.** Zero records where a notice was followed by no later successful
   read of the same key within the same window. This is the one failure that damages work
   rather than costing tokens.
3. **The handback never exceeded its budget**, and `dropped` is 0 on a typical window. A
   dropped ruling is the harm this whole lever exists to prevent.
4. **Hook latency p95 under 1,500 ms per event**, which is the budget already gated in CI. Not
   150 ms: an empty `python -c pass` is ~400 ms on this hardware, so a lower bound would be
   measuring Python.
5. **At least 8 compactions observed on T5**, so the handback has actually been exercised
   across window boundaries rather than only within one.

**Should hold (a failure is a conversation, not a block):**

6. The would-have-saved estimate is at least 0.5% of the window's cost units. Below that the
   lever is not worth its own latency.
7. `repeats/compaction` during observation is within the baseline band — measured at mean
   **0.39 per window**, with only **18% of windows showing any repeat at all**. Note what that
   implies: with a mean that low, detecting a *doubling* needs roughly **41 windows**. So this
   gate can confirm "no catastrophe" quickly and "no small regression" not at all.

---

## 4. What blocks going active outright

- any stranded read, or any handback that dropped an item on a normal window;
- a `CLAIMED` promotion rate above, say, a third of all notes: it would mean the promotion
  check is too strict to be useful, and a gate nobody can pass gets ignored;
- latency p95 above the CI budget on T5's real machine rather than on this one;
- telemetry gaps: any `torn` or `other-version` skips, or a session that should be `observed`
  appearing as `baseline`, because then group assignment is unreliable and so is everything
  built on it.

---

## 5. The recommendation, stated plainly

The two levers that the ACT vars control are worth a combined **~2.6%** of the
cost-weighted bill and cannot be validated by contrast inside ~170 windows. The cadence lever
is worth **~33.5%** and needs no ACT var at all: it needs the state file to be trusted and the
compaction threshold set lower, which is the owner's switch to throw.

So the sequence that spends effort where the money is:

1. Run the canary in observe-only until the five **must-hold** gates are met (≈8 compactions).
   This is a check that the machinery works, not a measurement of saving.
2. Flip both ACT vars with the holdout at **50%**, since the saving is exactly countable and
   the risk is what needs watching.
3. In parallel — not afterwards — put the effort into the cadence lever: establish that a
   handback carries a window's rulings across a boundary, then ask the owner to lower the
   compaction threshold on one team and measure `turns/window` and the repeats floor on it.
4. Drop the holdouts to 10% once harm has been looked for and not found, and say in the report
   that "not found" at that sample size means "no catastrophe", not "no cost".
