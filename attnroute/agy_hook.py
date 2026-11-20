#!/usr/bin/env python3
"""Antigravity (`agy`) lifecycle hook adapter for attnroute.

Translates Antigravity's camelCase `hooks.json` stdin/stdout contract into
attnroute's core modules (`context_router`, `read_ledger`, `output_cap`,
`session_state`) so the same install works natively in both Claude Code and
Antigravity.

Contract reference (Antigravity `hooks.json`):
  - SessionStart:   stdin common metadata -> stdout {"injectSteps": [...]}
  - PreInvocation:  stdin lastUserInput + common -> stdout {"injectSteps": [...]}
  - PreToolUse:     stdin toolCall.{id,name,args} -> stdout {"decision": ..., "reason": ..., "overwrite": ...}
  - PostToolUse:    stdin toolCall.{id,name,args}, error -> stdout {}
  - Stop:           stdin terminationReason + common -> stdout {"decision": "allow"}

Safety invariants (matching the Claude Code hooks):
  1. Zero network egress (`attnroute.no_egress.lock_down()`).
  2. Lazy imports: tool hooks never import `context_router` or optional ML deps.
  3. Fail-open: any unexpected error exits 0 with a non-blocking JSON response.
  4. Never auto-approve shell commands: `run_command` ALWAYS returns
     `"decision": "ask"` (respecting Antigravity's own allowlist/cache).
  5. Once-per-user-turn context injection: Antigravity fires `PreInvocation`
     before every model step in a tool loop, so we deduplicate on
     `(conversationId, lastUserInput)` to avoid re-injecting the same context
     repeatedly within a single user turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Antigravity tool names -> canonical roles
AGY_READ_TOOLS = frozenset({"read_file", "view_file"})
AGY_EDIT_TOOLS = frozenset({
    "edit_file",
    "write_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
})
AGY_SHELL_TOOLS = frozenset({"run_command"})


def _session_id(payload: dict[str, Any]) -> str:
    sid = payload.get("conversationId") or payload.get("session_id") or "agy-local"
    return str(sid)


def _cwd(payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd
    ws = payload.get("workspacePaths")
    if isinstance(ws, list) and ws and isinstance(ws[0], str) and ws[0].strip():
        return ws[0]
    return os.getcwd()


def _extract_path(args: dict[str, Any]) -> str:
    for key in ("Path", "AbsolutePath", "TargetFile", "file_path", "path"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_range(args: dict[str, Any]) -> tuple[int | None, int | None]:
    """Convert Antigravity StartLine/EndLine into (offset, limit) for read_ledger."""
    start = args.get("StartLine")
    end = args.get("EndLine")
    offset = start if isinstance(start, int) and start > 0 else None
    limit = None
    if isinstance(end, int) and end > 0:
        if offset is not None:
            limit = max(1, end - offset + 1)
        else:
            limit = end
    return offset, limit


def _estimate_read_tokens(path: str, offset: int | None, limit: int | None) -> int:
    """Estimate tokens for an Antigravity read_file/view_file call from disk.

    Antigravity's PostToolUse payload reports toolCall + error without the raw
    file body, so we size the read directly from the file on disk (bounded to
    the requested line range when StartLine/EndLine are provided).
    """
    if not path:
        return 0
    try:
        p = Path(path).expanduser()
        if not p.is_file():
            return 0
        if offset is None and limit is None:
            # Matches read_file's ~46 KB per-read cap and ~3.5 chars/token estimate
            size = min(p.stat().st_size, 46080)
            return max(0, int(size / 3.5))
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            start_idx = max(0, (offset or 1) - 1)
            max_lines = limit if limit is not None else 800
            chars = 0
            for idx, line in enumerate(fh):
                if idx < start_idx:
                    continue
                if idx >= start_idx + max_lines:
                    break
                chars += len(line)
                if chars >= 46080:
                    break
            return max(0, int(chars / 3.5))
    except OSError:
        return 0


def _inject_step(text: str) -> dict[str, Any]:
    """Build an Antigravity `injectSteps` systemMessage entry."""
    return {"systemMessage": {"systemMessage": text}}


def _prompt_dedup_file(sid: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in sid)[:80] or "local"
    return Path.home() / ".claude" / "attn_state" / f"agy_prompt_{safe}.json"


def _already_routed_prompt(sid: str, prompt: str) -> bool:
    """Return True if we already injected context for this exact prompt in this conversation."""
    digest = hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    path = _prompt_dedup_file(sid)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("digest") == digest:
                return True
    except (OSError, ValueError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"digest": digest, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass
    return False


def _wrap_powershell_cap(cmd: str, out_path: str, head_chars: int, tail_chars: int, cap_chars: int) -> str:
    """Wrap a PowerShell command so oversized output is truncated head+tail and saved to disk.

    Preserves the command's exit code via $LASTEXITCODE. Full output is always
    kept at `out_path` so nothing is lost.
    """
    esc_path = out_path.replace("'", "''")
    return (
        f"$__ar_out = '{esc_path}'; "
        f"$__ar_rc = 0; "
        f"& {{ {cmd} }} *> $__ar_out; "
        f"if ($null -ne $LASTEXITCODE) {{ $__ar_rc = $LASTEXITCODE }} elseif (-not $?) {{ $__ar_rc = 1 }}; "
        f"$__ar_raw = Get-Content -LiteralPath $__ar_out -Raw -ErrorAction SilentlyContinue; "
        f"if ($null -eq $__ar_raw) {{ $__ar_raw = '' }}; "
        f"if ($__ar_raw.Length -le {cap_chars}) {{ [Console]::Write($__ar_raw) }} "
        f"else {{ "
        f"[Console]::Write($__ar_raw.Substring(0, {head_chars})); "
        f"[Console]::Write(\"`n`n[attnroute output_cap: $($__ar_raw.Length) chars -> head {head_chars} + tail {tail_chars}; full output: $__ar_out]`n`n\"); "
        f"[Console]::Write($__ar_raw.Substring($__ar_raw.Length - {tail_chars})); "
        f"}}; "
        f"exit $__ar_rc"
    )


def _handle_session_start(payload: dict[str, Any]) -> dict[str, Any]:
    sid = _session_id(payload)
    cwd = _cwd(payload)
    transcript = payload.get("transcriptPath")

    from attnroute import read_ledger, session_state

    read_ledger.handle({
        "hook_event_name": "SessionStart",
        "session_id": sid,
        "transcript_path": transcript,
        "source": "startup",
    })

    # If the session (or repo) already has pinned rulings / explicit notes, surface them.
    state = session_state.load(sid)
    if state.get("notes"):
        built = session_state.handback(state, {}, repo=cwd)
        text = built.get("text") or ""
        if text.strip():
            session_state.mark_handed_back(state, built.get("included") or [])
            session_state.save(sid, state)
            return {"injectSteps": [_inject_step(text)]}
    return {}


def _handle_pre_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("lastUserInput")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}

    sid = _session_id(payload)
    if _already_routed_prompt(sid, prompt):
        return {}

    cwd = _cwd(payload)
    old_cwd = os.getcwd()
    try:
        if Path(cwd).is_dir():
            os.chdir(cwd)
    except OSError:
        pass

    try:
        import copy
        from attnroute import context_router as cr

        if cr.observe_only():
            return {}

        if cr.CORPUS_ROOT is not None:
            docs_root = cr.CORPUS_ROOT
        else:
            try:
                docs_root = cr.resolve_docs_root()
            except FileNotFoundError:
                # Antigravity workspaces do not require a .claude/ directory;
                # fall back cleanly to the workspace root for source routing.
                docs_root = Path(cwd)
        state_file = cr.get_state_file()
        prev_state = cr.load_state(state_file)
        state = copy.deepcopy(prev_state)

        state, activated = cr.update_attention(state, prompt)
        output, stats = cr.build_context_output(state, docs_root)
        stats["total_chars"] = len(output)

        cr.append_history(state, prev_state, activated, prompt, stats)
        cr.save_state(state_file, state)

        # Skip Claude-Code-specific plugins (BurnRate reads ~/.claude/projects,
        # VerifyFirst tracks Claude Read/Edit tool calls) and stay completely
        # silent on zero-hit conversational turns so chat turns pay 0 tokens.
        if (stats.get("hot", 0) == 0 and stats.get("warm", 0) == 0) or not output.strip():
            return {}
        return {"injectSteps": [_inject_step(output)]}
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass


def _handle_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return {"decision": "ask"}

    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        args = {}
    sid = _session_id(payload)

    # 1. File reads -> read_ledger
    if tool_name in AGY_READ_TOOLS:
        path = _extract_path(args)
        if not path:
            return {"decision": "allow"}
        offset, limit = _extract_range(args)
        from attnroute import read_ledger

        claude_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "transcript_path": payload.get("transcriptPath"),
            "tool_name": "Read",
            "tool_input": {
                "file_path": path,
                **({"offset": offset} if offset is not None else {}),
                **({"limit": limit} if limit is not None else {}),
            },
        }
        res = read_ledger.handle(claude_payload)
        if isinstance(res, dict):
            hso = res.get("hookSpecificOutput") or {}
            if hso.get("permissionDecision") == "deny":
                reason = str(hso.get("permissionDecisionReason") or "Unchanged file already read in this session.")
                return {"decision": "deny", "reason": reason}
        return {"decision": "allow"}

    # 2. Shell commands -> output_cap (NEVER returns "allow" — always "ask")
    if tool_name in AGY_SHELL_TOOLS:
        cmd = args.get("CommandLine")
        if not isinstance(cmd, str) or not cmd.strip():
            return {"decision": "ask"}
        if args.get("IsDaemon"):
            return {"decision": "ask"}

        from attnroute import output_cap as oc

        if oc.ESCAPE in cmd:
            return {"decision": "ask"}

        lever = oc.acting_source()
        if not lever["on"]:
            return {"decision": "ask"}

        arm = oc.arm_for(sid, cmd)
        if arm["arm"] != "cap":
            return {"decision": "ask"}

        if sys.platform == "win32":
            # Avoid wrapping commands with explicit PowerShell trap or interactive heredocs
            if "trap " in cmd.lower() or "@'" in cmd or '@"' in cmd:
                return {"decision": "ask"}
            try:
                d = oc.outputs_dir()
                d.mkdir(parents=True, exist_ok=True)
                oc.sweep(d)
                digest = hashlib.sha256(
                    f"{sid}\x1f{cmd}\x1f{os.getpid()}".encode("utf-8", "surrogatepass")
                ).hexdigest()
                out_path = str(d / f"{digest[:16]}.log")
            except OSError:
                return {"decision": "ask"}
            wrapped = _wrap_powershell_cap(cmd, out_path, oc.HEAD_CHARS, oc.TAIL_CHARS, oc.CAP_CHARS)
            return {"decision": "ask", "overwrite": {"CommandLine": wrapped}}

        # POSIX shell (macOS / Linux): reuse output_cap's exact battle-tested wrapper
        claude_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }
        res = oc.handle(claude_payload)
        if isinstance(res, dict):
            updated = (res.get("hookSpecificOutput") or {}).get("updatedInput") or {}
            wrapped_cmd = updated.get("command")
            if isinstance(wrapped_cmd, str) and wrapped_cmd != cmd:
                return {"decision": "ask", "overwrite": {"CommandLine": wrapped_cmd}}
        return {"decision": "ask"}

    return {"decision": "ask"}


def _handle_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return {}

    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        args = {}
    sid = _session_id(payload)

    if tool_name in AGY_EDIT_TOOLS:
        path = _extract_path(args)
        if path:
            from attnroute import read_ledger

            read_ledger.handle({
                "hook_event_name": "PostToolUse",
                "session_id": sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": path},
            })
        return {}

    if tool_name in AGY_READ_TOOLS:
        path = _extract_path(args)
        if path and not payload.get("error"):
            offset, limit = _extract_range(args)
            tokens = _estimate_read_tokens(path, offset, limit)
            from attnroute import read_ledger

            led = read_ledger.load(sid)
            key = read_ledger.read_key(path, offset, limit)
            if not led.take_shadow_deny(key):
                led.record(path, offset, limit, tokens=tokens)
            read_ledger.save(sid, led)
        return {}

    return {}


def _handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    sid = _session_id(payload)
    cwd = _cwd(payload)
    transcript = payload.get("transcriptPath")

    from attnroute import read_ledger, session_state

    read_ledger.handle({
        "hook_event_name": "Stop",
        "session_id": sid,
        "transcript_path": transcript,
    })
    session_state.hook({
        "hook_event_name": "Stop",
        "session_id": sid,
        "transcript_path": transcript,
        "cwd": cwd,
    }, repo=cwd)

    # Never trap the agent in a Stop continuation loop
    return {"decision": "allow"}


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one Antigravity hook event -> JSON-serializable response dict."""
    event = str(payload.get("hookEventName") or "")
    if event == "SessionStart":
        return _handle_session_start(payload)
    if event == "PreInvocation":
        return _handle_pre_invocation(payload)
    if event == "PreToolUse":
        return _handle_pre_tool_use(payload)
    if event == "PostToolUse":
        return _handle_post_tool_use(payload)
    if event == "Stop":
        return _handle_stop(payload)
    return {}


def main(argv: list[str] | None = None) -> int:
    """Antigravity hook entry point. Always exits 0 with valid JSON stdout."""
    from attnroute.no_egress import lock_down

    lock_down()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("{}")
        return 0
    if not isinstance(payload, dict):
        print("{}")
        return 0
    event = str(payload.get("hookEventName") or "")
    try:
        out = handle(payload)
    except Exception as exc:
        print(f"[attnroute] agy_hook error: {exc!r}", file=sys.stderr)
        if event == "PreToolUse":
            print(json.dumps({"decision": "ask"}))
        elif event == "Stop":
            print(json.dumps({"decision": "allow"}))
        else:
            print("{}")
        return 0
    print(json.dumps(out if isinstance(out, dict) else {}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
