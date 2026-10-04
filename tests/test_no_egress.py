"""A hook makes no network call, ever.

The `UserPromptSubmit` hook was measured spending 2.32 s in
`_ssl._SSLContext.load_verify_locations`, with 7 calls to `_ssl._SSLSocket.read`, and
`huggingface_hub`, `model2vec`, `httpcore`, `asyncio` and `ssl` all in `sys.modules` by the
time it finished: the search index builds a model2vec model, and that reaches out to Hugging
Face. On the user's machine, on every prompt.

The end-to-end class at the bottom is the one with teeth, and it earns that only because the
probe proves its own instrumentation first. Measured while writing it: the hook made no
connection attempt WITHOUT the lockdown either, because the model was already cached
locally -- so "no attempt" on its own proves nothing at all.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from attnroute import no_egress

HOOK_MODULES = ("attnroute.context_router", "attnroute.session_init",
                "attnroute.telemetry_record", "attnroute.read_ledger")

PROBE = Path(__file__).resolve().parent / "egress_probe.py"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _released():
    """Never leave the guard installed: it would break every other test in the suite."""
    no_egress.release()
    no_egress.REFUSED.clear()
    yield
    no_egress.release()
    no_egress.REFUSED.clear()


class TestTheOfflineVariables:
    """The real fix. A library told it is offline uses its cache and carries on; one whose
    socket merely fails may retry, back off and hang -- in front of the user's prompt."""

    def test_every_one_of_them_is_set(self, monkeypatch):
        for key in no_egress.OFFLINE_ENV:
            monkeypatch.delenv(key, raising=False)
        no_egress.lock_down()
        for key, value in no_egress.OFFLINE_ENV.items():
            assert os.environ.get(key) == value, key

    def test_the_user_can_still_override_them(self, monkeypatch):
        """`setdefault`, not assignment: someone who deliberately set HF_HUB_OFFLINE=0 has
        said something, and a hook should not contradict them silently."""
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        no_egress.lock_down()
        assert os.environ["HF_HUB_OFFLINE"] == "0"

    def test_hugging_face_is_covered_specifically(self):
        """The measured culprit. Named so that removing it fails a test, not a hook."""
        assert "HF_HUB_OFFLINE" in no_egress.OFFLINE_ENV
        assert "TRANSFORMERS_OFFLINE" in no_egress.OFFLINE_ENV


class TestTheConnectGuard:
    """The belt: a library that ignores the variables, or a dependency added later that
    knows nothing about them, still cannot reach the network from a hook."""

    def test_a_remote_connect_is_refused_and_recorded(self):
        no_egress.lock_down()
        s = socket.socket()
        try:
            with pytest.raises(no_egress.EgressRefused) as exc:
                s.connect(("93.184.216.34", 443))
        finally:
            s.close()
        assert "93.184.216.34:443" in str(exc.value)
        assert "93.184.216.34:443" in no_egress.refused()

    def test_loopback_is_not_egress(self, monkeypatch):
        """A hook talking to a local daemon is a different question from a hook talking to
        the internet. Blocking loopback would prove nothing and could break something."""
        seen = []
        monkeypatch.setattr(no_egress, "_ORIGINAL_CONNECT",
                            lambda self, address: seen.append(address))
        no_egress.lock_down()
        socket.socket().connect(("127.0.0.1", 8080))
        assert seen == [("127.0.0.1", 8080)]
        assert no_egress.refused() == []

    def test_it_is_idempotent(self):
        no_egress.lock_down()
        first = socket.socket.connect
        no_egress.lock_down()
        assert socket.socket.connect is first

    def test_release_puts_the_real_primitives_back(self):
        connect, resolve = socket.socket.connect, socket.getaddrinfo
        no_egress.lock_down()
        assert socket.socket.connect is not connect
        no_egress.release()
        assert socket.socket.connect is connect
        assert socket.getaddrinfo is resolve


class TestTheGuardCoversTheAsyncPath:
    """A CONNECT GUARD ALONE REPORTS "NO EGRESS" WHILE AN ASYNC REQUEST GOES OUT.

    `asyncio` and `httpcore` were both in `sys.modules` after the hook ran. An async client
    opens its socket through the event loop's transport, which never calls
    `socket.socket.connect`, so a guard on connect alone would have been a false clean bill
    of health. Name resolution is the step the sync and async paths share.
    """

    def test_getaddrinfo_is_guarded_and_not_only_connect(self):
        no_egress.lock_down()
        assert socket.getaddrinfo is not no_egress._ORIGINAL_GETADDRINFO
        assert socket.socket.connect is not no_egress._ORIGINAL_CONNECT

    def test_resolving_a_remote_name_is_refused(self):
        no_egress.lock_down()
        with pytest.raises(no_egress.EgressRefused):
            socket.getaddrinfo("huggingface.co", 443)
        assert any("huggingface.co" in r for r in no_egress.refused())

    def test_resolving_a_local_name_is_not(self):
        no_egress.lock_down()
        assert socket.getaddrinfo("127.0.0.1", 80)
        assert no_egress.refused() == []

    def test_a_literal_ip_still_needs_the_connect_guard(self):
        """Which is why both exist: an IP address needs no lookup."""
        no_egress.lock_down()
        with pytest.raises(no_egress.EgressRefused):
            socket.socket().connect(("8.8.8.8", 53))


class TestTheHooksActuallyUseIt:
    """END TO END, in a subprocess, via tests/egress_probe.py."""

    def _run(self, module, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir(exist_ok=True)
        # A corpus, so the search path -- the one that builds the model2vec model -- runs.
        (claude / "depth.md").write_text("# Depth gate\nIt refuses UNKNOWN legs.\n",
                                         encoding="utf-8")
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit", "session_id": "egress-probe",
            "transcript_path": "", "cwd": str(tmp_path),
            "prompt": "search the docs for the depth gate ruling",
        })
        out = tmp_path / "attempts.json"
        env = dict(os.environ)
        # The hook runs with `cwd` inside tmp_path, so the source tree goes on the path
        # explicitly -- otherwise this would be testing an ImportError.
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run([sys.executable, str(PROBE), module, str(out)],
                           input=payload, text=True, capture_output=True, timeout=300,
                           cwd=str(tmp_path), env=env)
        assert out.is_file(), f"the probe never wrote its result:\n{r.stderr[-3000:]}"
        return json.loads(out.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("module", HOOK_MODULES)
    def test_a_hook_event_reaches_no_remote_host(self, module, tmp_path):
        result = self._run(module, tmp_path)
        assert result["self_check"] == 2, (
            "the probe did not catch its own two attempts, so its silence about the hook "
            f"proves nothing: {result}")
        assert result["during_hook"] == [], f"{module} tried to reach {result['during_hook']}"
