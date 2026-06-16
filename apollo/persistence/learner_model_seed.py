"""Pure conversion core for the WU-3B bernoulli Layer-1 seed.

This module is the deterministic, DB-free, LLM-free heart of the Apollo learner
model seed (spec §8 v1 seed, §5, §6.1). It turns the hand-authored bernoulli
source files into migration-026 Layer-1 row specs:

  * ``concept_dag.json``        -> concept entities + prereq edges
  * ``canonical_symbols.json``  -> variable entities (+ ``var.q`` for the
                                   dynamic-pressure normalization target)
  * ``normalization_map.json``  -> aliases attached to their variable entity
  * each ``problem_*.json``     -> reference-derived entities (equations,
                                   conditions, simplifications, procedure steps)
                                   + an entity-link + declared-path annotation
  * ``misconceptions.json``     -> ``misc.*`` entities carrying an opposes-link
  * ``_AUTHORED_DEFINITIONS``   -> the single ``def.pressure_velocity_tradeoff``
                                   a misconception opposes (§6.9)

It also exposes ``validate_reference_graph`` — the executable §6.1 contract that
WU-4A's grading core consumes (every reference node must carry an entity link
AND a non-empty declared path that covers every node).

NO SQLAlchemy import lives here: the conversion functions take plain dicts (the
parsed JSON) and return frozen dataclasses / new dicts, so the fast unit suite
needs no DB. The DB write layer lives in ``scripts/seed_apollo_learner_model.py``.

Immutability (coding-style): ``EntitySpec`` and ``ReferenceGraphValidation`` are
frozen dataclasses; ``annotate_reference_solution`` returns a NEW dict and never
mutates its input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntitySpec:
    """A single migration-026 ``apollo_kg_entities`` row, pre-DB.

    ``canonical_key`` is unique PER CONCEPT (not global). ``kind`` is one of
    ``apollo.persistence.models.ENTITY_KINDS`` (the SQL CHECK set). ``payload``
    and ``aliases`` map to the JSONB columns of the same name.
    """

    canonical_key: str
    kind: str
    display_name: str
    payload: Mapping[str, object] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceGraphValidation:
    """Result of the §6.1 reference-graph validation contract."""

    ok: bool
    missing_entity_links: tuple[str, ...]
    undeclared_paths: bool
    errors: tuple[str, ...]


class SeedError(RuntimeError):
    """Raised by the seeder when a precondition is violated (e.g. the bernoulli
    concept row is missing — the registry seeder must have run first). Named
    error per the spec NO-FALLBACK convention."""


# ---------------------------------------------------------------------------
# Key / kind mapping (D5)
# ---------------------------------------------------------------------------

# reference-solution entry_type -> (entity kind, canonical_key prefix).
# 'simplification' has no kind in ENTITY_KINDS: simplifications are scoping
# conditions, so they map to kind 'condition' with a distinct 'simp.' prefix.
_ENTRY_TYPE_TO_KIND_PREFIX: dict[str, tuple[str, str]] = {
    "equation": ("equation", "eq"),
    "condition": ("condition", "cond"),
    "simplification": ("condition", "simp"),
    "procedure_step": ("procedure", "proc"),
    "definition": ("definition", "def"),
}

# Authored definition (D5): the pressure-velocity tradeoff a misconception
# opposes (§6.9). Not a reference-node id in any problem, so it is minted from
# this constant rather than from a problem.
_AUTHORED_DEFINITIONS: tuple[EntitySpec, ...] = (
    EntitySpec(
        canonical_key="def.pressure_velocity_tradeoff",
        kind="definition",
        display_name="Pressure-velocity tradeoff",
        payload={
            "statement": (
                "In steady incompressible flow, where the fluid speeds up the "
                "static pressure drops, and vice versa (the Bernoulli tradeoff)."
            ),
        },
        aliases=("pressure drops when speed rises", "inverse pressure-velocity relation"),
    ),
)


def _humanize(node_id: str) -> str:
    return node_id.replace("_", " ").title()


# ---------------------------------------------------------------------------
# concept_dag.json
# ---------------------------------------------------------------------------


def concept_dag_to_entities(dag: dict) -> list[EntitySpec]:
    """One ``concept.<id>`` EntitySpec per concept-dag node (14)."""
    specs: list[EntitySpec] = []
    for node in dag.get("nodes", []):
        node_id = node["id"]
        specs.append(
            EntitySpec(
                canonical_key=f"concept.{node_id}",
                kind="concept",
                display_name=node.get("label", _humanize(node_id)),
                payload={"scope_boundary": list(node.get("scope_boundary", []))},
                aliases=(),
            )
        )
    return specs


def concept_dag_to_prereqs(dag: dict) -> list[tuple[str, str]]:
    """One ``(concept.<from>, concept.<to>)`` prereq pair per edge (16),
    independent of edge ``type`` (requires/extends both encode a dependency
    direction in Layer 1 — R6)."""
    return [
        (f"concept.{edge['from']}", f"concept.{edge['to']}")
        for edge in dag.get("edges", [])
    ]


# ---------------------------------------------------------------------------
# canonical_symbols.json + normalization_map.json
# ---------------------------------------------------------------------------


def symbols_to_entities(symbols: dict, normalization: dict) -> list[EntitySpec]:
    """One ``var.<sym>`` EntitySpec per canonical symbol (7) PLUS one per extra
    normalization target that is not a canonical symbol (``var.q`` for
    "dynamic pressure" — R5/D5: mint it so no alias is silently lost).

    Aliases are the normalization_map keys whose value == that symbol. All 23
    mappings are placed; no alias is dropped.
    """
    descriptions: dict[str, str] = dict(symbols.get("description", {}))
    canonical: list[str] = list(symbols.get("symbols", []))

    # Aliases per target symbol, in normalization-map iteration order.
    aliases_by_target: dict[str, list[str]] = {}
    for phrase, target in normalization.items():
        aliases_by_target.setdefault(target, []).append(phrase)

    # Every target that appears in the normalization map but is not a canonical
    # symbol still gets an entity (so its aliases are not lost). Preserve a
    # stable order: canonical symbols first, then extra targets in first-seen
    # order from the normalization map.
    extra_targets = [
        t for t in aliases_by_target if t not in canonical
    ]
    ordered_targets = canonical + extra_targets

    # Minimal human-readable fallback names for non-canonical targets.
    _EXTRA_DISPLAY = {"q": "dynamic pressure"}

    specs: list[EntitySpec] = []
    for sym in ordered_targets:
        display = descriptions.get(sym) or _EXTRA_DISPLAY.get(sym) or _humanize(sym)
        specs.append(
            EntitySpec(
                canonical_key=f"var.{sym}",
                kind="variable",
                display_name=display,
                payload={"symbol": sym},
                aliases=tuple(aliases_by_target.get(sym, ())),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# problem_*.json reference solutions
# ---------------------------------------------------------------------------


def _entity_key_for_step(step: dict) -> str:
    entry_type = step["entry_type"]
    _kind, prefix = _ENTRY_TYPE_TO_KIND_PREFIX[entry_type]
    return f"{prefix}.{step['id']}"


def reference_solution_to_entities(problem: dict) -> list[EntitySpec]:
    """One EntitySpec per reference-solution step (kind+key from the D5 mapping).

    ``display_name`` comes from ``content.label`` when present, else a humanized
    node id. ``payload`` carries the step's ``symbolic`` / ``applies_when`` /
    ``transformation`` / ``order`` fields when present. NO dedup here — the
    seed-flow layer dedups by ``canonical_key`` across problems.
    """
    specs: list[EntitySpec] = []
    for step in problem.get("reference_solution", []):
        entry_type = step["entry_type"]
        kind, prefix = _ENTRY_TYPE_TO_KIND_PREFIX[entry_type]
        node_id = step["id"]
        content = step.get("content", {}) or {}

        display = content.get("label") or _humanize(node_id)
        payload: dict[str, object] = {"entry_type": entry_type}
        for carried in ("symbolic", "applies_when", "transformation", "order", "variables"):
            if carried in content:
                payload[carried] = content[carried]

        specs.append(
            EntitySpec(
                canonical_key=f"{prefix}.{node_id}",
                kind=kind,
                display_name=display,
                payload=payload,
                aliases=(),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# misconceptions.json + authored definitions
# ---------------------------------------------------------------------------


def misconceptions_to_entities(misc: dict) -> list[EntitySpec]:
    """One ``misc.<...>`` EntitySpec per misconceptions.json entry, kind
    'misconception', payload carrying ``opposes_entity_key`` (D3), trigger
    phrases as aliases (§5 — misconceptions compete in every resolution)."""
    specs: list[EntitySpec] = []
    for entry in misc.get("misconceptions", []):
        specs.append(
            EntitySpec(
                canonical_key=entry["key"],
                kind="misconception",
                display_name=entry.get("display_name", _humanize(entry["key"])),
                payload={
                    "description": entry.get("description", ""),
                    "opposes_entity_key": entry["opposes"],
                },
                aliases=tuple(entry.get("trigger_phrases", [])),
            )
        )
    return specs


def authored_definitions() -> list[EntitySpec]:
    """The authored ``def.*`` entities a misconception opposes (D5). Returns a
    fresh list of the immutable ``_AUTHORED_DEFINITIONS`` constant."""
    return list(_AUTHORED_DEFINITIONS)


# ---------------------------------------------------------------------------
# Reference-solution annotation (D2/D6) — immutable
# ---------------------------------------------------------------------------


def annotate_reference_solution(
    problem: dict, key_for_node: Callable[[str], str]
) -> dict:
    """Return a NEW problem dict with each reference-solution step carrying an
    ``entity_key`` and the problem carrying ``declared_paths`` (one complete
    ordered path, D6) + ``layer1_seeded: True``.

    Immutability (coding-style): the input ``problem`` is never mutated — a deep
    copy of every nested step is built. ``key_for_node`` maps a reference-node
    id to its minted canonical_key.
    """
    steps = problem.get("reference_solution", [])
    new_steps: list[dict] = []
    node_order: list[str] = []
    for step in steps:
        node_id = step["id"]
        node_order.append(node_id)
        new_step = dict(step)
        new_step["entity_key"] = key_for_node(node_id)
        new_steps.append(new_step)

    annotated = dict(problem)
    annotated["reference_solution"] = new_steps
    # v1 declares exactly ONE complete path covering every reference node, in
    # step (procedure) order (§6.2 degenerate single-path case).
    annotated["declared_paths"] = [list(node_order)]
    annotated["layer1_seeded"] = True
    return annotated


# ---------------------------------------------------------------------------
# §6.1 reference-graph validation contract (the WU-4A gate)
# ---------------------------------------------------------------------------


def validate_reference_graph(problem: dict) -> ReferenceGraphValidation:
    """Validate an annotated problem's reference graph (spec §6.1).

    ``ok`` is True iff ALL hold:
      (a) every reference-solution step has a non-empty ``entity_key``;
      (b) ``declared_paths`` is present and non-empty;
      (c) every node id in every declared path is a real reference-node id;
      (d) every reference-node id appears on >= 1 declared path.

    Any failure populates the relevant reason field/tuple — this is what would
    "block grading" at WU-4A pipeline step 3.
    """
    steps = problem.get("reference_solution", [])
    node_ids = [step.get("id") for step in steps]
    node_id_set = set(node_ids)

    errors: list[str] = []

    # (a) entity links
    missing_links: list[str] = []
    for step in steps:
        if not step.get("entity_key"):
            missing_links.append(step.get("id", "<unknown>"))
    if missing_links:
        errors.append(
            f"reference nodes missing an entity link: {sorted(missing_links)}"
        )

    # (b) declared paths present + non-empty
    declared_paths = problem.get("declared_paths")
    undeclared = not isinstance(declared_paths, list) or len(declared_paths) == 0
    if undeclared:
        errors.append("declared_paths is empty or absent (blocks grading at step 3)")
        declared_paths = []

    # (c) paths reference only real nodes
    covered: set[str] = set()
    for path in declared_paths:
        for nid in path:
            covered.add(nid)
            if nid not in node_id_set:
                errors.append(f"declared path references unknown node id: {nid!r}")

    # (d) every node covered by >= 1 path (only meaningful when paths exist)
    if not undeclared:
        uncovered = node_id_set - covered
        if uncovered:
            errors.append(
                f"reference nodes absent from every declared path: {sorted(uncovered)}"
            )

    ok = not errors
    return ReferenceGraphValidation(
        ok=ok,
        missing_entity_links=tuple(missing_links),
        undeclared_paths=undeclared,
        errors=tuple(errors),
    )
