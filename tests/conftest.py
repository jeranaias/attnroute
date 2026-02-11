"""Pytest configuration and fixtures for attnroute tests."""


import pytest


@pytest.fixture
def sample_python_code():
    """Sample Python code for testing."""
    return '''
def calculate_sum(a: int, b: int) -> int:
    """Calculate the sum of two numbers."""
    return a + b


class Calculator:
    """A simple calculator class."""

    def __init__(self):
        self.history = []

    def add(self, x, y):
        result = x + y
        self.history.append(result)
        return result

    def subtract(self, x, y):
        result = x - y
        self.history.append(result)
        return result


def main():
    calc = Calculator()
    print(calc.add(1, 2))
'''


@pytest.fixture
def sample_keywords_json():
    """Sample keywords.json content."""
    return {
        "keywords": {
            "src/api.py": ["api", "endpoint", "route", "handler"],
            "src/models.py": ["model", "database", "schema"],
            "docs/readme.md": ["documentation", "usage", "install"]
        },
        "pinned": ["src/config.py", "README.md"]
    }


@pytest.fixture
def temp_repo(tmp_path, sample_python_code):
    """Create a temporary repository structure for testing."""
    # Create directory structure
    src = tmp_path / "src"
    src.mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()

    # Create Python files
    (src / "main.py").write_text(sample_python_code)
    (src / "utils.py").write_text("def helper(): pass\n")
    (src / "__init__.py").write_text("")
    (tests / "test_main.py").write_text("def test_example(): pass\n")

    # Create config files
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (tmp_path / "README.md").write_text("# Test Project\n")

    return tmp_path
