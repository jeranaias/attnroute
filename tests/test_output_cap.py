"""The output cap: a long Bash result reaches the conversation as head + tail, and nothing else
about the command changes -- not its exit status, not its working directory, not its output on
disk, and not the permission flow.

The rewrite is exercised in a REAL shell, because every property worth having here is a property
of how bash runs the rewritten text, and a string comparison would pass a rewrite that breaks
them all.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from attnroute import output_cap as oc


def _bash():
    """A POSIX bash, or None. WSL's System32 bash.exe is NOT one: it runs in another OS."""
    b = shutil.which("bash")
    if not b:
        return None
    if sys.platform == "win32" and "system32" in b.lower():
        return None
    return b


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no POSIX bash on this runner")


def _run(script: str, cwd):
    return subprocess.run([BASH, "-c", script], cwd=str(cwd), capture_output=True, text=True,
                          timeout=60)


@pytest.fixture
def act(monkeypatch, tmp_path):
    monkeypatch.setenv(oc.ACT_ENV, "1")
    monkeypatch.setattr(oc, "outputs_dir", lambda: tmp_path / "outputs")
    # Pin the arm: the bucket depends on the command text, so without this a test would be a
    # one-in-ten coin flip for reasons that have nothing to do with the code.
    monkeypatch.setattr(oc, "arm_for", lambda sid, cmd, seed="x": {"arm": "cap", "bucket": 50,
                                                                   "seed": seed})
    return tmp_path


def _pre(cmd, **extra):
    ti = {"command": cmd, **extra}
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": ti,
            "session_id": "s1"}


class TestEligibility:
    def test_positive_control_a_plain_bash_command_is_eligible(self):
        assert oc.eligible("Bash", {"command": "ls"}) == (True, "")

    def test_read_is_never_capped(self):
        assert oc.eligible("Read", {"file_path": "/x"})[0] is False

    def test_background_command_is_left_alone(self):
        assert oc.eligible("Bash", {"command": "sleep 9", "run_in_background": True})[0] is False

    def test_a_command_with_its_own_trap_is_declined(self):
        """A trap is not a stack: the command's EXIT trap would replace the one that prints the
        captured output, and all of it would vanish with no marker."""
        ok, why = oc.eligible("Bash", {"command": 'trap "rm -rf $t" EXIT; seq 1 4000'})
        assert ok is False and "trap" in why

    def test_escape_marker_is_honoured(self):
        assert oc.eligible("Bash", {"command": "cat big.log  # attnroute:full"})[0] is False


class TestHookPayload:
    def test_positive_control_acting_rewrites_the_command(self, act):
        out = oc.handle(_pre("echo hi"))
        assert out is not None
        assert out["hookSpecificOutput"]["updatedInput"]["command"] != "echo hi"

    def test_never_sets_a_permission_decision(self, act):
        """updatedInput WITH "allow" would auto-approve the command; a cap must not do that."""
        out = oc.handle(_pre("echo hi"))
        assert "permissionDecision" not in out["hookSpecificOutput"]

    def test_other_tool_input_fields_survive(self, act):
        out = oc.handle(_pre("echo hi", timeout=1234, description="d"))
        ui = out["hookSpecificOutput"]["updatedInput"]
        assert ui["timeout"] == 1234 and ui["description"] == "d"

    def test_observe_by_default_changes_nothing(self, monkeypatch):
        monkeypatch.delenv(oc.ACT_ENV, raising=False)
        assert oc.handle(_pre("echo hi")) is None

    def test_held_out_command_is_left_alone(self, act, monkeypatch):
        monkeypatch.setattr(oc, "arm_for", lambda *a, **k: {"arm": "held-out", "bucket": 3,
                                                            "seed": "x"})
        assert oc.handle(_pre("echo hi")) is None

    def test_arm_is_deterministic(self):
        assert oc.arm_for("s", "ls -la") == oc.arm_for("s", "ls -la")


class TestObserveLog:
    def test_posttooluse_logs_would_save_for_a_big_result(self, monkeypatch, tmp_path):
        monkeypatch.delenv(oc.ACT_ENV, raising=False)
        monkeypatch.setattr(oc.Path, "home", lambda: tmp_path)
        p = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s",
             "tool_input": {"command": "x"}, "tool_response": {"stdout": "y" * 40000}}
        assert oc.handle(p) is None
        rec = json.loads((tmp_path / ".claude" / "telemetry" / oc.LOG_NAME).read_text()
                         .splitlines()[-1])
        assert rec["would_save_tokens_est"] > 0 and rec["capped"] is False

    def test_small_result_would_save_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(oc.Path, "home", lambda: tmp_path)
        p = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s",
             "tool_input": {"command": "x"}, "tool_response": {"stdout": "short"}}
        oc.handle(p)
        rec = json.loads((tmp_path / ".claude" / "telemetry" / oc.LOG_NAME).read_text()
                         .splitlines()[-1])
        assert rec["would_save_tokens_est"] == 0


