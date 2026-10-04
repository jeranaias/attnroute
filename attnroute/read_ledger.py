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

1. NO DENY THAT CANNOT BE UNDONE ON THE SPOT. Acting DOES deny the tool call -- that is the
   only mechanism that removes the tokens, because `additionalContext` ADDS text and lets the
   read run, which costs 40 tokens and saves none. So every deny carries its own way out in
   `permissionDecisionReason`, and the SECOND ask for the same key is always served in full.
   A hook that strands the model is worse than a re-read; a hook that only annotates it is
   worse than nothing.
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

    WARNING: A RANGE IS PART OF THE KEY, SO AN IDENTICAL RANGED REPEAT *IS* NOTICED. An
      earlier comment in `hook_decision` claimed an explicit range was "never
      second-guessed"; it is not exempt, it is keyed. Reading lines 1-50 twice, unchanged,
      is the same repeat as reading the whole file twice. A DIFFERENT range is a different
      key and is always allowed.
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
        self.state.setdefault("shadow", {})

    # ---- observing must follow the same trajectory as acting -------------------------
    def note_shadow_deny(self, key: str):
        """WARNING: WITHOUT THIS, OBSERVE MODE OVER-REPORTS BY UP TO 2x.

        When acting, a NOTICE denies the call: the read never happens, so nothing re-records
        the key and the one-notice allowance stays spent. When observing, the read DOES
        happen, PostToolUse records it afresh, the allowance resets -- and the next repeat
        logs another NOTICE. The log would then claim a saving on reads that acting mode
        could never have suppressed.

        So while observing, a key that WOULD have been denied is marked here, and the
        PostToolUse that follows declines to record it. The two arms then diverge only in
        what the model sees, which is the thing being measured.
        """
        self.state.setdefault("shadow", {})[key] = self.state.get("turn", 0)

    def take_shadow_deny(self, key: str) -> bool:
        """Was this key shadow-denied? Clears the mark; True means do not record."""
        return self.state.setdefault("shadow", {}).pop(key, None) is not None

    # ---- lifecycle -------------------------------------------------------------------
    def on_compaction(self):
        """⚠ EVERYTHING BEFORE THIS IS NO LONGER IN CONTEXT, so every entry is void.

        Measured: 8.6% of read tokens are repeats across a compaction, and suppressing those
        would strand the model on content it can no longer see.
        """
        self.state["window"] = int(self.state.get("window", 0)) + 1
        self.state["entries"] = {}
        self.state["shadow"] = {}

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

        Returns {"action", "reason", "saved_tokens_est", "prior_turn"}. NEVER raises. The two
        actions are ALLOW and NOTICE; NOTICE becomes a PreToolUse deny in `hook_decision`,
        which the next identical request overrides.
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
        # WARNING: A DENY READS AS A USER REFUSAL, AND THE MODEL IS TOLD TO TREAT IT THAT
        #   WAY. Claude Code's own instructions say "a denied call means the user declined
        #   it -- adjust, don't retry verbatim", which is the OPPOSITE of what this notice
        #   needs: the whole escape hatch is re-issuing the identical read. So the first
        #   words have to disown the user attribution, or the model will adjust around a
        #   refusal nobody made -- and may ask the user why their read was blocked.
        "NOT A USER DENIAL -- this is attnroute, an automatic token-saving hook.",
        f"{path} was read at turn {decision.get('prior_turn')} in this context window and "
        f"has not changed since, so its content is already above in this conversation.",
        # It used to say "add attnroute:full to the command". Read has no command, so for
        # the one tool this fires on, the instruction named a field that does not exist.
        "If you need it again in full, RE-ISSUE THE IDENTICAL READ and it will be served. "
        "`attnroute:full` anywhere in the input also forces a full read. There is no need "
        "to ask the user about this, and nothing is blocked.",
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
def ledger_id(payload: dict) -> str:
    """WARNING: THE LEDGER IS PER CONVERSATION, AND A SUBAGENT IS A DIFFERENT CONVERSATION.

    Subagent hook events carry the PARENT's `session_id` plus their own `agent_id`. Keyed on
    session alone, a subagent's read would make the MAIN loop get a notice for content that
    only ever existed in the subagent's context -- a notice about a read the recipient never
    saw, which is the exact failure this module exists to avoid.

    `agent_id` is absent for the main loop and present for a subagent; the installed CLI's
    own built-in PostToolUse hook distinguishes them the same way (`agent_id !== undefined`).
    """
    if not isinstance(payload, dict):
        return "x"
    sid = str(payload.get("session_id") or "x")
    aid = payload.get("agent_id")
    return sid if aid in (None, "") else f"{sid}.agent-{aid}"


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
    """WARNING: TEMP + RENAME, because parallel tool calls run this concurrently.

    Several PreToolUse and PostToolUse hooks for one assistant turn can be in flight at once.
    A plain `write_text` can be observed half-written by another process, and `load` would
    then fall back to an EMPTY ledger -- losing the window silently, which looks exactly like
    a ledger that simply never fires. `os.replace` is atomic on POSIX and on Windows, so a
    reader sees either the old file or the new one.

    The remaining race is last-writer-wins on the in-memory copy: two concurrent reads of
    different files can each save a state missing the other's entry. That costs a MISSED
    notice, never a wrong one, so it is left unlocked.
    """
    p = ledger_path(session_id)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(ledger.state), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        # A measurement must never cost the user their turn.
        try:
            tmp.unlink()
        except OSError:
            pass


