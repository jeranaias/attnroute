"""One corpus, resolved once, and a document that is not there cannot be routed. (#10 item 3.)

Two independent resolutions used to disagree: `load_keyword_config` walked
[project, global] at import time while `resolve_docs_root` walked [env, project, global] inside
main(). Issue #10 reported a corpus whose config named 23 documents of which 0 existed under
the resolved root -- which reads as "the user deleted them" and is more likely the two chains
picking different roots.

And the one document that WAS injected -- 1,699 times, 99.6% of all volume -- was the PINNED
one, which gets an unconditional score floor. A pin on a phantom wins a slot on every prompt.
"""

import json

import pytest

from attnroute import context_router as cr


class TestAbsentDocumentsAreDropped:
    def _corpus(self, tmp_path, present=(), named=()):
        for rel in present:
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# doc\n", encoding="utf-8")
        return {rel: ["kw"] for rel in named}

    def test_present_kept_absent_dropped(self, tmp_path):
        kw = self._corpus(tmp_path, present=["a.md"], named=["a.md", "b.md"])
        kept, pinned, dropped = cr._drop_absent_docs(kw, [], tmp_path)
        assert list(kept) == ["a.md"]
        assert dropped == ["b.md"]

    def test_a_pin_on_an_absent_document_is_dropped(self, tmp_path):
        """⚠ THE #10 CASE. Pinning sets an unconditional score floor, so a pin on a phantom
        wins a slot on every prompt and injects nothing useful 1,699 times."""
        kw = self._corpus(tmp_path, present=[], named=["systems/network.md"])
        kept, pinned, dropped = cr._drop_absent_docs(kw, ["systems/network.md"], tmp_path)
        assert kept == {}
        assert pinned == [], "a pin survived on a document that does not exist"
        assert dropped == ["systems/network.md"]

    def test_a_pin_on_a_present_document_survives(self, tmp_path):
        """Negative control: the filter must not eat a legitimate pin."""
        kw = self._corpus(tmp_path, present=["keep.md"], named=["keep.md"])
        kept, pinned, dropped = cr._drop_absent_docs(kw, ["keep.md"], tmp_path)
        assert pinned == ["keep.md"]
        assert dropped == []

    def test_no_root_means_no_filtering(self, tmp_path):
        """With nothing anchored anywhere, the filter must not silently empty the corpus --
        that case is for main() to refuse, not for this function to mask."""
        kw = {"x.md": ["kw"]}
        kept, pinned, dropped = cr._drop_absent_docs(kw, ["x.md"], None)
        assert kept == kw and pinned == ["x.md"] and dropped == []

    def test_a_directory_is_not_a_document(self, tmp_path):
        (tmp_path / "notes.md").mkdir()
        kept, _, dropped = cr._drop_absent_docs({"notes.md": ["kw"]}, [], tmp_path)
        assert kept == {} and dropped == ["notes.md"]


class TestTheCorpusRootIsReturnedWithTheConfig:
    def _write(self, d, payload):
        d.mkdir(parents=True, exist_ok=True)
        (d / "keywords.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_the_root_is_the_directory_the_config_came_from(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        self._write(proj / ".claude", {"keywords": {"a.md": ["kw"]}})
        monkeypatch.chdir(proj)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

        _, _, _, root = cr.load_keyword_config()
        assert root == proj / ".claude", "the docs must be looked for where they were named"

    def test_a_global_config_declaring_other_projects_is_not_applied_here(
            self, tmp_path, monkeypatch):
        """⚠ The opt-in tightening: this is what stops one project's corpus reaching another."""
        home = tmp_path / "home"
        self._write(home / ".claude", {"keywords": {"lab.md": ["kw"]},
                                       "projects": ["/some/other/place"]})
        proj = tmp_path / "meridian"
        proj.mkdir()
        monkeypatch.chdir(proj)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: home))

        keywords, _, _, root = cr.load_keyword_config()
        assert "lab.md" not in keywords, "a corpus declared for another project was applied"

    def test_a_global_config_declaring_THIS_project_is_applied(self, tmp_path, monkeypatch):
        """Positive control: the declaration mechanism must be able to say yes."""
        home = tmp_path / "home"
        proj = tmp_path / "meridian"
        proj.mkdir()
        self._write(home / ".claude", {"keywords": {"lab.md": ["kw"]},
                                       "projects": [str(proj)]})
        monkeypatch.chdir(proj)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: home))

        keywords, _, _, _ = cr.load_keyword_config()
        assert "lab.md" in keywords, "the declaration cannot say yes, so the test above is vacuous"

    def test_a_config_with_no_declaration_still_works(self, tmp_path, monkeypatch):
        """Back-compatibility: an existing single-project setup must not break."""
        home = tmp_path / "home"
        self._write(home / ".claude", {"keywords": {"lab.md": ["kw"]}})
        proj = tmp_path / "anywhere"
        proj.mkdir()
        monkeypatch.chdir(proj)
        monkeypatch.setattr(cr.Path, "home", staticmethod(lambda: home))

        keywords, _, _, _ = cr.load_keyword_config()
        assert "lab.md" in keywords


def test_main_prefers_the_corpus_root_over_a_second_resolution():
    """Structural: main() must not resolve the docs root independently of the config.

    Asserted on the source because the alternative is running the whole hook, and the property
    is about which expression is written, not about a value at runtime.
    """
    src = (cr.__file__ and open(cr.__file__, encoding="utf-8").read())
    assert "docs_root = CORPUS_ROOT if CORPUS_ROOT is not None else resolve_docs_root()" in src


def test_the_drop_is_reported_not_silent():
    """A phantom corpus looks exactly like a quiet one, so the count must be printed."""
    src = open(cr.__file__, encoding="utf-8").read()
    assert "corpus documents do not exist under" in src
    assert "the corpus is EMPTY after dropping absent documents" in src
    # ...to stderr, since stdout is the injection channel
    i = src.index("corpus documents do not exist under")
    assert "file=sys.stderr" in src[i:i + 500]


@pytest.mark.parametrize("pinned", [[], ["absent.md"]])
def test_an_empty_corpus_never_yields_a_pin(tmp_path, pinned):
    kept, kept_pinned, _ = cr._drop_absent_docs({"absent.md": ["k"]}, pinned, tmp_path)
    assert kept == {} and kept_pinned == []
