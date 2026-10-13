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

#: The SessionStart sources that deserve a handback. The enum in the installed CLI is
#: {startup, resume, clear, compact, fork}, and the choice is per value rather than "all":
#:
#:   compact  the case this was built for -- the window was summarised away.
#:   resume   THE SAME SESSION CONTINUING, and it got nothing. A resumed session has the
#:            same id and the same state file, and every reason a compaction needs a
#:            handback applies to it.
#:   clear    likewise: the id survives, the context does not.
#:
#: Not `startup`: a brand-new id has an empty state file, so a handback would be an empty
#: message. Not `fork`: a fork gets a new id too, and reaching into the parent's notes is a
#: question about whose rulings those are -- worth deciding on purpose, not by inclusion in
#: a tuple.
HANDBACK_SOURCES = ("compact", "resume", "clear")

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
            "facts": {}, "handbacks": 0, "ledger": [], "ledger_left": 0,
            "ledger_repeats": 0, "ledger_of": 0, "ledger_window": 0,
            "compacted_at": [], "alerted_window": None, "alerts_sent": 0}


def set_team(state: dict, team: str) -> dict:
    """Record which team this session is. -> {"team", "why"}

    ⚠⚠ THIS IS A STATEMENT, NOT A GUESS, WHICH IS THE ONLY REASON IT IS ALLOWED TO OUTRANK
       THE MAPPING FILE. `attnroute.team` refuses to read a team out of a directory name
       because that fails silently -- a renamed worktree files one team's work under another's
       name and nothing looks wrong. A session saying "I am T4" has the same authority as the
       environment variable saying it, and is recorded with `source: "session"` so a reader can
       tell the two apart from an inference drawn from a path.

    ⚠ NOT VALIDATED AGAINST A LIST OF KNOWN TEAMS, deliberately: there is no canonical list
      in this repository, and inventing one here would refuse a new team on the day it is
      created. The value is normalised and echoed back so a typo is visible at the moment it is
      made rather than six weeks later in a report.
    """
    name = " ".join(str(team or "").split())
    if not name:
        return {"team": state.get("team"), "why": "a team name cannot be empty"}
    if len(name) > 40:
        return {"team": state.get("team"), "why": "that is a sentence, not a team name"}
    before = state.get("team")
    state["team"] = name
    why = f"was {before}" if before and before != name else ""
    return {"team": name, "why": why}


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


def find_note(state: dict, wanted: str) -> dict:
    """One note by id or unambiguous id prefix. -> {"note", "why"}

    A prefix is accepted because the ids carry a unix timestamp -- `n4-1791113536` is not
    something anyone retypes correctly. An AMBIGUOUS prefix is refused rather than resolved
    to the first match: deleting or rewriting the wrong ruling is a worse outcome than
    being asked for another character.
    """
    wanted = str(wanted or "").strip()
    if not wanted:
        return {"note": None, "why": "no note id given"}
    notes = state.get("notes") or []
    exact = [n for n in notes if n.get("id") == wanted]
    if exact:
        return {"note": exact[0], "why": ""}
    matches = [n for n in notes if str(n.get("id", "")).startswith(wanted)]
    if not matches:
        return {"note": None, "why": f"no note with id {wanted!r}"}
    if len(matches) > 1:
        return {"note": None,
                "why": (f"{wanted!r} matches {len(matches)} notes: "
                        + ", ".join(n["id"] for n in matches[:5]))}
    return {"note": matches[0], "why": ""}


def remove_note(state: dict, wanted: str) -> dict:
    """Delete a note outright. -> {"removed", "why"}

    A real delete, not a tombstone: a note recorded by mistake -- a typo, the wrong
    `--promoted-to`, a duplicate -- is noise, and keeping noise for the sake of history
    just spends the handback budget on it. Amend is the operation that preserves history.
    """
    found = find_note(state, wanted)
    if not found["note"]:
        return {"removed": None, "why": found["why"]}
    note = found["note"]
    state["notes"] = [n for n in (state.get("notes") or []) if n is not note]
    # Its id may already be in `handed_back`; harmless, but leaving it there would keep a
    # deleted note's id in the file forever.
    state["handed_back"] = [i for i in (state.get("handed_back") or [])
                            if i != note.get("id")]
    return {"removed": note, "why": ""}


