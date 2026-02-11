"""Test version consistency between __init__.py and pyproject.toml."""
import re
import sys
from pathlib import Path


def test_version_sync():
    """Ensure __init__.py and pyproject.toml versions match."""
    import attnroute

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return  # Skip if pyproject.toml not found

    # Use tomllib on Python 3.11+, regex fallback for 3.10
    if sys.version_info >= (3, 11):
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        pyproject_version = data["project"]["version"]
    else:
        # Fallback: parse version with regex for Python 3.10
        content = pyproject.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not match:
            return  # Skip if version not found
        pyproject_version = match.group(1)

    assert attnroute.__version__ == pyproject_version, (
        f"Version mismatch: __init__.py={attnroute.__version__} "
        f"pyproject.toml={pyproject_version}"
    )
