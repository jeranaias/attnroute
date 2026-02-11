#!/usr/bin/env python3
"""
attnroute.outliner — Tree-sitter Code Outline Extraction

Extracts structured outlines from source code files using tree-sitter.
Produces compact summaries with imports + class/function signatures (no bodies).

Features:
  - ~90% token reduction compared to full file content
  - Language support: Python, JS/TS, Go, Rust, Java, C/C++
  - Graceful degradation when tree-sitter unavailable

Output format:
  # src/pipeline.py
  import torch
  class Pipeline(BaseModel):
      def __init__(self, config: dict) -> None: ...
      def process(self, input: Tensor) -> Tensor: ...
  def create_pipeline(config_path: str) -> Pipeline: ...
"""

import sys
from pathlib import Path

# ============================================================================
# DEPENDENCY DETECTION
# ============================================================================

try:
    import tree_sitter
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

try:
    import tree_sitter_language_pack as tslp
    LANGUAGE_PACK_AVAILABLE = True
except ImportError:
    LANGUAGE_PACK_AVAILABLE = False

# Language file extension mapping
LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "c",
    ".hpp": "cpp",
}


# ============================================================================
# OUTLINE EXTRACTION
# ============================================================================

def extract_outline(file_path: Path) -> str | None:
    """
    Extract code outline from a source file.

    Returns:
        Outline string with imports + class/function signatures, or None if unavailable.
    """
    if not TREE_SITTER_AVAILABLE or not LANGUAGE_PACK_AVAILABLE:
        return _fallback_outline(file_path)

    suffix = file_path.suffix.lower()
    lang_name = LANGUAGE_MAP.get(suffix)
    if not lang_name:
        return None

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    try:
        # Get language from pack
        language = tslp.get_language(lang_name)
        parser = tree_sitter.Parser(language)
        tree = parser.parse(bytes(content, "utf-8"))

        lines = []
        lines.append(f"# {file_path.name}")

        # Extract based on language
        if lang_name == "python":
            lines.extend(_extract_python(tree.root_node, content))
        elif lang_name in ("javascript", "typescript", "tsx"):
            lines.extend(_extract_js_ts(tree.root_node, content))
        elif lang_name == "go":
            lines.extend(_extract_go(tree.root_node, content))
        elif lang_name == "rust":
            lines.extend(_extract_rust(tree.root_node, content))
        elif lang_name == "java":
            lines.extend(_extract_java(tree.root_node, content))
        elif lang_name in ("c", "cpp"):
            lines.extend(_extract_c_cpp(tree.root_node, content))
        else:
            return None

        if len(lines) <= 1:  # Only header, no content extracted
            return None

        return "\n".join(lines)

    except Exception as e:
        print(f"[outliner] Error parsing {file_path}: {e}", file=sys.stderr)
        return _fallback_outline(file_path)


def _get_text(node, content: str) -> str:
    """Get text for a node."""
    return content[node.start_byte:node.end_byte]


def _extract_python(root, content: str) -> list:
    """Extract Python imports, classes, and functions."""
    lines = []

    for node in root.children:
        node_type = node.type

        # Imports
        if node_type in ("import_statement", "import_from_statement"):
            lines.append(_get_text(node, content))

        # Class definitions
        elif node_type == "class_definition":
            # Get class signature (name and bases)
            class_text = []
            for child in node.children:
                if child.type == "block":
                    break
                class_text.append(_get_text(child, content))
            class_sig = " ".join(class_text).strip()
            lines.append(class_sig)

            # Extract method signatures from class body
            for child in node.children:
                if child.type == "block":
                    for block_child in child.children:
                        if block_child.type == "function_definition":
                            method_sig = _extract_python_func_sig(block_child, content)
                            if method_sig:
                                lines.append("    " + method_sig)

        # Top-level functions
        elif node_type == "function_definition":
            func_sig = _extract_python_func_sig(node, content)
            if func_sig:
                lines.append(func_sig)

    return lines


def _extract_python_func_sig(node, content: str) -> str | None:
    """Extract Python function signature."""
    parts = []
    for child in node.children:
        if child.type == "block":
            parts.append(": ...")
            break
        parts.append(_get_text(child, content))
    sig = " ".join(parts).replace(" (", "(").replace(" :", ":")
    return sig if sig else None


def _extract_js_ts(root, content: str) -> list:
    """Extract JavaScript/TypeScript imports, classes, and functions."""
    lines = []

    for node in root.children:
        node_type = node.type

        # Imports
        if node_type in ("import_statement", "import_declaration"):
            lines.append(_get_text(node, content))

        # Export statements with declarations
        elif node_type == "export_statement":
            for child in node.children:
                if child.type in ("class_declaration", "function_declaration"):
                    sig = _extract_js_decl_sig(child, content)
                    if sig:
                        lines.append("export " + sig)

        # Class declarations
        elif node_type == "class_declaration":
            sig = _extract_js_decl_sig(node, content)
            if sig:
                lines.append(sig)

        # Function declarations
        elif node_type == "function_declaration":
            sig = _extract_js_decl_sig(node, content)
            if sig:
                lines.append(sig)

        # Arrow function assignments
        elif node_type in ("lexical_declaration", "variable_declaration"):
            text = _get_text(node, content)
            if "=>" in text:
                # Extract just the signature part
                arrow_idx = text.find("=>")
                if arrow_idx > 0:
                    sig = text[:arrow_idx + 2].strip() + " { ... }"
                    lines.append(sig)

    return lines


