"""The shared team board: one file per writer, on an orphan branch, never checked out.

WHY IT IS SHAPED LIKE THIS, since every part of it is a reaction to a way the obvious design
fails:

  * A COMMITTED FILE ON main would be edited by eight sessions at once. One writer per file
    is what removes merge conflicts -- not locking, not retrying, not merge drivers. `T4.md`
    has exactly one author, so there is nothing to resolve.
  * A FILE UNDER ~/.claude IS NOT SHARED. The teams sit on two machines. Anything that has
    to be read by another machine has to travel through the remote.
  * AN ORPHAN BRANCH keeps it out of `main`'s history and out of every working tree. A
    runtime file that appears in `git status` is a file somebody commits -- which already
    happened once in this project, to `.claude/attn_state.json`.
  * WRITES USE PLUMBING, so no checkout, no stash, no touching the user's index or working
    tree. `hash-object`, a temporary index, `write-tree`, `commit-tree`, then a push of the
    commit straight to the branch.
  * WRITES ARE AN EXPLICIT AGENT ACTION; READS ARE A HOOK. The hook therefore must not use
    the network: it reads the LOCAL `origin/board` ref, which rides on the fetches agents
    already do. Freshness is reported from each file's own commit time rather than assumed,
    so a stale row says it is stale instead of looking current.

Nothing here writes to the working tree, and nothing on the read path opens a socket.
"""

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

#: The orphan branch. Never main, never checked out.
BRANCH = "board"

#: The remote-tracking ref the read path uses. Local: reading it needs no network.
READ_REF = f"origin/{BRANCH}"

#: One file per writer. A team writes only its own; the lead writes only `_lead.md`.
LEAD_FILE = "_lead.md"

#: Rows older than this are reported stale. Not hidden -- a row that is quietly dropped is
#: indistinguishable from a team that never wrote one.
STALE_AFTER_HOURS = 24.0

#: How many times a push retries after a non-fast-forward. Each retry rebuilds the commit on
#: the new remote tip, carrying only this writer's file.
PUSH_RETRIES = 3

GIT_TIMEOUT = 60


class BoardError(RuntimeError):
    """A board operation could not be completed. Carries what git said."""


def team_file(team: str) -> str:
    """`teams/T4.md` for `T4`. The name is validated, because it becomes a git path."""
    name = str(team or "").strip()
    if name == LEAD_FILE or name == "_lead":
        return LEAD_FILE
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise BoardError(f"not a usable writer name: {team!r}")
    return f"teams/{name}.md"


def _git(args, repo: Path, stdin: str | None = None, env_extra: dict | None = None,
         check: bool = True) -> str:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    # A hook must never be stopped by a credential prompt, and a write must fail rather
    # than hang waiting for one.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(["git", *args], cwd=str(repo), input=stdin, text=True,
                          capture_output=True, env=env, timeout=GIT_TIMEOUT)
    if check and proc.returncode != 0:
        raise BoardError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


# ---- reading: hook-safe, local refs only ---------------------------------------------
def read(team: str, repo: Path | str = ".", ref: str = READ_REF) -> dict:
    """One writer's row. -> {"text", "written_at", "age_hours", "stale", "source", "why"}

    NEVER FETCHES. This is called from the SessionStart hook, and a hook that touches the
    network is a hook that hangs in front of the user's prompt -- so it reads whatever the
    last fetch left in `origin/board`. The answer therefore carries its own age, and
    `stale` is reported rather than the row being silently dropped.
    """
    path = team_file(team)
    out = {"text": "", "written_at": None, "age_hours": None, "stale": False,
           "source": f"{ref}:{path}", "why": ""}
    repo = Path(repo)
    try:
        out["text"] = _git(["show", f"{ref}:{path}"], repo)
    except BoardError as exc:
        out["why"] = str(exc)
        # Distinguish the two reasons there is no row, because they call for different
        # actions: nobody has written one, versus this clone has never seen the branch.
        try:
            _git(["rev-parse", "--verify", ref], repo)
            out["why"] = f"no row for {team} on {ref}"
        except BoardError:
            out["why"] = (f"{ref} is not present in this clone; run "
                          f"`git fetch origin {BRANCH}:refs/remotes/{ref}` once")
        return out

    # WARNING: ASK GIT FOR EPOCH SECONDS, NOT AN ISO STRING.
    #   This was `--format=%cI` parsed with `datetime.fromisoformat`, and it failed on
    #   ubuntu/Python 3.10 ONLY -- 3.11, 3.12, 3.13 and 3.14 all passed, as did macOS 3.10.
    #   Before 3.11, `fromisoformat` accepts only the exact subset that `isoformat()`
    #   emits, so the answer depended on the Python version AND on what that runner's git
    #   chose to print. `%ct` is an integer count of seconds: nothing to parse, no
    #   timezone subset, same answer on every version and platform.
    stamp = _git(["log", "-1", "--format=%ct", ref, "--", path], repo).strip()
    if not stamp:
        out["why"] = out["why"] or f"no commit found for {path} on {ref}"
        return out
    try:
        written = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        # Reported with the value, because a row whose age is silently unknown looks
        # exactly like a row that is fresh.
        out["why"] = f"unreadable commit time {stamp!r}: {exc!r}"
        return out
    out["written_at"] = written.isoformat()
    age = (datetime.now(timezone.utc) - written).total_seconds() / 3600.0
    out["age_hours"] = round(age, 2)
    out["stale"] = age > STALE_AFTER_HOURS
    if out["stale"]:
        out["why"] = f"written {age:.0f}h ago, older than {STALE_AFTER_HOURS:.0f}h"
    return out


