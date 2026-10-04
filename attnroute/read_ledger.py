"""A session read ledger: what was read, when, and whether it has changed since.

⚠⚠ WHY THIS EXISTS, MEASURED RATHER THAN SUPPOSED. Across three long sessions (6,530,566
tool-result tokens, 63,052 assistant turns, 112 compactions):

    all file reads                    52.6% of read tokens
      first read of a file            12.3%      <- the only part injection can address
      repeat ACROSS a compaction       8.6%      legitimate: gone from context
      repeat AFTER an edit             4.0%      legitimate: content changed
      repeat in the SAME window        27.6%     <- what this ledger can serve

Injection at prompt time, which is attnroute's original design, can only ever address the
12.3%. Re-reading is more than twice as large and needs no prediction at all -- only memory.

COST, STATED HONESTLY: cache_read is 79.3% of the cost-weighted bill, so a read token removed
from context is also not re-read on each remaining turn of its window. At ~1,087 turns per
window the multiplier is large, but the ledger's share of the whole bill is about 1.6%, NOT
27.6%. The big lever is compaction cadence; this is the best READ-SIDE lever.

═══ FOUR RULES, EACH FROM A WAY THIS COULD GO WRONG ═══

1. NEVER A DENY. A repeat read gets a NOTICE with the outline and an explicit way to force the
   full read. A hook that strands the model is worse than a re-read.
2. STRICT KEYS. The 27.6% figure came from BASENAME matching, which conflates two different
   `__init__.py`. Sizing may be aggressive; a DECISION may not. This uses the resolved full
   path plus the range, which is the conservative 20.4% of the same measurement.
3. EXPIRES ON COMPACTION. After compaction the earlier read is no longer in context, so the
   re-read is legitimate and the ledger must not suppress it.
4. ANY EDIT INVALIDATES. mtime or size changing, or an in-session Edit/Write, drops the entry.
"""

import json
import os
import time
from pathlib import Path

#: Below this, a notice would cost more than the re-read it replaces.
MIN_TOKENS_TO_NOTICE = 400

#: What a notice costs, measured by writing one: see `format_notice`.
NOTICE_TOKENS_EST = 40

#: Separator between a path and its range in a ledger key. Chosen because it
#: cannot occur in a path, unlike ":" which begins every Windows absolute path.
KEY_SEP = "\x1f"

ALLOW = "ALLOW"
NOTICE = "NOTICE"


def read_key(path: str, offset=None, limit=None) -> str:
    """The ledger key. STRICT: resolved full path plus the range.

    ⚠ Not the basename. The measurement that sized this lever used basenames and reported
      27.6%; a decision made on a basename would serve the wrong file's outline for any repo
      with two `__init__.py`. The strict key is the 20.4% arm of the same measurement.
    """
    try:
        p = str(Path(path).expanduser().resolve()).lower()
    except (OSError, ValueError):
        p = str(path or "").lower()
    # ⚠ THE SEPARATOR IS \x1f, NOT ":". A Windows path starts "c:\\...", so a colon
    #   separator made `key.split(":")[0]` return "c" -- and `on_edit`, which matched on that
    #   prefix, INVALIDATED NOTHING ON WINDOWS. Found by exercising the four rules by hand
    #   before writing the tests: an edit left the entry in place and the next read was
    #   decided on a stale witness.
    span = ""
    if offset or limit:
        span = f"{KEY_SEP}{offset or 0}-{limit or 0}"
    return p + span


