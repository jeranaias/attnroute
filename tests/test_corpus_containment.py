"""A corpus entry may not leave the corpus root, and the config is found from anywhere in the repo.

⚠⚠ CONTAINMENT IS A FILE-DISCLOSURE CHECK, NOT PATH HYGIENE. `keywords.json` is a plain JSON
file that gets committed to shared repositories, and a HOT document's content is read with
`read_text()` and written to stdout -- which IS the model's prompt. An entry of
`../../../.ssh/id_rsa` would inject that file.

`is_file()` alone does not stop it. Measured on this machine before the fix: with root
`~/.claude`, the path `../../../Windows/win.ini` returned `is_file() == True` and resolved to
`C:\\Windows\\win.ini`, so the absent-document filter KEPT it.
"""

import json

import pytest

from attnroute import context_router as cr


class TestContainment:
    def test_an_ordinary_relative_path_is_inside(self, tmp_path):
        assert cr._within_root(tmp_path, "notes/a.md") is True

    def test_a_dotdot_path_that_leaves_the_root_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")

        # The escape is REAL: the file exists and is_file() agrees.
        assert (root / "../secret.txt").is_file() is True
        assert cr._within_root(root, "../secret.txt") is False

    def test_a_deep_escape_is_refused(self, tmp_path):
        root = tmp_path / "a" / "b" / "c"
        root.mkdir(parents=True)
        (tmp_path / "top.txt").write_text("x", encoding="utf-8")
        assert cr._within_root(root, "../../../top.txt") is False

    def test_an_absolute_path_outside_the_root_is_refused(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        other = tmp_path / "other.md"
        other.write_text("x", encoding="utf-8")
        assert cr._within_root(root, str(other)) is False

    def test_the_root_itself_is_inside(self, tmp_path):
        assert cr._within_root(tmp_path, ".") is True

    def test_a_dotdot_that_comes_back_is_inside(self, tmp_path):
        """`a/../b.md` resolves inside, so it is not an escape -- the check is on the RESOLVED
        path, not on whether the string contains '..'. A string check would refuse this."""
        (tmp_path / "a").mkdir()
        assert cr._within_root(tmp_path, "a/../b.md") is True

    def test_a_symlink_out_of_the_root_is_refused(self, tmp_path):
        """resolve() collapses symlinks, so a link inside the corpus pointing outside is caught
        by the same comparison. Skipped where the OS will not let us make one -- NOT CHECKED,
        not silently passed."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("x", encoding="utf-8")
        link = root / "link.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a symlink here, so containment-vs-symlink is NOT "
                        f"CHECKED on this platform: {exc}")
        assert link.is_file() is True
        assert cr._within_root(root, "link.md") is False


class TestEscapedEntriesAreReportedApartFromAbsentOnes:
    def test_an_escaping_entry_is_dropped(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
        (root / "real.md").write_text("# r", encoding="utf-8")

        kept, pinned, dropped = cr._drop_absent_docs(
            {"../secret.txt": ["kw"], "real.md": ["kw"]}, [], root)

        assert list(kept) == ["real.md"]
        assert "../secret.txt" in dropped

    def test_the_two_kinds_get_different_messages(self, tmp_path, capsys):
        """⚠ Different remedies: an escaping entry is a config to FIX, an absent one is a
        document to WRITE -- and only one of the two is a disclosure risk."""
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
        cr._drop_absent_docs({"../secret.txt": ["kw"]}, [], root)
        err = capsys.readouterr().err
        assert "OUTSIDE" in err
        assert "READ INTO A PROMPT" in err, "the message must say why it matters"

    def test_an_escaping_pin_cannot_survive(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "secret.txt").write_text("s", encoding="utf-8")
        kept, pinned, _ = cr._drop_absent_docs(
            {"../secret.txt": ["kw"]}, ["../secret.txt"], root)
        assert pinned == []


class TestTheConfigIsFoundFromAnywhereInTheRepo:
    def _repo(self, tmp_path, payload):
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "keywords.json").write_text(json.dumps(payload), encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "a.md").write_text("# a", encoding="utf-8")
        deep = repo / "nav" / "guidance"
        deep.mkdir(parents=True)
        return repo, deep

    def test_found_when_the_cwd_is_a_subdirectory(self, tmp_path, monkeypatch):
        """⚠ THE ROW THAT FAILS ON THE OLD CODE. `Path(".claude/keywords.json")` is relative to
        the CWD, so a repo-local corpus was found only when the session started at the repo
        root. A hook's CWD can be any directory inside a worktree."""
        repo, deep = self._repo(tmp_path, {"keywords": {"docs/a.md": ["kw"]},
                                           "corpus_root": ".."})
        monkeypatch.chdir(deep)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

        keywords, _, _, root = cr.load_keyword_config()
        assert "docs/a.md" in keywords
        assert root == repo.resolve(), "corpus_root '..' must make the root the repository"

    def test_the_default_root_is_still_the_configs_own_directory(self, tmp_path, monkeypatch):
        """Back-compatibility: a config that declares no root behaves as before."""
        repo, deep = self._repo(tmp_path, {"keywords": {"docs/a.md": ["kw"]}})
        monkeypatch.chdir(deep)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

        _, _, _, root = cr.load_keyword_config()
        assert root == (repo / ".claude").resolve()

    def test_the_nearest_config_wins(self, tmp_path, monkeypatch):
        """A nested project must not inherit an outer one's corpus."""
        outer, _ = self._repo(tmp_path, {"keywords": {"docs/a.md": ["outer"]}})
        inner = outer / "sub" / "proj"
        (inner / ".claude").mkdir(parents=True)
        (inner / ".claude" / "keywords.json").write_text(
            json.dumps({"keywords": {"x.md": ["inner"]}}), encoding="utf-8")
        monkeypatch.chdir(inner)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

        keywords, _, _, root = cr.load_keyword_config()
        assert keywords.get("x.md") == ["inner"]
        assert root == (inner / ".claude").resolve()
