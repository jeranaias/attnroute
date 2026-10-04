"""The repo index must walk the repository once, and must not enter what it skips.

`RepoMapper.index` ran one `rglob(f"*{ext}")` per entry in `LANGUAGE_MAP` -- fourteen full
recursive walks -- and applied its skip list to the RESULT, so `node_modules`, `.git` and
`target` were descended into fourteen times and then discarded fourteen times. Measured
from the `UserPromptSubmit` hook: 10,908 `os.scandir` calls from this method alone, 3.1 s.

Two tests here are about cost and one is about meaning: `max_files` used to cap each
extension separately, because the `break` left the outer loop running.
"""

import os

import pytest

from attnroute.repo_map import LANGUAGE_MAP, RepoMapper


@pytest.fixture
def repo(tmp_path):
    """A small repo with source in it, and a big dependency directory that must be skipped."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.js").write_text("function two() { return 2; }\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# not source\n", encoding="utf-8")

    deps = tmp_path / "node_modules" / "left-pad" / "lib"
    deps.mkdir(parents=True)
    for i in range(20):
        (deps / f"dep{i}.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "c.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def scanned_paths(monkeypatch):
    """Record every directory `os.scandir` is called on."""
    seen = []
    real = os.scandir

    def traced(path=".", *args, **kwargs):
        seen.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", traced)
    return seen


class TestItDoesNotEnterWhatItSkips:

    def test_a_skipped_directory_is_never_read(self, repo, monkeypatch):
        """THE POINT OF THE CHANGE. Excluding a path from the RESULT still pays for walking
        it; pruning `dirnames` means the directory is never opened at all."""
        seen = scanned_paths(monkeypatch)
        RepoMapper(repo).index()
        entered = [p for p in seen if "node_modules" in p or ".git" in p]
        assert entered == [], f"walked into directories it then discarded: {entered[:3]}"

    def test_the_source_files_are_still_found(self, repo):
        """Positive control: a walk that enters nothing at all would pass the test above."""
        mapper = RepoMapper(repo)
        mapper.index()
        found = set(mapper.file_symbols)
        assert any(p.endswith("a.py") for p in found), found

    def test_nothing_from_a_skipped_directory_is_indexed(self, repo):
        mapper = RepoMapper(repo)
        mapper.index()
        assert not [p for p in mapper.file_symbols if "node_modules" in p or ".git" in p]

    def test_a_non_source_file_is_ignored(self, repo):
        mapper = RepoMapper(repo)
        mapper.index()
        assert not [p for p in mapper.file_symbols if p.endswith(".md")]


class TestItWalksOnce:

    def test_a_directory_is_scanned_once_not_once_per_language(self, repo, monkeypatch):
        seen = scanned_paths(monkeypatch)
        RepoMapper(repo).index()
        pkg = [p for p in seen if p.rstrip("\\/").endswith("pkg")]
        assert len(pkg) <= 1, (
            f"pkg/ was scanned {len(pkg)} times; LANGUAGE_MAP has {len(LANGUAGE_MAP)} "
            f"extensions, so a per-extension walk shows up here")


class TestMaxFilesMeansWhatItSays:

    def test_the_cap_is_total_and_not_per_extension(self, tmp_path):
        """⚠ The old `break` exited the inner loop only, so the real ceiling was up to
        `max_files` times the number of extensions."""
        src = tmp_path / "src"
        src.mkdir()
        for i in range(6):
            (src / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
            (src / f"m{i}.js").write_text(f"function g{i}() {{ return {i}; }}\n",
                                          encoding="utf-8")

        mapper = RepoMapper(tmp_path, max_files=4)
        mapper.index()
        assert len(mapper.file_symbols) <= 4, len(mapper.file_symbols)

    def test_it_indexes_everything_when_the_cap_is_generous(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(3):
            (src / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
        mapper = RepoMapper(tmp_path, max_files=500)
        mapper.index()
        assert len(mapper.file_symbols) == 3
