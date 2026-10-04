"""Pytest configuration and fixtures for attnroute tests."""


import os

import pytest

#: Every environment variable attnroute reads. The suite must not inherit any of them.
ATTNROUTE_ENV = (
    "ATTNROUTE_LEDGER_ACT",
    "ATTNROUTE_CAP_ACT",
    "ATTNROUTE_LEDGER_HOLDOUT_PCT",
    "ATTNROUTE_LEDGER_TURN_HOLDOUT_PCT",
    "ATTNROUTE_CAP_HOLDOUT_PCT",
    "ATTNROUTE_OBSERVE_ONLY",
    "ATTNROUTE_TEAM",
    "ATTNROUTE_TRUST_USED_SIGNAL",
)


@pytest.fixture(autouse=True)
def _no_ambient_attnroute_env(monkeypatch):
    """WARNING: A TEST MUST NOT INHERIT THE OPERATOR'S CONFIGURATION.

    attnroute is now installed on the machines it is developed on, and the canary runs with
    `ATTNROUTE_LEDGER_HOLDOUT_PCT=50` and `ATTNROUTE_CAP_HOLDOUT_PCT=50` in
    `~/.claude/settings.json`'s env block. That turned a passing test --
    `test_the_arms_land_near_their_targets`, which asserts the DEFAULT 10% split -- into a
    local failure reporting 48.175%, while CI stayed green because the runner has no such
    settings.

    Failing locally and passing in CI is the worst combination there is: it trains people to
    distrust their own test run. So every attnroute variable is cleared for every test, and
    a test that wants one sets it explicitly with `monkeypatch.setenv` -- which still works,
    because this runs first and the test's own call runs after.
    """
    for name in ATTNROUTE_ENV:
        monkeypatch.delenv(name, raising=False)
    assert not [n for n in ATTNROUTE_ENV if n in os.environ], "env not cleared"


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
