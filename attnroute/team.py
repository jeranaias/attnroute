"""Which team is this session? Answered from a mapping file, not from a guess.

The hooks are loaded globally on the shared machine, while `ATTNROUTE_TEAM` is set per
worktree, so any session started outside a worktree records `team=null`. A null team means
the handback cannot fetch that team's board row and the telemetry cannot be split by team --
both of which are the point of having teams at all.

So the cwd decides, via `~/.claude/attnroute-teams.json`:

    {"C:/Users/Jesse/meridian-t5": "T5",
     "C:/Users/Jesse/meridian-t6": "T6",
     "D:/projects/meridian": "architect"}

The environment variable still wins when it is set, because a session that has been told who
it is should not be second-guessed by a path.

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
        key = _normalise(prefix)
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


def _normalise(path) -> tuple:
    """A path as a tuple of lowercased components, so comparison is per directory.

    Lowercased because the shared machine is Windows, where `C:/Users` and `c:/users` are
    the same directory and a case difference in a hand-written mapping file is not a
    different team.
    """
    text = str(path or "").replace("\\", "/").rstrip("/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    return tuple(p.lower() for p in parts)


def _resolve(path) -> tuple:
    """The cwd, resolved where possible. A path that does not exist is still usable."""
    try:
        return _normalise(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        return _normalise(path)


def team_for(cwd=None, env=None, map_file=None) -> dict:
    """Which team is this? -> {"team", "source", "why"}

    `source` is "env", "map", or "none", so a record carries not only the answer but where it
    came from -- a team attributed by a stale mapping file and a team declared by the session
    are different kinds of fact.
    """
    env = env if env is not None else os.environ
    declared = str(env.get(TEAM_ENV) or "").strip()
    if declared:
        return {"team": declared, "source": "env", "why": ""}

    mapping, why = load_map(map_file)
    if not mapping:
        return {"team": None, "source": "none",
                "why": why or f"no {MAP_NAME} entry applies and {TEAM_ENV} is unset"}

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
        return {"team": None, "source": "none",
                "why": f"no {MAP_NAME} prefix matches {'/'.join(here) or '.'}"}
    return {"team": best, "source": "map", "why": ""}


#: Resolved once per process. A hook process handles one event, so this is one file read per
#: event at most, and `emit` can be called several times within it.
_CACHE = {}


def current(cwd=None) -> dict:
    """`team_for` for this process, cached. Never raises."""
    key = str(cwd) if cwd is not None else ""
    if key not in _CACHE:
        try:
            _CACHE[key] = team_for(cwd)
        except Exception as exc:          # noqa: BLE001 - a label must not cost a turn
            _CACHE[key] = {"team": None, "source": "none", "why": repr(exc)[:200]}
    return _CACHE[key]
