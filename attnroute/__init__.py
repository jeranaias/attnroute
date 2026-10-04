"""
attnroute - Context routing for AI coding assistants

Reduces token usage through intelligent context selection.
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

__version__ = "1.0.2"
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

# ═══ WHY THESE ARE LAZY ════════════════════════════════════════════════════════════════
#
# WARNING: EAGER RE-EXPORTS MADE EVERY HOOK COST SIX SECONDS. Measured with
#   `python -X importtime -c "import attnroute"`:
#
#       attnroute.compressor   3.95 s   <- imports chromadb
#         chromadb             2.24 s        <- imports opentelemetry
#       attnroute              5.67 s
#
#   and end to end, `py -3 -m attnroute.read_ledger` on a trivial event took 5.9-6.4 s
#   across three runs. attnroute is installed as HOOKS: UserPromptSubmit, Stop, and -- with
#   the read ledger -- PreToolUse and PostToolUse on every Read and every Edit. Six seconds
#   of import on each of those is wall-clock time the user waits, to save tokens worth a
#   fraction of a second. The lever was negative and the package was the reason.
#
#   PEP 562 module __getattr__ keeps `from attnroute import RepoMapper` working unchanged
#   while deferring the cost to whoever actually asks for the name. A hook that imports
#   `attnroute.read_ledger` now pulls in only what that module needs.
#
#   The try/except around each import is preserved as a RETURN OF None-by-AttributeError:
#   an optional dependency that is missing still raises AttributeError from the package, not
#   ImportError at interpreter start, which is what the previous code arranged for.
_LAZY = {
    "build_context_output": "attnroute.context_router",
    "get_tier": "attnroute.context_router",
    "update_attention": "attnroute.context_router",
    "RepoMapper": "attnroute.repo_map",
    "ObservationCompressor": "attnroute.compressor",
    "ProgressiveRetriever": "attnroute.compressor",
    "Learner": "attnroute.learner",
}


def __getattr__(name):
    """Import the owning module on first use. PEP 562."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'attnroute' has no attribute {name!r}")
    import importlib
    try:
        value = getattr(importlib.import_module(module), name)
    except ImportError as exc:
        # Same outcome as before for a missing optional dependency: the name is simply not
        # available. It is reported with its cause rather than silently absent.
        raise AttributeError(
            f"attnroute.{name} needs {module}, which could not be imported: {exc}") from exc
    globals()[name] = value          # second access is a plain attribute lookup
    return value


def __dir__():
    return sorted(set(__all__) | set(_LAZY))
