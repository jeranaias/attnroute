"""One telemetry stream for every attnroute lever, so a single report can read them all.

Every record is one JSON object on one line of ~/.claude/telemetry/attnroute.jsonl:

    v           schema version (int). Bumped on any change a reader must know about.
    ts          unix seconds (float), when the record was written
    component   "read_ledger" | "output_cap" | "session_state" | ...
    event       what happened, in that component's own words
    session_id  the Claude Code session (the PARENT's id for a subagent)
    agent_id    the subagent's id, or null for the main loop
    arm         the holdout arm the decision was made in, or null when no holdout applies
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


def read(path=None):
    """Yield every well-formed record. A torn or foreign line is skipped, not fatal."""
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
                continue
            if isinstance(rec, dict) and "component" in rec:
                yield rec
