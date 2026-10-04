"""What a turn cost in tool reads, and what the router missed. OBSERVATIONS ONLY.

⚠ WHY THIS MODULE EXISTS. A turn recorded `tool_calls` as an INTEGER, and the existing
extractor kept only each call's NAME and INPUT -- it never looked at `tool_result`, where the
returned content lives. So a Read of 20 lines and a Read of 2,000 lines were both 1, and "did
routing save tokens" could only ever be answered with a COUNT.

Nothing here feeds a reward, a penalty or a recommendation. The replacement signal is a holdout
comparison, and these fields are its raw material.

═══ WHY IT READS FROM A STORED OFFSET RATHER THAN A FIXED TAIL ═══

The first version of this module read the last 200 KB and walked forward from the last
`assistant` entry. Measured against a real 311 MB transcript it reported ZERO tool results on a
session that had been making tool calls all day, and the reason is structural:

  * **The last `assistant` entry is the END of a turn, not its start.** In a real transcript the
    final assistant entry is followed only by metadata, so walking forward from it sees nothing.
  * **`tool_result` blocks live in `user` entries**, paired to their `tool_use` by id and
    arriving in a LATER entry than the call.
  * And a 200 KB window on a 311 MB file is 0.07% of it, so any fixed tail understates exactly
    the turns with the largest reads -- the ones that matter most to the comparison this feeds.

So this reads the bytes appended SINCE THE PREVIOUS STOP, from an offset carried in session
state. That is exact rather than approximate, removes the turn-boundary guess, removes the
truncation bias, and is cheaper: a seek and a short read instead of 200 KB.

The one incomplete case is the FIRST turn after install, where no prior offset exists. It is
reported as `turn_view_incomplete` rather than counted as zero.

═══ THE TWO KINDS OF MISS ARE RECORDED APART ═══

They have different remedies, and conflating them would teach a router to send documents that
do not exist:

  ROUTING MISS   a file was read, a .md doc DESCRIBES it, and that doc was NOT injected.
                 Remedy: route it. This is the positive signal that survives successful
                 injection -- unlike "used", which needs a tool call that a working injection
                 removes.
  CORPUS GAP     a file was read and NO doc describes it. Remedy: write one, or nothing.

READ-AFTER-INJECT is recorded separately from both, excluding reads that precede an edit of the
same file: an injected doc whose subject is then READ suggests the tier was too thin, but one
whose subject is then EDITED suggests nothing -- no outline prevents an edit.

Each read carries its OWN token cost, paired through `tool_use_id`, because the reward the owner
asked for is weighted by tokens and is negative when injecting would have cost more. A per-file
signal cannot be built from a per-turn total.
"""

import json
import time
from pathlib import Path

try:
    from attnroute.telemetry_lib import estimate_tokens_from_chars
except ImportError:          # pragma: no cover - mirrors the fallback in context_router
    def estimate_tokens_from_chars(chars, kind="markdown"):
        return max(0, int(chars / 3.3))

#: Bytes read when no prior offset exists (first turn after install). Small on purpose: the
#: first turn is reported as incomplete anyway, so there is nothing to gain by reading more.
FIRST_TURN_TAIL_BYTES = 200_000

#: A ceiling so a pathological turn cannot make the hook slow. Exceeding it is REPORTED.
MAX_SPAN_BYTES = 8_000_000

READ_TOOLS = ("Read", "Grep", "Glob", "NotebookRead")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")


def _blocks(entry: dict) -> list:
    content = entry.get("message", {}).get("content", []) if isinstance(
        entry.get("message"), dict) else []
    if not content:
        content = entry.get("content", [])
    return content if isinstance(content, list) else []


