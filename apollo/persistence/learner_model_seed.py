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
                                   a misconception opposes (§6.9). This is the
                                   bernoulli-specific case; generalized concepts
                                   either oppose a REAL reference key or supply
                                   their own definitions from an optional
                                   ``authored_definitions.json`` via
                                   ``authored_definitions_from_spec``.

It also exposes ``validate_reference_graph`` — the executable §6.1 contract that
WU-4A's grading core consumes (every reference node must carry an entity link
AND a non-empty declared path that covers every node). Object-shaped strategy
paths additionally require a non-empty milestone set containing a mechanically
derived DEPENDS_ON sink (a final-result step); legacy list paths are unchanged.

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


@dataclass(frozen=True)
class NormalizedPath:
    """One legacy or object-shaped declared path in a common immutable form."""

    strategy_id: str
    node_ids: tuple[str, ...]
    milestone_ids: tuple[str, ...]
    is_object: bool = False


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
# 'variable_mapping' (WU-3B2d additive): a symbol-binding step (e.g. "P maps to
# pressure"); maps to kind 'variable' with a 'varmap.' prefix so a §8B auto-
# provisioned problem carrying one mints (no KeyError) and 3B2b's gate-1 mint-map
# membership sub-check ACCEPTS it. Additive only — no seeded problem uses
# variable_mapping, so the WU-6A2 reference_entity_keys golden vectors are
# byte-identical (the test_personalization_select anchor proves it).
_ENTRY_TYPE_TO_KIND_PREFIX: dict[str, tuple[str, str]] = {
    "equation": ("equation", "eq"),
    "condition": ("condition", "cond"),
    "simplification": ("condition", "simp"),
    "procedure_step": ("procedure", "proc"),
    "definition": ("definition", "def"),
    "variable_mapping": ("variable", "varmap"),
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
    return [(f"concept.{edge['from']}", f"concept.{edge['to']}") for edge in dag.get("edges", [])]


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
    extra_targets = [t for t in aliases_by_target if t not in canonical]
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


def derive_entity_key(entry_type: str, node_id: str) -> str | None:
    """Pure, never-raising entity_key derivation (emergent-map design §5.1).

    Returns ``f"{prefix}.{node_id}"`` for an ``entry_type`` present in the
    frozen ``_ENTRY_TYPE_TO_KIND_PREFIX`` mint map, else ``None`` for an
    unknown/unrecognized entry_type (guard — never raises).
    """
    entry = _ENTRY_TYPE_TO_KIND_PREFIX.get(entry_type)
    if entry is None:
        return None
    _kind, prefix = entry
    return f"{prefix}.{node_id}"


def _entity_key_for_step(step: dict) -> str:
    """Delegates to ``derive_entity_key`` for known entry types.

    Behavior unchanged for every existing caller (all pass a known
    ``entry_type``): raises ``KeyError`` via the same mint-map lookup for an
    unrecognized ``entry_type`` rather than silently returning ``None`` —
    those callers rely on a definite ``str`` result.
    """
    entry_type = step["entry_type"]
    if entry_type not in _ENTRY_TYPE_TO_KIND_PREFIX:
        raise KeyError(entry_type)
    return derive_entity_key(entry_type, step["id"])  # type: ignore[return-value]


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
    fresh list of the immutable ``_AUTHORED_DEFINITIONS`` constant.

    Backward-compatible bernoulli entry point: bernoulli's
    ``def.pressure_velocity_tradeoff`` is not a reference-solution node, so it
    must be minted from this constant for the existing seed to link. Generalized
    concepts whose misconceptions oppose REAL reference keys (the macro content
    guarantee) instead supply their authored definitions from disk via
    :func:`authored_definitions_from_spec` and pass an empty list when none.
    """
    return list(_AUTHORED_DEFINITIONS)


def authored_definitions_from_spec(entries: list[dict]) -> list[EntitySpec]:
    """Convert a list of authored-definition dicts (parsed from a concept dir's
    optional ``authored_definitions.json``) into ``definition`` EntitySpecs.

    A generic concept rarely needs this: if a misconception opposes a real
    reference-solution node (``cond.*`` / ``def.*`` / ``eq.*`` minted from a
    problem) the opposes-link resolves without any standalone definition. The
    file exists only for the bernoulli-shaped case where the opposed concept is
    NOT a reference node. Each entry shape mirrors ``misconceptions.json`` style::

        {"key": "def.<slug>", "display_name": "...", "statement": "...",
         "aliases": ["...", ...]}

    ``key`` and ``statement`` are required; ``display_name``/``aliases`` default.
    Returns a fresh list; never mutates ``entries``.
    """
    specs: list[EntitySpec] = []
    for entry in entries:
        key = entry["key"]
        specs.append(
            EntitySpec(
                canonical_key=key,
                kind="definition",
                display_name=entry.get("display_name", _humanize(key.split(".", 1)[-1])),
                payload={"statement": entry["statement"]},
                aliases=tuple(entry.get("aliases", [])),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Reference-solution annotation (D2/D6) — immutable
# ---------------------------------------------------------------------------


def annotate_reference_solution(
    problem: dict,
    key_for_node: Callable[[str], str],
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


def normalize_declared_paths(raw: object) -> tuple[NormalizedPath, ...]:
    """Normalize legacy lists and strategy objects without problem-specific checks.

    Shape errors raise ``ValueError`` with a path-indexed diagnostic. Reference
    node membership and cross-path distinctness belong to
    :func:`validate_reference_graph`, which has the problem's node set.
    """
    if not isinstance(raw, list):
        raise ValueError("declared_paths must be a list")

    normalized: list[NormalizedPath] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, list):
            if not all(isinstance(node_id, str) for node_id in entry):
                raise ValueError(f"declared_paths[{index}] legacy nodes must all be strings")
            normalized.append(
                NormalizedPath(
                    strategy_id=f"path_{index}",
                    node_ids=tuple(entry),
                    milestone_ids=(),
                )
            )
            continue

        if not isinstance(entry, dict):
            raise ValueError(f"declared_paths[{index}] must be a legacy node list or a path object")
        unexpected = set(entry) - {"strategy_id", "nodes", "milestones"}
        if unexpected:
            raise ValueError(f"declared_paths[{index}] has unexpected fields: {sorted(unexpected)}")
        strategy_id = entry.get("strategy_id")
        nodes = entry.get("nodes")
        milestones = entry.get("milestones")
        if not isinstance(strategy_id, str):
            raise ValueError(f"declared_paths[{index}].strategy_id must be a string")
        if not isinstance(nodes, list) or not all(isinstance(node_id, str) for node_id in nodes):
            raise ValueError(f"declared_paths[{index}].nodes must be a list of strings")
        if not isinstance(milestones, list) or not all(
            isinstance(node_id, str) for node_id in milestones
        ):
            raise ValueError(f"declared_paths[{index}].milestones must be a list of strings")
        normalized.append(
            NormalizedPath(
                strategy_id=strategy_id,
                node_ids=tuple(nodes),
                milestone_ids=tuple(milestones),
                is_object=True,
            )
        )
    return tuple(normalized)


def validate_reference_graph(problem: dict) -> ReferenceGraphValidation:
    """Validate an annotated problem's reference graph (spec §6.1).

    ``ok`` is True iff ALL hold:
      (a) every reference-solution step has a non-empty ``entity_key``;
      (b) ``declared_paths`` is present and non-empty;
      (c) every node id in every declared path is a real reference-node id;
      (d) every reference-node id appears on >= 1 declared path.
      (e) every object path has at least one milestone, and at least one of its
          milestones is a DEPENDS_ON sink (a step no other step depends on).

    Rule (e) is object-only. Legacy node-id list paths retain their historical
    validation semantics.

    Any failure populates the relevant reason field/tuple — this is what would
    "block grading" at WU-4A pipeline step 3.
    """
    steps = problem.get("reference_solution", [])
    node_ids = [step.get("id") for step in steps]
    node_id_set = set(node_ids)
    depended_on_ids = {
        dependency
        for step in steps
        for dependency in step.get("depends_on", [])
        if isinstance(dependency, str)
    }
    sink_ids = node_id_set - depended_on_ids

    errors: list[str] = []

    # (a) entity links
    missing_links: list[str] = []
    for step in steps:
        if not step.get("entity_key"):
            missing_links.append(step.get("id", "<unknown>"))
    if missing_links:
        errors.append(f"reference nodes missing an entity link: {sorted(missing_links)}")

    # (b) declared paths present + non-empty
    raw_declared = problem.get("declared_paths")
    try:
        declared_paths = normalize_declared_paths(raw_declared)
    except ValueError as exc:
        declared_paths = ()
        errors.append(str(exc))
    undeclared = len(declared_paths) == 0
    if undeclared:
        errors.append("declared_paths is empty or absent (blocks grading at step 3)")

    # (c) paths are well-formed and reference only real nodes
    covered: set[str] = set()
    strategy_ids: set[str] = set()
    for path_index, path in enumerate(declared_paths):
        if not path.strategy_id:
            errors.append(f"declared_paths[{path_index}].strategy_id must be non-empty")
        if path.strategy_id in strategy_ids:
            errors.append(f"duplicate declared path strategy_id: {path.strategy_id!r}")
        strategy_ids.add(path.strategy_id)
        if not path.node_ids:
            errors.append(f"declared_paths[{path_index}].nodes must be non-empty")
        for nid in path.node_ids:
            covered.add(nid)
            if nid not in node_id_set:
                errors.append(f"declared path references unknown node id: {nid!r}")
        milestone_set = set(path.milestone_ids)
        node_set = set(path.node_ids)
        if path.is_object and not path.milestone_ids:
            errors.append(f"declared_paths[{path_index}].milestones must be non-empty")
        if not milestone_set <= node_set:
            errors.append(
                f"declared_paths[{path_index}] milestones are not on the path: "
                f"{sorted(milestone_set - node_set)}"
            )
        if path.is_object and not milestone_set & sink_ids:
            errors.append(
                f"declared_paths[{path_index}].milestones must include a reference graph sink: "
                f"{sorted(sink_ids)}"
            )

    # Multi-path MAX is safe only when paths are genuinely distinct and none is
    # an easier strict subset of another.
    if len(declared_paths) >= 2:
        node_sets = [set(path.node_ids) for path in declared_paths]
        for left in range(len(node_sets)):
            for right in range(left + 1, len(node_sets)):
                if node_sets[left] == node_sets[right]:
                    errors.append(
                        f"declared paths have identical node sets: indexes {left} and {right}"
                    )
                elif node_sets[left] < node_sets[right]:
                    errors.append(
                        f"declared path index {left} is a strict subset of path index {right}"
                    )
                elif node_sets[right] < node_sets[left]:
                    errors.append(
                        f"declared path index {right} is a strict subset of path index {left}"
                    )

    # (d) every node covered by >= 1 path (only meaningful when paths exist)
    if not undeclared:
        uncovered = node_id_set - covered
        if uncovered:
            errors.append(f"reference nodes absent from every declared path: {sorted(uncovered)}")

    ok = not errors
    return ReferenceGraphValidation(
        ok=ok,
        missing_entity_links=tuple(missing_links),
        undeclared_paths=undeclared,
        errors=tuple(errors),
    )
