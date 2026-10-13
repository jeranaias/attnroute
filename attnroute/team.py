"""Which team is this session? Answered from a mapping file, not from a guess.

The hooks are loaded globally on the shared machine, while `ATTNROUTE_TEAM` is set per
worktree, so any session started outside a worktree records `team=null`. A null team means
the handback cannot fetch that team's board row and the telemetry cannot be split by team --
both of which are the point of having teams at all.

So the cwd decides, via `~/.claude/attnroute-teams.json`:

    {"C:/Users/Jesse/meridian-t5": "T5",
     "C:/Users/Jesse/meridian-t6": "T6",
     "D:/projects/meridian": "architect"}

Write those paths with FORWARD slashes, on every platform. Case is folded on Windows, where
the filesystem folds it, and NOT on POSIX, where two spellings are two directories.

The environment variable still wins when it is set, because a session that has been told who
it is should not be second-guessed by a path.

═══ AND A SESSION CAN DECLARE ITS OWN TEAM, BECAUSE ON THE SHARED MACHINE THE CWD CANNOT
═══

Measured on jeranaias 2026-10-13: the mapping file held 128 prefixes, every one of them a real
directory, and NOT ONE was the cwd of a live session. Every team session there starts in
`C:/Users/Jesse` and `cd`s into its worktree per command, so all twenty live sessions -- eleven
over 90 MB, touched that day -- resolved to `team=None`. Adding entries could not have helped:
the mapping keys the directory a session runs in, and these sessions all run in the same one. It
looked complete while answering a question nobody was asking.

So `attnroute team set T4` records the team in that session's own state file, and `team_for`
reads it. The order is ENV, then DECLARED, then MAPPED: the first two are statements by the
session about itself, the third is an inference from where it happens to be sitting, and an
inference should not overrule a statement.

⚠ THE LAUNCH-TIME VEHICLE IS STILL `ATTNROUTE_TEAM`, and this does not replace it. What it
  does is work for a session that is ALREADY RUNNING -- which, at the point this was written,
  was twenty of them holding two gigabytes of transcript. Setting an environment variable would
  have meant restarting every one.

═══ WHY A MAPPING AND NOT A HEURISTIC ══════════════════════════════════════════════════

The obvious shortcut is to read the team out of the directory name -- find `t5` or `team-5`
in the path and be done. That is a guess, and the failure mode is silent: a worktree renamed,
or a path that happens to contain `t5` for another reason, and the session is filed under
another team's name. Telemetry attributed to the wrong team is worse than telemetry
attributed to none, because nobody can tell it is wrong. A mapping file is explicit, and when
it does not cover a path the answer is "unknown", which is visible.

═══ THREE THINGS THE MATCHING HAS TO GET RIGHT ═════════════════════════════════════════

1. COMPONENTS, NOT CHARACTERS. A plain `startswith` makes `/repo/meridian-t50` a match for
   the prefix `/repo/meridian-t5`, and files T50's work under T5. Matching is done on path
   components, so a prefix matches only at a directory boundary.
2. LONGEST PREFIX WINS. Otherwise the answer depends on dictionary order, and a specific
   worktree inside a mapped parent would resolve to the parent.
2a. BOTH SIDES GO THROUGH THE SAME FUNCTION. The session path is resolved, so the mapping
   prefixes are resolved as well. Comparing a resolved path against a literal spelling is
   comparing two different things, and it only looks correct where the two happen to agree:
   on macOS `/home` is a firmlink, so every match returned None while Linux and Windows
   stayed green. Off CI it is the symlinked worktree that bites -- the mapping names one
   spelling and the session reports the other.
3. A COLLIDING MAPPING IS REPORTED, NOT RESOLVED, and the collision is not where I first
   looked for it. Since comparison is case-insensitive and separator-insensitive, two
   DIFFERENTLY SPELLED keys can mean the same directory -- `C:/a` and `C:\\A` -- and a plain
   dict would keep whichever came last, filing one team's sessions under another with
   nothing to show it. That is caught at load time. (Two different prefixes matching the
   same path at the same depth, which is what I first wrote a check for, is impossible: a
   prefix of length n that matches is exactly `here[:n]`, so there is only one per length.
   A check that cannot fire is worse than no check, because it reads as if the case were
   handled.)
"""

