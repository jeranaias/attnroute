"""`attnroute savings`: what attnroute saved, measured from the BILL, and what it cost in quality.

THE RULE THIS FILE EXISTS TO KEEP: a saving is reported from the `usage` blocks Claude Code writes
into each transcript, never from an estimate. The levers' own logs ("would have saved N tokens")
are shown, but in a separate section labelled ESTIMATE, because an estimate that agrees with
itself is not evidence.

WHAT IS MEASURED, per session, from the transcript:

    cost units   0.1 x cache_read + 1.25 x cache_creation + 1 x input + 5 x output
                 (the relative prices, so units compare across sessions; not dollars)
    turns        assistant messages carrying usage, de-duplicated by message id -- one message
                 is written as several lines while it streams, all with the same usage
    windows      spans between compaction summaries; turns/window is the cadence lever
    repeats      the quality proxy: a tool call identical (name + input) to one made in an
                 EARLIER window of the same session, i.e. work redone after a compaction

GROUPS come from the telemetry stream, not from a guess:

    treated    the session has at least one stream record with acting=true
    observed   the session has stream records, none acting -- attnroute watched, changed nothing
    baseline   no stream records at all

THE HEADLINE IS THE WITHIN-SESSION ARM CONTRAST: the same session, the same task, the same
window, split only by a deterministic sha256 holdout. See `arm_contrast`. It is the headline
because it is the only comparison here with no confound in it.

Cost units PER TURN, treated against baseline, is reported too -- SECONDARY and labelled
CONFOUNDED, because those sessions differ by project, task, phase and driver, so a gap between
the groups is largely task mix. It is kept because the cadence lever (turns per window) lives
there and nowhere else. Per turn, because sessions differ wildly in length; bootstrapped over
sessions, because turns inside one session are not independent; no interval at all below five
sessions in a group, and an interval that crosses zero is reported as NOT ESTABLISHED.

⚠ `repeats_per_compaction` is a FLOOR on the quality cost, not a measure of it: it counts
repeated tool calls and cannot see a ruling re-derived or contradicted after a compaction.
"""

import json
import random
from collections import defaultdict
from pathlib import Path

from attnroute import telemetry_stream

WEIGHTS = {"cache_read_input_tokens": 0.1, "cache_creation_input_tokens": 1.25,
           "input_tokens": 1.0, "output_tokens": 5.0}


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _is_compaction(entry: dict) -> bool:
    return bool(entry.get("isCompactSummary")) or "compactMetadata" in entry


def scan_transcript(path) -> dict:
    """One transcript -> {session_id, turns, units, usage{}, windows, repeats, compactions}."""
    seen_msgs = set()
    usage = defaultdict(int)
    turns = 0
    windows = 1
    compactions = 0
    earlier = set()          # tool calls made in previous windows
    current = set()          # tool calls made in this window
    repeats = 0
    sid = None
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return {}
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            sid = sid or e.get("sessionId")
            if _is_compaction(e):
                compactions += 1
                windows += 1
                earlier |= current
                current = set()
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        key = (b.get("name"), json.dumps(b.get("input"), sort_keys=True))
                        if key in earlier and key not in current:
                            repeats += 1
                        current.add(key)
            u = msg.get("usage")
            mid = msg.get("id")
            if not isinstance(u, dict) or (mid and mid in seen_msgs):
                continue
            if mid:
                seen_msgs.add(mid)
            turns += 1
            for k in WEIGHTS:
                v = u.get(k)
                if isinstance(v, int):
                    usage[k] += v
    units = sum(usage[k] * w for k, w in WEIGHTS.items())
    return {"session_id": sid, "path": str(path), "turns": turns, "units": units,
            "usage": dict(usage), "windows": windows, "compactions": compactions,
            "repeats": repeats}


def stream_summary(records) -> dict:
    """Per-session acting/observing flags plus the levers' own ESTIMATES."""
    by_sid = defaultdict(lambda: {"acting": False, "records": 0})
    est = defaultdict(lambda: defaultdict(float))
    missed = []
    for r in records:
        sid = r.get("session_id")
        s = by_sid[sid]
        s["records"] += 1
        if r.get("acting"):
            s["acting"] = True
        comp = r.get("component") or "?"
        mode = "acting" if r.get("acting") else "observing"
        saved = r.get("saved_tokens_est") or r.get("would_save_tokens_est") or 0
        try:
            saved = float(saved)
        except (TypeError, ValueError):
            saved = 0.0
        if r.get("arm") in (None, "ledger", "cap"):
            est[comp][mode] += saved
        if saved and (r.get("arm") not in ("ledger", "cap") or not r.get("acting")):
            missed.append((saved, comp, r.get("path") or r.get("command") or "", sid))
    missed.sort(key=lambda m: m[0], reverse=True)
    lat = [float(r["latency_ms"]) for r in records if isinstance(r.get("latency_ms"), (int, float))]
    return {"sessions": dict(by_sid), "estimates": {k: dict(v) for k, v in est.items()},
            "missed": missed[:5], "latencies_ms": lat}


