"""Two bugs the profiler found, and they turned out to be one.

`_build_import_graph` returns `dict[str, set[str]]`. The reverse graph was converted to
lists before being stored; the forward graph was stored raw. So `json.dumps` in
`save_state` raised `TypeError: Object of type set is not JSON serializable` at the END of
every single hook event, after all the work -- and because the hook's output had already
been printed, nothing noticed.

The consequences were both invisible:

  * state was NEVER saved, so every attention score and every decay was discarded;
  * `turn_count` stayed 0, which is the condition guarding the import-graph build, so a
    full filesystem walk ran on EVERY turn.

The crash and the repeated expensive walk were the same bug.

Separately: `resolve_docs_root` answered "does this directory contain any .md files?" by
building the complete recursive list of them. Under `~/.claude` that walks the transcript
archive -- measured at 1,806 ms and 759 files, 10,736 `scandir` calls -- to populate a log
line that was then thrown away.
"""

import json
from pathlib import Path

import pytest

from attnroute import context_router as cr


class TestSaveStateSurvivesAValueItCannotSerialise:
    """It must not raise, it must say which key, and it must save the rest."""

    def test_a_set_in_the_state_does_not_raise(self, tmp_path, capsys):
        f = tmp_path / "state.json"
        cr.save_state(f, {"scores": {"a.py": 0.5}, "import_graph": {"a.py": {"b.py"}}})
        err = capsys.readouterr().err
        assert "state NOT saved" in err
        assert "import_graph" in err, "the offending key must be named"

    def test_the_serialisable_part_is_still_written(self, tmp_path):
        f = tmp_path / "state.json"
        cr.save_state(f, {"scores": {"a.py": 0.5}, "bad": {object()}})
        saved = json.loads(f.read_text(encoding="utf-8"))
        assert saved["scores"] == {"a.py": 0.5}
        assert "bad" not in saved

    def test_an_ordinary_state_is_written_whole(self, tmp_path):
        f = tmp_path / "state.json"
        cr.save_state(f, {"scores": {"a.py": 0.5}, "turn_count": 3})
        saved = json.loads(f.read_text(encoding="utf-8"))
        assert saved["turn_count"] == 3
        assert "last_update" in saved

    def test_json_safe_answers_the_question_it_is_named_for(self):
        assert cr._json_safe({"a": [1, 2]}) is True
        assert cr._json_safe({"a": {1, 2}}) is False


class TestTheImportGraphIsStoredAsLists:
    """The regression test for the crash itself, exercised through the real builder."""

    def test_the_builder_really_does_return_sets(self, tmp_path):
        """Positive control. If this ever returns lists, the test below stops meaning
        anything, and the comment in context_router would be describing a bug that no
        longer exists."""
        from attnroute.warmup import _build_import_graph

        (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
        graph = _build_import_graph(tmp_path)
        assert all(isinstance(v, (set, frozenset)) for v in graph.values()), graph

    def test_a_graph_converted_the_way_context_router_converts_it_is_serialisable(
            self, tmp_path):
        from attnroute.warmup import _build_import_graph

        (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
        graph = _build_import_graph(tmp_path)
        state = {"import_graph": {k: sorted(v) for k, v in graph.items()}}
        json.dumps(state)          # must not raise
        assert cr._json_safe(state)

    def test_the_source_no_longer_stores_the_raw_graph(self):
        """Blunt, and it is here because the subject is one assignment inside `main` that
        cannot be reached without running a whole hook event. It fails if anyone restores
        the line that crashed every turn."""
        src = Path(cr.__file__).read_text(encoding="utf-8")
        assert 'state["import_graph"] = import_graph' not in src
        assert 'state["import_graph"] = {k: sorted(v) for k, v in import_graph.items()}' in src


class TestCountingMarkdownIsBoundedAndPruned:

    @pytest.fixture
    def root(self, tmp_path):
        """A `.claude` root shaped like a real one: a few docs, and a transcript archive."""
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "one.md").write_text("# one", encoding="utf-8")
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "two.md").write_text("# two", encoding="utf-8")
        archive = tmp_path / "projects" / "C--Users-jesse"
        archive.mkdir(parents=True)
        for i in range(30):
            (archive / f"notes{i}.md").write_text("# noise", encoding="utf-8")
        return tmp_path

    def test_it_finds_the_docs(self, root):
        count, capped = cr.count_markdown(root)
        assert count == 2, "skills/one.md and memory/two.md"
        assert capped is False

    def test_it_does_not_walk_the_transcript_archive(self, root):
        """`projects/` held 322, 299 and 269 MB transcripts on the machine this was
        measured on. Pruning it is most of the saving."""
        assert "projects" in cr.CORPUS_PRUNE_DIRS
        assert cr.count_markdown(root)[0] == 2

    def test_directories_a_user_writes_docs_in_are_NOT_pruned(self):
        """A speed-up that quietly changes the answer is not a speed-up: these can hold
        markdown somebody wrote, so they stay in the walk."""
        for keep in ("skills", "memory", "memory-archive", "plugins", "backups"):
            assert keep not in cr.CORPUS_PRUNE_DIRS, keep

    def test_it_stops_at_the_cap_and_says_so(self, tmp_path):
        d = tmp_path / "docs"
        d.mkdir()
        for i in range(25):
            (d / f"{i}.md").write_text("#", encoding="utf-8")
        count, capped = cr.count_markdown(tmp_path, cap=10)
        assert (count, capped) == (10, True)

    def test_a_missing_directory_is_zero_rather_than_an_exception(self, tmp_path):
        assert cr.count_markdown(tmp_path / "nope") == (0, False)

    def test_an_empty_directory_is_zero(self, tmp_path):
        assert cr.count_markdown(tmp_path) == (0, False)

    def test_the_directory_limit_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cr, "CORPUS_WALK_DIR_LIMIT", 3)
        deep = tmp_path
        for i in range(10):
            deep = deep / f"d{i}"
            deep.mkdir()
        (deep / "late.md").write_text("#", encoding="utf-8")
        count, capped = cr.count_markdown(tmp_path)
        assert capped is True, "a bound that is never reported is not a bound"
        assert count == 0
