"""Fast pure-unit tests for the WU-3B conversion core
(``apollo.persistence.learner_model_seed``).

No DB, no LLM, no network — every function takes the parsed bernoulli source
JSON (read from the REAL on-disk files) and returns plain frozen dataclasses /
dicts. These tests pin the exact entity/prereq/alias/misconception/annotation
output the seeder (``scripts.seed_apollo_learner_model``) writes to Postgres, so
the seed flow can be reasoned about off-DB.

Counts are the ACTUAL file counts (binding constraint, plan "Verified source
facts"): 14 concept nodes, 16 prereq edges, 7 canonical symbols + ``var.q``
(8 variable entities), 23 normalization mappings, 5 problems.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from apollo.persistence.learner_model_seed import (
    EntitySpec,
    annotate_reference_solution,
    authored_definitions,
    concept_dag_to_entities,
    concept_dag_to_prereqs,
    misconceptions_to_entities,
    reference_solution_to_entities,
    symbols_to_entities,
)
from apollo.persistence.models import ENTITY_KINDS

_BERNOULLI = (
    Path(__file__).resolve().parents[3]
    / "apollo"
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
)


def _load(name: str) -> dict:
    return json.loads((_BERNOULLI / name).read_text(encoding="utf-8"))


def _load_problem(n: int) -> dict:
    return json.loads(
        (_BERNOULLI / "problems" / f"problem_{n:02d}.json").read_text(encoding="utf-8")
    )


def _strip_annotation(problem: dict) -> dict:
    """Return a deep copy of a problem with the seed annotation removed.

    The seeder writes the entity-link + declared-path annotation back to the
    on-disk problem_*.json by design (D2/R1), so the source files may already
    carry ``entity_key`` / ``declared_paths`` / ``layer1_seeded``. Tests that
    exercise the BEFORE-annotation shape strip these so they are independent of
    whether the seed has run on disk."""
    clean = copy.deepcopy(problem)
    clean.pop("declared_paths", None)
    clean.pop("layer1_seeded", None)
    for step in clean.get("reference_solution", []):
        step.pop("entity_key", None)
    return clean


def _by_key(specs: list[EntitySpec]) -> dict[str, EntitySpec]:
    return {s.canonical_key: s for s in specs}


# ---------------------------------------------------------------------------
# concept_dag_to_entities / concept_dag_to_prereqs
# ---------------------------------------------------------------------------


def test_concept_dag_to_entities_one_per_node():
    dag = _load("concept_dag.json")
    specs = concept_dag_to_entities(dag)

    assert len(specs) == 14
    assert all(s.kind == "concept" for s in specs)
    assert all(s.kind in ENTITY_KINDS for s in specs)
    keys = {s.canonical_key for s in specs}
    assert keys == {f"concept.{node['id']}" for node in dag["nodes"]}

    by_key = _by_key(specs)
    bern = by_key["concept.bernoulli_principle"]
    assert bern.display_name == "Bernoulli's Principle"
    # scope_boundary carried in payload for bernoulli_principle.
    assert bern.payload["scope_boundary"] == ["viscosity", "compressible_flow", "turbulence"]


def test_concept_dag_to_prereqs_one_per_edge():
    dag = _load("concept_dag.json")
    pairs = concept_dag_to_prereqs(dag)

    assert len(pairs) == 16
    assert all(len(p) == 2 for p in pairs)
    # 'requires' edge becomes a prereq pair (from depends on to).
    assert ("concept.bernoulli_principle", "concept.energy_conservation_fluid") in pairs
    # The lone 'extends' edge is ALSO a prereq pair, independent of edge type.
    assert (
        "concept.horizontal_flow_simplification",
        "concept.bernoulli_principle",
    ) in pairs


def test_concept_dag_to_prereqs_independent_of_edge_type():
    """Mutating an edge 'type' from requires->extends does not change the output:
    Layer 1 drops the requires/extends distinction (R6)."""
    dag = _load("concept_dag.json")
    baseline = concept_dag_to_prereqs(dag)
    mutated = copy.deepcopy(dag)
    for edge in mutated["edges"]:
        edge["type"] = "extends"
    assert concept_dag_to_prereqs(mutated) == baseline


# ---------------------------------------------------------------------------
# symbols_to_entities
# ---------------------------------------------------------------------------


def test_symbols_to_entities_seven_canonical_plus_q():
    symbols = _load("canonical_symbols.json")
    normalization = _load("normalization_map.json")
    specs = symbols_to_entities(symbols, normalization)

    assert len(specs) == 8  # 7 canonical + var.q (R5)
    assert all(s.kind == "variable" for s in specs)
    by_key = _by_key(specs)
    for sym in ["P", "rho", "v", "A", "h", "g", "Q", "q"]:
        assert f"var.{sym}" in by_key
    # Display names come from the canonical descriptions; q is the dynamic-pressure target.
    assert by_key["var.P"].display_name == "pressure"
    assert by_key["var.q"].display_name == "dynamic pressure"


def test_symbols_aliases_from_normalization_map():
    symbols = _load("canonical_symbols.json")
    normalization = _load("normalization_map.json")
    by_key = _by_key(symbols_to_entities(symbols, normalization))

    assert "static pressure" in by_key["var.P"].aliases
    assert "flow speed" in by_key["var.v"].aliases
    assert "dynamic pressure" in by_key["var.q"].aliases
    # Every one of the 23 normalization keys lands as exactly one alias somewhere.
    total = sum(len(s.aliases) for s in by_key.values())
    assert total == len(normalization) == 23


# ---------------------------------------------------------------------------
# reference_solution_to_entities
# ---------------------------------------------------------------------------


def test_reference_solution_to_entities_kinds_and_keys():
    problem = _load_problem(1)
    specs = reference_solution_to_entities(problem)

    assert len(specs) == 7
    by_key = _by_key(specs)

    cont = by_key["eq.continuity"]
    assert cont.kind == "equation"
    assert cont.payload["symbolic"] == "rho*A1*v1 - rho*A2*v2"

    incomp = by_key["cond.incompressibility"]
    assert incomp.kind == "condition"
    assert incomp.payload["applies_when"] == "density is constant"

    # simplification -> kind condition, key prefix simp.
    simp = by_key["simp.horizontal_simplification"]
    assert simp.kind == "condition"
    assert simp.payload["applies_when"] == "h1 == h2"

    # procedure_step -> kind procedure, key prefix proc.
    proc = by_key["proc.plan_apply_continuity"]
    assert proc.kind == "procedure"
    assert proc.payload["order"] == 1


def test_reference_solution_display_name_from_label():
    problem = _load_problem(1)
    by_key = _by_key(reference_solution_to_entities(problem))
    # display_name comes from content.label for every labeled reference step —
    # equations, conditions, AND the conceptual procedure/simplification steps
    # (the legacy label backfill authored a content.label on all of them).
    assert by_key["eq.continuity"].display_name == "Continuity (mass conservation)"
    assert (
        by_key["proc.plan_apply_continuity"].display_name
        == "Apply the continuity equation to solve for the outlet velocity v2"
    )


def test_reference_solution_display_name_humanize_fallback():
    # A reference step with NO content.label still falls back to a humanized
    # node id — the label path is preferred, humanize is the safety net.
    problem = {
        "reference_solution": [
            {
                "id": "plan_do_the_thing",
                "step": 1,
                "content": {"order": 1, "action": "do the thing"},
                "entity_key": "proc.plan_do_the_thing",
                "entry_type": "procedure_step",
            }
        ]
    }
    by_key = _by_key(reference_solution_to_entities(problem))
    assert by_key["proc.plan_do_the_thing"].display_name == "Plan Do The Thing"


def test_reference_entities_dedup_shared_ids_across_problems():
    specs: list[EntitySpec] = []
    for n in (1, 3, 5):
        specs.extend(reference_solution_to_entities(_load_problem(n)))
    # Raw conversion does NOT dedup (the seed-flow layer does); the same key
    # appears multiple times across problems.
    keys = [s.canonical_key for s in specs]
    assert keys.count("eq.continuity") >= 2
    # Deduping by canonical_key yields exactly one of each shared entity.
    deduped = {s.canonical_key: s for s in specs}
    assert "eq.continuity" in deduped
    assert "eq.bernoulli" in deduped
    assert "cond.incompressibility" in deduped
    assert keys.count("eq.bernoulli") >= 2


# ---------------------------------------------------------------------------
# misconceptions_to_entities / authored_definitions
# ---------------------------------------------------------------------------


def test_misconceptions_to_entities():
    misc = _load("misconceptions.json")
    specs = misconceptions_to_entities(misc)

    assert len(specs) == len(misc["misconceptions"])
    assert all(s.kind == "misconception" for s in specs)
    by_key = _by_key(specs)
    dens = by_key["misc.density_ignored"]
    assert dens.payload["opposes_entity_key"] == "cond.incompressibility"
    assert "ignore density" in dens.aliases


def test_authored_definitions_includes_pressure_velocity_tradeoff():
    specs = authored_definitions()
    by_key = _by_key(specs)
    assert "def.pressure_velocity_tradeoff" in by_key
    assert by_key["def.pressure_velocity_tradeoff"].kind == "definition"


def test_every_misconception_opposes_target_is_minted():
    """Guards a dangling opposes-link before it reaches the DB: every
    opposes_entity_key referenced by misconceptions.json must be a key the seed
    mints (concept + var + reference-derived + authored definition)."""
    minted: set[str] = set()
    minted.update(s.canonical_key for s in concept_dag_to_entities(_load("concept_dag.json")))
    minted.update(
        s.canonical_key
        for s in symbols_to_entities(
            _load("canonical_symbols.json"), _load("normalization_map.json")
        )
    )
    for n in range(1, 6):
        minted.update(s.canonical_key for s in reference_solution_to_entities(_load_problem(n)))
    minted.update(s.canonical_key for s in authored_definitions())

    misc_specs = misconceptions_to_entities(_load("misconceptions.json"))
    for spec in misc_specs:
        target = spec.payload["opposes_entity_key"]
        assert target in minted, f"dangling opposes target: {target}"


# ---------------------------------------------------------------------------
# annotate_reference_solution (immutability + declared path)
# ---------------------------------------------------------------------------


def _key_for_node(problem: dict):
    """Map each reference-node id to its minted canonical_key (mirrors the
    seed-flow node->key resolution)."""
    by_id = {node_id: spec_key for node_id, spec_key in _node_key_pairs(problem)}
    return lambda node_id: by_id[node_id]


def _node_key_pairs(problem: dict):
    specs = reference_solution_to_entities(problem)
    # reference_solution_to_entities preserves step order; zip with node ids.
    for step, spec in zip(problem["reference_solution"], specs, strict=True):
        yield step["id"], spec.canonical_key


def test_annotate_reference_solution_adds_entity_keys_and_path():
    problem = _load_problem(1)
    annotated = annotate_reference_solution(problem, _key_for_node(problem))

    # Every step gains a non-empty entity_key.
    for step in annotated["reference_solution"]:
        assert step["entity_key"]

    # declared_paths: exactly one complete ordered path covering all 7 node ids.
    assert len(annotated["declared_paths"]) == 1
    path = annotated["declared_paths"][0]
    node_ids = [s["id"] for s in problem["reference_solution"]]
    assert path == node_ids
    assert annotated["layer1_seeded"] is True


def test_annotate_is_immutable():
    # Start from the un-annotated shape so the BEFORE assertions hold regardless
    # of whether the seeder has written the annotation to the on-disk source.
    problem = _strip_annotation(_load_problem(2))
    before = copy.deepcopy(problem)
    annotated = annotate_reference_solution(problem, _key_for_node(problem))

    # The input dict is byte-identical after the call (no in-place mutation).
    assert problem == before
    # The result is a different object with the new keys.
    assert annotated is not problem
    assert "declared_paths" not in problem
    assert "declared_paths" in annotated
    for step in problem["reference_solution"]:
        assert "entity_key" not in step


def test_annotate_all_five_problems_cover_every_node():
    for n in range(1, 6):
        problem = _load_problem(n)
        annotated = annotate_reference_solution(problem, _key_for_node(problem))
        node_ids = {s["id"] for s in problem["reference_solution"]}
        covered = set(annotated["declared_paths"][0])
        assert covered == node_ids


# ---------------------------------------------------------------------------
# EntitySpec immutability
# ---------------------------------------------------------------------------


def test_entityspec_is_frozen():
    spec = concept_dag_to_entities(_load("concept_dag.json"))[0]
    with pytest.raises(Exception):  # noqa: B017 - any error proves the frozen spec rejects mutation
        spec.kind = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CLI arg-parsing (fast, no DB) — the missing-URL branch
# ---------------------------------------------------------------------------


def test_main_requires_database_url(monkeypatch, capsys):
    """main() with no --database-url and no DATABASE_URL env returns code 2."""
    from scripts.seed_apollo_learner_model import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "database-url" in err.lower() or "database_url" in err.lower()
