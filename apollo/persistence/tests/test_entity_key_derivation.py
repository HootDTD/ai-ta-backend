"""Pure unit tests for ``derive_entity_key`` (T1, entity_key derivation).

``derive_entity_key`` is the never-raises core used by both the pre-existing
``_entity_key_for_step`` (dict-shaped step, known entry types only) and the
new ``Problem.to_kg_graph`` population path (§5.1 of the emergent-map design).

NO DB, NO LLM, NO network.
"""

from __future__ import annotations

import pytest

from apollo.persistence.learner_model_seed import (
    _entity_key_for_step,
    derive_entity_key,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("entry_type", "node_id", "expected"),
    [
        ("definition", "real_basis", "def.real_basis"),
        ("equation", "gdp_deflator", "eq.gdp_deflator"),
        ("simplification", "steady_flow", "simp.steady_flow"),
        ("procedure_step", "do_it", "proc.do_it"),
        ("condition", "incompressible", "cond.incompressible"),
        ("variable_mapping", "p_maps", "varmap.p_maps"),
    ],
)
def test_derive_entity_key_known_types(entry_type: str, node_id: str, expected: str) -> None:
    assert derive_entity_key(entry_type, node_id) == expected


def test_derive_entity_key_unknown_type_returns_none() -> None:
    assert derive_entity_key("gibberish", "whatever") is None


def test_derive_entity_key_unknown_type_never_raises() -> None:
    # Guard: no exception for any string outside the frozen mint map.
    try:
        result = derive_entity_key("not_a_real_entry_type", "x")
    except Exception as exc:  # pragma: no cover - guard must never trip
        pytest.fail(f"derive_entity_key raised {exc!r} instead of returning None")
    assert result is None


def test_entity_key_for_step_delegates_and_is_unchanged_for_known_types() -> None:
    step = {"entry_type": "definition", "id": "real_basis"}
    assert _entity_key_for_step(step) == derive_entity_key("definition", "real_basis")
    assert _entity_key_for_step(step) == "def.real_basis"