def amend_note(state: dict, wanted: str, text: str, kind: str | None = None,
               promoted_to: str | None = None) -> dict:
    """Replace a note with a corrected one. -> {"note", "superseded", "why"}

    WARNING: SUPERSEDES RATHER THAN EDITS IN PLACE. A state file that silently rewrites
    what a session decided is a state file nobody can audit -- the record would say the
    ruling had always read this way. So the original stays, carrying `superseded_by`, and
    the replacement carries `supersedes`.

    A superseded note is NOT handed back: the next window needs the current ruling, not its
    drafts, and the handback budget is 1,500 tokens. It is still in the file, and
    `state show` lists it.
    """
    found = find_note(state, wanted)
    if not found["note"]:
        return {"note": None, "superseded": None, "why": found["why"]}
    old = found["note"]
    if old.get("superseded_by"):
        return {"note": None, "superseded": None,
                "why": (f"{old['id']} was already superseded by {old['superseded_by']}; "
                        f"amend that one instead")}
    new = add_note(state, text, kind=kind or old.get("kind", "note"),
                   promoted_to=promoted_to if promoted_to is not None
                   else old.get("promoted_to"),
                   source=old.get("source"))
    new["supersedes"] = old["id"]
    old["superseded_by"] = new["id"]
    return {"note": new, "superseded": old, "why": ""}


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
        # WARNING: THIS WORDING CAUSED A TEAM TO DO THE EXACT OPPOSITE OF THE DESIGN.
        #   It used to read "exists only in a context window". T7 read that as "this note
        #   will not survive compaction", concluded bare notes were worthless, and
        #   re-recorded everything by hand -- which is precisely the work the handback
        #   exists to remove. An UNPROMOTED note is the SAFE case: it is in the state file
        #   and it is handed back EVERY window until it is written down somewhere. The
        #   message now says what is true of the note, not what is absent from the repo.
        return {"state": "UNPROMOTED",
                "why": ("THIS SESSION ONLY -- handed back every window of this session, "
                        "invisible to a new session, another team, or this one after a "
                        "restart; not yet written into a repo file")}
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
#: A pull-request URL as `gh` prints it when a PR is created or viewed. The NUMBER is what a
#: resuming window needs; the host and owner are noise once you have it.
#:
#: ⚠ MATCHED IN TOOL OUTPUT, NOT IN PROSE. A number this session merely TALKED about is not a
#:   pull request it touched, and handing one over as though it were would be the guess this
#:   module refuses to make.
PR_URL = re.compile(r"https?://[^\s]*?/pull/(\d+)")


# ═══ THE COMPACTION QUALITY LEDGER ══════════════════════════════════════
#
# ⚠⚠ WHAT THIS MEASURES, AND WHY A COST PROGRAMME NEEDS IT. Every lever in this package buys
#    cheaper context, and the bill is the only half anybody has measured. The other half --
#    whether the work got worse -- has been asserted ("without quality loss") and never counted.
#    This counts ONE objective thing: a tool call issued after a compaction that this session had
#    ALREADY issued before it. That is re-derivation: work done twice because its answer was
#    summarised away. It is the cheapest honest proxy available, and it is a proxy.
#
# ⚠⚠ IT IS NOT A WASTE COUNT AND MUST NEVER BE REPORTED AS ONE. Re-reading a file you are
#    editing is legitimate and is counted here. The per-session spread measured below was 2.4 to
#    18.7 -- nearly eightfold between seats doing the same kind of work -- so a LEVEL says nothing
#    about any of them. **A RISE, in one session against its own earlier windows, is the whole
#    signal.** See `ledger_summary`, which is why it refuses to compare seats.
#
# ⚠⚠ AND IT DELIBERATELY DOES NOT COUNT CONTRADICTED RULINGS OR OWNER CORRECTIONS, which are
#    the two things that actually matter and were both asked for. Both need a judgement about
#    prose, and this module's rule is that a fact handed to the next window is something already
#    written down, never inferred. A classifier guessing at "contradicted ruling" would find
#    nothing in most windows and report quality fine -- a check that cannot fire, wearing the name
#    of the one that matters, which is the exact failure shape this programme refuses. Those
#    belong to a human: `attnroute note add --kind correction` records one, and it is counted
#    separately and honestly, as a thing somebody noticed rather than a thing a regex found.

#: How many calls after a boundary count as the resuming window re-establishing itself. Past this
#: the session is doing new work and a repeat means little.
LEDGER_AFTER = 40