# ═══ ACTING vs OBSERVING, AND THE HOLDOUT ══════════════════════════════════════════════════
#
# ⚠ DEFAULT IS OBSERVE. The ledger computes its decision and LOGS it, and the read proceeds
#   untouched. That is a true baseline arm: the log says what would have been saved without
#   anything having changed, which is the only way to earn the right to act.
ACT_ENV = "ATTNROUTE_LEDGER_ACT"

#: Per-file holdout: this share of keys NEVER get a notice, so the two arms can be compared
#: within one session. Whole-turn holdout: this share of turns is left entirely alone.
#:
#: These are the STEADY-STATE defaults. 10% is right once a lever is established and wrong for
#: a trial: equal allocation maximises power, and the measured decision rate for this lever is
#: 1.9 repeat Reads per window, so a 10% control arm takes about 257 windows to reach 50
#: control decisions. Hence the environment overrides below -- a trial runs at 50% without a
#: code fork, and drops back afterwards.
HOLDOUT_FILE_PCT = 10
HOLDOUT_TURN_PCT = 3

#: Overrides, read per decision so a change takes effect without a restart.
FILE_HOLDOUT_ENV = "ATTNROUTE_LEDGER_HOLDOUT_PCT"
TURN_HOLDOUT_ENV = "ATTNROUTE_LEDGER_TURN_HOLDOUT_PCT"


