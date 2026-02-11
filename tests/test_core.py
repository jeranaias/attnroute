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