def _result_text(block: dict) -> str:
    """A tool_result's text, whatever shape it arrived in."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _target(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    return str(tool_input.get("file_path") or tool_input.get("path")
               or tool_input.get("notebook_path") or "")


def _describing_docs(read_path: str, relationships: dict) -> list:
    """Which .md docs describe this file, per the relationship map."""
    low = (read_path or "").lower().replace("\\", "/")
    if not low:
        return []
    out = []
    for md_file, info in (relationships or {}).items():
        for described in (info or {}).get("describes", []) or []:
            d = (described or "").lower()
            if d and (d in low or low.endswith(d)):
                out.append(md_file)
                break
    return out


def _at_line_start(path: Path, offset: int) -> bool:
    """Is `offset` the first byte of a line?

    WARNING: WITHOUT THIS, EVERY SPAN DROPPED ITS FIRST ENTRY. A stored offset is the file
    size after reading whole lines, so it is ALWAYS a line boundary -- and the old
    unconditional `readline()` "to discard a partial line" therefore discarded a complete
    one. The read ledger found it: a compaction marker written as the first line after the
    stored offset was never seen, so the second witness for compaction caught nothing.

    One byte is read to decide. Cheaper than being wrong.
    """
    if offset <= 0:
        return True
    try:
        with open(path, "rb") as fh:
            fh.seek(offset - 1)
            return fh.read(1) in (b'\n', b'\r')
    except OSError:
        return False


def read_span(path: Path, start_offset) -> tuple:
    """(lines, new_offset, complete). The bytes appended since `start_offset`.

    `complete` is False when no prior offset existed, or when the span exceeded
    MAX_SPAN_BYTES -- both are reported rather than silently counted as a full view.
    """
    size = path.stat().st_size
    complete = True
    if not isinstance(start_offset, int) or start_offset < 0 or start_offset > size:
        # No usable prior offset: the first turn after install, or a rotated transcript.
        start = max(0, size - FIRST_TURN_TAIL_BYTES)
        complete = False
    else:
        start = start_offset
    if size - start > MAX_SPAN_BYTES:
        start = size - MAX_SPAN_BYTES
        complete = False
    with open(path, encoding="utf-8", errors="replace") as fh:
        if start > 0 and not _at_line_start(path, start):
            fh.seek(start)
            fh.readline()      # discard the partial line this offset lands inside
        else:
            fh.seek(start)
        lines = fh.readlines()
    return lines, size, complete


def turn_read_cost(transcript_path, files_injected, relationships=None,
                   start_offset=None) -> dict:
    """Everything this module observes about the turn that just ended. -> dict of fields.

    NEVER RAISES. This runs inside a Stop hook, and an exception would cost the user their turn
    record for a measurement they did not ask for. A failure is reported as `turn_cost_error`.
    """
    started = time.perf_counter()
    out = {
        "tool_read_chars": 0,
        "tool_read_tokens_est": 0,
        "tool_result_count": 0,
        "turn_view_incomplete": False,
        "transcript_offset": None,
        "routing_misses": [],
        "corpus_gaps": [],
        "read_after_inject": [],
        # ⚠ EVERY tool_use WITH ITS PAIRED COST, UNCLASSIFIED. The three lists above are the
        #   CONFIDENT subset; this is the raw observation, so a later analysis can reclassify
        #   without re-instrumenting. In particular `Bash` reads files via cat/sed/head and no
        #   file path can be attributed to it reliably -- those reads appear here with the
        #   command as the target and are deliberately NOT folded into routing_misses, because
        #   an unreliable extraction must not feed a reward.
        "reads": [],
        "turn_cost_error": None,
    }
    try:
        if relationships is None:
            try:
                from attnroute.learner import build_file_relationship_map
                relationships = build_file_relationship_map()
            except Exception:
                relationships = {}

        path = Path(transcript_path).expanduser() if transcript_path else None
        if not path or not path.name or not path.exists():
            out["turn_cost_error"] = "transcript not found"
            out["turn_view_incomplete"] = True
            return out

        lines, new_offset, complete = read_span(path, start_offset)
        out["transcript_offset"] = new_offset
        out["turn_view_incomplete"] = not complete

        injected = {str(f) for f in (files_injected or [])}
        # tool_use_id -> (tool_name, target); results arrive in a later entry.
        calls = {}
        # tool_use_id -> result chars
        result_chars = {}
        edited = set()

        for raw in lines:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for block in _blocks(entry):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    calls[block.get("id")] = (block.get("name", ""),
                                              _target(block.get("input", {})))
                elif btype == "tool_result":
                    text = _result_text(block)
                    if not text:
                        continue
                    out["tool_read_chars"] += len(text)
                    out["tool_result_count"] += 1
                    rid = block.get("tool_use_id")
                    if rid is not None:
                        result_chars[rid] = result_chars.get(rid, 0) + len(text)

        out["tool_read_tokens_est"] = estimate_tokens_from_chars(out["tool_read_chars"], "mixed")

        for use_id, (name, target) in calls.items():
            if not target:
                continue
            if name in EDIT_TOOLS:
                edited.add(target)

        for use_id, (name, target) in calls.items():
            tokens_any = estimate_tokens_from_chars(result_chars.get(use_id, 0), "mixed")
            out["reads"].append({"tool": name, "target": target[:200],
                                 "read_tokens_est": tokens_any})

        for use_id, (name, target) in calls.items():
            if not target or name not in READ_TOOLS:
                continue
            # ⚠ PER-READ COST, paired by id. A per-turn total cannot support a per-file signal,
            #   and the reward the owner asked for is token-weighted.
            tokens = estimate_tokens_from_chars(result_chars.get(use_id, 0), "mixed")
            docs = _describing_docs(target, relationships)
            if not docs:
                out["corpus_gaps"].append({"read": target, "read_tokens_est": tokens})
                continue
            not_injected = [d for d in docs if d not in injected]
            if not_injected:
                out["routing_misses"].append({"read": target, "read_tokens_est": tokens,
                                              "would_have_routed": not_injected})
            elif target not in edited:
                out["read_after_inject"].append({"read": target, "read_tokens_est": tokens,
                                                 "injected_docs": [d for d in docs
                                                                   if d in injected]})
    except Exception as exc:          # never cost the user their turn record
        out["turn_cost_error"] = f"{type(exc).__name__}: {exc}"

    out["instrumentation_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    return out
