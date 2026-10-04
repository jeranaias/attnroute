"""Output cap: a long Bash result enters the conversation as its head and tail, not in full.

WHY THIS EXISTS. A tool result stays in context for every remaining turn of its window, so a
4,000-line test log is paid for hundreds of times. Measured across three long sessions, capping
results at ~1k tokens is worth about 1% of the cost-weighted bill -- small, because most result
tokens sit in many SMALL results, but it is automatic and has no wrong-answer risk as long as
the parts that carry answers survive. Errors, failing tests and exit summaries live at the END
of an output, so the tail is kept larger than the head.

THE MECHANISM, verified against the installed CLI rather than assumed. A PreToolUse hook that
returns `hookSpecificOutput.updatedInput` WITHOUT a `permissionDecision` replaces the tool's
arguments and leaves the permission flow exactly as it was: the CLI only applies updatedInput
on its own path when `permissionBehavior === undefined`. Returning "allow" alongside it would
auto-approve the command, which a cap has no business doing -- so this module never sets one.

THE REWRITE, and the four properties it must keep:

  1. EXIT STATUS. The command's own status is what the caller sees. The output goes to a file
     and the trap re-raises the status, so a failing test run still fails.
  2. WORKING DIRECTORY. The command runs in a `{ ...; }` group in the CURRENT shell, not a
     pipeline or a subshell, so a `cd` inside it persists the way it would have.
  3. NOTHING LOST. The full output is kept on disk under ~/.claude/telemetry/outputs/ and the
     marker names the file, so the rest is one Read away. An `exit` inside the command still
     reaches the EXIT trap, which prints the capped output before the shell ends.
  4. NO NEW PROCESS ON THE HOT PATH. The capping is plain shell (wc, head, tail), so a Bash call
     pays no interpreter start for it.

NEVER REWRITTEN: background commands, commands carrying the escape marker `attnroute:full`, and
anything that is not the Bash tool. Read is never capped -- an explicit range is the caller
saying what it wants, and an unranged Read is already bounded by the tool itself.

DEFAULT IS OBSERVE, like the ledger: the PostToolUse half measures each Bash result and logs what
the cap WOULD have saved, and the command is not touched unless ATTNROUTE_CAP_ACT is set.
"""

import hashlib
import json
import os
from pathlib import Path

#: Results at or under this many characters are never touched. ~1.5k tokens of mixed output:
#: a cap that trims a result barely over its size saves nothing worth the marker.
CAP_CHARS = 6000

#: What survives a cap. The tail is larger because that is where the answer usually is.
HEAD_CHARS = 1500
TAIL_CHARS = 2500

#: What the marker itself costs, measured by writing one.
MARKER_TOKENS_EST = 60

ESCAPE = "attnroute:full"
ACT_ENV = "ATTNROUTE_CAP_ACT"
LOG_NAME = "output_cap.jsonl"

#: Per-command holdout: this share of commands is never capped, so the arms compare.
HOLDOUT_PCT = 10


