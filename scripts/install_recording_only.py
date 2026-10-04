#!/usr/bin/env python3
"""Install attnroute in RECORDING-ONLY mode, with a one-line rollback.

    python scripts/install_recording_only.py --install
    python scripts/install_recording_only.py --rollback      <- the one line

WHAT "RECORDING-ONLY" MEANS HERE. The router runs and injects as it normally would; what is
NOT active is anything that ACTS on the injected-vs-used ratio. Those are already gated off at
their source by `ATTNROUTE_TRUST_USED_SIGNAL` (default off, see attnroute/used_signal.py), so
recording-only is the package's default state and this script does not need to enforce it.
It is asserted below anyway, because a default that nothing checks is a default that drifts.

⚠ IT TAKES A BACKUP FIRST AND THE ROLLBACK RESTORES IT. `attnroute init` merges hooks rather
  than overwriting them -- verified behaviourally, not taken from the issue being closed:
  `merge_hooks` preserves a PreToolUse hook and a second UserPromptSubmit hook, and is
  idempotent. But a backup costs nothing and the rollback has to be ONE command.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
BACKUP_DIR = Path.home() / ".claude" / "attnroute-backups"


def _latest_backup():
    if not BACKUP_DIR.exists():
        return None
    backups = sorted(BACKUP_DIR.glob("settings.*.json"))
    return backups[-1] if backups else None


def install() -> int:
    if not SETTINGS.parent.exists():
        print(f"no {SETTINGS.parent} -- is Claude Code installed for this user?")
        return 2

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = BACKUP_DIR / f"settings.{stamp}.json"
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
        print(f"backed up  {SETTINGS}\n        -> {backup}")
    else:
        backup.write_text("{}\n", encoding="utf-8")
        print(f"no existing settings.json; recorded an empty backup at {backup}")

    before = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    before_events = sorted((before.get("hooks") or {}).keys())

    from attnroute.installer import main as installer_main

    rc = installer_main(["init"]) if callable(installer_main) else 1
    if rc not in (0, None):
        print(f"installer returned {rc}; settings left as-is, backup at {backup}")
        return 1

    after = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    after_events = sorted((after.get("hooks") or {}).keys())

    # ⚠ THE CHECK THAT MATTERS: no pre-existing hook EVENT may have disappeared. This is the
    #   failure issue #4 reported, and it is cheap to assert rather than trust.
    lost = [e for e in before_events if e not in after_events]
    if lost:
        shutil.copy2(backup, SETTINGS)
        print(f"REFUSED: hook events would have been lost: {lost}\n"
              f"settings.json restored from {backup}")
        return 1

    from attnroute.used_signal import trust_used_signal
    print(f"hook events before: {before_events}")
    print(f"hook events after : {after_events}")
    print(f"used-signal acted on: {trust_used_signal()}  (must be False for recording-only)")
    print(f"\nROLLBACK, one line:\n  python {Path(__file__).name} --rollback")
    return 0


def rollback() -> int:
    backup = _latest_backup()
    if backup is None:
        print(f"no backup found in {BACKUP_DIR}; nothing restored")
        return 2
    shutil.copy2(backup, SETTINGS)
    print(f"restored {SETTINGS} from {backup}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--rollback", action="store_true")
    args = ap.parse_args(argv)
    return install() if args.install else rollback()


if __name__ == "__main__":
    sys.exit(main())
