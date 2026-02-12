"""Core functionality tests for attnroute."""


import pytest


class TestTokenEstimation:
    """Test token estimation functions."""

    def test_estimate_tokens_empty(self):
        """Empty string returns 0 tokens."""
        from attnroute.telemetry_lib import estimate_tokens
        assert estimate_tokens("") == 0

    def test_estimate_tokens_simple(self):
        """Simple text returns reasonable estimate."""
        from attnroute.telemetry_lib import estimate_tokens
        result = estimate_tokens("Hello, world!")
        assert result > 0
        assert result < 10  # Should be ~3-4 tokens

    def test_estimate_tokens_code(self):
        """Code gets higher token density estimate."""
        from attnroute.telemetry_lib import estimate_tokens
        code = "def foo(x, y): return x + y"
        prose = "The quick brown fox jumps over"
        # Code should have more tokens per character
        code_ratio = len(code) / max(1, estimate_tokens(code))
        prose_ratio = len(prose) / max(1, estimate_tokens(prose))
        # Code typically has ~2.5 chars/token, prose ~4 chars/token
        assert code_ratio < prose_ratio

    def test_tiktoken_available_flag(self):
        """TIKTOKEN_AVAILABLE flag exists."""
        from attnroute.telemetry_lib import TIKTOKEN_AVAILABLE
        assert isinstance(TIKTOKEN_AVAILABLE, bool)


class TestRepoMapper:
    """Test repository mapping functionality."""

    def test_repo_mapper_import(self):
        """RepoMapper can be imported."""
        from attnroute.repo_map import RepoMapper
        assert RepoMapper is not None

    def test_repo_mapper_empty_dir(self, tmp_path):
        """RepoMapper handles empty directory."""
        from attnroute.repo_map import RepoMapper
        mapper = RepoMapper(str(tmp_path))
        mapper.index()
        result = mapper.get_map()
        assert "No source files found" in result or "Repository Map" in result

    def test_repo_mapper_python_file(self, tmp_path):
        """RepoMapper indexes Python files."""
        from attnroute.repo_map import RepoMapper

        # Create a simple Python file
        py_file = tmp_path / "test_module.py"
        py_file.write_text("def hello():\n    pass\n\nclass Foo:\n    pass\n")

        mapper = RepoMapper(str(tmp_path))
        mapper.index()
        result = mapper.get_map()

        assert "test_module.py" in result or "hello" in result or "Foo" in result


class TestCLI:
    """Test CLI functionality."""

    def test_cli_import(self):
        """CLI module can be imported."""
        from attnroute import cli
        assert hasattr(cli, 'main')

    def test_cli_commands_exist(self):
        """All expected commands are defined."""
        from attnroute import cli
        expected = ['cmd_init', 'cmd_status', 'cmd_report', 'cmd_benchmark',
                    'cmd_compress', 'cmd_graph', 'cmd_history', 'cmd_version',
                    'cmd_diagnostic']
        for cmd in expected:
            assert hasattr(cli, cmd), f"Missing command: {cmd}"


class TestRepoMapperBehavior:
    """Test RepoMapper produces correct output for known inputs."""

    def test_finds_functions(self, tmp_path):
        """RepoMapper output includes function names from indexed files."""
        from attnroute.repo_map import RepoMapper

        py_file = tmp_path / "calculator.py"
        py_file.write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def subtract(a, b):\n    return a - b\n\n"
            "class MathUtils:\n    def multiply(self, x, y):\n        return x * y\n"
        )

        mapper = RepoMapper(str(tmp_path))
        mapper.index()
        result = mapper.get_map()

        assert "calculator.py" in result
        symbols_found = sum(1 for name in ["add", "subtract", "MathUtils", "multiply"]
                            if name in result)
        assert symbols_found >= 1, f"Expected symbols in map, got: {result[:200]}"

    def test_respects_token_budget(self, tmp_path):
        """RepoMapper output respects token budget constraints."""
        from attnroute.repo_map import RepoMapper

        for i in range(10):
            f = tmp_path / f"module_{i}.py"
            f.write_text(f"def func_{i}():\n    pass\n\nclass Class_{i}:\n    pass\n")

        mapper = RepoMapper(str(tmp_path))
        mapper.index()

        small_result = mapper.get_map(token_budget=100)
        large_result = mapper.get_map(token_budget=5000)

        assert len(small_result) <= len(large_result)

    def test_excludes_pycache(self, tmp_path):
        """RepoMapper should skip __pycache__ directories."""
        from attnroute.repo_map import RepoMapper

        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-312.pyc").write_bytes(b"")
        (tmp_path / "real_module.py").write_text("def real(): pass\n")

        mapper = RepoMapper(str(tmp_path))
        mapper.index()
        result = mapper.get_map()

        assert "real_module" in result
        assert "__pycache__" not in result

    def test_handles_empty_files(self, tmp_path):
        """RepoMapper handles empty source files gracefully."""
        from attnroute.repo_map import RepoMapper

        (tmp_path / "empty.py").write_text("")
        (tmp_path / "nonempty.py").write_text("x = 1\n")

        mapper = RepoMapper(str(tmp_path))
        mapper.index()
        result = mapper.get_map()
        assert isinstance(result, str)

    def test_output_is_always_string(self, tmp_path):
        """get_map always returns a string, even with no files."""
        from attnroute.repo_map import RepoMapper

        mapper = RepoMapper(str(tmp_path))
        mapper.index()
        assert isinstance(mapper.get_map(), str)