MIN_SESSIONS_FOR_CI = 5

CONTROL_ARMS = ("held-out", "held-out-file", "held-out-turn")


def arm_contrast(records) -> dict:
    """The HEADLINE for the read-side levers: same session, same task, same window, split only
    by a deterministic sha256 holdout. Measured result sizes, not the levers' estimates.

      read_ledger  tokens actually put into context by Read, per Read decision, per arm
      output_cap   characters actually returned by Bash, per command, per arm

    Only ACTING records count: while observing, both arms are untouched and equal by design.
    """
    out = {}
    led = {"t": [0, 0], "c": [0, 0]}          # [result tokens, decisions]
    cap = {"t": [], "c": []}
    for r in records:
        if not r.get("acting"):
            continue
        arm = r.get("arm")
        side = "c" if arm in CONTROL_ARMS else "t" if arm in ("ledger", "cap") else None
        if side is None:
            continue
        if r.get("component") == "read_ledger":
            if r.get("event") == "read_ledger":
                led[side][1] += 1
            elif r.get("event") == "read_result":
                led[side][0] += int(r.get("tokens") or 0)
        elif r.get("component") == "output_cap" and str(r.get("event", "")).endswith("result"):
            cap[side].append(int(r.get("chars") or 0))
    if led["t"][1] and led["c"][1]:
        t, c = led["t"][0] / led["t"][1], led["c"][0] / led["c"][1]
        out["read_ledger"] = {"treated": t, "control": c, "n_t": led["t"][1],
                              "n_c": led["c"][1], "saving": (1 - t / c) if c else None}
    if cap["t"] and cap["c"]:
        t, c = sum(cap["t"]) / len(cap["t"]), sum(cap["c"]) / len(cap["c"])
        out["output_cap"] = {"treated": t, "control": c, "n_t": len(cap["t"]),
                             "n_c": len(cap["c"]), "saving": (1 - t / c) if c else None}
    return out


def _group(sid, sessions) -> str:
    s = sessions.get(sid)
    if not s:
        return "baseline"
    return "treated" if s["acting"] else "observed"


def _per_turn(rows):
    t = sum(r["turns"] for r in rows)
    return (sum(r["units"] for r in rows) / t) if t else None


def bootstrap_ci(treated, baseline, n=2000, seed=7):
    """95% interval on (treated per-turn cost / baseline per-turn cost - 1), resampling sessions."""
    if len(treated) < MIN_SESSIONS_FOR_CI or len(baseline) < MIN_SESSIONS_FOR_CI:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        a = [rng.choice(treated) for _ in treated]
        b = [rng.choice(baseline) for _ in baseline]
        pa, pb = _per_turn(a), _per_turn(b)
        if pa is not None and pb:
            out.append(pa / pb - 1.0)
    if not out:
        return None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out)) - 1]


def analyse(project_filter=None, transcripts=None, stream=None) -> dict:
    records = list(telemetry_stream.read(stream))
    ss = stream_summary(records)
    if transcripts is None:
        root = projects_dir()
        transcripts = [p for p in root.glob("*/*.jsonl")
                       if not project_filter or project_filter.lower() in str(p.parent).lower()]
    rows = [r for r in (scan_transcript(p) for p in transcripts) if r and r.get("turns")]
    groups = defaultdict(list)
    for r in rows:
        groups[_group(r["session_id"], ss["sessions"])].append(r)

    def agg(rs):
        turns = sum(r["turns"] for r in rs)
        comps = sum(r["compactions"] for r in rs)
        wins = sum(r["windows"] for r in rs)
        return {"sessions": len(rs), "turns": turns, "units": sum(r["units"] for r in rs),
                "units_per_turn": _per_turn(rs),
                "turns_per_window": (turns / wins) if wins else None,
                "compactions": comps,
                "repeats_per_compaction": (sum(r["repeats"] for r in rs) / comps) if comps else None}

    out = {"groups": {g: agg(rs) for g, rs in groups.items()}, "stream": ss,
           "stream_records": len(records), "skipped": dict(telemetry_stream.SKIPPED),
           "arms": arm_contrast(records)}
    t, b = groups.get("treated", []), groups.get("baseline", [])
    pt, pb = _per_turn(t), _per_turn(b)
    out["saving_per_turn"] = (1.0 - pt / pb) if (pt is not None and pb) else None
    ci = bootstrap_ci(t, b)
    out["ci95"] = None if ci is None else (-ci[1], -ci[0])   # as a SAVING, so signs flip
    return out


