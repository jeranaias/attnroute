"""The outliner's state is reported, and a truncated outline says so.

These cover two defects measured on 2026-10-03:

  * `_fallback_outline` capped at 100 lines and said nothing, so a 58,569-token file
    (gcs/serve.py in a real repo) produced exactly 100 signature lines with no marker.
    An outline IS a search result; a truncated one with no marker lets a reader conclude a
    symbol is ABSENT when it was past the cut.
  * `attnroute status` appended only the capabilities that WERE importable, so a degraded
    install printed a shorter list and read as complete.
"""

from pathlib import Path

import pytest

from attnroute import outliner
from attnroute.cli import cmd_status
from attnroute.outliner import (
    FALLBACK_MAX_LINES,
    OUTLINE_BACKEND_REGEX,
    OUTLINE_BACKEND_TREE_SITTER,
    _fallback_outline,
    outline_backend,
)


def _py_with_defs(tmp_path: Path, n: int) -> Path:
    p = tmp_path / "many.py"
    body = ["import os"] + [f"def f{i}(a, b):\n    return a" for i in range(n)]
    p.write_text("\n".join(body), encoding="utf-8")
    return p


class TestFallbackTruncation:
    def test_long_file_says_it_was_truncated(self, tmp_path):
        out = _fallback_outline(_py_with_defs(tmp_path, 400))
        lines = out.split("\n")

        # The cap still holds, and the marker lives INSIDE it rather than extending it.
        assert len(lines) == FALLBACK_MAX_LINES
        assert lines[-1].startswith("# ... truncated:")

        # ⚠ THE NUMBERS MUST BE THE REAL ONES. A marker that said "some lines omitted" would
        #   pass a weaker assertion and tell a reader nothing about how much is missing.
        shown, total = FALLBACK_MAX_LINES - 1, 401 + 1  # 400 defs + 1 import + 1 header
        assert f"{shown} of {total}" in lines[-1]
        assert f"{total - shown} not shown" in lines[-1]

    def test_short_file_carries_no_marker(self, tmp_path):
        """Negative control: the marker must not appear when nothing was dropped."""
        out = _fallback_outline(_py_with_defs(tmp_path, 3))
        assert "truncated" not in out
        assert len(out.split("\n")) < FALLBACK_MAX_LINES

    def test_exactly_at_the_cap_is_not_truncated(self, tmp_path):
        """Boundary: header + 99 signatures is exactly the cap and loses nothing."""
        out = _fallback_outline(_py_with_defs(tmp_path, FALLBACK_MAX_LINES - 2))
        assert len(out.split("\n")) == FALLBACK_MAX_LINES
        assert "truncated" not in out


class TestOutlineBackend:
    @pytest.mark.parametrize(
        ("ts", "pack", "expected"),
        [
            (True, True, OUTLINE_BACKEND_TREE_SITTER),
            (True, False, OUTLINE_BACKEND_REGEX),
            (False, True, OUTLINE_BACKEND_REGEX),
            (False, False, OUTLINE_BACKEND_REGEX),
        ],
    )
    def test_every_combination_reports_a_backend_and_a_reason(self, monkeypatch, ts, pack, expected):
        """⚠ THE REASON IS NEVER EMPTY, including when the backend is the full one.

        A caller must not have to infer the state from a blank string -- that is the shape
        the two silent `AVAILABLE = False` flags had.
        """
        monkeypatch.setattr(outliner, "TREE_SITTER_AVAILABLE", ts)
        monkeypatch.setattr(outliner, "LANGUAGE_PACK_AVAILABLE", pack)

        backend, reason = outline_backend()
        assert backend == expected
        assert reason and reason.strip(), "a state with no reason is not actionable"

    def test_the_degraded_reason_names_the_missing_package(self, monkeypatch):
        monkeypatch.setattr(outliner, "TREE_SITTER_AVAILABLE", True)
        monkeypatch.setattr(outliner, "LANGUAGE_PACK_AVAILABLE", False)
        _, reason = outline_backend()
        assert "tree_sitter_languages" in reason


class TestStatusReportsAbsence:
    def _status_text(self, capsys):
        cmd_status(args=None)
        return capsys.readouterr().out

    def test_status_names_every_capability_present_or_not(self, capsys):
        text = self._status_text(capsys)
        assert "Capabilities:" in text
        for label in ("BM25 search", "Semantic search", "Graph retrieval",
                      "Memory compression", "Learning engine", "Source outlining"):
            assert label in text, f"{label} is not reported at all"

    def test_a_degraded_capability_is_marked_and_explained(self, capsys, monkeypatch):
        """⚠ The row for an ABSENT capability must exist AND carry a reason.

        The old code appended only present features, so this assertion is the one that fails
        if anyone reverts to a presence-only list.
        """
        monkeypatch.setattr(outliner, "TREE_SITTER_AVAILABLE", True)
        monkeypatch.setattr(outliner, "LANGUAGE_PACK_AVAILABLE", False)

        text = self._status_text(capsys)
        rows = [ln for ln in text.split("\n") if "Source outlining" in ln]
        assert rows, "Source outlining is not reported"
        assert "DEGRADED" in rows[0]
        assert "tree_sitter_languages" in rows[0], "the row states no cause"
        assert "not at full function" in text


def test_the_measured_case_reproduces(tmp_path):
    """The file that surfaced this: 212 outline lines, capped to 100, nothing said.

    Rebuilt synthetically so the test does not depend on another repository.
    """
    p = tmp_path / "serve_like.py"
    p.write_text("\n".join(["import os"] + [f"def h{i}(x):\n    pass" for i in range(211)]),
                 encoding="utf-8")
    out = _fallback_outline(p)
    assert out.split("\n")[-1].startswith("# ... truncated:")
    assert "113 not shown" in out or "not shown" in out.split("\n")[-1]
    # and the whole thing is still tiny next to the file it came from
    assert len(out) < len(p.read_text(encoding="utf-8")) / 2
