"""Regression lock for the authored registry seeder's SQL-default trap."""

from __future__ import annotations

import ast
import inspect

from scripts.seed_apollo_concept_registry import _upsert_problem


def test_seed_problem_constructor_passes_tier_two_explicitly():
    tree = ast.parse(inspect.getsource(_upsert_problem))
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_pydantic_payload"
    ]
    assert constructors
    for constructor in constructors:
        tier = next((kw.value for kw in constructor.keywords if kw.arg == "tier"), None)
        assert isinstance(tier, ast.Constant)
        assert tier.value == 2
