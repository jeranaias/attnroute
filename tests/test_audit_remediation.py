"""Regression tests for security, concurrency, and resilience fixes."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from attnroute import board, context_router, intent_map, levers, no_egress, predictor
from attnroute.compressor import CompressedObservation, ProgressiveRetriever
from attnroute.data.feature_extractor import FEATURE_DIM, extract_features
from attnroute.integrations import ContinuousClaudeAdapter, _merge_avg, _merge_max
from attnroute.learner import repo_namespace_id
from attnroute.repo_map import RepoMapper


class TestPredictorSafeJsonSerialization:
    def test_roundtrip_json_without_pickle(self, tmp_path: Path) -> None:
        model = predictor.PredictorModelV5()
        model.co_occurrence["a.py"] = Counter({"b.py": 3})
        model.project_files["proj"] = Counter({"a.py": 5})
        model.name_to_paths["a.py"] = {"src/a.py", "lib/a.py"}
        model.sequences[("a.py", "b.py")] = Counter({"c.py": 2})
        model.strong_keywords["auth"] = "src/auth.py"
        model.file_popularity = Counter({"src/a.py": 7})
        model.project_popularity["proj"] = ["src/a.py"]

        cache_path = tmp_path / "predictor_model.json"
        predictor.save_model(model, path=cache_path)
        # File on disk must be valid UTF-8 JSON, not a binary pickle stream.
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        assert raw["version"] == 5

        loaded = predictor.load_model(path=cache_path)
        assert loaded is not None
        assert loaded.co_occurrence["a.py"]["b.py"] == 3
        assert loaded.name_to_paths["a.py"] == {"src/a.py", "lib/a.py"}
        assert loaded.sequences[("a.py", "b.py")]["c.py"] == 2
        assert loaded.strong_keywords["auth"] == "src/auth.py"

    def test_rejects_corrupt_or_pickle_payload(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_bytes(b"\x80\x04\x95\x00\x00\x00\x00\x00\x00\x00\x00.")
        assert predictor.load_model(path=bad) is None


class TestProjectScopeBoundaryMatching:
    def test_sibling_directory_prefix_does_not_match(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        allowed = tmp_path / "app"
        sibling = tmp_path / "app-staging"
        allowed.mkdir()
        sibling.mkdir()
        (sibling / ".git").mkdir()

        (home / ".claude" / "keywords.json").write_text(
            json.dumps({"keywords": {"secret.md": ["deploy"]}, "projects": [str(allowed)]}),
            encoding="utf-8",
        )
        monkeypatch.chdir(sibling)
        monkeypatch.setattr(context_router.Path, "home", staticmethod(lambda: home))

        keywords, _, _, _ = context_router.load_keyword_config()
        assert "secret.md" not in keywords


class TestRepoMapperBrokenNetworkxFallback:
    def test_init_handles_nx_returning_none(self, tmp_path: Path) -> None:
        with (
            patch("attnroute.repo_map.NETWORKX_AVAILABLE", True),
            patch("attnroute.repo_map._nx", return_value=None),
        ):
            mapper = RepoMapper(str(tmp_path))
            assert mapper.dependency_graph is None


class TestBoardRemoteValidation:
    @pytest.mark.parametrize("bad_remote", ["--upload-pack=id", "../evil", "a/b", "bad remote", ""])
    def test_write_rejects_unsafe_remote_names(self, tmp_path: Path, bad_remote: str) -> None:
        with pytest.raises(board.BoardError, match="invalid git remote"):
            board.write("T1", "hello\n", repo=tmp_path, remote=bad_remote)


class TestNoEgressLoopbackRange:
    def test_allows_all_rfc1122_loopback_addresses(self) -> None:
        assert no_egress._is_local_host("127.0.0.1") is True
        assert no_egress._is_local_host("127.0.0.2") is True
        assert no_egress._is_local_host("127.255.255.254") is True
        assert no_egress._is_local_host("::1") is True
        assert no_egress._is_local_host("1.1.1.1") is False
        assert no_egress._is_local_host("example.com") is False


class TestHookHotPathResilience:
    def test_empty_git_worktree_file_does_not_raise_index_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "worktree"
        repo.mkdir()
        (repo / ".git").write_text("", encoding="utf-8")
        ns = repo_namespace_id(str(repo))
        assert isinstance(ns, str) and len(ns) > 0

    def test_extract_features_handles_none_and_empty(self) -> None:
        assert extract_features(None) == [0.0] * FEATURE_DIM  # type: ignore[arg-type]
        assert extract_features("") == [0.0] * FEATURE_DIM

    def test_boost_by_intent_handles_none_state(self) -> None:
        scores = {"src/app.py": 0.5}
        out = intent_map.boost_by_intent(["recent_edits"], scores, None)  # type: ignore[arg-type]
        assert out["src/app.py"] == 0.5

    def test_build_context_output_handles_missing_scores_and_turn_count(self) -> None:
        text, stats = context_router.build_context_output({}, {})
        assert isinstance(text, str)
        assert stats["hot"] == 0


class TestIntegrationsAndCompressorHardening:
    def test_merge_max_and_avg_tolerate_scalar_vs_dict_mismatch(self) -> None:
        s1 = {"prompt_file_affinity": {"k": 0.5, "m": {"x": 1.0}}}
        s2 = {"prompt_file_affinity": {"k": {"x": 2.0}, "m": 3.0}}
        m_max = _merge_max(s1, s2)
        m_avg = _merge_avg(s1, s2)
        assert m_max["prompt_file_affinity"]["k"]["x"] == 2.0
        assert m_avg["prompt_file_affinity"]["k"]["x"] == 1.0

    def test_load_ledger_returns_empty_on_corrupt_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "ledger.json"
        bad.write_text("{corrupt", encoding="utf-8")
        adapter = ContinuousClaudeAdapter()
        assert adapter.load_ledger(bad) == []

    def test_layer3_full_queries_db_once_per_id(self) -> None:
        calls: list[str] = []

        class _FakeDB:
            def get(self, oid: str):
                calls.append(oid)
                return CompressedObservation(
                    id=oid,
                    timestamp=__import__("datetime").datetime.now(),
                    session_id="s1",
                    tool_name="Read",
                    observation_type="read",
                    semantic_summary="ok",
                    concepts=[],
                    raw_tokens=10,
                    compressed_tokens=5,
                    key_facts=[],
                    related_files=[],
                    raw_content_hash="abc",
                )

        retriever = ProgressiveRetriever(db=_FakeDB())  # type: ignore[arg-type]
        out = retriever.layer3_full(["o1", "o2"])
        assert len(out) == 2
        assert calls == ["o1", "o2"]


class TestHistoryAndLeversHardening:
    def test_history_rejects_non_positive_last(self, monkeypatch) -> None:
        from attnroute import history
        monkeypatch.setattr(sys, "argv", ["attnroute-history", "--last", "0"])
        with pytest.raises(SystemExit) as exc:
            history.main()
        assert exc.value.code == 2

    def test_set_lever_leaves_no_tmp_files(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(levers, "levers_path", lambda: tmp_path / "levers.json")
        levers.set_lever("cap_act", True)
        assert (tmp_path / "levers.json").exists()
        assert list(tmp_path.glob("*.tmp")) == []