def writers(repo: Path | str = ".", ref: str = READ_REF) -> list[str]:
    """Every writer with a row, lead last. Local refs only."""
    try:
        listing = _git(["ls-tree", "-r", "--name-only", ref], Path(repo))
    except BoardError:
        return []
    names = []
    for line in listing.splitlines():
        line = line.strip()
        if line == LEAD_FILE:
            names.append(LEAD_FILE)
        elif line.startswith("teams/") and line.endswith(".md"):
            names.append(line[len("teams/"):-len(".md")])
    return sorted(n for n in names if n != LEAD_FILE) + (
        [LEAD_FILE] if LEAD_FILE in names else [])


# ---- writing: explicit agent action, plumbing only -----------------------------------
def _signing_args(repo: Path) -> list[str]:
    """`-S` when the repository asks for signed commits, nothing otherwise.

    A board write must not bypass a signing policy, and must not invent one either.
    """
    out = _git(["config", "--get", "commit.gpgsign"], repo, check=False).strip().lower()
    return ["-S"] if out == "true" else []


def _build_commit(repo: Path, path: str, text: str, parent: str | None, message: str) -> str:
    """A commit containing `parent`'s tree with `path` replaced. -> commit sha

    Uses a TEMPORARY INDEX, so the user's own index and working tree are untouched. This is
    the whole reason for plumbing: a board write must be invisible to whatever the person
    or another agent is doing in that repository at the time.
    """
    blob = _git(["hash-object", "-w", "--stdin"], repo, stdin=text).strip()
    with tempfile.TemporaryDirectory() as tmp:
        index = str(Path(tmp) / "index")
        env = {"GIT_INDEX_FILE": index}
        if parent:
            _git(["read-tree", parent], repo, env_extra=env)
        _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], repo,
             env_extra=env)
        tree = _git(["write-tree"], repo, env_extra=env).strip()
    args = ["commit-tree", tree, *_signing_args(repo)]
    if parent:
        args += ["-p", parent]
    return _git([*args, "-m", message], repo).strip()


def write(team: str, text: str, repo: Path | str = ".", remote: str = "origin",
          message: str | None = None) -> dict:
    """Publish one writer's row. -> {"commit", "path", "attempts", "created_branch"}

    EXPLICIT, NEVER A HOOK. This pushes, which is the one network operation in this module,
    and it is why writes are an agent action: a hook may not reach the network.

    On a non-fast-forward it does NOT merge. It rebuilds its single file on the new remote
    tip and pushes again, because one writer per file means the only possible conflict is
    with somebody else's file -- which carrying only ours cannot lose.
    """
    repo = Path(repo)
    path = team_file(team)
    if not text.endswith("\n"):
        text += "\n"
    note = message or f"board: {team}"
    last = ""
    for attempt in range(1, PUSH_RETRIES + 1):
        # Re-read the tip on every attempt: that is what makes the retry a rebase of this
        # one file rather than a clobber of whatever arrived in between.
        parent = _git(["rev-parse", "--verify", f"refs/remotes/{remote}/{BRANCH}"], repo,
                      check=False).strip() or None
        commit = _build_commit(repo, path, text, parent, note)
        pushed = subprocess.run(
            ["git", "push", remote, f"{commit}:refs/heads/{BRANCH}"],
            cwd=str(repo), text=True, capture_output=True, timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if pushed.returncode == 0:
            # Keep the local remote-tracking ref in step, so the read path sees this write
            # without waiting for a fetch.
            _git(["update-ref", f"refs/remotes/{remote}/{BRANCH}", commit], repo,
                 check=False)
            return {"commit": commit, "path": path, "attempts": attempt,
                    "created_branch": parent is None}
        last = (pushed.stderr or pushed.stdout).strip()
        if not any(sign in last for sign in
                   ("non-fast-forward", "fetch first", "rejected")):
            raise BoardError(f"push failed: {last[:400]}")
        # Somebody else wrote. Get their tip and rebuild on it.
        _git(["fetch", remote, f"{BRANCH}:refs/remotes/{remote}/{BRANCH}"], repo,
             check=False)
    raise BoardError(f"push kept being rejected after {PUSH_RETRIES} attempts: {last[:400]}")