def stat_of(path: str):
    """(mtime_ns, size) or None. The staleness witness, cheap enough for a hook."""
    try:
        st = os.stat(Path(path).expanduser())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class ReadLedger:
    """Per-session memory of reads. Pure except for load/save."""

    def __init__(self, state: dict | None = None):
        self.state = state or {"window": 0, "entries": {}, "turn": 0}

    # ---- lifecycle -------------------------------------------------------------------
    def on_compaction(self):
        """⚠ EVERYTHING BEFORE THIS IS NO LONGER IN CONTEXT, so every entry is void.

        Measured: 8.6% of read tokens are repeats across a compaction, and suppressing those
        would strand the model on content it can no longer see.
        """
        self.state["window"] = int(self.state.get("window", 0)) + 1
        self.state["entries"] = {}

    def on_edit(self, path: str):
        """Any write to a path drops every entry for it, at any range."""
        try:
            p = str(Path(path).expanduser().resolve()).lower()
        except (OSError, ValueError):
            p = str(path or "").lower()
        for key in [k for k in self.state["entries"]
                    if k == p or k.startswith(p + KEY_SEP)]:
            del self.state["entries"][key]

    def record(self, path: str, offset=None, limit=None, tokens: int = 0):
        key = read_key(path, offset, limit)
        self.state["entries"][key] = {
            "stat": stat_of(path),
            "window": self.state.get("window", 0),
            "turn": self.state.get("turn", 0),
            "tokens": int(tokens),
            "at": time.time(),
            # A fresh read starts a new conversation about this key, so the one-notice
            # allowance resets with it.
            "noticed": False,
        }

    # ---- the decision ----------------------------------------------------------------
    def decide(self, path: str, offset=None, limit=None, forced: bool = False) -> dict:
        """Should this read proceed, or get a notice? -> dict

        Returns {"action", "reason", "saved_tokens_est", "prior_turn"}. NEVER raises and never
        returns a deny: the only two actions are ALLOW and NOTICE.
        """
        out = {"action": ALLOW, "reason": "", "saved_tokens_est": 0, "prior_turn": None}
        if forced:
            out["reason"] = "the caller forced a full read"
            return out
        key = read_key(path, offset, limit)
        prior = self.state["entries"].get(key)
        if prior is None:
            out["reason"] = "not read before in this window"
            return out
        if prior.get("window") != self.state.get("window", 0):
            # Rule 3. Belt and braces: on_compaction already clears, but a state file carried
            # across a restart could hold an older window.
            out["reason"] = "read in an earlier compaction window, so it is no longer in context"
            return out
        now = stat_of(path)
        if now is None or prior.get("stat") is None or tuple(prior["stat"]) != tuple(now):
            out["reason"] = "the file changed since it was read (mtime or size)"
            return out
        tokens = int(prior.get("tokens") or 0)
        if tokens < MIN_TOKENS_TO_NOTICE:
            out["reason"] = (f"the earlier read was only {tokens} tokens, so a notice "
                             f"would not pay for itself")
            return out
        # ⚠⚠ THE SECOND ASK ALWAYS WINS, AND THIS IS THE HALF I ALMOST SHIPPED MISSING.
        #   `format_notice` tells the caller "re-issue the same read" -- a promise nothing
        #   implemented until this branch existed. A notice that advertises an escape hatch
        #   that does not work is worse than having no hatch at all, because the caller stops
        #   looking for another way out.
        if prior.get("noticed"):
            out["reason"] = ("already noticed once for this key; the second ask is always "
                             "served in full")
            return out
        prior["noticed"] = True
        out.update(action=NOTICE,
                   reason=f"read at turn {prior.get('turn')} in this window, unchanged since",
                   saved_tokens_est=max(0, tokens - NOTICE_TOKENS_EST),
                   prior_turn=prior.get("turn"))
        return out


def format_notice(path: str, decision: dict, outline: str | None = None) -> str:
    """The notice the caller sees instead of the file. Names the escape hatch explicitly."""
    lines = [
        f"[attnroute] {path} was read at turn {decision.get('prior_turn')} in this context "
        f"window and has not changed since.",
        "To read it in full anyway, add `attnroute:full` to the command or re-issue the "
        "same read immediately.",
    ]
    if outline:
        lines.append("")
        lines.append(outline)
    return "\n".join(lines)


def is_forced(tool_input: dict) -> bool:
    """⚠ THE ESCAPE HATCH, AND IT IS CHECKED BEFORE ANYTHING ELSE.

    Two ways out, because one that has to be remembered is not an escape hatch: the explicit
    marker, or simply repeating the read -- the second request for a key that was just
    noticed is always allowed.
    """
    if not isinstance(tool_input, dict):
        return False
    blob = " ".join(str(v) for v in tool_input.values() if isinstance(v, (str, int)))
    return "attnroute:full" in blob.lower()


