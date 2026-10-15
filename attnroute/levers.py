"""Which levers are ACTING, from a file as well as the environment.

⚠⚠ WHY A FILE AT ALL. `acting()` already read the environment per decision, and every hook event
   is a fresh interpreter, so the CODE needed no restart to pick a change up. THE ENVIRONMENT DID.
   A hook child inherits its environment from the running Claude Code session, whose environment
   block was fixed when it launched -- so `setx`, or a change in the Settings dialog, reaches NEW
   sessions and never reaches the twenty already running. Turning a lever on for a fleet therefore
   meant restarting the fleet, which is exactly the thing the owner ruled out: a flip has to reach
   a running session with nothing for anyone to do.

   A file is read by the hook child itself, so it reaches every live session on its next hook
   event. That is the whole reason this module exists.

⚠⚠ THE ENVIRONMENT STILL WINS, AND "WINS" HAS TO INCLUDE TURNING IT OFF. This is the sharp edge.
   `ATTNROUTE_CAP_ACT` has no false value today: unset and `=0` both mean observe, because
   `acting()` just tested membership in a true-list. If `=0` fell through to the file, then the
   documented rollback -- *"unset it or set it to 0"* -- would appear to do nothing whenever the
   file said on, and the operator would be looking at a lever they believed they had turned off.
   So: SET-AND-FALSEY means OFF and does not consult the file. Only a genuinely absent variable
   defers.

ℹ An EMPTY value counts as absent. On Windows setting a variable to the empty string is how it is
  removed, so treating `VAR=` as "set to something false" would make a removal behave like an
  override.

⚠ NOTHING HERE MAY RAISE. It runs on every hook event inside the p95 budget. A missing file, a
  malformed one, a value of the wrong type: each means "not acting", which is the safe default,
  and each explains itself in `why` so a trial can tell a deliberate off from a broken file.
  A lever that failed CLOSED by crashing the hook would cost the user their turn.

⚠ AND THERE IS NO CACHE, DELIBERATELY. Caching the file would defeat the point. It is not needed:
  the process is per hook event, so reading once per call is already "fresh every event".
"""

import json
import os
from pathlib import Path

#: The levers this module resolves, and the environment variable that overrules each.
#: The names are the file's keys and the CLI's arguments.
LEVERS = {
    "cap_act": "ATTNROUTE_CAP_ACT",
    "ledger_act": "ATTNROUTE_LEDGER_ACT",
}

#: The same tokens `acting()` has always accepted, kept identical so a file and an environment
#: variable cannot mean different things by the same word.
TRUE_TOKENS = ("1", "true", "yes", "on")


def levers_path() -> Path:
    """The flag file. Beside the session state, because it is state about this machine."""
    return Path.home() / ".claude" / "attn_state" / "levers.json"


def _from_file(name: str) -> tuple:
    """-> (on, found, why). `found` says whether the file decided, so the caller can report
    `default` rather than claim the file said no."""
    path = levers_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False, False, ""                      # absent is the ordinary case, not an error
    try:
        data = json.loads(raw)
    except ValueError:
        return False, False, f"{path} is not valid JSON, so no lever was read from it"
    if not isinstance(data, dict):
        return False, False, f"{path} does not hold an object, so no lever was read from it"
    if name not in data:
        return False, False, ""
    value = data[name]
    if isinstance(value, bool):
        return value, True, ""
    if isinstance(value, str) and value.strip().lower() in TRUE_TOKENS:
        return True, True, ""
    if isinstance(value, str):
        return False, True, ""
    return False, True, (f"{path} set {name} to {value!r}, which is neither true/false nor one of "
                         f"{'/'.join(TRUE_TOKENS)} -- taken as not acting")


def resolve(name: str) -> dict:
    """Is this lever acting, and WHAT DECIDED IT. -> {"on", "source", "why"}

    `source` is one of "env", "file", "default". ⚠ It is not decoration: a trial has to be able
    to say which sessions were acting AND WHY, or the arm sizes are unverifiable -- a lever
    believed on through the file while an inherited environment variable held it off produces a
    treated arm that is quietly empty, and nothing in the report looks wrong.
    """
    env_name = LEVERS.get(name)
    if env_name is None:
        return {"on": False, "source": "default", "why": f"{name} is not a known lever"}

    raw = os.environ.get(env_name, "")
    token = raw.strip()
    if token:
        if token.lower() in TRUE_TOKENS:
            return {"on": True, "source": "env", "why": ""}
        # Set, and not a true value: an explicit OFF. It must not fall through to the file,
        # or the rollback would be silently overruled by it.
        return {"on": False, "source": "env",
                "why": f"{env_name}={raw!r} is not a true value, so the lever is off and the "
                       f"file was not consulted"}

    on, found, why = _from_file(name)
    if found:
        return {"on": bool(on), "source": "file", "why": why}
    return {"on": False, "source": "default", "why": why}


def acting(name: str) -> bool:
    """The one-line form for a hot path that only needs the answer."""
    return resolve(name)["on"]


def set_lever(name: str, on: bool) -> dict:
    """Write the flag. -> {"path", "lost"}

    ⚠ IF THE EXISTING FILE CANNOT BE READ, THIS STILL WRITES, and says what it lost. Refusing
      would be defensible for the file's sake and wrong for the operator's: this command is the
      documented way to turn a lever OFF, and a rollback that refuses because the file it is
      rolling back is corrupt is a rollback that does not work. The other levers return to
      default and `lost` names them so that is a reported fact rather than a surprise.
    """
    if name not in LEVERS:
        raise KeyError(f"{name} is not a known lever ({', '.join(sorted(LEVERS))})")
    path = levers_path()
    data, lost = {}, []
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            data = existing
        else:
            lost = sorted(LEVERS)
    except OSError:
        pass                                          # first write
    except ValueError:
        lost = sorted(k for k in LEVERS if k != name)

    data[name] = bool(on)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    # Temp-and-replace: a hook event reading this file while it is written must see the old
    # contents or the new ones, never a half-written object. `open(path, "w")` truncates first.
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"path": path, "lost": lost}
