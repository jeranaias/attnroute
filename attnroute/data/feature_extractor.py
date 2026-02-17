#!/usr/bin/env python3
"""
Feature-based file representation for cross-project transfer learning.

Instead of mapping file paths to integer indices (project-specific, can't
transfer), this module extracts a fixed-size feature vector per file that
is meaningful across any codebase.

Feature vector (12 dimensions):
  [0-7]  Extension one-hot (py, js, ts, tsx, go, rs, java, other)
  [8]    Directory depth (normalized)
  [9]    Is test file (0/1)
  [10]   Is init/index file (0/1)
  [11]   Is config file (0/1)
"""
from pathlib import Path

FEATURE_DIM = 12

# Extension vocabulary (one-hot positions 0-7)
_EXT_INDEX = {
    ".py": 0,
    ".js": 1,
    ".ts": 2,
    ".tsx": 3,
    ".go": 4,
    ".rs": 5,
    ".java": 6,
}
_OTHER_EXT = 7

# Config file indicators
_CONFIG_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "cargo.toml", "go.mod", "makefile", "dockerfile",
    ".env", ".gitignore", "tsconfig.json", "jest.config.js",
    "webpack.config.js", "vite.config.ts", "babel.config.js",
}

_CONFIG_EXTS = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}

# Max depth for normalization
_MAX_DEPTH = 8


def extract_features(file_path: str) -> list[float]:
    """Extract a 12-dimensional feature vector from a file path.

    The features are designed to be meaningful across different codebases,
    enabling the neural predictor to transfer learned patterns.

    Args:
        file_path: Relative file path (e.g., "src/auth/controller.py")

    Returns:
        List of 12 floats representing the file's features
    """
    features = [0.0] * FEATURE_DIM
    path = Path(file_path.replace("\\", "/"))
    name = path.name.lower()
    stem = path.stem.lower()
    ext = path.suffix.lower()

    # [0-7] Extension one-hot
    ext_idx = _EXT_INDEX.get(ext, _OTHER_EXT)
    features[ext_idx] = 1.0

    # [8] Directory depth (normalized 0-1)
    depth = len(path.parts) - 1  # Exclude filename
    features[8] = min(1.0, depth / _MAX_DEPTH)

    # [9] Is test file
    if stem.startswith("test_") or stem.endswith("_test") or "test" in path.parts:
        features[9] = 1.0

    # [10] Is init/index file
    if stem in ("__init__", "index", "main", "mod", "lib"):
        features[10] = 1.0

    # [11] Is config file
    if name in _CONFIG_NAMES or ext in _CONFIG_EXTS:
        features[11] = 1.0

    return features


def extract_features_batch(file_paths: list[str]) -> list[list[float]]:
    """Extract features for a batch of file paths."""
    return [extract_features(p) for p in file_paths]
