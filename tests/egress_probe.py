"""Runs ONE hook event with the network instrumented, and reports what it tried to reach.

Invoked as a subprocess by `tests/test_no_egress.py`:

    python tests/egress_probe.py <module> <output.json>     # hook payload on stdin

It is a separate file rather than a string inside the test because it has to be readable:
the self-check below is the difference between a test with teeth and a test that reports
"no attempts" forever because its instrumentation silently failed to install.
"""

import json
import runpy
import socket
import sys

from attnroute import no_egress

SELF_CHECK_HOST = "example.invalid"
SELF_CHECK_IP = "93.184.216.34"


def main() -> int:
    module, out_path = sys.argv[1], sys.argv[2]
    seen = []

    def record_connect(self, address):
        seen.append(["connect", str(address)])
        raise OSError("held by the egress probe")

    def record_resolve(host, port, *args, **kwargs):
        seen.append(["resolve", str(host)])
        raise OSError("held by the egress probe")

    # Stand in for the real primitives, so an attempt is RECORDED rather than merely
    # refused: a refusal can be caught and retried by the library that made it, and then
    # the attempt would never show up at all.
    no_egress._ORIGINAL_CONNECT = record_connect
    no_egress._ORIGINAL_GETADDRINFO = record_resolve
    no_egress.lock_down()

    # THE SELF-CHECK. Two attempts the probe makes itself, which it must catch. Without
    # this, a probe that failed to install would report a clean result for every hook
    # forever and the test would pass.
    for attempt in (lambda: socket.getaddrinfo(SELF_CHECK_HOST, 443),
                    lambda: socket.socket().connect((SELF_CHECK_IP, 443))):
        try:
            attempt()
        except BaseException:          # noqa: BLE001 - the refusal is the point
            pass
    self_check = len(no_egress.refused())
    no_egress.REFUSED.clear()
    seen.clear()

    try:
        runpy.run_module(module, run_name="__main__")
    except BaseException as exc:       # noqa: BLE001 - a hook crash is not this test's job
        print(f"HOOK RAISED: {exc!r}", file=sys.stderr)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"self_check": self_check,
                   "during_hook": seen + no_egress.refused()}, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
