"""
Compatibility utilities for attnroute.

Centralizes the dual import pattern used throughout the codebase.
Supports both package imports (pip installed) and standalone imports (development).

Also provides security utilities:
- safe_read_stdin(): Size-limited stdin reading to prevent memory exhaustion
- validate_path(): Path traversal prevention with Windows-specific checks
- safe_atomic_write(): Cross-platform atomic file writes with fallback
- normalize_path_key(): Consistent path keys across platforms
- validate_plugin_name(): Plugin name validation to prevent path traversal
"""

import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

# Maximum stdin size to prevent memory exhaustion (10MB in bytes)
MAX_STDIN_BYTES = 10 * 1024 * 1024

# Windows reserved device names (case-insensitive)
_WINDOWS_RESERVED_NAMES = frozenset({
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
})


def safe_read_stdin(max_bytes: int = MAX_STDIN_BYTES) -> str:
    """
    Read stdin with size limit to prevent memory exhaustion DoS.

    Args:
        max_bytes: Maximum bytes to read (default 10MB)

    Returns:
        Content read from stdin, truncated if over limit
    """
    if sys.stdin is None or (hasattr(sys.stdin, 'closed') and sys.stdin.closed):
        return ""
    try:
        # Read bytes directly to accurately count memory usage
        # This prevents 4-byte Unicode chars from consuming 4x expected memory
        if hasattr(sys.stdin, 'buffer'):
            chunks = []
            total = 0
            while total < max_bytes:
                chunk = sys.stdin.buffer.read(min(65536, max_bytes - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return b''.join(chunks).decode('utf-8', errors='replace')
        else:
            # Fallback for non-binary stdin (e.g., StringIO in tests)
            chunks = []
            total = 0
            while total < max_bytes:
                chunk = sys.stdin.read(min(65536, max_bytes - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return ''.join(chunks)
    except Exception:
        return ""


def validate_path(
    path: Path | str | None,
    allowed_roots: list[Path] | None = None
) -> Path:
    """
    Validate a path to prevent path traversal attacks.

    Args:
        path: Path to validate (cannot be None)
        allowed_roots: List of allowed root directories. If None, allows:
                      - User home directory (~/.claude/)
                      - Current working directory

    Returns:
        Resolved absolute path

    Raises:
        ValueError: If path is None, escapes allowed roots, or contains dangerous patterns
    """
    # Check for None
    if path is None:
        raise ValueError("Path cannot be None")

    if isinstance(path, str):
        path = Path(path)

    path_str = str(path)

    # Check for null bytes (path injection)
    if '\x00' in path_str:
        raise ValueError("Path contains null bytes")

    # Check for Windows Alternate Data Streams (ADS)
    # Format: filename:streamname or filename:streamname:$DATA
    # Only check the filename part (after last separator)
    filename = path.name
    if ':' in filename:
        # Allow drive letter (e.g., C:) but block ADS
        if not (len(filename) == 2 and filename[1] == ':' and filename[0].isalpha()):
            raise ValueError("Path contains Windows Alternate Data Stream")

    # Check for Windows reserved device names
    # These can cause hangs or special behavior
    stem = path.stem.upper().split('.')[0]
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Path uses reserved Windows device name: {stem}")

    # Resolve to absolute, following symlinks
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"Cannot resolve path: {path}") from e

    # Check resolved path for null bytes too (symlink could point to path with nulls)
    if '\x00' in str(resolved):
        raise ValueError("Resolved path contains null bytes")

    # Default allowed roots
    if allowed_roots is None:
        allowed_roots = [
            Path.home() / ".claude",
            Path.cwd(),
        ]

    # Resolve allowed roots
    resolved_roots = []
    for root in allowed_roots:
        try:
            resolved_roots.append(root.resolve())
        except (OSError, RuntimeError):
            continue

    # Check if path is under any allowed root
    for root in resolved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    raise ValueError(f"Path {resolved} is outside allowed directories")


def validate_plugin_name(name: str) -> str:
    """
    Validate a plugin name to prevent path traversal.

    Args:
        name: Plugin name to validate

    Returns:
        Validated plugin name

    Raises:
        ValueError: If name contains path traversal characters
    """
    if not name:
        raise ValueError("Plugin name cannot be empty")

    # Block path separators and traversal patterns
    if '/' in name or '\\' in name or '..' in name:
        raise ValueError(f"Invalid plugin name (contains path characters): {name}")

    # Block null bytes
    if '\x00' in name:
        raise ValueError("Plugin name contains null bytes")

    # Block Windows reserved names
    if name.upper().split('.')[0] in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Plugin name uses reserved Windows device name: {name}")

    # Only allow alphanumeric, underscore, hyphen
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(f"Invalid plugin name (use only alphanumeric, underscore, hyphen): {name}")

    return name


def normalize_path_key(path: Path | str) -> str:
    """
    Normalize a path for use as a dictionary key.

    Ensures consistent forward slashes across Windows and Unix.

    Args:
        path: Path to normalize

    Returns:
        Normalized path string with forward slashes
    """
    return str(path).replace("\\", "/")


def safe_atomic_write(path: Path | str, content: str, encoding: str = 'utf-8') -> bool:
    """
    Write file atomically with cross-platform fallback.

    On Windows, os.replace() can fail if the target file is locked.
    This function tries atomic write first, then falls back to direct write.

    Args:
        path: Target file path
        content: Content to write
        encoding: Text encoding (default utf-8)

    Returns:
        True if write succeeded, False otherwise
    """
    path = Path(path)

    # Create parent directory (inside try to handle permission errors)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    # Try atomic write first
    temp_fd = None
    temp_path = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            suffix='.tmp',
            prefix=path.stem + '_'
        )
        with os.fdopen(temp_fd, 'w', encoding=encoding) as f:
            f.write(content)
        temp_fd = None  # fd now owned by file object

        # Atomic rename
        Path(temp_path).replace(path)
        return True
    except OSError:
        # Clean up temp file if atomic failed
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass

        # Fallback: direct write (not atomic but better than failing)
        try:
            path.write_text(content, encoding=encoding)
            return True
        except Exception:
            return False
    except Exception:
        # Clean up on any other error
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        return False


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


class LazyLoader(Generic[T]):
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
