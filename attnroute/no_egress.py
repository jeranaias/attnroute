"""A hook makes no network call, ever.

WARNING: IT DID, ON EVERY PROMPT, AND NOBODY COULD HAVE SEEN IT. Profiling the
`UserPromptSubmit` hook turned up 2.32 s in `_ssl._SSLContext.load_verify_locations` and
9.34 s waiting on a thread lock, with `huggingface_hub`, `model2vec`, `httpcore` and `ssl`
all in `sys.modules` by the time the hook finished. The search index constructs a model2vec
StaticModel, and that reaches out to Hugging Face.

Three things are wrong with that, in order of seriousness:

  1. EGRESS. A hook runs on the user's machine on every prompt. Whatever it contacts, it
     contacts from there, in their name, without their having asked for it. That is their
     call to make, not this package's.
  2. LATENCY. It is most of the remaining per-event cost.
  3. RELIABILITY. A hook that depends on a network is a hook that hangs on a bad network,
     and it hangs in front of the user's prompt.

So the hook entrypoints call `lock_down()` before anything heavy is imported. It does two
separate jobs, and both are needed:

  * the OFFLINE environment variables, which make the libraries not try. This is the real
    fix: a library told it is offline uses its cache and carries on.
  * a CONNECT GUARD, which makes an attempt fail loudly instead of hanging. This is the
    belt: a library that ignores the env vars, or a dependency added later that knows
    nothing about them, still cannot reach the network from a hook. The attempt is recorded
    so it shows up in telemetry rather than vanishing into an `except Exception: pass`.

`lock_down()` is idempotent and never raises.
"""

import os
import socket
import ssl

#: Set to "1" for every library known to consult them. Being explicit beats being clever:
#: a library that is told it is offline uses its cache, where one whose socket merely fails
#: may retry, back off, and hang -- which is the behaviour this is meant to prevent.
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "ANONYMIZED_TELEMETRY": "false",          # chromadb's posthog
    "CHROMA_TELEMETRY_IMPL": "none",
    "TOKENIZERS_PARALLELISM": "false",        # not egress, but it spawns threads in a hook
    "DO_NOT_TRACK": "1",
}

#: Loopback is not egress, and a hook that talks to a local daemon is a different question
#: from a hook that talks to the internet. Blocking loopback would break nothing today, but
#: it would also prove nothing, so the guard is about what leaves the machine.
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})

#: Every attempt that was refused, for telemetry. Kept in memory: a hook process is
#: short-lived and whoever calls `lock_down` reports these.
REFUSED: list[str] = []

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_LOAD_VERIFY = ssl.SSLContext.load_verify_locations
_LOCKED = False

#: How many times the trust store was skipped. Reported, not silent.
TRUST_STORE_SKIPS = [0]


class EgressRefused(OSError):
    """A hook tried to open a non-local connection."""


def _address_of(address) -> tuple[str, object]:
    """(host, port) from whatever a socket family hands us."""
    if isinstance(address, tuple) and address:
        return str(address[0]), (address[1] if len(address) > 1 else None)
    return str(address), None


def _guarded_connect(self, address):
    host, port = _address_of(address)
    if host in LOCAL_HOSTS:
        return _ORIGINAL_CONNECT(self, address)
    REFUSED.append(f"{host}:{port}")
    raise EgressRefused(
        f"attnroute: a hook may not open a network connection ({host}:{port}). "
        f"This is refused by design -- see attnroute/no_egress.py.")


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    """WARNING: PATCHING `connect` ALONE IS NOT ENOUGH, AND I NEARLY SHIPPED IT THAT WAY.

    `socket.socket.connect` is the sync path. An async client -- and `asyncio` WAS in
    `sys.modules` after the hook ran, alongside `httpcore` -- opens its connection through
    the event loop's own transport, which never touches `socket.socket.connect`. A guard on
    connect would have reported "no egress" while an async request went out underneath it.

    Name resolution is the one step almost every outbound connection takes first, sync or
    async, so this is where the guard actually bites. A literal IP address needs no lookup,
    which is why the connect guard stays as well: neither alone is sufficient.
    """
    name = str(host or "")
    if name in LOCAL_HOSTS or name.startswith("127.") or name.endswith(".localhost"):
        return _ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)
    REFUSED.append(f"{name}:{port} (resolve)")
    raise EgressRefused(
        f"attnroute: a hook may not resolve a remote host ({name}). "
        f"This is refused by design -- see attnroute/no_egress.py.")


def _skip_trust_store(self, *args, **kwargs):
    """WARNING: SIX SECONDS. Loading the Windows certificate store took 6.02 s of a single
    hook event, and it was not even for a request that happened -- `huggingface_hub` builds
    an `httpx.Client`, whose constructor calls `ssl.create_default_context`, whose
    constructor loads the system trust store. Offline or not, the client gets built.

    A trust store exists to verify a peer. This process is not allowed to reach a peer, so
    there is nothing to verify and nothing to load. Skipped rather than faked: if a caller
    then tries to connect, the guards above refuse it first.
    """
    TRUST_STORE_SKIPS[0] += 1


def lock_down() -> None:
    """Make this process offline. Idempotent, never raises.

    Called at the TOP of a hook entrypoint, before the imports that would otherwise reach
    out. Calling it after `huggingface_hub` has already been imported still helps, because
    it consults the variables per call, but earlier is better.
    """
    global _LOCKED
    for key, value in OFFLINE_ENV.items():
        os.environ.setdefault(key, value)
    if not _LOCKED:
        socket.socket.connect = _guarded_connect
        socket.getaddrinfo = _guarded_getaddrinfo
        ssl.SSLContext.load_verify_locations = _skip_trust_store
        _LOCKED = True


def release() -> None:
    """Undo the connect guard. For tests, and for any caller that is NOT a hook."""
    global _LOCKED
    socket.socket.connect = _ORIGINAL_CONNECT
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    ssl.SSLContext.load_verify_locations = _ORIGINAL_LOAD_VERIFY
    _LOCKED = False


def refused() -> list[str]:
    """Non-local addresses this process tried to reach. Empty is the expected answer."""
    return list(REFUSED)
