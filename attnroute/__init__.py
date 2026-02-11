"""
attnroute - Context routing for AI coding assistants

Reduces token usage by 90%+ through intelligent context selection.
Learns which files you actually use and predicts what you'll need next.

Features:
- Repo mapping with tree-sitter and PageRank
- Usage pattern learning
- Smart file prediction
- Memory compression (optional)
- Zero required dependencies

Quick start:
    pip install attnroute[all]
    attnroute init
    attnroute status
"""

__version__ = "0.5.8"
__author__ = "jeranaias"
__all__ = [
    "__version__",
    "__author__",
    "build_context_output",
    "get_tier",
    "update_attention",
    "RepoMapper",
    "ObservationCompressor",
    "ProgressiveRetriever",
    "Learner",
]

# Core exports (noqa comments suppress F401 for intentional re-exports)
try:
    from attnroute.context_router import (  # noqa: F401
        build_context_output,
        get_tier,
        update_attention,
    )
except ImportError:
    pass

try:
    from attnroute.repo_map import RepoMapper  # noqa: F401
except ImportError:
    pass

try:
    from attnroute.compressor import (  # noqa: F401
        ObservationCompressor,
        ProgressiveRetriever,
    )
except ImportError:
    pass

try:
    from attnroute.learner import Learner  # noqa: F401
except ImportError:
    pass
