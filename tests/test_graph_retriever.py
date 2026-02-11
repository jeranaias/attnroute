"""Tests for graph_retriever module."""




def test_graph_available_import():
    """Test that GRAPH_AVAILABLE can be imported."""
    from attnroute.graph_retriever import GRAPH_AVAILABLE
    assert isinstance(GRAPH_AVAILABLE, bool)


def test_get_stats_no_deps():
    """Test get_stats returns proper structure when deps unavailable."""
    from attnroute.graph_retriever import GRAPH_AVAILABLE, get_stats

    stats = get_stats(".")
    assert "available" in stats

    if not GRAPH_AVAILABLE:
        assert stats["available"] is False
        assert "reason" in stats


def test_get_graph_context():
    """Test get_graph_context function."""
    from attnroute.graph_retriever import GRAPH_AVAILABLE, get_graph_context

    result = get_graph_context("test query", ".")

    if not GRAPH_AVAILABLE:
        assert result is None
    else:
        assert isinstance(result, (str, type(None)))
