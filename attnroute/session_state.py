"""The per-session half of the state file: what this session knows that a summary loses.

WHY THIS IS THE LEVER. Replaying 59 real compaction windows and 62,790 turns: cutting each
window to half its length removes 42.5% of `cache_read`, which at 0.1x is 79.3% of the
cost-weighted bill -- about 33.7% of the whole bill. Nothing else measured comes close. The
read ledger is ~1.6%; the per-turn floor is ~5%.

The reason sessions do not already compact more often is that compaction loses things. So
the saving is not in compacting; it is in making compaction SAFE. That is this module.

═══ WHAT IT DOES NOT DO, AND WHY ═══════════════════════════════════════════════════════

IT DOES NOT GUESS WHAT A RULING WAS. The obvious design reads the transcript and infers
"decisions" from the assistant's prose. That is the defect this project keeps finding in
other people's code: a check that answers a narrower question than the one asked while
looking authoritative. A keyword-matched "ruling" is a guess with a confident face, and a
handback built from guesses is worse than no handback, because the next window would act on
it.

So the state has two layers with different epistemic status, and they are kept apart:

  * EXPLICIT NOTES, written by an agent on purpose (`attnroute note add --kind ruling`).
    These are the rulings. Nothing infers them.
  * DERIVED FACTS, extracted from the transcript, and ONLY things the transcript states
    outright: which files were edited, what the last test result said, which commits and
    pushes happened. No intent, no summarising -- just what is already recorded.

A handback says which layer each line came from, so the next window can tell a thing
somebody decided from a thing a program noticed.

═══ THE BUDGET IS A PROMISE, NOT A HOPE ════════════════════════════════════════════════

1,500 tokens, enforced by measuring, in a fixed priority order, with what was dropped
REPORTED in the handback itself. A handback that silently truncates is how a window loses
the one ruling it needed while believing it has everything.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

#: The handback ceiling. Everything above this is dropped, and the drop is stated.
TOKEN_BUDGET = 1500

#: What a note can be. A kind is not decoration: it sets priority in the handback, because
#: an un-promoted ruling is the thing a summary must not lose, and a stray measurement is
#: not.
KINDS = ("ruling", "decision", "blocker", "measurement", "next", "note")

#: Priority order for the budget. Un-promoted rulings first; see `handback`.
KIND_PRIORITY = {"ruling": 0, "decision": 1, "blocker": 2, "next": 3, "measurement": 4,
                 "note": 5}

#: Notes older than this stop being handed back. A window does not need last week's plan,
#: and the budget is better spent on what is current.
NOTE_MAX_AGE_HOURS = 72.0

STATE_VERSION = 1

EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")

#: Lines in a tool result that state a fact worth carrying. Deliberately narrow: each one
#: is a thing the transcript says in so many words, not a thing inferred from prose.
TEST_RESULT = re.compile(r"\b(\d+) (passed|failed)[^\n]{0,80}", re.I)
LINT_RESULT = re.compile(r"(All checks passed!|Found \d+ error)", re.I)


def estimate_tokens(text: str) -> int:
    """Token count, exact when tiktoken is available and an estimate otherwise.

    The estimate is labelled as one wherever it reaches the handback, because a budget
    enforced with an unlabelled guess is a budget nobody can check.
    """
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(
            text or "", disallowed_special=()))
    except Exception:
        return max(0, len(text or "") // 4)


#: The variable the CLI actually exports into a tool's environment. VERIFIED, not assumed:
#: in a Bash tool call under 2.1.280, `CLAUDE_SESSION_ID` is unset and
#: `CLAUDE_CODE_SESSION_ID` holds the id. The shorter name survives only as a fallback,
#: because it appears in the binary as a template placeholder and may be a documented alias
#: somewhere this was not tested.
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def session_from_env(env=None) -> dict:
    """Which session is this? -> {"session", "var", "why"}; `session` is None when unknown.

    WARNING: THERE IS NO DEFAULT, AND THAT IS THE POINT. An earlier version fell back to a
    session literally named "local" when the variable was missing -- which is exactly what
    happened, because it was reading the wrong variable name. Notes went to "local", the
    handback read the real session id, and the two never met. Nothing failed; the lever
    simply did nothing, which is the worst way for a measurement tool to be broken.

    A caller that cannot name its session is REFUSED. A refusal is visible; a wrong default
    is not.

    SUBAGENTS, deliberately: if a subagent's Bash carries the PARENT's id, its notes land on
    the parent's key -- and that is wanted here, unlike in the read ledger. The ledger is
    about what is in a context window, so a subagent's reads must not affect the parent's.
    A RULING is about the work, and the parent is who carries the work on. A ruling made
    inside a subagent that vanished with the subagent would be the bug, not the feature.
    """
    import os as _os

    env = env if env is not None else _os.environ
    for name in SESSION_ENV:
        value = str(env.get(name) or "").strip()
        if value:
            return {"session": value, "var": name, "why": ""}
    return {"session": None, "var": None,
            "why": ("no session id in the environment: looked for "
                    + " and ".join(SESSION_ENV)
                    + ". Pass --session explicitly. (Refusing rather than defaulting: a "
                      "note filed under the wrong session is never handed back, and "
                      "nothing would look broken.)")}


def state_path(session_id: str) -> Path:
    """Under the home directory. NEVER in a working tree -- see context_router's
    `get_state_file` for what happened the last time runtime state lived in a repo."""
    return (Path.home() / ".claude" / "attn_state" / "sessions"
            / f"{session_id or 'x'}.json")


def empty_state(session_id: str = "") -> dict:
    return {"version": STATE_VERSION, "session": session_id, "notes": [],
            "handed_back": [], "transcript_offset": None, "window": 0,
            "facts": {}, "handbacks": 0}


def load(session_id: str) -> dict:
    try:
        state = json.loads(state_path(session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return empty_state(session_id)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        # A state file from another version is DISCARDED rather than guessed at. The cost
        # is one window's notes; the cost of the alternative is acting on a field that no
        # longer means what it did.
        return empty_state(session_id)
    for key, value in empty_state(session_id).items():
        state.setdefault(key, value)
    return state


def save(session_id: str, state: dict) -> None:
    """Temp + `os.replace`, because several hooks for one turn run at once."""
    path = state_path(session_id)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---- the explicit layer --------------------------------------------------------------
def add_note(state: dict, text: str, kind: str = "note", promoted_to: str | None = None,
             source: str | None = None) -> dict:
    """Record something this session decided. -> the note

    `promoted_to` is the path where this now lives permanently -- an ADR, a tracker row, a
    docstring. A note WITHOUT one is the interesting case: it exists only in a context
    window, and is exactly what compaction loses.
    """
    kind = kind if kind in KINDS else "note"
    note = {"id": f"n{len(state.get('notes', [])) + 1}-{int(time.time())}",
            "kind": kind, "text": " ".join(str(text).split()),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "promoted_to": promoted_to or None, "source": source or None,
            "window": state.get("window", 0)}
    state.setdefault("notes", []).append(note)
    return note


def check_promotion(note: dict, repo: Path | str = ".") -> dict:
    """Is this note where it claims to be? -> {"state", "why"}

    WARNING: A CLAIM IS NOT A CHECK. "promoted_to: docs/decisions/0023.md" is a sentence an
    agent typed; whether the file exists, and whether it actually says anything about this
    note, is a different question. Both are answered here, and the three answers are kept
    distinct:

        PROMOTED      the file exists and contains the note's distinctive words
        CLAIMED       the file exists but does not mention it -- the claim is unsupported
        UNPROMOTED    no claim at all; this lives only in a context window

    CLAIMED is the dangerous state, because it looks like PROMOTED in any listing that only
    checks whether the field is set.
    """
    target = note.get("promoted_to")
    if not target:
        return {"state": "UNPROMOTED", "why": "exists only in a context window"}
    path = Path(repo) / target
    try:
        if not path.is_file():
            return {"state": "CLAIMED", "why": f"{target} does not exist"}
        body = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        return {"state": "CLAIMED", "why": f"{target} could not be read: {exc!r}"}

    # The distinctive words of the note, not the whole sentence: a file rarely repeats a
    # note verbatim, but it does carry its nouns.
    words = [w for w in re.findall(r"[a-z0-9_./-]{4,}", note.get("text", "").lower())
             if w not in ("that", "this", "with", "from", "have", "been", "will", "must")]
    if not words:
        return {"state": "CLAIMED", "why": "the note has no distinctive words to look for"}
    hits = sum(1 for w in words[:12] if w in body)
    if hits >= max(2, min(3, len(words))):
        return {"state": "PROMOTED", "why": f"{target} mentions {hits} of its terms"}
    return {"state": "CLAIMED",
            "why": f"{target} exists but mentions only {hits} of the note's terms"}

# ---- the derived layer: only what the transcript states outright ---------------------
def derive_facts(state: dict, transcript_path) -> dict:
    """Update `state["facts"]` from the transcript written since the last look.

    ONLY THINGS ALREADY WRITTEN DOWN. Which files were edited, what the last test run
    printed, which commits and pushes happened. No intent, no summarising, nothing that
    needs a judgement -- because a derived fact is handed to the next window as a fact, and
    a guess handed over as a fact is how a session acts on something nobody decided.

    Scans forward from a stored byte offset, so the cost is the bytes written since the
    previous turn rather than the whole transcript.
    """
    facts = state.setdefault("facts", {})
    edited = facts.setdefault("edited", {})
    commands = facts.setdefault("commands", [])
    #: Tool-call ids already counted. Bounded: the only ones that can overlap a future
    #: span are the recent ones.
    seen_calls = facts.setdefault("seen_calls", [])

    try:
        from attnroute.turn_cost import read_span
        path = Path(str(transcript_path or ""))
        if not path.is_file():
            return facts
        lines, new_offset, complete = read_span(path, state.get("transcript_offset"))
        state["transcript_offset"] = new_offset
        facts["view_complete"] = bool(complete)
    except Exception as exc:                     # noqa: BLE001 - never cost the user a turn
        facts["derive_error"] = repr(exc)[:200]
        return facts

    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") or entry.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                # WARNING: COUNT EACH CALL ONCE, BY ITS ID. A span can overlap a previous
                #   one -- a rewound offset, a rotated transcript, two hooks racing -- and
                #   an accumulating counter then reports work that happened once as having
                #   happened twice. A test caught it reporting 4 edits for 2. The id is
                #   already unique per tool call, so the fix is a membership check.
                call_id = str(block.get("id") or "")
                if call_id and call_id in seen_calls:
                    continue
                if call_id:
                    seen_calls.append(call_id)
                name = block.get("name", "")
                data = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name in EDIT_TOOLS:
                    target = str(data.get("file_path") or data.get("notebook_path") or "")
                    if target:
                        edited[target] = int(edited.get(target, 0)) + 1
                elif name == "Bash":
                    command = str(data.get("command") or "")
                    # Two kinds of command change the world rather than observing it, and
                    # those are the two the next window needs to know happened.
                    if re.search(r"\bgit (commit|push|merge|rebase)\b", command):
                        commands.append(" ".join(command.split())[:160])
            elif block.get("type") == "tool_result":
                body = block.get("content")
                text = body if isinstance(body, str) else "".join(
                    x.get("text", "") for x in body if isinstance(x, dict)
                ) if isinstance(body, list) else ""
                if not text:
                    continue
                found = TEST_RESULT.search(text)
                if found:
                    facts["last_test"] = found.group(0).strip()[:120]
                found = LINT_RESULT.search(text)
                if found:
                    facts["last_lint"] = found.group(1)

    # Bounded: the handback has 1,500 tokens, so keeping more than this helps nobody.
    facts["commands"] = commands[-8:]
    facts["seen_calls"] = seen_calls[-500:]
    if len(edited) > 40:
        facts["edited"] = dict(sorted(edited.items(), key=lambda kv: -kv[1])[:40])
    facts["derived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return facts


def _age_hours(stamp: str) -> float | None:
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(stamp)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _note_lines(state: dict, repo) -> list:
    """(priority, token_cost_hint, line, note_id) for every note worth handing back."""
    handed = set(state.get("handed_back") or [])
    out = []
    for note in state.get("notes") or []:
        age = _age_hours(note.get("at", ""))
        if age is not None and age > NOTE_MAX_AGE_HOURS:
            continue
        promotion = check_promotion(note, repo)
        live = promotion["state"] in ("UNPROMOTED", "CLAIMED")

        # ⚠ "DELTAS ONLY" CANNOT MEAN "ONLY WHAT IS NEW", AND THIS IS THE CORRECTION THAT
        #   MATTERS. A note handed back into window 2 is gone again by window 3 -- the
        #   handback was part of window 2's context, and window 3 inherits a summary of it.
        #   So anything still LIVE is repeated every window; only notes that have been
        #   handed back AND are genuinely written down somewhere drop out. Repeating an
        #   un-promoted ruling forever is the point, not a leak.
        if note["id"] in handed and not live:
            continue

        flag = {"UNPROMOTED": "",
                "CLAIMED": f"  [claimed: {promotion['why']}]",
                "PROMOTED": f"  [in {note.get('promoted_to')}]"}[promotion["state"]]
        when = "" if age is None else f"{age:.0f}h ago"
        line = f"- {note['kind'].upper()}: {note['text']}  ({when}){flag}"
        out.append((KIND_PRIORITY.get(note["kind"], 9), line, note["id"]))
    out.sort(key=lambda item: item[0])
    return out


def handback(state: dict, board_rows: dict | None = None, repo: Path | str = ".",
             budget: int = TOKEN_BUDGET) -> dict:
    """What to hand the next context window. -> {"text", "tokens", "included", "dropped"}

    Priority order, because the budget WILL bind on a long session: live rulings, then
    decisions and blockers, then the lead's row from the board, then this team's row, then
    the derived facts, then anything else.

    Whatever does not fit is COUNTED AND STATED in the text. A handback that truncates
    silently is how a window loses the one ruling it needed while believing it has
    everything.
    """
    board_rows = board_rows or {}
    header = [
        "[attnroute] state carried over from the previous context window.",
        "EXPLICIT lines were written down on purpose by this session. DERIVED lines were "
        "read out of the transcript. Nothing here is inferred from prose.",
    ]
    text = "\n".join(header)
    included, dropped = [], 0

    # WARNING: RESERVE ROOM FOR THE DROP NOTICE. A test caught this at 1,538 tokens
    #   against a 1,500 budget: every content line was checked, and then the "N items did
    #   not fit" line was appended unchecked. The one line whose whole job is to report the
    #   budget was the line that broke it.
    reserve = estimate_tokens(
        f"- 9999 further item(s) did not fit the {budget}-token handback budget and "
        f"were NOT carried over. `attnroute state show` lists them all.") + 4
    content_budget = max(0, budget - reserve)

    def fits(candidate: str) -> bool:
        return estimate_tokens(text + "\n" + candidate) <= content_budget

    # 1. the explicit layer
    note_lines = _note_lines(state, repo)
    if note_lines:
        block = "\nEXPLICIT -- decided by this session:"
        if fits(block):
            text += block
    for _, line, note_id in note_lines:
        if fits(line):
            text += "\n" + line
            included.append(note_id)
        else:
            dropped += 1

    # 2. the board: reference, do not copy. A row can be long, and the next window can read
    #    it in full with `attnroute board get` if it needs to.
    for name, row in board_rows.items():
        if not row or not row.get("text"):
            continue
        first = next((ln for ln in row["text"].splitlines() if ln.strip()), "")
        age = row.get("age_hours")
        stale = f"  [STALE: {row.get('why')}]" if row.get("stale") else ""
        line = (f"- BOARD {name}: {first[:160]}"
                f"  ({'?' if age is None else f'{age:.0f}h'} old, "
                f"`attnroute board get --team {name}`){stale}")
        if fits(line):
            text += "\n" + line
        else:
            dropped += 1

    # 3. the derived layer
    facts = state.get("facts") or {}
    derived = []
    edited = facts.get("edited") or {}
    if edited:
        top = sorted(edited.items(), key=lambda kv: -kv[1])[:8]
        derived.append("- DERIVED edited this session: "
                       + ", ".join(f"{p} x{n}" for p, n in top)
                       + (f" (+{len(edited) - len(top)} more)" if len(edited) > len(top)
                          else ""))
    if facts.get("last_test"):
        derived.append(f"- DERIVED last test result seen: {facts['last_test']}")
    if facts.get("last_lint"):
        derived.append(f"- DERIVED last lint result seen: {facts['last_lint']}")
    if facts.get("commands"):
        derived.append("- DERIVED commits/pushes: "
                       + "; ".join(facts["commands"][-4:]))
    if facts.get("view_complete") is False:
        derived.append("- DERIVED ⚠ the transcript view was INCOMPLETE, so the facts above "
                       "may be missing earlier work in this window")
    for line in derived:
        if fits(line):
            text += "\n" + line
        else:
            dropped += 1

    if dropped:
        text += (f"\n- ⚠ {dropped} further item(s) did not fit the "
                 f"{budget}-token handback budget and were NOT carried over. "
                 f"`attnroute state show` lists them all.")

    return {"text": text, "tokens": estimate_tokens(text), "included": included,
            "dropped": dropped, "budget": budget}


def mark_handed_back(state: dict, included: list) -> None:
    """Remember what was carried over, so the next handback is a delta where it can be."""
    handed = set(state.get("handed_back") or [])
    handed.update(included or [])
    state["handed_back"] = sorted(handed)
    state["handbacks"] = int(state.get("handbacks", 0)) + 1

# ═══ THE WIRING ════════════════════════════════════════════════════════════════════════
#
#     PreCompact                  -> derive the facts one last time, close the window
#     SessionStart source=compact -> HAND BACK, as additionalContext
#     Stop                        -> derive the facts; nudge, but only when one is owed
#
# WARNING: THE NUDGE IS ON Stop, NOT TaskCompleted. TaskCompleted exists as an input event,
#   but the installed CLI declares no `hookEventName:"TaskCompleted"` OUTPUT schema, so
#   there is no documented channel to say anything back on it. `Stop` declares
#   `additionalContext`, described as non-error feedback delivered to the model. Wiring the
#   nudge to an event that cannot answer would have looked correct and done nothing.

#: A nudge costs tokens on a turn that may not need one, so it fires only when the session
#: has plainly done something worth recording and has recorded nothing.
NUDGE_AFTER_EDITS = 6

#: And never more often than this many turns apart, measured in Stop events.
NUDGE_EVERY_TURNS = 25


def nudge_due(state: dict) -> dict:
    """Should this Stop carry a reminder? -> {"due", "why"}

    The condition is MEASURED, not periodic: real work has happened in this window --
    several edits, or a commit -- and no note has been written in it. A reminder that fires
    on a timer is noise, and noise on every turn is the thing this whole project is trying
    to remove.
    """
    facts = state.get("facts") or {}
    edits = sum(int(n) for n in (facts.get("edited") or {}).values())
    commits = len(facts.get("commands") or [])
    window = state.get("window", 0)
    noted_here = any(n.get("window") == window for n in (state.get("notes") or []))
    turns = int(state.get("turns_since_nudge", 0))

    if noted_here:
        return {"due": False, "why": "a note has already been written in this window"}
    if edits < NUDGE_AFTER_EDITS and commits == 0:
        return {"due": False,
                "why": f"only {edits} edit(s) and {commits} commit(s) so far"}
    if turns < NUDGE_EVERY_TURNS:
        return {"due": False, "why": f"nudged {turns} turn(s) ago"}
    return {"due": True, "why": f"{edits} edit(s), {commits} commit(s), no note yet"}


NUDGE_TEXT = (
    "[attnroute] This window has {edits} edit(s) and no recorded ruling. Anything you have "
    "DECIDED -- a ruling, a trade-off, a figure you withdrew -- is lost at the next "
    "compaction unless it is written down. One line is enough:\n"
    "  attnroute note add --kind ruling \"<the ruling>\" [--promoted-to <path>]\n"
    "Nothing infers these from your prose, by design."
)


def _promotion_counts(state: dict, repo) -> dict:
    """How many notes are PROMOTED / CLAIMED / UNPROMOTED right now."""
    counts = {"PROMOTED": 0, "CLAIMED": 0, "UNPROMOTED": 0}
    for n in state.get("notes") or []:
        try:
            counts[check_promotion(n, repo)["state"]] += 1
        except Exception:                        # noqa: BLE001
            pass
    return counts


def _emit(event: str, payload: dict, **fields) -> None:
    """One record on the shared telemetry stream. Best-effort: never costs the turn."""
    try:
        from attnroute.telemetry_stream import emit
        emit("session_state", event, session_id=payload.get("session_id"),
             agent_id=payload.get("agent_id"), arm=None, acting=True, **fields)
    except Exception:                            # noqa: BLE001
        pass


def hook(payload: dict, repo: Path | str = ".") -> dict | None:
    """One hook event -> the payload to print, or None. Never raises on its own account."""
    event = str(payload.get("hook_event_name") or "")
    session = str(payload.get("session_id") or "")
    # A subagent is a different conversation with different state: keyed like the read
    # ledger, for the same reason.
    agent = payload.get("agent_id")
    key = session if agent in (None, "") else f"{session}.agent-{agent}"
    state = load(key)
    out = None

    if event == "PreCompact":
        derive_facts(state, payload.get("transcript_path"))
        state["window"] = int(state.get("window", 0)) + 1

    elif event == "SessionStart" and str(payload.get("source") or "") == "compact":
        rows = {}
        try:
            from attnroute.board import LEAD_FILE, read as board_read
            team = os.environ.get("ATTNROUTE_TEAM", "").strip()
            # The lead's row always; this team's row when the session says who it is.
            # Reading is local-ref only, so this costs no network -- see board.read.
            rows[LEAD_FILE] = board_read(LEAD_FILE, repo=repo)
            if team:
                rows[team] = board_read(team, repo=repo)
        except Exception as exc:                 # noqa: BLE001
            rows = {}
            state.setdefault("facts", {})["board_error"] = repr(exc)[:200]
        built = handback(state, rows, repo=repo)
        mark_handed_back(state, built["included"])
        _emit("handback", payload, tokens=built["tokens"], dropped_n=built["dropped"],
              included_n=len(built["included"]), budget=built.get("budget"),
              promotion=_promotion_counts(state, repo))
        state["last_handback_tokens"] = built["tokens"]
        state["turns_since_nudge"] = NUDGE_EVERY_TURNS      # a fresh window may be nudged
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": built["text"]}}

    elif event == "Stop":
        derive_facts(state, payload.get("transcript_path"))
        state["turns_since_nudge"] = int(state.get("turns_since_nudge", 0)) + 1
        due = nudge_due(state)
        if due["due"]:
            edits = sum(int(n) for n in (state.get("facts", {}).get("edited") or {}).values())
            state["turns_since_nudge"] = 0
            _emit("nudge", payload, edits=edits, why=due["why"])
            out = {"hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": NUDGE_TEXT.format(edits=edits)}}

    save(key, state)
    return out


def main(argv=None) -> int:
    """Hook entry point. Exits 0 whatever happens: a state file is a convenience, and a
    convenience must never cost the user their turn."""
    import sys

    from attnroute.no_egress import lock_down
    lock_down()                     # a hook makes no network call, ever
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:               # noqa: BLE001
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = hook(payload, repo=payload.get("cwd") or ".")
    except Exception as exc:        # noqa: BLE001 - see the docstring
        print(f"[attnroute] session_state: {exc!r}", file=sys.stderr)
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":          # pragma: no cover - exercised through main()
    raise SystemExit(main())