def acting() -> bool:
    """May the ledger change a read, or only log what it would have done?"""
    return os.environ.get(ACT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def holdout_pct(env_name: str, default: int) -> tuple:
    """A holdout percentage from the environment. -> (pct, why)

    WARNING: A BAD VALUE DOES NOT SILENTLY BECOME THE DEFAULT. `why` is non-empty whenever
    the environment said something that could not be used, and the caller puts it in the
    telemetry record. A trial that ran at 10% because of a typo, while everyone believed it
    ran at 50%, would produce a control arm five times smaller than the analysis assumes --
    and nothing would look wrong.

    0 and 100 are both allowed: 0 turns the holdout off (no control arm, so no contrast),
    and 100 holds everything out (the lever observes but never acts, which is a useful way
    to measure the decision rate without changing anything).
    """
    raw = os.environ.get(env_name)
    if raw is None or not str(raw).strip():
        return default, ""
    text = str(raw).strip().rstrip("%")
    try:
        value = int(text)
    except ValueError:
        return default, f"{env_name}={raw!r} is not a whole number; using {default}"
    if not 0 <= value <= 100:
        return default, f"{env_name}={raw!r} is not a percentage; using {default}"
    return value, ""


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
    file_pct, file_why = holdout_pct(FILE_HOLDOUT_ENV, HOLDOUT_FILE_PCT)
    turn_pct, turn_why = holdout_pct(TURN_HOLDOUT_ENV, HOLDOUT_TURN_PCT)
    fb = _bucket(seed, session_id, key)
    tb = _bucket(seed, session_id, "turn", turn)
    if tb < turn_pct:
        arm = "held-out-turn"
    elif fb < file_pct:
        arm = "held-out-file"
    else:
        arm = "ledger"
    # ⚠ THE ALLOCATION IS LOGGED WITH EVERY DECISION. An arm without the percentage it was
    #   drawn at is not a measurement: an analysis spanning a 10% period and a 50% period
    #   has to be able to tell them apart, and "the trial ran at 50%" is a claim somebody
    #   remembers rather than something the data says.
    out = {"arm": arm, "file_bucket": fb, "turn_bucket": tb, "seed": seed,
           "file_holdout_pct": file_pct, "turn_holdout_pct": turn_pct}
    why = "; ".join(w for w in (file_why, turn_why) if w)
    if why:
        out["holdout_env_ignored"] = why
    return out


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

    # A held-out read is left alone so its arm measures the world without the ledger in it.
    if arm["arm"] != "ledger" or decision["action"] != NOTICE or not acting():
        return None, log

    # WARNING: DENY, NOT additionalContext. THIS WAS THE WHOLE POINT AND I HAD IT WRONG.
    #   `additionalContext` on PreToolUse APPENDS text and the tool still runs: the file is
    #   read anyway and the notice costs 40 tokens on top. Acting that way would have made
    #   every "saved_tokens_est" in the observe-mode logs a fiction. Only
    #   `permissionDecision: "deny"` stops the call, and the reason string is what the model
    #   sees -- so the reason IS the notice, escape hatch and all. Verified against the hook
    #   contract in the installed CLI: PreToolUse takes allow|deny|ask plus
    #   permissionDecisionReason.
    log["mechanism"] = "deny"
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": format_notice(path, decision)}}, log


# ═══ THE WIRING: WHICH HOOK EVENT DOES WHAT ════════════════════════════════════════════
#
# WARNING: UNWIRED, THIS MODULE IS INERT AND ITS OBSERVE LOG IS EMPTY -- which reads exactly
#   like a lever that does not pay. `record`, `on_edit`, `on_compaction` and the turn counter
#   are each driven by a different event, and all five registrations are needed:
#
#     PreToolUse   Read                      -> decide (and deny, when acting)
#     PostToolUse  Read                      -> record, with tokens from the result
#     PostToolUse  Edit|Write|NotebookEdit   -> on_edit
#     SessionStart source=compact / PreCompact -> on_compaction
#     Stop                                   -> turn += 1
#
# The turn counter lives on Stop because that is the one event that fires once per assistant
# turn. Counting tool calls instead would make "read at turn 9" mean something the transcript
# does not say.
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")

#: Where the observe-mode log lands, one JSON object per decision.
LOG_NAME = "read_ledger.jsonl"


def response_tokens(payload: dict) -> int:
    """How big was the read that just happened? Estimated from the result text.

    The exact number only has to be good enough to compare against MIN_TOKENS_TO_NOTICE and
    to total a saving, and it is recorded as an ESTIMATE in the log for that reason.
    """
    r = payload.get("tool_response") if isinstance(payload, dict) else None
    text = ""
    if isinstance(r, str):
        text = r
    elif isinstance(r, dict):
        for k in ("file", "content", "stdout", "text"):
            v = r.get(k)
            if isinstance(v, str):
                text += v
            elif isinstance(v, dict) and isinstance(v.get("content"), str):
                text += v["content"]
    elif isinstance(r, list):
        text = "".join(x.get("text", "") for x in r if isinstance(x, dict))
    try:
        from attnroute.telemetry_lib import estimate_tokens_from_chars
        return int(estimate_tokens_from_chars(len(text), kind="code"))
    except Exception:
        return max(0, len(text) // 4)


def compactions_since(transcript_path, offset):
    """(count, new_offset): compaction markers appended to the transcript since `offset`.

    WARNING: THE SECOND WITNESS FOR RULE 3, AND IT EXISTS BECAUSE THE FIRST ONE CAN MISS.
      `on_compaction` depends on a SessionStart:compact or PreCompact hook firing. If that
      registration is absent, fails, or the compaction happens in a way that does not reach
      it, every entry survives into a window that no longer holds the content -- and then the
      ledger suppresses reads of material the model genuinely cannot see. That is the worst
      failure this module has, so it does not rest on one signal.

      Compaction APPENDS a summary entry to the same transcript rather than truncating it, so
      the witness is the marker, not the file size. Scanning starts at the stored offset, so
      the cost is the bytes written since the last tool call.

      On the FIRST call there is no stored offset and the scan would see markers from earlier
      in the session, so it only records the offset and reports none. The ledger is empty at
      that point anyway, so there is nothing to protect.
    """
    try:
        from attnroute.turn_cost import read_span
        from pathlib import Path as _P
        p = _P(str(transcript_path))
        if not p.is_file():
            return 0, offset
        lines, new_offset, complete = read_span(p, offset)
        if not complete:
            return 0, new_offset          # first look: establish the offset, claim nothing
        n = 0
        for line in lines:
            if '"isCompactSummary":true' in line.replace(" ", "") or '"compactMetadata"' in line:
                n += 1
        return n, new_offset
    except Exception:
        return 0, offset


def _witness(ledger: "ReadLedger", payload: dict) -> int:
    """Catch a compaction the compact hook did not report. Returns how many it caught."""
    n, new_offset = compactions_since(payload.get("transcript_path"),
                                      ledger.state.get("transcript_offset"))
    ledger.state["transcript_offset"] = new_offset
    for _ in range(n):
        ledger.on_compaction()
    if n:
        ledger.state["missed_compactions"] = int(
            ledger.state.get("missed_compactions", 0)) + n
    return n


_T0 = None   # set by main(); lets every record carry the hook's own latency


def _log(record: dict) -> None:
    if _T0 is not None:
        record["latency_ms"] = round((time.perf_counter() - _T0) * 1000.0, 2)
    try:
        from attnroute.telemetry_stream import emit
        r = dict(record)
        emit("read_ledger", r.pop("event", "?"), session_id=r.pop("session_id", None),
             agent_id=r.pop("agent_id", None), arm=r.pop("arm", None),
             acting=r.pop("acting", None), **r)
    except Exception:
        pass
    try:
        d = Path.home() / ".claude" / "telemetry"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass          # a measurement must never cost the user their turn


def handle(payload: dict) -> dict | None:
    """One hook event -> the payload to print, or None. Pure apart from load/save."""
    event = str(payload.get("hook_event_name") or "")
    sid = ledger_id(payload)
    led = load(sid)
    out = None

    if event == "SessionStart":
        if str(payload.get("source") or "") == "compact":
            led.on_compaction()
    elif event == "PreCompact":
        led.on_compaction()
    elif event == "Stop":
        led.state["turn"] = int(led.state.get("turn", 0)) + 1
    elif event == "PreToolUse":
        caught = _witness(led, payload)
        out, log = hook_decision(led, sid, str(payload.get("tool_name") or ""),
                                 payload.get("tool_input") or {},
                                 turn=led.state.get("turn", 0))
        if log:
            log["missed_compactions_caught"] = caught
            log["ledger_id"] = sid
            log["session_id"] = payload.get("session_id")
            log["agent_id"] = payload.get("agent_id")
            if log["action"] == NOTICE and not acting() and log.get("arm") == "ledger":
                led.note_shadow_deny(log["key"])
            _log(log)
    elif event == "PostToolUse":
        tool = str(payload.get("tool_name") or "")
        ti = payload.get("tool_input") or {}
        if not isinstance(ti, dict):
            ti = {}
        if tool in EDIT_TOOLS:
            led.on_edit(str(ti.get("file_path") or ti.get("notebook_path") or ""))
        elif tool == "Read" and ti.get("file_path"):
            key = read_key(str(ti["file_path"]), ti.get("offset"), ti.get("limit"))
            tokens = response_tokens(payload)
            # The MEASURED side of the arm contrast: what each read actually put into context,
            # tagged with the arm it was decided in. Reads the ledger denied never get here.
            _log({"event": "read_result", "key": key, "tokens": tokens, "acting": acting(),
                  "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
                  **holdout(sid, key, led.state.get("turn", 0))})
            if not led.take_shadow_deny(key):
                led.record(str(ti["file_path"]), ti.get("offset"), ti.get("limit"),
                           tokens=tokens)

    save(sid, led)
    return out


def main(argv=None) -> int:
    """Hook entry point. NEVER fails the caller's turn: every error exits 0 and prints
    nothing, because a lost notice costs tokens and a raised exception costs the turn."""
    import sys

    global _T0
    _T0 = time.perf_counter()
    from attnroute.no_egress import lock_down
    lock_down()                 # a hook makes no network call, ever
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle(payload)
    except Exception as exc:                       # noqa: BLE001 - see the docstring
        _log({"event": "read_ledger_error", "error": repr(exc)[:300]})
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":        # pragma: no cover - exercised through main()
    raise SystemExit(main())