def _extract_js_decl_sig(node, content: str) -> str | None:
    """Extract JS/TS class or function signature."""
    parts = []
    for child in node.children:
        if child.type in ("statement_block", "class_body"):
            parts.append("{ ... }")
            break
        parts.append(_get_text(child, content))
    sig = " ".join(parts)
    return sig.strip() if sig.strip() else None


def _extract_go(root, content: str) -> list:
    """Extract Go imports, types, and functions."""
    lines = []

    for node in root.children:
        node_type = node.type

        # Package declaration
        if node_type == "package_clause":
            lines.append(_get_text(node, content))

        # Imports
        elif node_type == "import_declaration":
            lines.append(_get_text(node, content))

        # Type declarations (structs, interfaces)
        elif node_type == "type_declaration":
            # Get just the signature, not the body
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + "{ ... }"
                lines.append(sig.strip())
            else:
                lines.append(text.strip())

        # Function declarations
        elif node_type == "function_declaration":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + "{ ... }"
                lines.append(sig.strip())
            else:
                lines.append(text.strip())

        # Method declarations
        elif node_type == "method_declaration":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + "{ ... }"
                lines.append(sig.strip())
            else:
                lines.append(text.strip())

    return lines


def _extract_rust(root, content: str) -> list:
    """Extract Rust imports, structs, traits, and functions."""
    lines = []

    for node in root.children:
        node_type = node.type

        # Use statements
        if node_type == "use_declaration":
            lines.append(_get_text(node, content))

        # Struct definitions
        elif node_type == "struct_item":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + " { ... }"
                lines.append(sig.strip())
            else:
                lines.append(text.strip())

        # Trait definitions
        elif node_type == "trait_item":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + " { ... }"
                lines.append(sig.strip())

        # Impl blocks
        elif node_type == "impl_item":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + " { ... }"
                lines.append(sig.strip())

        # Function definitions
        elif node_type == "function_item":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + " { ... }"
                lines.append(sig.strip())

    return lines


def _extract_java(root, content: str) -> list:
    """Extract Java imports, classes, and methods."""
    lines = []

    for node in root.children:
        node_type = node.type

        # Package declaration
        if node_type == "package_declaration":
            lines.append(_get_text(node, content))

        # Import declarations
        elif node_type == "import_declaration":
            lines.append(_get_text(node, content))

        # Class declarations
        elif node_type == "class_declaration":
            _extract_java_class(node, content, lines, indent=0)

    return lines


def _extract_java_class(node, content: str, lines: list, indent: int):
    """Extract Java class signature and methods."""
    # Get class signature
    class_parts = []
    for child in node.children:
        if child.type == "class_body":
            class_parts.append("{ ... }")
            break
        class_parts.append(_get_text(child, content))

    prefix = "    " * indent
    lines.append(prefix + " ".join(class_parts))

    # Extract method signatures from body
    for child in node.children:
        if child.type == "class_body":
            for body_child in child.children:
                if body_child.type == "method_declaration":
                    method_parts = []
                    for mc in body_child.children:
                        if mc.type == "block":
                            method_parts.append("{ ... }")
                            break
                        method_parts.append(_get_text(mc, content))
                    lines.append(prefix + "    " + " ".join(method_parts))


def _extract_c_cpp(root, content: str) -> list:
    """Extract C/C++ includes, structs, and functions."""
    lines = []

    for node in root.children:
        node_type = node.type

        # Include directives
        if node_type == "preproc_include":
            lines.append(_get_text(node, content))

        # Struct/class definitions
        elif node_type in ("struct_specifier", "class_specifier"):
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + " { ... };"
                lines.append(sig.strip())

        # Function definitions
        elif node_type == "function_definition":
            text = _get_text(node, content)
            if "{" in text:
                sig = text[:text.find("{")] + " { ... }"
                lines.append(sig.strip())

        # Function declarations
        elif node_type == "declaration":
            text = _get_text(node, content)
            if "(" in text and ")":  # Likely a function declaration
                lines.append(text.strip())

    return lines


def _fallback_outline(file_path: Path) -> str | None:
    """Fallback: extract function/class signatures via regex when tree-sitter unavailable."""
    import re

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    lines = [f"# {file_path.name} (regex fallback)"]
    suffix = file_path.suffix.lower()

    if suffix == ".py":
        # Extract import, class, def lines
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                lines.append(stripped)
            elif stripped.startswith("class "):
                lines.append(stripped.split(":")[0] + ": ...")
            elif stripped.startswith("def "):
                indent = len(line) - len(line.lstrip())
                prefix = "    " if indent > 0 else ""
                lines.append(prefix + stripped.split(":")[0] + ": ...")

    elif suffix in (".js", ".ts", ".tsx", ".jsx"):
        # Extract import, function, class lines
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("import "):
                lines.append(stripped)
            elif re.match(r'^(export\s+)?(class|function|const)\s+\w+', stripped):
                lines.append(stripped[:80] + "...")

    else:
        return None

    if len(lines) <= 1:
        return None

    return "\n".join(lines[:100])  # Cap at 100 lines


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for testing outline extraction."""
    import argparse

    parser = argparse.ArgumentParser(description="Extract code outline from a file")
    parser.add_argument("file", type=Path, help="Source file to extract outline from")
    args = parser.parse_args()

    print(f"Tree-sitter available: {TREE_SITTER_AVAILABLE}")
    print(f"Language pack available: {LANGUAGE_PACK_AVAILABLE}")
    print()

    outline = extract_outline(args.file)
    if outline:
        print(outline)
    else:
        print("No outline extracted (unsupported language or parse error)")


if __name__ == "__main__":
    main()