def acting() -> bool:
    """May the cap change a command, or only log what it would have done?"""
    return os.environ.get(ACT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def outputs_dir() -> Path:
    return Path.home() / ".claude" / "telemetry" / "outputs"


def _bucket(*parts) -> int:
    """A stable 0-99 bucket; `hash()` is randomised per process and would not replay."""
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def arm_for(session_id: str, command: str, seed: str = "attnroute-v1") -> dict:
    """Which arm is this command in? Deterministic and logged, so the split can be re-derived."""
    b = _bucket(seed, session_id, "cap", command)
    return {"arm": "held-out" if b < HOLDOUT_PCT else "cap", "bucket": b, "seed": seed}


def eligible(tool_name: str, tool_input) -> tuple:
    """(ok, reason). Is this a call the cap may rewrite at all?"""
    if tool_name != "Bash":
        return False, "not the Bash tool"
    if not isinstance(tool_input, dict):
        return False, "no tool input"
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return False, "no command"
    if tool_input.get("run_in_background"):
        return False, "background command: its output is read later, not returned now"
    if ESCAPE in cmd:
        return False, "the caller asked for the full output"
    return True, ""


def _sq(s: str) -> str:
    """Single-quote for POSIX sh."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def wrap(command: str, out_path: str) -> str:
    """The rewritten command. See the module docstring for the four properties it keeps.

    The group is opened and closed on their own lines so a heredoc or a trailing comment in
    the original cannot swallow the closing brace.
    """
    f = _sq(out_path)
    marker = (
        f"[attnroute] output capped: $((__ar_h + __ar_t)) of $__ar_n chars shown "
        f"(head {HEAD_CHARS} + tail {TAIL_CHARS}). "
        f"Full output: {out_path} -- Read it, or re-run with '# {ESCAPE}' to see everything."
    )
    m = marker.replace("'", "'\"'\"'")
    show = (
        "__ar_show() { __ar_rc=$?; "
        f"if [ -f {f} ]; then "
        f"__ar_n=$(wc -c < {f} | tr -d ' '); "
        f"if [ \"$__ar_n\" -gt {CAP_CHARS} ]; then "
        f"__ar_h={HEAD_CHARS}; __ar_t={TAIL_CHARS}; "
        f"head -c {HEAD_CHARS} {f}; printf '\\n...\\n{m}\\n...\\n'; tail -c {TAIL_CHARS} {f}; "
        f"else cat {f}; rm -f {f}; fi; fi; "
        "return $__ar_rc; }; "
        "trap '__ar_show' EXIT"
    )
    return f"{show}\n{{\n{command}\n}} > {f} 2>&1\n"


def capped_chars(n: int) -> int:
    """How many characters of an n-character result would reach the conversation."""
    return n if n <= CAP_CHARS else HEAD_CHARS + TAIL_CHARS + MARKER_TOKENS_EST * 4


def _result_text(payload: dict) -> str:
    r = payload.get("tool_response") if isinstance(payload, dict) else None
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        return "".join(str(r.get(k) or "") for k in ("stdout", "stderr", "output"))
    return ""


def _tokens(chars: int) -> int:
    return max(0, int(chars / 3.3))


def _log(record: dict) -> None:
    try:
        d = Path.home() / ".claude" / "telemetry"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass          # a measurement must never cost the user their turn


def handle(payload: dict) -> dict | None:
    """One hook event -> the payload to print, or None."""
    event = str(payload.get("hook_event_name") or "")
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    sid = str(payload.get("session_id") or "x")
    aid = payload.get("agent_id")

    ok, why = eligible(tool, ti)
    if not ok:
        return None
    cmd = ti["command"]
    arm = arm_for(sid, cmd)

    if event == "PostToolUse":
        # The measurement half, and the only half that runs while observing.
        n = len(_result_text(payload))
        would = max(0, _tokens(n) - _tokens(capped_chars(n)))
        _log({"event": "output_cap", "phase": "result", "session_id": sid, "agent_id": aid,
              "chars": n, "would_save_tokens_est": would if n > CAP_CHARS else 0,
              "capped": bool(acting() and arm["arm"] == "cap" and n > CAP_CHARS),
              "acting": acting(), **arm})
        return None

    if event != "PreToolUse" or not acting() or arm["arm"] != "cap":
        return None

    try:
        d = outputs_dir()
        d.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{sid}\x1f{cmd}\x1f{os.getpid()}".encode()).hexdigest()
        name = f"{digest[:16]}.log"
        out_path = (d / name).as_posix()
    except OSError:
        return None   # nowhere to keep the full output, so the cap does not apply
    new_input = dict(ti)
    new_input["command"] = wrap(cmd, out_path)
    # updatedInput WITHOUT permissionDecision: the input changes, the permission flow does not.
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new_input}}


def main(argv=None) -> int:
    """Hook entry point. Never fails the caller's turn: every path exits 0."""
    import sys

    from attnroute.no_egress import lock_down
    lock_down()                 # a hook makes no network call, ever
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = handle(payload)
    except Exception as exc:                       # noqa: BLE001 - see the docstring
        _log({"event": "output_cap_error", "error": repr(exc)[:300]})
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":        # pragma: no cover - exercised through main()
    raise SystemExit(main())