#: How many call signatures are remembered.
#:
#: ⚠⚠ A BOUND MEANS THIS UNDER-COUNTS, AND THE BASELINE WAS RE-MEASURED AT THE SAME BOUND
#:   BECAUSE OF IT. The signatures live in a state file every hook reads every turn, so an
#:   unbounded set is not available. A bounded memory misses a repeat of a call that fell out of
#:   it. Measured on the same five transcripts: unbounded 8.9 repeats per compaction, bounded at
#:   400 8.4 -- the bound costs 6% of the signal, in the FLATTERING direction. Comparing a bounded
#:   implementation against the unbounded baseline would therefore have shown an improvement that
#:   was nothing but the measure getting weaker. `LEDGER_BASELINE_1M` below is the bounded figure,
#:   and `benchmarks/compact_quality.py` takes the bound as an argument so anyone can re-derive
#:   both.
LEDGER_SIGS = 400

#: Repeats per counted call at a 1,000,000-token `autoCompactWindow`: 8.4 in the first 40 calls
#: after each compaction, over 74 compactions in five transcripts, signature memory 400.
#:
#: ⚠⚠ AND IT IS 8.4 BECAUSE THE BENCHMARK USES THE COUNTER BELOW RATHER THAN A COPY OF IT.
#:   A standalone script measured 10.4 on the same transcripts, a 24% difference, for one reason:
#:   it truncated a command signature at 120 characters where `call_signature` truncates at 200,
#:   so long `git`/`python` commands sharing a prefix were counted as the same call. That is what
#:   a re-implemented measure does -- it reports a number of the right magnitude, with the same
#:   decimal places, that cannot be compared to anything the product does. The benchmark now
#:   imports this module.
#:
#: ⚠ A SNAPSHOT, NOT A CONSTANT OF NATURE -- re-derive it with `benchmarks/compact_quality.py`
#:   rather than trusting this line. It is here so the 400,000-era number has something measured
#:   to be compared against, and it is a FLEET figure, which makes it the weaker of the two
#:   comparisons available. The strong one is a session against itself.
LEDGER_BASELINE_1M = 8.4 / 40

#: The parameters that figure was measured with. Checked against the constants above by
#: `tests/test_session_state.py`, because a baseline measured over 40 calls with a memory of 400
#: is NOT comparable to a ledger counting 80 calls with a memory of 1,000 -- and the comparison
#: would still print, with the same decimal places and no sign that it had stopped meaning
#: anything. Change either constant and this number has to be re-derived, not re-labelled.
LEDGER_BASELINE_MEASURED_AT = {"after": 40, "sigs": 400, "compactions": 74, "sessions": 5,
                               "window": 1000000}

#: How many rows are kept. NOT the same 40 as `LEDGER_AFTER` -- that one counts CALLS inside one
#: window, this one counts WINDOWS. They are equal today by coincidence and are not the same
#: quantity, which is exactly how a literal gets reused into a bug.
LEDGER_ROWS = 40

#: Windows needed before `rising` is allowed to say anything. Two rows is one comparison, and one
#: comparison on a measure this noisy is a coin flip with a verdict attached.
LEDGER_MIN_ROWS = 3

#: Which input identifies a call, per tool. Anything unlisted signs on the tool name alone --
#: deliberately blunt, because a blunt signature OVER-counts, and over-counting is the safe
#: direction for a number whose job is to reveal a rise.
LEDGER_KEYS = {"Bash": "command", "Read": "file_path", "Edit": "file_path",
               "Write": "file_path", "Grep": "pattern", "Glob": "pattern",
               "NotebookEdit": "notebook_path"}


def call_signature(name: str, data) -> str:
    """What makes two tool calls "the same call" for re-derivation purposes. -> 12 hex chars

    HASHED RATHER THAN STORED AS TEXT, because 400 of these live in a file that every hook reads
    on every turn and the full text would add tens of kilobytes to it. Repeat detection needs
    equality and nothing else, so the digest loses nothing the ledger uses.
    """
    # Imported here rather than at module scope: every hook event is a fresh interpreter, and
    # the modules that load this one without deriving facts should not pay for it.
    import hashlib
    key = LEDGER_KEYS.get(name)
    val = str((data or {}).get(key) or "") if (key and isinstance(data, dict)) else ""
    raw = name + "|" + " ".join(val.split())[:200]
    return hashlib.blake2s(raw.encode("utf-8", "replace"), digest_size=6).hexdigest()


