"""Behavioral tests for the context router pipeline."""


class TestGetTier:
    """Test the tier classification function."""

    def test_hot_score(self):
        """Scores well above HOT_THRESHOLD are classified as HOT."""
        from attnroute.context_router import get_tier
        assert get_tier(1.0) == "HOT"
        assert get_tier(0.95) == "HOT"

    def test_warm_score(self):
        """Scores between WARM and HOT thresholds are WARM."""
        from attnroute.context_router import HOT_THRESHOLD, WARM_THRESHOLD, get_tier
        mid = (HOT_THRESHOLD + WARM_THRESHOLD) / 2
        assert get_tier(mid) == "WARM"

    def test_cold_score(self):
        """Scores below WARM_THRESHOLD are COLD."""
        from attnroute.context_router import get_tier
        assert get_tier(0.0) == "COLD"
        assert get_tier(0.1) == "COLD"

    def test_boundary_hot(self):
        """Score at exactly HOT_THRESHOLD is HOT."""
        from attnroute.context_router import HOT_THRESHOLD, get_tier
        assert get_tier(HOT_THRESHOLD) == "HOT"

    def test_boundary_just_below_hot(self):
        """Score just below HOT_THRESHOLD is WARM."""
        from attnroute.context_router import HOT_THRESHOLD, get_tier
        assert get_tier(HOT_THRESHOLD - 0.001) == "WARM"


class TestSourceFileClassification:
    """Test source vs doc file classification."""

    def test_python_is_source(self):
        from attnroute.context_router import _is_source_file
        assert _is_source_file("src/auth.py") is True

    def test_javascript_is_source(self):
        from attnroute.context_router import _is_source_file
        assert _is_source_file("src/app.js") is True

    def test_typescript_is_source(self):
        from attnroute.context_router import _is_source_file
        assert _is_source_file("components/App.tsx") is True

    def test_go_is_source(self):
        from attnroute.context_router import _is_source_file
        assert _is_source_file("cmd/main.go") is True

    def test_markdown_is_not_source(self):
        from attnroute.context_router import _is_source_file
        assert _is_source_file("docs/guide.md") is False

    def test_json_is_not_source(self):
        from attnroute.context_router import _is_source_file
        assert _is_source_file("package.json") is False


class TestDocFileClassification:
    """Test doc file detection."""

    def test_markdown_is_doc(self):
        from attnroute.context_router import _is_doc_file
        assert _is_doc_file("docs/guide.md") is True

    def test_claude_dir_is_doc(self):
        from attnroute.context_router import _is_doc_file
        assert _is_doc_file(".claude/overview.md") is True

    def test_source_not_doc(self):
        from attnroute.context_router import _is_doc_file
        assert _is_doc_file("src/main.py") is False


class TestDecayRates:
    """Test that decay configuration is sensible."""

    def test_decay_rates_within_bounds(self):
        """All decay rates should be between 0 and 1."""
        from attnroute.context_router import DECAY_RATES
        for category, rate in DECAY_RATES.items():
            assert 0.0 < rate < 1.0, f"Decay rate for {category} is {rate}, expected (0, 1)"

    def test_default_decay_exists(self):
        """Default decay rate must exist."""
        from attnroute.context_router import DECAY_RATES
        assert "default" in DECAY_RATES


class TestConfiguration:
    """Test configuration constants are coherent."""

    def test_hot_above_warm(self):
        """HOT threshold must be above WARM threshold."""
        from attnroute.context_router import HOT_THRESHOLD, WARM_THRESHOLD
        assert HOT_THRESHOLD > WARM_THRESHOLD

    def test_warm_above_zero(self):
        """WARM threshold must be positive."""
        from attnroute.context_router import WARM_THRESHOLD
        assert WARM_THRESHOLD > 0

    def test_max_files_positive(self):
        """File limits must be positive."""
        from attnroute.context_router import MAX_HOT_FILES, MAX_WARM_FILES
        assert MAX_HOT_FILES > 0
        assert MAX_WARM_FILES > 0

    def test_source_limits_positive(self):
        """Source file limits must be positive."""
        from attnroute.context_router import SOURCE_MAX_HOT_FILES, SOURCE_MAX_WARM_FILES
        assert SOURCE_MAX_HOT_FILES > 0
        assert SOURCE_MAX_WARM_FILES > 0

    def test_total_chars_positive(self):
        """Total character budget must be positive."""
        from attnroute.context_router import MAX_TOTAL_CHARS
        assert MAX_TOTAL_CHARS > 0
