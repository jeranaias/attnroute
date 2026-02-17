#!/usr/bin/env python3
"""
Repo configuration and management for benchmarks.

Central definition of public repos used across all benchmark scenarios.
Handles cloning, caching, and repo metadata.
"""

import subprocess
from pathlib import Path

# Cache directory for cloned repos
REPO_CACHE_DIR = Path(__file__).parent / ".repos"

# Language-to-extension mapping
LANGUAGE_EXTENSIONS = {
    "python": [".py"],
    "javascript": [".js", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
    "go": [".go"],
    "rust": [".rs"],
    "java": [".java"],
    "c": [".c", ".h"],
    "cpp": [".cpp", ".hpp", ".cc", ".h"],
}

# All source extensions for baseline measurement
ALL_SOURCE_EXTENSIONS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cc",
}

# Directories to skip during measurement
EXCLUDED_DIRS = {
    "node_modules", "vendor", ".git", "__pycache__", "venv", ".venv",
    "dist", "build", "target", ".tox", "eggs", ".eggs", "third_party",
}

# ============================================================================
# REPO DEFINITIONS
# ============================================================================

REPOS = [
    {
        "name": "flask",
        "url": "https://github.com/pallets/flask.git",
        "branch": "main",
        "language": "python",
        "category": "Web framework",
        "description": "Python web microframework",
    },
    {
        "name": "express",
        "url": "https://github.com/expressjs/express.git",
        "branch": "master",
        "language": "javascript",
        "category": "Web framework",
        "description": "Node.js web framework",
    },
    {
        "name": "gin",
        "url": "https://github.com/gin-gonic/gin.git",
        "branch": "master",
        "language": "go",
        "category": "Web framework",
        "description": "Go HTTP framework",
    },
    {
        "name": "ripgrep",
        "url": "https://github.com/BurntSushi/ripgrep.git",
        "branch": "master",
        "language": "rust",
        "category": "CLI tool",
        "description": "Fast grep replacement in Rust",
    },
    {
        "name": "spring-petclinic",
        "url": "https://github.com/spring-projects/spring-petclinic.git",
        "branch": "main",
        "language": "java",
        "category": "Enterprise app",
        "description": "Spring Boot sample application",
    },
    {
        "name": "redis",
        "url": "https://github.com/redis/redis.git",
        "branch": "unstable",
        "language": "c",
        "category": "Database",
        "description": "In-memory data store",
    },
    {
        "name": "TypeScript",
        "url": "https://github.com/microsoft/TypeScript.git",
        "branch": "main",
        "language": "typescript",
        "category": "Compiler",
        "description": "TypeScript compiler (large)",
    },
    {
        "name": "fastapi",
        "url": "https://github.com/fastapi/fastapi.git",
        "branch": "master",
        "language": "python",
        "category": "Web framework",
        "description": "Modern Python web framework",
    },
    {
        "name": "deno",
        "url": "https://github.com/denoland/deno.git",
        "branch": "main",
        "language": "rust",
        "category": "Runtime",
        "description": "Deno runtime (large, Rust+TS)",
    },
    {
        "name": "jq",
        "url": "https://github.com/jqlang/jq.git",
        "branch": "master",
        "language": "c",
        "category": "CLI tool",
        "description": "JSON processor (small C project)",
    },
    {
        "name": "vue-core",
        "url": "https://github.com/vuejs/core.git",
        "branch": "main",
        "language": "typescript",
        "category": "Frontend framework",
        "description": "Vue.js 3 core",
    },
    {
        "name": "tokio",
        "url": "https://github.com/tokio-rs/tokio.git",
        "branch": "master",
        "language": "rust",
        "category": "Async runtime",
        "description": "Rust async runtime",
    },
]

# Quick mode uses just these 3 (diverse languages, manageable size)
QUICK_REPOS = ["flask", "express", "gin"]


def get_repos(mode: str = "quick") -> list[dict]:
    """Get repo list based on mode ('quick' or 'full')."""
    if mode == "full":
        return REPOS
    return [r for r in REPOS if r["name"] in QUICK_REPOS]


def clone_repo(repo: dict) -> tuple[bool, Path, str]:
    """
    Clone a repo to the cache directory (reuses if already cloned).

    Returns:
        (success, repo_path, commit_sha_or_error)
    """
    REPO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = REPO_CACHE_DIR / repo["name"]

    if target.exists() and (target / ".git").exists():
        # Already cloned — get current SHA
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target, capture_output=True, text=True
        )
        if result.returncode == 0:
            return True, target, result.stdout.strip()
        # .git is broken, remove and re-clone
        import shutil
        shutil.rmtree(target, ignore_errors=True)

    # Clone with shallow depth
    print(f"  Cloning {repo['url']}...")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", repo["branch"],
         repo["url"], str(target)],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        return False, target, result.stderr.strip()

    # Get commit SHA
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target, capture_output=True, text=True
    )
    sha = result.stdout.strip() if result.returncode == 0 else "unknown"

    return True, target, sha


def get_repo_info(repo_path: Path, language: str = None) -> dict:
    """
    Measure repository statistics.

    Returns:
        {files, lines, chars, extensions_found}
    """
    if language and language in LANGUAGE_EXTENSIONS:
        extensions = set(LANGUAGE_EXTENSIONS[language])
    else:
        extensions = ALL_SOURCE_EXTENSIONS

    total_files = 0
    total_lines = 0
    total_chars = 0

    for f in repo_path.rglob("*"):
        if not f.is_file() or f.suffix not in extensions:
            continue
        if any(part in EXCLUDED_DIRS for part in f.parts):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            total_files += 1
            total_lines += content.count("\n") + 1
            total_chars += len(content)
        except Exception:
            continue

    return {
        "files": total_files,
        "lines": total_lines,
        "chars": total_chars,
    }


def get_all_source_content(repo_path: Path) -> str:
    """Get concatenated content of all source files (for baseline measurement)."""
    content_parts = []

    for f in repo_path.rglob("*"):
        if not f.is_file() or f.suffix not in ALL_SOURCE_EXTENSIONS:
            continue
        if any(part in EXCLUDED_DIRS for part in f.parts):
            continue
        try:
            content_parts.append(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue

    return "".join(content_parts)


if __name__ == "__main__":
    print("attnroute benchmark repos:")
    print()
    for repo in REPOS:
        tag = " [QUICK]" if repo["name"] in QUICK_REPOS else ""
        print(f"  {repo['name']:<20} {repo['language']:<12} {repo['category']:<20} {repo['description']}{tag}")
    print()
    print(f"Quick mode: {len(QUICK_REPOS)} repos")
    print(f"Full mode:  {len(REPOS)} repos")