import json
import os
from pathlib import Path

#: Set per worktree today. It wins: a session that has been told who it is is not
#: second-guessed by its path.
TEAM_ENV = "ATTNROUTE_TEAM"

MAP_NAME = "attnroute-teams.json"


def map_path() -> Path:
    return Path.home() / ".claude" / MAP_NAME


def _normalise(path) -> tuple:
    """A path as a tuple of comparable components, so comparison is per directory.

    WARNING: CASE IS FOLDED ONLY WHERE THE FILESYSTEM FOLDS IT, AND I HAD THIS WRONG.
      This lower-cased every component unconditionally. On Windows that is correct --
      `C:/Users` and `c:/users` are one directory, and a case difference in a hand-written
      mapping file is not a different team. On POSIX it is WRONG: `/work/Meridian-T5` and
      `/work/meridian-t5` are two different directories, and folding them together is
      exactly the harm this module exists to prevent, one team's sessions filed under
      another's name.

      `os.path.normcase` is the standard library's answer to this question: lower-case and
      translate separators on Windows, identity on POSIX. The backslash translation is the
      same question -- a separator on Windows, an ordinary filename character on POSIX.

      Consequence for the mapping file: write its paths with FORWARD slashes. A Windows
      path spelled with backslashes is understood on Windows and, read on a POSIX machine,
      is one long filename rather than a path.
    """
    text = str(path or "")
    if os.name == "nt":
        text = text.replace("\\", "/")
    parts = [p for p in text.rstrip("/").split("/") if p not in ("", ".")]
    return tuple(os.path.normcase(p) for p in parts)


def _resolve(path) -> tuple:
    """The cwd as comparable components. A path that does not exist is still usable.

    WARNING: ONLY AN ABSOLUTE PATH IS RESOLVED, AND THAT IS NOT A TEST CONCESSION.
      `Path(path).resolve()` on a RELATIVE path joins it to the current directory of
      whatever process is asking -- and a hook process does not necessarily run in the
      session's directory. Relocating a path to the hook's own cwd is not "making it
      absolute"; it is answering a different question, and the answer would be a team
      attributed from the wrong directory.

      It showed up first as CI failing on Linux and macOS, because "C:/x/y" is not absolute
      on POSIX, so every prefix match was being made against
      /home/runner/work/attnroute/attnroute/C:/x/y. The test paths exposed it; the defect
      was in the resolver.
    """
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            return _normalise(candidate.resolve())
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return _normalise(path)


def load_map(path=None) -> tuple:
    """-> ({normalised prefix: team}, why)

    `why` is non-empty when the file exists but could not be used. An absent file is not a
    problem and says nothing.
    """
    p = Path(path) if path else map_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"{p} could not be read: {exc!r}"
    if not isinstance(raw, dict):
        return {}, f"{p} is not an object of path -> team"
    out = {}
    spelling = {}
    clashes = []
    for prefix, team in raw.items():
        if not isinstance(prefix, str) or not isinstance(team, str) or not team.strip():
            continue
        # ⚠ _resolve, NOT _normalise. THE SESSION PATH IS RESOLVED, SO THE PREFIX MUST BE
        #   TOO. Comparing a resolved path against an unresolved spelling is comparing two
        #   different things and getting away with it only where they happen to agree. On
        #   macOS `/home` is a firmlink, so a session in /home/jesse/meridian-t5 resolves
        #   elsewhere while the mapping key stayed literal -- every match returned None. It
        #   also fixes the case that matters off CI: a worktree reached through a symlink,
        #   where the mapping names one spelling and the session reports the other.
        key = _resolve(prefix)
        team = team.strip()
        if key in out and out[key] != team:
            # Two spellings of one directory naming two teams. Reported, because a dict
            # would simply keep the last one and nothing would look wrong.
            clashes.append(f"{spelling[key]!r} and {prefix!r} both mean the same directory "
                           f"but name {out[key]} and {team}")
            continue
        out[key] = team
        spelling[key] = prefix
    if clashes:
        return {}, f"{p} is ambiguous: " + "; ".join(clashes)
    return out, ""


