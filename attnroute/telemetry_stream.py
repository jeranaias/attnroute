"""One telemetry stream for every attnroute lever, so a single report can read them all.

Every record is one JSON object on one line of ~/.claude/telemetry/attnroute.jsonl:

    v           schema version (int). Bumped on any change a reader must know about.
    ts          unix seconds (float), when the record was written
    component   "read_ledger" | "output_cap" | "session_state" | ...
    event       what happened, in that component's own words
    session_id  the Claude Code session (the PARENT's id for a subagent)
    agent_id    the subagent's id, or null for the main loop
    arm         the holdout arm the decision was made in, or null when no holdout applies
    team        the team this session belongs to, and `team_source` where that came from
                ("env", "map" or "none"). Filled in automatically: the hooks are loaded
                globally on the shared machine while ATTNROUTE_TEAM is set per worktree, so
                a session started outside a worktree would otherwise record no team at all,
                and telemetry that cannot be split by team is most of the point of having
                teams. A caller may pass `team=` to override it.
    acting      true when the lever was allowed to change what the model saw
    ... plus the component's own fields.

The file is APPEND-ONLY and is never truncated or rotated by attnroute: the owner asked for a
permanent record, and a log that trims itself silently loses the baseline weeks later.

Best-effort by design: a record that cannot be written is dropped rather than raised, because a
measurement must never cost the user their turn. Drops are therefore possible and the report says
so instead of treating the stream as complete.
"""

import json
import time
from pathlib import Path

SCHEMA_VERSION = 1
STREAM_NAME = "attnroute.jsonl"


def stream_path() -> Path:
    return Path.home() / ".claude" / "telemetry" / STREAM_NAME


def emit(component: str, event: str, *, session_id=None, agent_id=None, arm=None,
         acting=None, **fields) -> None:
    rec = {"v": SCHEMA_VERSION, "ts": round(time.time(), 3), "component": component,
           "event": event, "session_id": session_id, "agent_id": agent_id, "arm": arm,
           "acting": acting}
    # The team is resolved once per process and attached here rather than at each call
    # site, so no lever can forget it. `team_source` travels with it because a team taken
    # from a mapping file and a team declared by the session are different kinds of fact,
    # and a stale mapping should be auditable after the event rather than indistinguishable.
    # WARNING: THE CALLER'S OWN `team=` HAS TO WIN, AND IT DID NOT. Setting these before the
    #   `fields` loop below meant "team" was already a key, so an explicit `team=` was
    #   dropped on the floor -- while the docstring said a caller could override it. Caught
    #   by noticing the test I had written for it was tautological.
    if "team" not in fields:
        try:
            from attnroute.team import current as _team
            found = _team()
            rec["team"] = found.get("team")
            rec["team_source"] = found.get("source")
        except Exception:                  # noqa: BLE001 - a label must not cost a turn
            rec["team"] = None
            rec["team_source"] = "none"
    else:
        rec["team_source"] = fields.get("team_source", "caller")
    for k, v in fields.items():
        if k not in rec:
            rec[k] = v
    try:
        p = stream_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


#: Lines read() could not use, by reason, from its most recent call. A reader that silently
#: skipped a newer schema would read changed meanings as old ones, so skips are COUNTED.
SKIPPED = {"torn": 0, "foreign_version": 0}


def read(path=None):
    """Yield every well-formed record of THIS schema version. Torn lines and records of another
    version are skipped and counted in SKIPPED, never guessed at."""
    SKIPPED["torn"] = SKIPPED["foreign_version"] = 0
    p = Path(path) if path else stream_path()
    try:
        fh = open(p, encoding="utf-8")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                SKIPPED["torn"] += 1
                continue
            if not (isinstance(rec, dict) and "component" in rec):
                SKIPPED["torn"] += 1
                continue
            if rec.get("v") != SCHEMA_VERSION:
                SKIPPED["foreign_version"] += 1
                continue
            yield rec
