"""Fixtures shared across apollo/resolution/tests.

Provides ``make_def_node`` — a factory that builds a DefinitionNode whose
``student_surface_text`` equals the given input text.  Splits the text on the
first space so the Pydantic model gets a non-empty ``concept`` and a non-empty
``meaning``; for single-token texts both fields receive the token.
"""

from __future__ import annotations

import pytest

from apollo.ontology.nodes import Node, build_node


@pytest.fixture()
def make_def_node():
    """Return a callable that builds a ``definition`` Node from a text string.

    ``student_surface_text(node)`` will equal the input text (for multi-word
    inputs the text is reproduced exactly; for single-word inputs the surface
    becomes ``"{word} {word}"`` — still unambiguous for matching purposes).
    """

    def _factory(text: str) -> Node:
        parts = text.split(" ", 1)
        concept = parts[0]
        meaning = parts[1] if len(parts) > 1 else concept
        return build_node(
            node_type="definition",
            node_id="test-def-1",
            attempt_id=1,
            source="parser",
            content={"concept": concept, "meaning": meaning},
        )

    return _factory