class TestLearner:
    """Test learning functionality."""

    def test_learner_import(self):
        """Learner can be imported."""
        from attnroute.learner import Learner
        assert Learner is not None

    def test_learner_init(self):
        """Learner initializes without error."""
        from attnroute.learner import Learner
        learner = Learner()
        assert learner is not None

    def test_learner_has_state(self):
        """Learner initializes with a state dict."""
        from attnroute.learner import Learner
        learner = Learner()
        assert hasattr(learner, "state")
        assert isinstance(learner.state, dict)


class TestPredictor:
    """Test file prediction functionality."""

    def test_predictor_import(self):
        """FilePredictor can be imported."""
        from attnroute.predictor import FilePredictor
        assert FilePredictor is not None

    def test_predictor_empty_history(self):
        """Predictor handles empty history gracefully."""
        from attnroute.predictor import PredictorModelV5, predict_files_v5
        model = PredictorModelV5()
        result = predict_files_v5("test prompt", model, [])
        assert isinstance(result, list)

    def test_predictor_returns_tuples(self):
        """Predictor returns list of (path, score) tuples."""
        from attnroute.predictor import PredictorModelV5, predict_files_v5

        model = PredictorModelV5()
        model.name_to_paths["main.py"] = {"src/main.py"}
        model.file_popularity["src/main.py"] = 10

        result = predict_files_v5("update main.py", model, ["src/main.py"])
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2

    def test_predictor_file_mention_boost(self):
        """Predictor boosts files explicitly mentioned in prompt."""
        from attnroute.predictor import PredictorModelV5, predict_files_v5

        model = PredictorModelV5()
        model.name_to_paths["auth.py"] = {"src/auth.py"}
        model.name_to_paths["utils.py"] = {"src/utils.py"}

        result = predict_files_v5("fix the bug in auth.py", model, [])
        result_paths = [r[0] for r in result]
        # auth.py should appear since it's mentioned by name
        assert any("auth" in p for p in result_paths), f"Expected auth.py in results: {result_paths}"


class TestDiagnostic:
    """Test diagnostic report generation."""

    def test_diagnostic_import(self):
        """Diagnostic module can be imported."""
        from attnroute.diagnostic import format_report_text, generate_report
        assert generate_report is not None
        assert format_report_text is not None

    def test_diagnostic_report_structure(self, tmp_path):
        """Diagnostic report has expected structure."""
        from attnroute.diagnostic import generate_report
        report = generate_report(tmp_path, run_bench=False)

        assert "generated_at" in report
        assert "system" in report
        assert "dependencies" in report
        assert "repository" in report
        assert "configuration" in report


class TestCompressor:
    """Test memory compression functionality."""

    def test_compressor_import(self):
        """Compressor can be imported."""
        try:
            from attnroute.compressor import ANTHROPIC_AVAILABLE, ObservationCompressor
            assert ObservationCompressor is not None
            assert isinstance(ANTHROPIC_AVAILABLE, bool)
        except ImportError:
            pytest.skip("Compressor dependencies not installed")


class TestGraphRetriever:
    """Test graph retrieval functionality."""

    def test_graph_retriever_import(self):
        """Graph retriever can be imported."""
        try:
            from attnroute.graph_retriever import GRAPH_AVAILABLE
            assert isinstance(GRAPH_AVAILABLE, bool)
        except ImportError:
            pytest.skip("Graph dependencies not installed")
