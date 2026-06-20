"""WU-2B nit-2: `from typing import Any` is unused in parser_llm — assert removed.

Pure introspection — no LLM, no Neo4j. Guards against the unused import
creeping back (and documents the nit if `Any` ever becomes needed again).
"""

from __future__ import annotations

import ast
import inspect

import apollo.parser.parser_llm as m


def test_typing_any_import_removed():
    """`Any` must not be imported into the parser_llm namespace."""
    assert "Any" not in m.__dict__


def test_no_from_typing_import_any_statement():
    """Source-level guard: no `from typing import ... Any ...` line remains."""
    src = inspect.getsource(m)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            names = {alias.name for alias in node.names}
            assert "Any" not in names, "parser_llm still imports typing.Any"