def declared_team(session_id, state_dir=None) -> str:
    """The team this session recorded for itself, or "". Never raises.

    ⚠⚠ READ AS PLAIN JSON, NOT THROUGH `session_state`, AND NOT BECAUSE OF AN IMPORT CYCLE
       ALONE. `session_state` imports this module, so the dependency has to run one way -- but
       the stronger reason is that resolving a team must not depend on the rest of that module
       loading, parsing or agreeing about its schema. One key is read, defensively, and anything
       unexpected means "not declared" rather than an exception on a hook path.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    base = Path(state_dir) if state_dir else Path.home() / ".claude" / "attn_state" / "sessions"
    try:
        raw = json.loads((base / f"{sid}.json").read_text(encoding="utf-8"))
        found = raw.get("team") if isinstance(raw, dict) else None
        return str(found).strip() if isinstance(found, str) and found.strip() else ""
    except Exception:                     # noqa: BLE001 - a label must not cost a turn
        return ""


def team_for(cwd=None, env=None, map_file=None, session_id=None, state_dir=None) -> dict:
    """Which team is this? -> {"team", "source", "why"}

    `source` is "env", "map", or "none", so a record carries not only the answer but where it
    came from -- a team attributed by a stale mapping file and a team declared by the session
    are different kinds of fact.
    """
    env = env if env is not None else os.environ
    declared = str(env.get(TEAM_ENV) or "").strip()
    if declared:
        return {"team": declared, "source": "env", "why": ""}

    # Before the mapping: a session's statement about itself outranks an inference from where
    # it is sitting. See the module docstring for the measurement that put this here.
    said = declared_team(session_id, state_dir)
    if said:
        return {"team": said, "source": "session", "why": ""}

    mapping, why = load_map(map_file)
    if not mapping:
        return {"team": None, "source": "none",
                "why": why or f"no {MAP_NAME} entry applies, {TEAM_ENV} is unset, and this "
                              f"session has not run `attnroute team set`"}

    here = _resolve(cwd if cwd is not None else Path.cwd())
    # Longest prefix wins, so a worktree inside a mapped parent resolves to the worktree.
    # Only one prefix can match at each length -- it is exactly `here[:n]` -- so the longest
    # match is unique and there is nothing to disambiguate here. Collisions between two
    # spellings of one directory are caught in `load_map`, which is where they live.
    best, best_len = None, 0
    for prefix, team in mapping.items():
        n = len(prefix)
        if n == 0 or len(here) < n or here[:n] != prefix:
            continue                      # a boundary match, not a character match
        if n > best_len:
            best, best_len = team, n

    if best is None:
        # ⚠ THE WAY OUT IS NAMED, because this is the message a reader gets when telemetry
        #   has no team on it -- and on the shared machine it is the EXPECTED message, not a
        #   misconfiguration: every session there runs from one directory. A diagnostic that
        #   states a problem without naming its remedy sends people to edit a mapping file
        #   that cannot answer.
        return {"team": None, "source": "none",
                "why": f"no {MAP_NAME} prefix matches {'/'.join(here) or '.'}; run "
                       f"`attnroute team set <TEAM>` in this session, or set {TEAM_ENV}"}
    return {"team": best, "source": "map", "why": ""}


#: Resolved once per process. A hook process handles one event, so this is one file read per
#: event at most, and `emit` can be called several times within it.
_CACHE = {}


def current(cwd=None, session_id=None) -> dict:
    """`team_for` for this process, cached. Never raises.

    ⚠ THE CACHE KEY CARRIES THE SESSION. It used to be the cwd alone, which was right while
      the cwd was the only thing consulted; with a declared team in play, one hook process
      asking about two sessions would have been served the first answer twice.
    """
    key = (str(cwd) if cwd is not None else "", str(session_id or ""))
    if key not in _CACHE:
        try:
            _CACHE[key] = team_for(cwd, session_id=session_id)
        except Exception as exc:          # noqa: BLE001 - a label must not cost a turn
            _CACHE[key] = {"team": None, "source": "none", "why": repr(exc)[:200]}
    return _CACHE[key]
