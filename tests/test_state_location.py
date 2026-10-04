"""Runtime state must never appear in a working tree.

`get_state_file` did `PROJECT_STATE.parent.mkdir(...)` and returned the project path, so
"project-local preferred" meant "always project-local": attnroute created `.claude/` in
whatever repository the hook ran in and wrote attention scores into it every turn.

Nobody saw it because the write never succeeded -- `save_state` crashed on a `set` in the
state. The moment that was fixed, `.claude/attn_state.json` appeared in `git status` and was
committed by accident in the very PR that fixed it. Across eight team worktrees, a runtime
file in `git status` is a file somebody commits.
"""

import json
import os
from pathlib import Path

import pytest

from attnroute import context_router as cr


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A working tree, with HOME redirected so nothing touches the real one."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(cr, "STATE_DIR", home / ".claude" / "attn_state")
    monkeypatch.setattr(cr, "GLOBAL_STATE", home / ".claude" / "attn_state.json")
    monkeypatch.chdir(repo)
    return repo, home


class TestTheStateFileIsOutsideTheWorkingTree:

    def test_it_is_under_the_home_directory(self, project):
        repo, home = project
        path = cr.get_state_file()
        assert str(path).startswith(str(home)), path

    def test_the_working_tree_is_left_alone(self, project):
        """THE BLOCKER THIS FIXES. Not just "the file is elsewhere" -- the directory must
        not be created either, because an empty `.claude/` in a worktree invites one."""
        repo, _ = project
        cr.get_state_file()
        assert not (repo / ".claude").exists(), sorted(os.listdir(repo))

    def test_two_projects_do_not_share_a_file(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(cr, "STATE_DIR", home / ".claude" / "attn_state")
        paths = set()
        for name in ("alpha", "beta"):
            d = tmp_path / name
            d.mkdir()
            monkeypatch.chdir(d)
            paths.add(cr.get_state_file())
        assert len(paths) == 2, paths

    def test_the_same_project_always_gets_the_same_file(self, project):
        assert cr.get_state_file() == cr.get_state_file()

    def test_the_slug_is_filesystem_safe(self, tmp_path, monkeypatch):
        """Passed explicitly rather than chdir'd into, because Windows will not create a
        directory with a colon in its name -- and a path like `C:/x` is exactly the input
        the slug has to survive."""
        for awkward in ("C:/Users/jesse/a dir (with) odd:chars",
                        "/home/u/été/pro ject",
                        "D:/projects/meridian"):
            slug = cr.project_slug(awkward)
            assert all(c.isalnum() or c == "-" for c in slug), (awkward, slug)
            assert not slug.startswith("-") and not slug.endswith("-"), slug

    def test_the_slug_keeps_a_readable_part_and_a_hash(self, tmp_path, monkeypatch):
        d = tmp_path / "meridian"
        d.mkdir()
        monkeypatch.chdir(d)
        slug = cr.project_slug()
        assert slug == cr.project_slug(d), "an explicit cwd and the real one must agree"
        assert "meridian" in slug, slug
        assert len(slug.rsplit("-", 1)[1]) == 8, "the collision hash is what makes it safe"


class TestALegacyFileIsMigratedNotAdopted:
    """Adopting it would keep writing inside the repo forever. Reading it while writing
    elsewhere would be worse: the stale copy would win on every later run."""

    def test_the_contents_are_carried_over(self, project):
        repo, home = project
        legacy = repo / ".claude" / "attn_state.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"scores": {"a.py": 0.9}}), encoding="utf-8")

        path = cr.get_state_file()
        assert json.loads(path.read_text(encoding="utf-8"))["scores"] == {"a.py": 0.9}

    def test_the_legacy_file_is_removed(self, project):
        repo, _ = project
        legacy = repo / ".claude" / "attn_state.json"
        legacy.parent.mkdir()
        legacy.write_text("{}", encoding="utf-8")

        cr.get_state_file()
        assert not legacy.exists(), "left behind, so it would reappear in git status"

    def test_an_existing_destination_is_not_overwritten(self, project):
        """The destination is the live state; a stale file in the repo must not clobber it."""
        repo, home = project
        destination = cr.get_state_file()
        destination.write_text(json.dumps({"scores": {"new.py": 1.0}}), encoding="utf-8")

        legacy = repo / ".claude" / "attn_state.json"
        legacy.parent.mkdir(exist_ok=True)
        legacy.write_text(json.dumps({"scores": {"stale.py": 0.1}}), encoding="utf-8")

        again = cr.get_state_file()
        assert json.loads(again.read_text(encoding="utf-8"))["scores"] == {"new.py": 1.0}
        assert not legacy.exists()

    def test_a_failed_migration_does_not_raise(self, project, monkeypatch):
        """A state file is a cache. Failing to move it must not cost the user their turn."""
        repo, _ = project
        legacy = repo / ".claude" / "attn_state.json"
        legacy.parent.mkdir()
        legacy.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cr.Path, "unlink",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        assert cr.get_state_file()          # must not raise


def test_the_gitignore_covers_the_legacy_path():
    """Belt: an older install writing to the working tree must not show up in git status."""
    ignore = (Path(cr.__file__).resolve().parents[1] / ".gitignore").read_text(
        encoding="utf-8")
    assert ".claude/attn_state.json" in ignore
