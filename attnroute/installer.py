#!/usr/bin/env python3
"""
attnroute.installer — Cross-platform Setup for Claude Code Hooks

Detects OS and Python command, generates settings.json with correct hook
commands, scans for projects, verifies dependencies, and offers
keywords.json template generation.

v0.5.0 features:
- Dependency verification (bm25s, model2vec, tree-sitter)
- Auto-build search index after setup
- Model download guidance for first-time users

Usage: attnroute init
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from attnroute import __version__ as VERSION
    from attnroute.telemetry_lib import windows_utf8_io
    windows_utf8_io()
except ImportError:
    VERSION = "0.5.0"
    # Fallback UTF-8 setup for Windows
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except Exception:
                    pass


# ============================================================================
# DEPENDENCY VERIFICATION
# ============================================================================

REQUIRED_DEPS = {
    "bm25s": {"import": "bm25s", "purpose": "BM25 sparse retrieval", "fallback": "TF-IDF fallback"},
    "model2vec": {"import": "model2vec", "purpose": "Semantic reranking", "fallback": "BM25 only"},
    "numpy": {"import": "numpy", "purpose": "Vector operations", "fallback": "Basic math"},
    "tree_sitter": {"import": "tree_sitter", "purpose": "Code outlines", "fallback": "Regex extraction"},
    "tree_sitter_languages": {"import": "tree_sitter_languages", "purpose": "Multi-language parsing", "fallback": "Python only"},
}


def check_dependencies() -> dict:
    """
    Check which optional dependencies are installed.

    Returns:
        {"dep_name": {"installed": bool, "version": str|None, "purpose": str, "fallback": str}}
    """
    results = {}
    for name, info in REQUIRED_DEPS.items():
        try:
            mod = __import__(info["import"])
            version = getattr(mod, "__version__", "?")
            results[name] = {
                "installed": True,
                "version": version,
                "purpose": info["purpose"],
                "fallback": info["fallback"],
            }
        except ImportError:
            results[name] = {
                "installed": False,
                "version": None,
                "purpose": info["purpose"],
                "fallback": info["fallback"],
            }
    return results


def maybe_build_search_index():
    """
    Build search index if BM25 is available and index doesn't exist.

    Returns True if index was built or already exists.
    """
    try:
        from attnroute.indexer import SEARCH_AVAILABLE, SearchIndex
        if not SEARCH_AVAILABLE:
            return False

        idx = SearchIndex()
        # Check if index has any documents
        stats = idx.get_stats()
        if stats["total_documents"] == 0:
            print("  Building search index (first time)...")
            claude_dir = Path.home() / ".claude"
            if claude_dir.exists():
                idx.build(docs_root=claude_dir)
                stats = idx.get_stats()
                print(f"  Index built: {stats['total_documents']} documents")
            return True
        else:
            print(f"  Search index: {stats['total_documents']} documents")
            return True
    except ImportError:
        return False
    except Exception as e:
        print(f"  Search index: error ({e})")
        return False


SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
TELEMETRY_DIR = Path.home() / ".claude" / "telemetry"
SCRIPTS_DIR = Path(__file__).parent.parent  # scripts/ dir (parent of attnroute/)

# Pool scripts that live alongside attnroute but aren't part of the package
POOL_SCRIPTS = ["pool-auto-update.py", "pool-loader.py", "pool-extractor.py"]


def detect_python_command() -> str:
    """Find the correct Python 3 command for this OS."""
    candidates = ["py -3", "python3", "python"]
    for cmd in candidates:
        try:
            parts = cmd.split()
            result = subprocess.run(
                parts + ["--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "Python 3" in result.stdout:
                return cmd
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return "python3"  # Best guess fallback


def detect_entry_points() -> bool:
    """Check if attnroute entry points are available on PATH."""
    return shutil.which("attnroute-router") is not None


def find_script(name: str) -> str:
    """Find a script by name, checking scripts/ dir."""
    candidate = SCRIPTS_DIR / name
    if candidate.exists():
        return str(candidate).replace("\\", "/")
    return name


def build_hook_command(module_name: str, python_cmd: str) -> str:
    """Build the correct hook command using module invocation."""
    return f'{python_cmd} -m attnroute.{module_name}'


def build_settings(python_cmd: str, use_entry_points: bool = False, include_pool: bool = True) -> dict:
    """Build the Claude Code settings.json hook configuration."""
    hooks = {
        "UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": build_hook_command("context_router", python_cmd)},
        ]}],
        "SessionStart": [{"hooks": [
            {"type": "command", "command": build_hook_command("session_init", python_cmd)},
        ]}],
        "Stop": [{"hooks": [
            {"type": "command", "command": build_hook_command("telemetry_record", python_cmd)},
        ]}],
    }

    # Add pool scripts if they exist (they're not part of attnroute package)
    if include_pool:
        pool_auto = SCRIPTS_DIR / "pool-auto-update.py"
        pool_loader = SCRIPTS_DIR / "pool-loader.py"
        pool_extractor = SCRIPTS_DIR / "pool-extractor.py"

        if pool_auto.exists():
            hooks["UserPromptSubmit"][0]["hooks"].append({
                "type": "command",
                "command": f'{python_cmd} "{str(pool_auto).replace(chr(92), "/")}"'
            })
        if pool_loader.exists():
            hooks["SessionStart"][0]["hooks"].append({
                "type": "command",
                "command": f'{python_cmd} "{str(pool_loader).replace(chr(92), "/")}"'
            })
        if pool_extractor.exists():
            hooks["Stop"][0]["hooks"].insert(0, {
                "type": "command",
                "command": f'{python_cmd} "{str(pool_extractor).replace(chr(92), "/")}"'
            })

    return {"hooks": hooks}


def scan_projects() -> list:
    """Find directories with .claude/ that might benefit from keywords.json."""
    projects = []
    seen = set()

    def add_if_project(path: Path):
        """Check if path has .claude/ and add to results."""
        try:
            resolved = str(path.resolve())
        except (OSError, ValueError):
            return
        if resolved in seen:
            return
        claude_dir = path / ".claude"
        if claude_dir.is_dir():
            seen.add(resolved)
            projects.append({
                "path": resolved,
                "has_keywords": (claude_dir / "keywords.json").exists(),
                "has_md": any(claude_dir.glob("**/*.md")),
            })

    # 1. Always check CWD first
    add_if_project(Path.cwd())

    # 2. Check immediate children of home (original behavior)
    home = Path.home()
    try:
        for item in home.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                add_if_project(item)
    except PermissionError:
        pass

    # 3. Check two levels deep for common structures like ~/code/project/
    try:
        for item in home.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue
            try:
                for subitem in item.iterdir():
                    if subitem.is_dir() and not subitem.name.startswith("."):
                        add_if_project(subitem)
            except PermissionError:
                continue
    except PermissionError:
        pass

    # 4. On Windows, also check drive roots (one level deep)
    if sys.platform == "win32":
        for drive in ["C:\\", "D:\\"]:
            if Path(drive).exists():
                try:
                    for item in Path(drive).iterdir():
                        if item.is_dir() and not item.name.startswith("."):
                            add_if_project(item)
                except PermissionError:
                    continue

    return projects


def generate_keywords_template(project_path: Path) -> dict:
    """
    Generate a starter keywords.json from project structure.

    Scans .md files in .claude/ and extracts keywords from:
    - Filename (split on - and _)
    - Parent directory name
    - Markdown headings (## and ###) from file content
    - Project README.md top-level headings
    """
    claude_dir = project_path / ".claude"
    template = {"keywords": {}, "co_activation": {}, "pinned": []}

    # Find all .md files in .claude/
    md_files = list(claude_dir.rglob("*.md"))
    all_file_keys = []

    for md_file in md_files:
        rel = str(md_file.relative_to(claude_dir)).replace("\\", "/")
        if rel == "CLAUDE.md":
            continue
        all_file_keys.append(rel)

        # Extract keywords from filename and directory
        stem = md_file.stem.lower().replace("-", " ").replace("_", " ")
        parts = [p for p in stem.split() if len(p) > 2]

        # Add directory name as keyword too
        if md_file.parent != claude_dir:
            dir_name = md_file.parent.name.lower()
            parts.append(dir_name)

        # Extract keywords from markdown headings inside the file
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("#"):
                    # Strip leading # symbols and whitespace
                    heading_text = line.lstrip("#").strip().lower()
                    # Split heading into keyword candidates
                    words = heading_text.replace("-", " ").replace("_", " ").split()
                    for w in words:
                        if len(w) > 3 and w not in parts:
                            parts.append(w)
        except Exception:
            pass

        # Deduplicate while preserving order
        seen = set()
        unique_parts = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                unique_parts.append(p)

        template["keywords"][rel] = unique_parts

    # Auto-generate co-activation: files in the same directory are related
    dirs = {}
    for rel in all_file_keys:
        parent = str(Path(rel).parent)
        if parent != ".":
            dirs.setdefault(parent, []).append(rel)
    for dir_name, files in dirs.items():
        if len(files) > 1:
            for f in files:
                others = [o for o in files if o != f]
                if others:
                    template["co_activation"][f] = others

    # Find pinned candidates (architecture/overview files)
    for md_file in md_files:
        rel = str(md_file.relative_to(claude_dir)).replace("\\", "/")
        name_lower = md_file.stem.lower()
        if any(k in name_lower for k in ["architecture", "overview", "network", "readme"]):
            template["pinned"].append(rel)

    return template


def main():
    """Main installer entry point."""
    print()
    print(f"  attnroute v{VERSION}")
    print("  Attentional context routing for Claude Code")
    print("─" * 56)
    print()

    # Detect environment
    platform = sys.platform
    python_cmd = detect_python_command()
    use_entry_points = detect_entry_points()

    mode = "pip entry points" if use_entry_points else f"script paths ({python_cmd})"
    print(f"  Platform   {platform}")
    print(f"  Python     {python_cmd}")
    print(f"  Mode       {mode}")
    print()

    # Check dependencies
    deps = check_dependencies()
    installed = sum(1 for d in deps.values() if d["installed"])
    total = len(deps)
    print(f"  Dependencies ({installed}/{total}):")
    for name, info in deps.items():
        if info["installed"]:
            print(f"    + {name:<26} v{info['version']} ({info['purpose']})")
        else:
            print(f"    - {name:<26} missing ({info['fallback']})")

    if installed < total:
        print("\n  To install all dependencies:")
        print("    pip install bm25s model2vec numpy tree-sitter-languages")
    print()

    # Ensure telemetry directory
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  Telemetry dir: {TELEMETRY_DIR}")

    # Check for existing settings
    existing = {}
    if SETTINGS_FILE.exists():
        try:
            existing = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Check for pool scripts
    has_pool = any((SCRIPTS_DIR / p).exists() for p in POOL_SCRIPTS)

    # Build new settings
    new_settings = build_settings(python_cmd, use_entry_points, include_pool=has_pool)

    # Merge with existing non-hook settings
    for key, value in existing.items():
        if key != "hooks":
            new_settings[key] = value

    # Write settings
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(new_settings, indent=2), encoding="utf-8")
    print(f"  Settings: {SETTINGS_FILE} (updated)")

    # Show hook summary
    hook_count = sum(
        len(group["hooks"])
        for event_hooks in new_settings["hooks"].values()
        for group in event_hooks
    )
    print(f"  Hooks installed: {hook_count} across {len(new_settings['hooks'])} events")
    if has_pool:
        print(f"  Pool scripts: included (found in {SCRIPTS_DIR})")

    # Try to build search index if deps available
    maybe_build_search_index()

    # Scan projects
    projects = scan_projects()
    if projects:
        needs_keywords = [p for p in projects if p["has_md"] and not p["has_keywords"]]
        has_keywords = [p for p in projects if p["has_keywords"]]

        print(f"\n  Projects ({len(projects)} found):")
        for p in has_keywords:
            name = Path(p["path"]).name
            print(f"    + {name:<24} keywords.json")
        for p in needs_keywords:
            name = Path(p["path"]).name
            print(f"    - {name:<24} no keywords.json")

        # Generate templates for projects without keywords.json
        templates_written = 0
        for p in needs_keywords:
            project_path = Path(p["path"])
            template_file = project_path / ".claude" / "keywords.json.template"
            if not template_file.exists():
                template = generate_keywords_template(project_path)
                if template["keywords"]:
                    template_file.write_text(json.dumps(template, indent=2), encoding="utf-8")
                    templates_written += 1

        if templates_written:
            print(f"\n  Generated {templates_written} keywords.json.template file{'s' if templates_written != 1 else ''}")
            print("  Rename to keywords.json and customize to activate routing.")
    else:
        print("\n  No projects with .claude/ directories found.")

    print()
    print("─" * 56)
    print("  Ready. Hooks activate on next Claude Code session.")
    print()

    # Quick reference
    report_cmd = 'attnroute-report' if use_entry_points else f"{python_cmd} \"{find_script('telemetry-report.py')}\""
    history_cmd = 'attnroute-history' if use_entry_points else f"{python_cmd} \"{find_script('history.py')}\""
    suggest_cmd = 'attnroute-suggest' if use_entry_points else f"{python_cmd} \"{find_script('advisor.py')}\""
    oracle_cmd = 'attnroute-oracle' if use_entry_points else f"{python_cmd} \"{find_script('oracle.py')}\""
    index_cmd = 'attnroute-index' if use_entry_points else f"{python_cmd} \"{find_script('indexer.py')}\""
    print("  Commands:")
    print(f"    {report_cmd} --days 7        # Usage report")
    print(f"    {history_cmd} --stats        # Session history")
    print(f"    {suggest_cmd}                # CLAUDE.md suggestions")
    print(f"    {oracle_cmd}                 # Cost predictions")
    print(f"    {index_cmd} status           # Search index status")
    if not use_entry_points:
        scripts_dir = Path(sys.executable).parent / "Scripts"
        print(f"\n  Tip: add {scripts_dir} to PATH for shorter commands")
    print()


if __name__ == "__main__":
    main()