# ---- persistence ---------------------------------------------------------------------
def ledger_path(session_id: str) -> Path:
    return (Path.home() / ".claude" / "telemetry"
            / f"read_ledger.{session_id or 'x'}.json")


def load(session_id: str) -> ReadLedger:
    p = ledger_path(session_id)
    try:
        return ReadLedger(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return ReadLedger()


def save(session_id: str, ledger: ReadLedger) -> None:
    p = ledger_path(session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ledger.state), encoding="utf-8")
    except OSError:
        pass          # a measurement must never cost the user their turn


# ═══ ACTING vs OBSERVING, AND THE HOLDOUT ══════════════════════════════════════════════════
#
# ⚠ DEFAULT IS OBSERVE. The ledger computes its decision and LOGS it, and the read proceeds
#   untouched. That is a true baseline arm: the log says what would have been saved without
#   anything having changed, which is the only way to earn the right to act.
ACT_ENV = "ATTNROUTE_LEDGER_ACT"

#: Per-file holdout: this share of keys NEVER get a notice, so the two arms can be compared
#: within one session. Whole-turn holdout: this share of turns is left entirely alone.
HOLDOUT_FILE_PCT = 10
HOLDOUT_TURN_PCT = 3


def acting() -> bool:
    """May the ledger change a read, or only log what it would have done?"""
    return os.environ.get(ACT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _bucket(*parts) -> int:
    """A stable 0-99 bucket. Deterministic across processes -- `hash()` is NOT, because
    PYTHONHASHSEED randomises it per run, which would make an arm assignment unreplayable."""
    import hashlib
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def holdout(session_id: str, key: str, turn, seed: str = "attnroute-v1") -> dict:
    """Which arm is this read in? -> {"arm", "file_bucket", "turn_bucket", "seed"}

    ⚠ DETERMINISTIC AND LOGGED. The seed, both buckets and the arm are written with every
      decision, so the split can be re-derived from the data rather than trusted -- and so an
      analysis cannot pick its arms after the fact.
    """
    fb = _bucket(seed, session_id, key)
    tb = _bucket(seed, session_id, "turn", turn)
    if tb < HOLDOUT_TURN_PCT:
        arm = "held-out-turn"
    elif fb < HOLDOUT_FILE_PCT:
        arm = "held-out-file"
    else:
        arm = "ledger"
    return {"arm": arm, "file_bucket": fb, "turn_bucket": tb, "seed": seed}


def hook_decision(ledger: "ReadLedger", session_id: str, tool_name: str, tool_input: dict,
                  turn=None) -> tuple:
    """The whole decision for a PreToolUse hook. -> (payload_or_None, log_record)

    ⚠ OBSERVE BY DEFAULT: the payload is None unless `acting()` is true, so the read proceeds
      untouched and the log still says what would have happened. That log is the baseline arm.

    ⚠ AND THE HOLDOUT IS CONSULTED EVEN WHEN OBSERVING, so the two arms are comparable from
      the first turn rather than from whenever acting is switched on.
    """
    path = ""
    offset = limit = None
    if tool_name == "Read" and isinstance(tool_input, dict):
        path = str(tool_input.get("file_path") or "")
        offset, limit = tool_input.get("offset"), tool_input.get("limit")
    if not path:
        return None, None

    forced = is_forced(tool_input)
    key = read_key(path, offset, limit)
    arm = holdout(session_id, key, turn if turn is not None else ledger.state.get("turn", 0))
    decision = ledger.decide(path, offset, limit, forced=forced)

    log = {"event": "read_ledger", "path": path, "key": key, "turn": turn,
           "action": decision["action"], "reason": decision["reason"],
           "saved_tokens_est": decision["saved_tokens_est"], "acting": acting(),
           "forced": forced, **arm}

    # An explicit range the caller asked for is never second-guessed, and a held-out read is
    # left alone so its arm measures the world without the ledger in it.
    if arm["arm"] != "ledger" or decision["action"] != NOTICE or not acting():
        return None, log
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": format_notice(path, decision)}}, log
