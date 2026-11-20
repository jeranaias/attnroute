"""Tests for the Antigravity (`agy`) hook adapter and dual-runtime installer."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from attnroute import agy_hook, installer, output_cap, read_ledger, session_state


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~/.claude and ~/.gemini state to tmp_path so live user state is never touched."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(output_cap.ACT_ENV, raising=False)
    monkeypatch.delenv(read_ledger.ACT_ENV, raising=False)
    monkeypatch.delenv("ATTNROUTE_OBSERVE_ONLY", raising=False)
    return tmp_path


class TestAgyReadLedger:
    def test_read_edit_invalidation_and_second_ask_escape(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(read_ledger.ACT_ENV, "1")
        monkeypatch.setattr(
            read_ledger, "holdout", lambda *a, **k: {"arm": "ledger", "bucket": 99, "seed": "t"}
        )

        big_file = isolated_home / "module.py"
        big_file.write_text("x = 1\n" * 600, encoding="utf-8")  # > MIN_TOKENS_TO_NOTICE

        sid = "conv-agy-read-1"
        pre = {
            "hookEventName": "PreToolUse",
            "conversationId": sid,
            "toolCall": {"id": "t1", "name": "read_file", "args": {"Path": str(big_file)}},
        }
        post = {
            "hookEventName": "PostToolUse",
            "conversationId": sid,
            "toolCall": {"id": "t1", "name": "read_file", "args": {"Path": str(big_file)}},
        }

        # 1st read: allowed and recorded
        assert agy_hook.handle(pre) == {"decision": "allow"}
        assert agy_hook.handle(post) == {}

        # 2nd identical read (unchanged): denied with explanation
        res2 = agy_hook.handle(pre)
        assert res2["decision"] == "deny"
        assert "reason" in res2 and res2["reason"]

        # 3rd ask (escape hatch): served in full so the model is never stranded
        res3 = agy_hook.handle(pre)
        assert res3 == {"decision": "allow"}

        # Edit invalidates the cached read entry
        edit_post = {
            "hookEventName": "PostToolUse",
            "conversationId": sid,
            "toolCall": {"id": "t2", "name": "edit_file", "args": {"Path": str(big_file)}},
        }
        assert agy_hook.handle(edit_post) == {}
        assert agy_hook.handle(pre) == {"decision": "allow"}

    def test_ranged_reads_have_distinct_keys(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(read_ledger.ACT_ENV, "1")
        monkeypatch.setattr(
            read_ledger, "holdout", lambda *a, **k: {"arm": "ledger", "bucket": 99, "seed": "t"}
        )

        big_file = isolated_home / "ranged.py"
        big_file.write_text("line_content_padding_abcdef = 1\n" * 400, encoding="utf-8")
        sid = "conv-agy-range"

        r1 = {
            "hookEventName": "PreToolUse",
            "conversationId": sid,
            "toolCall": {
                "id": "r1",
                "name": "read_file",
                "args": {"Path": str(big_file), "StartLine": 1, "EndLine": 120},
            },
        }
        r1_post = dict(r1, hookEventName="PostToolUse")
        r2 = {
            "hookEventName": "PreToolUse",
            "conversationId": sid,
            "toolCall": {
                "id": "r2",
                "name": "read_file",
                "args": {"Path": str(big_file), "StartLine": 121, "EndLine": 240},
            },
        }

        assert agy_hook.handle(r1) == {"decision": "allow"}
        assert agy_hook.handle(r1_post) == {}
        # Different line range must be allowed
        assert agy_hook.handle(r2) == {"decision": "allow"}
        # Exact same line range gets noticed/denied
        assert agy_hook.handle(r1)["decision"] == "deny"


class TestAgyOutputCap:
    def test_run_command_never_returns_allow_even_when_acting(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(output_cap.ACT_ENV, "1")
        monkeypatch.setattr(
            output_cap, "arm_for", lambda *a, **k: {"arm": "cap", "bucket": 99, "seed": "t"}
        )

        payload = {
            "hookEventName": "PreToolUse",
            "conversationId": "conv-shell-1",
            "toolCall": {
                "id": "c1",
                "name": "run_command",
                "args": {"CommandLine": "pytest tests/"},
            },
        }
        res = agy_hook.handle(payload)
        assert res["decision"] == "ask"
        assert "overwrite" in res
        assert res["overwrite"]["CommandLine"] != "pytest tests/"

    def test_daemon_and_escape_marker_are_never_rewritten(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(output_cap.ACT_ENV, "1")
        monkeypatch.setattr(
            output_cap, "arm_for", lambda *a, **k: {"arm": "cap", "bucket": 99, "seed": "t"}
        )

        daemon_payload = {
            "hookEventName": "PreToolUse",
            "conversationId": "conv-shell-2",
            "toolCall": {
                "id": "c2",
                "name": "run_command",
                "args": {"CommandLine": "npm run dev", "IsDaemon": True},
            },
        }
        assert agy_hook.handle(daemon_payload) == {"decision": "ask"}

        escape_payload = {
            "hookEventName": "PreToolUse",
            "conversationId": "conv-shell-2",
            "toolCall": {
                "id": "c3",
                "name": "run_command",
                "args": {"CommandLine": f"echo ok # {output_cap.ESCAPE}"},
            },
        }
        assert agy_hook.handle(escape_payload) == {"decision": "ask"}

    def test_observe_mode_leaves_command_untouched(self, isolated_home: Path):
        payload = {
            "hookEventName": "PreToolUse",
            "conversationId": "conv-shell-obs",
            "toolCall": {
                "id": "c4",
                "name": "run_command",
                "args": {"CommandLine": "echo hi"},
            },
        }
        assert agy_hook.handle(payload) == {"decision": "ask"}


class TestAgySessionAndPreInvocation:
    def test_session_start_surfaces_pinned_notes(self, isolated_home: Path):
        sid = "conv-session-notes"
        st = session_state.load(sid)
        session_state.add_note(st, "Never bypass Signet capability gates", kind="ruling")
        session_state.save(sid, st)

        res = agy_hook.handle({
            "hookEventName": "SessionStart",
            "conversationId": sid,
            "cwd": str(isolated_home),
        })
        assert "injectSteps" in res
        msg = res["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert "Never bypass Signet capability gates" in msg

    def test_pre_invocation_deduplicates_within_same_user_turn(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls = {"n": 0}

        from attnroute import context_router as cr

        monkeypatch.setattr(cr, "observe_only", lambda: False)
        monkeypatch.setattr(cr, "update_attention", lambda st, p: (st, ["src/app.py"]))

        def fake_build(st, root):
            calls["n"] += 1
            return ("## Routed Context\n- src/app.py", {"hot": 1, "warm": 0, "cold": 0})

        monkeypatch.setattr(cr, "build_context_output", fake_build)
        monkeypatch.setattr(cr, "append_history", lambda *a, **k: None)
        monkeypatch.setattr(cr, "save_state", lambda *a, **k: None)

        payload = {
            "hookEventName": "PreInvocation",
            "conversationId": "conv-dedup",
            "cwd": str(isolated_home),
            "lastUserInput": "Fix the auth race condition",
            "invocationNum": 1,
        }
        res1 = agy_hook.handle(payload)
        assert "injectSteps" in res1
        assert "Routed Context" in res1["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert calls["n"] == 1

        # Second model call inside the SAME user turn must not re-inject
        payload["invocationNum"] = 2
        res2 = agy_hook.handle(payload)
        assert res2 == {}
        assert calls["n"] == 1

        # Next user message triggers routing again
        payload["lastUserInput"] = "Now add unit tests for refresh tokens"
        res3 = agy_hook.handle(payload)
        assert "injectSteps" in res3
        assert calls["n"] == 2

    def test_pre_invocation_stays_silent_on_zero_hit_chat_turns(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from attnroute import context_router as cr

        monkeypatch.setattr(cr, "observe_only", lambda: False)
        monkeypatch.setattr(cr, "update_attention", lambda st, p: (st, []))
        monkeypatch.setattr(
            cr,
            "build_context_output",
            lambda st, root: ("Hot: 0 Warm: 0 Cold: 0", {"hot": 0, "warm": 0, "cold": 0}),
        )
        monkeypatch.setattr(cr, "append_history", lambda *a, **k: None)
        monkeypatch.setattr(cr, "save_state", lambda *a, **k: None)

        res = agy_hook.handle({
            "hookEventName": "PreInvocation",
            "conversationId": "conv-chat-only",
            "cwd": str(isolated_home),
            "lastUserInput": "Help me reply to a LinkedIn comment",
            "invocationNum": 1,
        })
        assert res == {}

    def test_stop_never_traps_in_continuation_loop(self, isolated_home: Path):
        res = agy_hook.handle({
            "hookEventName": "Stop",
            "conversationId": "conv-stop",
            "cwd": str(isolated_home),
            "terminationReason": "NO_TOOL_CALL",
        })
        assert res.get("decision") != "continue"

    def test_main_is_fail_open_on_bad_json(self, monkeypatch: pytest.MonkeyPatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{not valid json"))
        rc = agy_hook.main()
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert json.loads(out) == {}


class TestAgyInstaller:
    def test_build_and_merge_agy_hooks_preserves_existing_hooks(self, isolated_home: Path):
        hooks_path = isolated_home / ".gemini" / "config" / "hooks.json"
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "custom-linter": {
                "PostToolUse": [
                    {"matcher": "write_file", "hooks": [{"type": "command", "command": "./lint.sh"}]}
                ]
            }
        }
        hooks_path.write_text(json.dumps(existing), encoding="utf-8")

        written = installer.install_agy_hooks("python3", hooks_file=hooks_path)
        assert written == hooks_path
        assert hooks_path.with_suffix(".json.bak").exists()

        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        # Existing user hooks untouched
        assert "custom-linter" in data
        # attnroute hook entry follows Antigravity hooks.json contract
        ar = data["attnroute"]
        assert ar["enabled"] is True
        # Flat arrays for SessionStart, PreInvocation, Stop
        for flat_ev in ("SessionStart", "PreInvocation", "Stop"):
            assert isinstance(ar[flat_ev], list) and len(ar[flat_ev]) == 1
            assert "attnroute.agy_hook" in ar[flat_ev][0]["command"]
        # Matcher-grouped arrays for PreToolUse, PostToolUse
        for grouped_ev in ("PreToolUse", "PostToolUse"):
            assert isinstance(ar[grouped_ev], list) and len(ar[grouped_ev]) == 1
            group = ar[grouped_ev][0]
            assert "matcher" in group and "hooks" in group
            assert "attnroute.agy_hook" in group["hooks"][0]["command"]