def _fmt(x, pct=False, digits=1):
    if x is None:
        return "NOT CHECKED"
    return f"{x * 100:.{digits}f}%" if pct else f"{x:,.{digits}f}"


def render(res: dict) -> str:
    L = []
    L.append("attnroute savings -- measured, not estimated")
    L.append("")
    L.append("HEADLINE: within-session arm contrast (same session and task, sha256 holdout):")
    arms = res.get("arms") or {}
    for comp, unit in (("read_ledger", "tok/read"), ("output_cap", "chars/cmd")):
        a = arms.get(comp)
        if not a:
            L.append(f"  {comp:<12} NOT CHECKED -- no acting records in both arms yet")
            continue
        L.append(f"  {comp:<12} treated {a['treated']:,.0f} vs control {a['control']:,.0f} "
                 f"{unit}  saving {_fmt(a['saving'], True)}  (n {a['n_t']} vs {a['n_c']})")
    L.append("")
    L.append("SECONDARY, CONFOUNDED: across sessions (different tasks; cadence lever lives here)")
    L.append("  cost units = 0.1 cache_read + 1.25 cache_creation + input + 5 output, from usage")
    L.append(f"{'group':<10} {'sessions':>8} {'turns':>9} {'units/turn':>12} "
             f"{'turns/window':>13} {'repeats/compact*':>19}")
    for g in ("treated", "observed", "baseline"):
        a = res["groups"].get(g)
        if not a:
            L.append(f"{g:<10} {'0':>8}")
            continue
        L.append(f"{g:<10} {a['sessions']:>8} {a['turns']:>9,} {_fmt(a['units_per_turn']):>12} "
                 f"{_fmt(a['turns_per_window']):>13} {_fmt(a['repeats_per_compaction'], digits=2):>19}")
    L.append("")
    s, ci = res.get("saving_per_turn"), res.get("ci95")
    if s is None:
        L.append("SAVING per turn (treated vs baseline): NOT CHECKED -- a group is empty")
    else:
        nt = res["groups"].get("treated", {}).get("sessions", 0)
        nb = res["groups"].get("baseline", {}).get("sessions", 0)
        verdict = ("NOT ESTABLISHED (interval crosses zero)" if ci and ci[0] <= 0 <= ci[1]
                   else "established (confounded)" if ci
                   else f"TOO FEW SESSIONS FOR AN INTERVAL (n={nt} vs {nb}, "
                        f"need {MIN_SESSIONS_FOR_CI} each)")
        cis = f"[{_fmt(ci[0], True)}, {_fmt(ci[1], True)}]" if ci else "n/a"
        L.append(f"SAVING per turn (treated vs baseline): {_fmt(s, True)}  95% CI {cis}  {verdict}")
    L.append("  * repeats/compaction is a FLOOR on quality cost: it counts repeated tool calls and")
    L.append("    cannot see a ruling re-derived or contradicted.")
    L.append("")
    L.append("ESTIMATES from the levers' own logs (not evidence of a saving):")
    for comp, d in sorted(res["stream"]["estimates"].items()):
        L.append(f"  {comp:<14} acting {d.get('acting', 0):>12,.0f} tok   "
                 f"observing (would-save) {d.get('observing', 0):>12,.0f} tok")
    lat = sorted(res["stream"]["latencies_ms"])
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else None
    L.append(f"  hook latency p95: {_fmt(p95, digits=0)} ms over {len(lat)} events")
    L.append("")
    L.append("TOP MISSED SAVINGS (held out or observing):")
    for saved, comp, what, sid in res["stream"]["missed"]:
        L.append(f"  {saved:>10,.0f} tok  {comp:<12} {str(what)[:70]}")
    if not res["stream"]["missed"]:
        L.append("  none recorded")
    L.append("")
    sk = res.get("skipped") or {}
    L.append(f"stream records read: {res['stream_records']}  skipped: {sk.get('torn', 0)} torn, "
             f"{sk.get('foreign_version', 0)} other-version  (best-effort writes; a session whose "
             f"records all dropped is misgrouped as baseline)")
    return "\n".join(L)