@needs_bash
class TestRewriteInARealShell:
    def _wrapped(self, cmd, tmp_path):
        f = (tmp_path / "out.log").as_posix()
        return oc.wrap(cmd, f), tmp_path / "out.log"

    def test_positive_control_a_big_output_is_capped_head_and_tail(self, tmp_path):
        script, f = self._wrapped(
            "printf 'HEADMARK\\n'; head -c 20000 /dev/zero | tr '\\0' 'x'; printf '\\nTAILMARK\\n'",
            tmp_path)
        r = _run(script, tmp_path)
        assert "HEADMARK" in r.stdout and "TAILMARK" in r.stdout
        assert "[attnroute] output capped" in r.stdout
        assert len(r.stdout) < 6000
        assert f.stat().st_size > 20000          # the full output is kept on disk

    def test_marker_numbers_are_expanded_not_literal(self, tmp_path):
        script, _ = self._wrapped("head -c 20000 /dev/zero | tr '\\0' 'x'", tmp_path)
        out = _run(script, tmp_path).stdout
        assert f"{oc.HEAD_CHARS + oc.TAIL_CHARS} of 20000 chars shown" in out
        assert "$((" not in out and "$__ar_n" not in out

    def test_small_output_passes_through_whole_and_leaves_no_file(self, tmp_path):
        script, f = self._wrapped("echo hello", tmp_path)
        r = _run(script, tmp_path)
        assert r.stdout.strip() == "hello"
        assert "[attnroute]" not in r.stdout
        assert not f.exists()

    def test_exit_status_is_preserved(self, tmp_path):
        script, _ = self._wrapped("echo out; false", tmp_path)
        assert _run(script, tmp_path).returncode == 1
        script, _ = self._wrapped("echo out; exit 7", tmp_path)
        r = _run(script, tmp_path)
        assert r.returncode == 7 and "out" in r.stdout   # exit inside still prints

    def test_big_output_then_exit_n_still_reaches_stdout(self, tmp_path):
        """The OUTPUT must arrive, not just the status: with an early exit the trap runs while
        the group's redirection is live, and without fd 9 the cap went back into the file."""
        script, _ = self._wrapped(
            "printf 'HEADMARK\\n'; head -c 20000 /dev/zero | tr '\\0' 'x'; "
            "printf '\\nTAILMARK\\n'; exit 7", tmp_path)
        r = _run(script, tmp_path)
        assert r.returncode == 7
        assert "HEADMARK" in r.stdout and "TAILMARK" in r.stdout
        assert "[attnroute] output capped" in r.stdout

    def test_small_output_then_exit_n_reaches_stdout(self, tmp_path):
        script, _ = self._wrapped("echo small-body; exit 3", tmp_path)
        r = _run(script, tmp_path)
        assert r.returncode == 3 and r.stdout.strip() == "small-body"

    def test_cd_persists_in_the_same_shell(self, tmp_path):
        (tmp_path / "sub").mkdir()
        script, _ = self._wrapped("cd sub", tmp_path)
        r = _run(script + "pwd\n", tmp_path)
        assert r.stdout.strip().endswith("sub")

    def test_stderr_is_captured_too(self, tmp_path):
        script, _ = self._wrapped("echo err 1>&2", tmp_path)
        assert "err" in _run(script, tmp_path).stdout

    def test_heredoc_in_the_command_survives(self, tmp_path):
        script, _ = self._wrapped("cat <<'EOF'\nline one\nEOF", tmp_path)
        assert "line one" in _run(script, tmp_path).stdout

    def test_single_quotes_in_the_path_are_safe(self, tmp_path):
        d = tmp_path / "it's"
        d.mkdir()
        f = (d / "o.log").as_posix()
        r = _run(oc.wrap("echo ok", f), tmp_path)
        assert r.stdout.strip() == "ok"


def test_registered_for_the_egress_suite():
    """A hook module must be in the egress suite's list, or CI never checks it."""
    src = (os.path.join(os.path.dirname(__file__), "test_no_egress.py"))
    with open(src, encoding="utf-8") as fh:
        assert "attnroute.output_cap" in fh.read()


class TestSweep:
    def test_positive_control_old_outputs_are_removed(self, tmp_path):
        old = tmp_path / "a.log"
        old.write_text("x")
        os.utime(old, (1, 1))
        assert oc.sweep(tmp_path, now=10 * oc.OUTPUT_TTL_S) == 1 and not old.exists()

    def test_fresh_outputs_are_kept(self, tmp_path):
        f = tmp_path / "b.log"
        f.write_text("x")
        assert oc.sweep(tmp_path) == 0 and f.exists()

    def test_count_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oc, "OUTPUT_KEEP", 3)
        for i in range(6):
            (tmp_path / f"{i}.log").write_text("x")
        oc.sweep(tmp_path)
        assert len(list(tmp_path.glob("*.log"))) == 3