def _close_ledger_row(state: dict) -> None:
    """Write one compaction's row, if a window was open. Idempotent."""
    counted = int(state.get("ledger_of", 0))
    if counted:
        rows = state.setdefault("ledger", [])
        rows.append({"window": int(state.get("ledger_window", state.get("window", 0))),
                     "repeats": int(state.get("ledger_repeats", 0)), "of": counted,
                     "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        # Bounded for the same reason as everything else in this file: it is read every turn.
        del rows[:-LEDGER_ROWS]
    state["ledger_repeats"] = 0
    state["ledger_of"] = 0
    state["ledger_left"] = 0


def ledger_summary(state: dict) -> dict:
    """-> {"rows", "rate", "baseline", "fleet_baseline", "rising", "why"}

    ⚠⚠ THE COMPARISON IS THIS SESSION AGAINST ITSELF, AND THE MEASUREMENT IS WHY.
       Per-session rates at a 1,000,000 window were 18.7, 10.0, 5.1, 4.0 and 2.4 repeats per
       compaction. A fleet average across a spread like that hides one seat getting worse behind
       another getting better -- the one thing a check on quality must not do. The fleet figure is
       returned as well, clearly named, because it is what the 1M era can be compared against at
       all; it is the weaker claim and it is labelled as the weaker claim.

    ⚠ AND IT IS REPORTED, NEVER ENFORCED. This function knows nothing about what the session was
      doing; a window that legitimately re-reads twenty files is indistinguishable here from one
      that lost its place. A gate blocking on a number this blunt would be switched off within a
      week, and the measurement would go with it.
    """
    rows = [r for r in (state.get("ledger") or []) if r.get("of")]
    if not rows:
        return {"rows": 0, "rate": None, "baseline": None,
                "fleet_baseline": LEDGER_BASELINE_1M, "rising": False,
                "why": "no window has been measured yet"}
    rate = rows[-1]["repeats"] / rows[-1]["of"]
    prior = rows[:-1]
    own = (sum(r["repeats"] for r in prior) / sum(r["of"] for r in prior)) if prior else None
    if len(rows) < LEDGER_MIN_ROWS:
        rising = False
        why = (f"{len(rows)} window(s) measured; {LEDGER_MIN_ROWS} needed before a rise "
               f"means anything")
    elif rate > own:
        rising = True
        why = (f"this window re-derived {rate:.0%} of its first calls, against "
               f"{own:.0%} over this session's earlier windows")
    else:
        rising = False
        why = f"{rate:.0%} against {own:.0%} over this session's earlier windows"
    return {"rows": len(rows), "rate": rate, "baseline": own,
            "fleet_baseline": LEDGER_BASELINE_1M, "rising": rising, "why": why}


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
    prs = facts.setdefault("prs", [])
    #: Call signatures, least recently used first -- see `call_signature`.
    sigs = facts.setdefault("sigs", [])
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
                # The ledger counts HERE, below the count-once-by-id guard above, so a re-read
                # span cannot report one call as two repeats.
                sig = call_signature(name, data)
                if int(state.get("ledger_left", 0)) > 0:
                    state["ledger_left"] = int(state["ledger_left"]) - 1
                    state["ledger_of"] = int(state.get("ledger_of", 0)) + 1
                    if sig in sigs:
                        state["ledger_repeats"] = int(state.get("ledger_repeats", 0)) + 1
                    if int(state["ledger_left"]) == 0:
                        _close_ledger_row(state)
                # ⚠ OUTSIDE THAT GUARD ON PURPOSE. Signatures accumulate through the window
                #   BEFORE a compaction as well; they are what the window after it is compared
                #   against. Inside the guard, the first row of every session would have nothing
                #   to find and would read as a perfect score.
                if sig in sigs:
                    sigs.remove(sig)
                sigs.append(sig)
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
                for num in PR_URL.findall(text):
                    if num not in prs:
                        prs.append(num)

    # Bounded: the handback has 1,500 tokens, so keeping more than this helps nobody.
    facts["commands"] = commands[-8:]
    facts["prs"] = prs[-6:]
    facts["sigs"] = sigs[-LEDGER_SIGS:]
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
        if note.get("superseded_by"):
            # The next window needs the current ruling, not its drafts. The original stays
            # in the file for audit; `state show` lists it.
            continue
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
        f"- (!) 9999 further item(s) did not fit the {budget}-token handback budget and "
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
    # ⚠⚠ FIRST IN THE DERIVED BLOCK, because it is the fact a resuming window acts on
    #    SOONEST -- "what did I leave open" is the first question after a compaction, and the
    #    answer was costing a `gh pr list` every time.
    #
    # ⚠⚠ AND IT SAYS THE STATE IS NOT CHECKED, WHICH IS THE WHOLE POINT OF THE LINE. These
    #    numbers were SEEN in this window's tool output; one of them may have been merged or
    #    closed since, here or by somebody else. A handback that said "open PRs" would be
    #    asserting a live fact from a stale observation -- and a merged PR reported as open is
    #    exactly how a session pushes to a dead branch. Say what was seen; let the reader check.
    if facts.get("prs"):
        derived.append("- DERIVED PR(s) seen in this window (state NOT checked -- verify "
                       "before citing or pushing): "
                       + ", ".join("#" + n for n in facts["prs"]))
    if facts.get("last_test"):
        derived.append(f"- DERIVED last test result seen: {facts['last_test']}")
    if facts.get("last_lint"):
        derived.append(f"- DERIVED last lint result seen: {facts['last_lint']}")
    if facts.get("commands"):
        derived.append("- DERIVED commits/pushes: "
                       + "; ".join(facts["commands"][-4:]))
    if facts.get("view_complete") is False:
        derived.append("- DERIVED (!) the transcript view was INCOMPLETE, so the facts "
                       "above may be missing earlier work in this window")
    for line in derived:
        if fits(line):
            text += "\n" + line
        else:
            dropped += 1

    if dropped:
        text += (f"\n- (!) {dropped} further item(s) did not fit the "
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
    # ⚠⚠ THE MEASURING WINDOW OPENS HERE BECAUSE THIS IS THE ONLY PLACE A COMPACTION IS KNOWN
    #    TO HAVE HAPPENED. `PreCompact` fires when one is ABOUT to, and a session can be killed
    #    between the two; a handback is what `SessionStart(source=compact)` does, after the fact.
    #    Any row left half-counted by a session that stopped mid-window is CLOSED rather than
    #    discarded -- a short row is honest about how short it is (`of`), and dropping it would
    #    quietly delete the windows most likely to have gone badly.
    _close_ledger_row(state)
    state["ledger_left"] = LEDGER_AFTER
    state["ledger_window"] = int(state.get("window", 0))

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


# ═══ THE APPROACHING-BOUNDARY ALERT ══════════════════════════════
#
# ⚠⚠ WHY A WARNING AND NOT A SAVING. Auto-compact arrives wherever the token count happens to
#    reach the window -- in the middle of a half-applied refactor as readily as at a natural
#    break. The handback above is what makes that survivable, and it is 1,500 tokens against a
#    window of hundreds of thousands: most of what the session knows still goes. A human who
#    knows the boundary is close can take it at a break instead, which costs nothing and loses
#    much less. This fires once per window and then shuts up.
#
# ⚠⚠ AND IT REFUSES TO GUESS THE DENOMINATOR. 80% OF WHAT is the whole question. If
#    `autoCompactWindow` is not configured, this does NOT fall back to a plausible-looking
#    default: a percentage of an invented window is a number with no condition attached, and it
#    would read exactly like a measured one. It falls back to where compaction has ACTUALLY
#    happened in this session, which is observed rather than assumed, and if that is unknown too
#    it says so and names the setting that would fix it. A warning that cannot say what it is a
#    warning about is worse than silence, because silence does not get believed.

#: Fraction of the window at which to speak. Deliberately not 0.95 -- see `context_tokens`: the
#: figure available to a hook is the context the LAST request carried, so the turn that actually
#: crosses the line is the turn that never got a warning. The margin is the one turn of slack.
ALERT_AT = 0.80

#: Bytes of transcript tail read to find the last usage block. Bounded because this runs on a
#: hook: the Stop path once re-parsed 300 MB from byte zero and cost 15 s.
USAGE_TAIL_BYTES = 262_144

#: Where `autoCompactWindow` is read from, nearest first. Claude Code resolves project settings
#: over user settings, and this follows that order rather than inventing one.
SETTINGS_FILES = (".claude/settings.local.json", ".claude/settings.json")


def auto_compact_window(state: dict, repo: Path | str = ".") -> dict:
    """The token count a compaction will happen at. -> {"tokens", "source", "why"}

    `tokens` is None when nothing measured or configured says what it is, and then `why` says
    which setting would answer it. Never a default.
    """
    for name in SETTINGS_FILES:
        path = Path(str(repo)) / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found = int(data["autoCompactWindow"])
        except Exception:                        # noqa: BLE001 - absent, unreadable, not an int
            continue
        if found > 0:
            return {"tokens": found, "source": name, "why": f"{name} sets it to {found:,}"}
    try:
        data = json.loads((Path.home() / ".claude" / "settings.json")
                          .read_text(encoding="utf-8"))
        found = int(data["autoCompactWindow"])
        if found > 0:
            return {"tokens": found, "source": "~/.claude/settings.json",
                    "why": f"~/.claude/settings.json sets it to {found:,}"}
    except Exception:                            # noqa: BLE001
        pass

    # ⚠⚠ OBSERVED, NOT ASSUMED. The context size recorded at this session's own PreCompact
    #    events: where compaction has actually happened here. The SMALLEST is used, because the
    #    alert's job is to arrive before the boundary does and the earliest observed boundary is
    #    the only one that guarantees that.
    seen = [int(n) for n in (state.get("compacted_at") or []) if int(n or 0) > 0]
    if seen:
        return {"tokens": min(seen), "source": "observed",
                "why": f"no autoCompactWindow setting found; this session has compacted at "
                       f"{min(seen):,} tokens"}
    return {"tokens": None, "source": None,
            "why": "no autoCompactWindow in settings and no compaction observed yet in this "
                   "session, so there is no denominator to be a percentage of"}


def context_tokens(transcript_path) -> dict:
    """How much context the last request carried. -> {"tokens", "why"}

    ⚠⚠ THIS IS THE LAST REQUEST, NOT THE NEXT ONE, AND THE DIFFERENCE IS THE POINT. A hook
       can only read what has already been written, so the figure is one turn stale -- and the
       turn that crosses the window is therefore the turn that gets no warning. `ALERT_AT` leaves
       a margin for exactly that; it is not caution for its own sake.

    ⚠ CACHE READS COUNT. They are context: a request carrying 300,000 cached tokens is 300,000
      tokens into the window. BurnRate shipped with them excluded and hid up to 95% of real usage
      (CHANGELOG 1.0.2); the same mistake here would put the alert off by the same factor.
    """
    try:
        path = Path(str(transcript_path or ""))
        size = path.stat().st_size
        start = max(0, size - USAGE_TAIL_BYTES)
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError as exc:
        return {"tokens": None, "why": f"transcript unreadable: {exc.__class__.__name__}"}
    lines = raw.decode("utf-8", "replace").splitlines()
    # ⚠ CONDITIONED ON THE READ HAVING STARTED MID-FILE, not on the file being large. The
    #   first line is a fragment only because the seek landed inside one; keying this on the
    #   SIZE instead says the same thing in the one case that occurs and something false in
    #   every other -- and it masked a mutation, which is how it was found: unbounding the read
    #   left the test green, because this line then threw away the very block being looked for.
    if start and lines:
        lines = lines[1:]
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            usage = (json.loads(line).get("message") or {}).get("usage") or {}
            total = (int(usage.get("input_tokens", 0))
                     + int(usage.get("cache_read_input_tokens", 0))
                     + int(usage.get("cache_creation_input_tokens", 0)))
        except (ValueError, TypeError, AttributeError):
            continue
        if total > 0:
            return {"tokens": total, "why": "from the last usage block in the transcript"}
    return {"tokens": None,
            "why": f"no usage block in the last {USAGE_TAIL_BYTES:,} bytes of transcript"}


def compact_alert(state: dict, transcript_path, repo: Path | str = ".") -> dict:
    """Should this Stop warn that the boundary is close? -> {"due", "text", "why"}

    ⚠ ONCE PER WINDOW. The condition stays true for every remaining turn of the window, so a
      per-turn warning would be noise on precisely the turns that are already expensive -- and
      noise on every turn is what this whole package exists to remove.
    """
    window = int(state.get("window", 0))
    if state.get("alerted_window") == window:
        return {"due": False, "text": "", "why": "already warned in this window"}
    limit = auto_compact_window(state, repo)
    if not limit["tokens"]:
        return {"due": False, "text": "", "why": limit["why"]}
    now = context_tokens(transcript_path)
    if not now["tokens"]:
        return {"due": False, "text": "", "why": now["why"]}
    threshold = int(limit["tokens"] * ALERT_AT)
    if now["tokens"] < threshold:
        return {"due": False, "text": "",
                "why": f"{now['tokens']:,} of {limit['tokens']:,} "
                       f"({now['tokens'] / limit['tokens']:.0%}), under {threshold:,}"}
    return {"due": True, "why": f"{now['tokens']:,} of {limit['tokens']:,} "
                                f"({now['tokens'] / limit['tokens']:.0%})",
            "text": ALERT_TEXT.format(now=f"{now['tokens']:,}",
                                      limit=f"{limit['tokens']:,}",
                                      pct=f"{now['tokens'] / limit['tokens']:.0%}",
                                      source=limit["source"])}


#: ⚠ IT NAMES THE NUMBERS AND WHERE THE LIMIT CAME FROM. "Context is getting large" is a
#:   feeling; a reader cannot check it or decide on it. And it tells the model to SAY SOMETHING
#:   rather than to act: whether a break is a good place to stop is the human's call, and a hook
#:   that started compacting on its own judgement would be taking it.
ALERT_TEXT = (
    "[attnroute] CONTEXT {now} of {limit} tokens ({pct} of autoCompactWindow, from "
    "{source}). Auto-compact will land wherever the count reaches the window -- possibly "
    "mid-task, mid-refactor. FINISH THE STEP YOU ARE ON, bring it to a committable or "
    "reportable state, then ask the user to run /compact before starting anything new. You "
    "cannot compact yourself and neither can this hook, so the ask is the whole mechanism: "
    "say it in one line and carry on. Do not abandon work in progress to compact sooner, and "
    "do not start a long new task on this context."
)


def compaction_compliance(state: dict, limit_tokens=None) -> dict:
    """Did this session act on its warnings? -> {"alerted", "early", "at_the_wall", "rate",
    "why"}

    ⚠⚠ THE SAVING FROM ITEM 3 IS CONTINGENT ON SESSIONS ACTUALLY ACTING, AND THIS IS THE
       NUMBER THAT SAYS WHETHER THEY DID. The arithmetic is that the bill follows the MEAN
       context, `(post-compact floor + trigger) / 2`, so moving the trigger from the window down
       to the alert is worth something only if the trigger really moves. A nudge nobody acts on
       saves nothing, and reporting the projected figure as achieved would be the reassuring
       failure this programme exists to refuse.

    ⚠⚠ IT NEEDED NO NEW INSTRUMENTATION, which is why item 3 is first. Two things were
       already recorded: the context size at each compaction (`compacted_at`, written at
       PreCompact) and the window an alert fired in (`alerted_window`). A compaction materially
       BELOW the window was taken at a break; one AT the window is auto-compact arriving on its
       own schedule. That difference is the whole measurement.

    ⚠ AND "early" IS NOT "because of the alert". A session may compact early for its own
      reasons, and the alert fires before the compaction either way, so this is an
      ASSOCIATION over a population and not a cause for any single window. The control is the
      unalerted windows in the same table; `why` says so in words, because a bare percentage
      invites the stronger reading.
    """
    seen = [int(n) for n in (state.get("compacted_at") or []) if int(n or 0) > 0]
    if not seen:
        return {"alerted": 0, "early": 0, "at_the_wall": 0, "rate": None,
                "why": "no compaction has been observed in this session yet"}
    limit = int(limit_tokens or 0)
    if limit <= 0:
        return {"alerted": 0, "early": 0, "at_the_wall": 0, "rate": None,
                "why": "no autoCompactWindow is known, so early and at-the-wall cannot be "
                       "told apart"}
    # A compaction at or above the alert threshold is the wall arriving; one below it is a
    # session that stopped early. The threshold is the same ALERT_AT the warning uses, so the
    # two cannot drift apart into a measurement of a different thing.
    threshold = int(limit * ALERT_AT)
    early = [n for n in seen if n < threshold]
    wall = [n for n in seen if n >= threshold]
    alerted = int(state.get("alerts_sent", 0))
    rate = len(early) / len(seen)
    return {"alerted": alerted, "early": len(early), "at_the_wall": len(wall), "rate": rate,
            "why": f"{len(early)} of {len(seen)} observed compaction(s) happened below "
                   f"{threshold:,} tokens, {alerted} alert(s) sent -- association over a "
                   f"population, not a cause for any one window"}


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


def _quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


def note_command() -> dict:
    """The exact command that records a note FROM THIS INSTALL. -> {"command", "why"}

    WARNING: `attnroute` ON PATH IS NOT NECESSARILY THIS attnroute. On the shared machine
    the PATH copy was an older install with no `note` subcommand at all, so a session that
    followed the nudge got an error and concluded the feature did not exist. A hook knows
    which interpreter is running it, so it can name the one that works.

    Two candidates, in order, and the answer is CHECKED rather than assumed:

      1. the console script for this interpreter -- `sysconfig`'s scripts directory, which
         is the venv's Scripts/bin when running in a venv, and next to the interpreter is
         tried as well for unusual layouts;
      2. `"<python>" -m attnroute.cli`, which needs no console script and therefore always
         works, including from a source checkout with PYTHONPATH set.

    Naming a path that does not exist would be the same mistake in a new costume, so (1) is
    used only when the file is really there.
    """
    import sys
    import sysconfig

    names = ("attnroute.exe", "attnroute") if os.name == "nt" else ("attnroute",)
    roots = []
    try:
        roots.append(Path(sysconfig.get_path("scripts")))
    except (KeyError, OSError):
        pass
    try:
        roots.append(Path(sys.executable).parent)
    except (TypeError, ValueError):
        pass
    for root in roots:
        for name in names:
            candidate = root / name
            try:
                if candidate.is_file():
                    return {"command": _quote(str(candidate)), "why": "console script"}
            except OSError:
                continue
    return {"command": f"{_quote(sys.executable)} -m attnroute.cli",
            "why": "no console script for this interpreter; using the module form"}


#: `{command}` is filled with `note_command()` so the advice names an executable that
#: exists, not whatever PATH happens to resolve.
NUDGE_TEXT = (
    "[attnroute] This window has {edits} edit(s) and no recorded ruling. Anything you have "
    "DECIDED -- a ruling, a trade-off, a figure you withdrew -- is lost at the next "
    "compaction unless it is written down. One line is enough:\n"
    "  {command} note add --kind ruling \"<the ruling>\" [--promoted-to <path>]\n"
    "Nothing infers these from your prose, by design. (That path is this session's own "
    "install; `attnroute` on PATH may be a different one.)\n"
    "An UNPROMOTED note is THIS SESSION ONLY: a new session, another team, or this one "
    "after a restart will not see it. For a ruling that must outlive the session, add "
    "--promoted-to <repo path> (the claim is checked), or publish it to the team board."
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
        # ⚠ WHERE COMPACTION ACTUALLY HAPPENS, recorded because it is the only honest
        #   denominator available when `autoCompactWindow` is not configured -- see
        #   `auto_compact_window`. Bounded, like everything else kept in this file.
        here = context_tokens(payload.get("transcript_path"))["tokens"]
        if here:
            state["compacted_at"] = ([*(state.get("compacted_at") or []), here])[-8:]
        state["window"] = int(state.get("window", 0)) + 1

    elif event == "SessionStart" and str(payload.get("source") or "") in HANDBACK_SOURCES:
        rows = {}
        try:
            from attnroute.board import LEAD_FILE, read as board_read
            from attnroute.team import current as current_team
            # WARNING: NOT `os.environ["ATTNROUTE_TEAM"]` ALONE. The hooks are loaded
            #   globally on the shared machine and the variable is set per worktree, so a
            #   session started outside a worktree got no team -- and therefore no board
            #   row, silently, which looks exactly like a team that has not written one.
            #   `team.current` falls back to the cwd via a mapping file; the variable still
            #   wins when set.
            found = current_team(repo)
            team = found.get("team")
            # The lead's row always; this team's row when we know which team this is.
            # Reading is local-ref only, so this costs no network -- see board.read.
            rows[LEAD_FILE] = board_read(LEAD_FILE, repo=repo)
            if team:
                rows[team] = board_read(team, repo=repo)
            else:
                # Said out loud in the handback rather than left as an absent row.
                state.setdefault("facts", {})["team_unknown"] = found.get("why")
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
        # ⚠⚠ THE BOUNDARY WARNING OUTRANKS THE NOTE NUDGE, AND ONLY ONE OF THEM SPEAKS.
        #    `additionalContext` is one string; two messages in it compete for the same attention
        #    and the nudge would be the one acted on, being the easier instruction. A note that
        #    goes unwritten for another 25 turns costs a note. A boundary that lands mid-task
        #    costs the window. The nudge keeps its turn counter either way, so it is deferred and
        #    not skipped.
        alert = compact_alert(state, payload.get("transcript_path"),
                              repo=payload.get("cwd") or repo)
        due = nudge_due(state)
        if alert["due"]:
            state["alerted_window"] = int(state.get("window", 0))
            # The denominator for `compaction_compliance`. Without it the early/at-the-wall
            # split has no idea how many warnings it is the response to.
            state["alerts_sent"] = int(state.get("alerts_sent", 0)) + 1
            _emit("compact_alert", payload, why=alert["why"])
            out = {"hookSpecificOutput": {"hookEventName": "Stop",
                                          "additionalContext": alert["text"]}}
        elif due["due"]:
            edits = sum(int(n) for n in (state.get("facts", {}).get("edited") or {}).values())
            state["turns_since_nudge"] = 0
            _emit("nudge", payload, edits=edits, why=due["why"])
            out = {"hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": NUDGE_TEXT.format(
                    edits=edits, command=note_command()["command"])}}

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
