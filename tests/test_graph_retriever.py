"""Tests for graph_retriever module."""


def test_graph_available_import():
    """Test that GRAPH_AVAILABLE can be imported."""
    from attnroute.graph_retriever import GRAPH_AVAILABLE
    assert isinstance(GRAPH_AVAILABLE, bool)


def test_get_stats_returns_structure():
    """Test get_stats returns proper structure regardless of deps."""
    from attnroute.graph_retriever import get_stats

    stats = get_stats(".")
    assert "available" in stats
    assert isinstance(stats["available"], bool)


def test_get_stats_no_deps_has_reason():
    """When graph deps unavailable, stats includes a reason."""
    from attnroute.graph_retriever import GRAPH_AVAILABLE, get_stats

    stats = get_stats(".")
    if not GRAPH_AVAILABLE:
        assert stats["available"] is False
        assert "reason" in stats


def test_get_graph_context_returns_none_or_string():
    """get_graph_context returns None (no deps) or string (with deps)."""
    from attnroute.graph_retriever import GRAPH_AVAILABLE, get_graph_context

    result = get_graph_context("test query", ".")
    if not GRAPH_AVAILABLE:
        assert result is None
    else:
        assert isinstance(result, (str, type(None)))


def test_get_graph_context_with_temp_dir(tmp_path):
    """get_graph_context works on a temp directory without crashing."""
    from attnroute.graph_retriever import get_graph_context

    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    result = get_graph_context("main function", str(tmp_path))
    assert result is None or isinstance(result, str)
