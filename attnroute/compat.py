"""
Compatibility utilities for attnroute.

Centralizes the dual import pattern used throughout the codebase.
Supports both package imports (pip installed) and standalone imports (development).
"""

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar('T')


def try_import(
    package_path: str,
    standalone_path: str,
    names: list[str],
) -> tuple[dict[str, Any], bool]:
    """
    Try importing from package first, then standalone.

    Args:
        package_path: Full package path (e.g., "attnroute.learner")
        standalone_path: Standalone module name (e.g., "learner")
        names: List of names to import from the module

    Returns:
        Tuple of (dict of imported names, success bool)

    Example:
        imports, available = try_import(
            "attnroute.telemetry_lib",
            "telemetry_lib",
            ["rotate_jsonl", "get_session_id"]
        )
        if available:
            rotate_jsonl = imports["rotate_jsonl"]
    """
    # Try package import first
    try:
        module = __import__(package_path, fromlist=names)
        return {name: getattr(module, name) for name in names}, True
    except ImportError:
        pass

    # Try standalone import
    try:
        module = __import__(standalone_path, fromlist=names)
        return {name: getattr(module, name) for name in names}, True
    except ImportError:
        pass

    return {}, False


def try_import_class(
    package_path: str,
    standalone_path: str,
    class_name: str,
) -> tuple[type | None, bool]:
    """
    Try importing a single class from package or standalone.

    Args:
        package_path: Full package path
        standalone_path: Standalone module name
        class_name: Name of the class to import

    Returns:
        Tuple of (class or None, success bool)
    """
    imports, available = try_import(package_path, standalone_path, [class_name])
    if available:
        return imports[class_name], True
    return None, False


class LazyLoader:
    """
    Lazy loader for module-level objects.

    Avoids side effects at import time by deferring instantiation
    until first access.

    Example:
        _learner = LazyLoader(lambda: Learner())

        # Later, when actually needed:
        learner = _learner.get()  # Instantiates on first call
    """

    def __init__(self, factory: Callable[[], T]):
        self._factory = factory
        self._instance: T | None = None
        self._initialized = False

    def get(self) -> T | None:
        """Get the lazily-loaded instance."""
        if not self._initialized:
            try:
                self._instance = self._factory()
            except Exception:
                self._instance = None
            self._initialized = True
        return self._instance

    def is_available(self) -> bool:
        """Check if the instance was successfully created."""
        if not self._initialized:
            self.get()
        return self._instance is not None

    def reset(self) -> None:
        """Reset the loader (useful for testing)."""
        self._instance = None
        self._initialized = False
