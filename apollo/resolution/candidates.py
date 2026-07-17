"""WU-3C2 — the closed candidate set for one attempt's resolution (§5).

Per attempt the resolver matches student evidence nodes against a CLOSED
candidate set: *this problem's reference nodes* + *the course's misconception
entities*. Misconceptions are always appended so they compete in every
resolution (§5 anti-over-normalization guardrail). The set is small
(~15-25 candidates), which is what makes resolution a tiny matching problem
rather than a search over a global ontology.

Everything here is pure + DB-free. The caller (WU-4A; here the tests) supplies
a ``canon_key_by_canonical_key`` map (the WU-3C1 ``:Canon`` surrogate-id
projection, keyed on ``apollo_kg_entities.id``) so each ``Candidate`` carries
both its ``canonical_key`` (the matching space) and its ``canon_key`` (the
``RESOLVES_TO`` edge target).
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.ontology.nodes import NodeType

# Resolution methods, in tier order ending with the non-match sentinel.
RESOLUTION_METHODS: tuple[str, ...] = (
    "exact",
    "symbolic",
    "derived",
    "alias",
    "fuzzy",
    "llm",
    "unresolved",
)

# Confidence caps by method (§3 — the damper input). Frozen mapping: a match's
# capped confidence is exactly this value for its winning method.
METHOD_CONFIDENCE_CAP: dict[str, float] = {
    "exact": 1.00,
    "symbolic": 0.98,
    "derived": 0.95,
    "alias": 0.92,
    "fuzzy": 0.80,
    "llm": 0.75,
    "unresolved": 0.00,
}

# reference-solution entry_type -> ontology NodeType. simplifications map to
# the 'simplification' node type (the resolver's type-compat constraint is over
# node types, not the Layer-1 'condition' kind the seed uses for storage).
#
# G4 (variable_mapping contract): ``variable_mapping`` MUST be here — it is a
# real ontology ``NodeType`` (nodes.py), the schema ``EntryType`` accepts it, and
# the mint map ``persistence.learner_model_seed._ENTRY_TYPE_TO_KIND_PREFIX``
# emits it (``varmap.*`` steps). A WU-AAS-authored ``ConceptProblem.payload``
# whose ``reference_solution`` carries a ``variable_mapping`` step therefore
# reaches the graph-sim chain, and its ABSENCE from THIS map is what raised
# ``KeyError: 'variable_mapping'`` in ``candidates_from_reference_solution`` /
# ``build_reference_canonical`` (the F1c linear_motion crash). This map and the
# mint map must stay in lock-step; ``_node_type_for_entry`` below degrades any
# residual drift instead of crashing the whole grade.
_ENTRY_TYPE_TO_NODE_TYPE: dict[str, NodeType] = {
    "equation": "equation",
    "condition": "condition",
    "simplification": "simplification",
    "procedure_step": "procedure_step",
    "definition": "definition",
    "variable_mapping": "variable_mapping",
}


def _node_type_for_entry(entry_type: str) -> NodeType | None:
    """Map a reference-step ``entry_type`` to its ontology ``NodeType``.

    Returns ``None`` for an entry_type this map does not know (G4 tolerance /
    defense-in-depth): the graph-sim chain DEGRADES that step — it is dropped
    from the candidate set / reference graph — rather than raising ``KeyError``
    and voiding the (already-committed) learner update. Callers that need to
    SURFACE the degradation read :func:`unknown_reference_entry_types`."""
    return _ENTRY_TYPE_TO_NODE_TYPE.get(entry_type)


def unknown_reference_entry_types(problem: dict) -> tuple[str, ...]:
    """The DISTINCT reference-step ``entry_type`` values that have no ontology
    ``NodeType`` (sorted, deduped). Empty when every step is recognized.

    The graph-sim chain reads this to record a structured degradation marker
    (G4) when a minted payload carries an entry_type the resolver map does not
    know — after the map/mint-map drift, that step is dropped and this names the
    offending type(s) so paired analysis can see WHY the simulation degraded."""
    seen: set[str] = set()
    for step in problem.get("reference_solution", []):
        entry_type = step.get("entry_type")
        if entry_type is not None and _node_type_for_entry(entry_type) is None:
            seen.add(str(entry_type))
    return tuple(sorted(seen))

@dataclass(frozen=True)
class Candidate:
    """One resolution target in the closed candidate set for an attempt.

    Immutable (§ coding rule). ``canonical_key`` is the matching-space key
    (``eq.bernoulli`` / ``cond.incompressibility`` / ``misc.*``); ``canon_key``
    is the ``:Canon`` surrogate-id target for the ``RESOLVES_TO`` edge.
    ``exact_aliases`` holds curated reference-solution phrasings matched
    EXACT-only (the alias tier reads them; the fuzzy tier never does).
    """

    canonical_key: str
    canon_key: int
    node_type: NodeType
    is_misconception: bool
    symbolic: str | None
    aliases: tuple[str, ...]
    display_name: str
    opposes_key: str | None
    exact_aliases: tuple[str, ...] = ()


def candidates_from_reference_solution(
    problem: dict,
    *,
    canon_key_by_canonical_key: dict[str, int],
) -> tuple[Candidate, ...]:
    """One :class:`Candidate` per reference-solution step.

    ``canonical_key`` is the step's ``entity_key`` (WU-3B-annotated);
    ``node_type`` comes from ``entry_type``; ``symbolic`` is carried for
    equations (the symbolic tier input). ``canon_key`` is looked up from the
    supplied projection map (``-1`` when a key has no projected ``:Canon`` node
    yet — the resolver still matches but emits no edge for it)."""
    out: list[Candidate] = []
    for step in problem.get("reference_solution", []):
        entry_type = step["entry_type"]
        node_type = _node_type_for_entry(entry_type)
        if node_type is None:
            # G4 tolerance: an entry_type outside the map degrades to NO candidate
            # (the step is not a resolution target) rather than KeyError-ing the
            # whole attempt. run_graph_simulation records the marker separately.
            continue
        canonical_key = step["entity_key"]
        content = step.get("content", {}) or {}
        symbolic = content.get("symbolic") if node_type == "equation" else None
        display = content.get("label") or canonical_key
        out.append(
            Candidate(
                canonical_key=canonical_key,
                canon_key=canon_key_by_canonical_key.get(canonical_key, -1),
                node_type=node_type,
                is_misconception=False,
                symbolic=symbolic,
                aliases=(),  # unchanged (refs carry no fuzzy aliases)
                display_name=display,
                opposes_key=None,
                exact_aliases=tuple(content.get("aliases", ())),
            )
        )
    return tuple(out)


def candidates_from_misconceptions(
    misc: dict,
    *,
    canon_key_by_canonical_key: dict[str, int],
) -> tuple[Candidate, ...]:
    """One :class:`Candidate` per ``misconceptions.json`` entry.

    ``trigger_phrases`` become the alias surface forms (the fuzzy/alias tiers
    use these to make misconception competition algorithmic); ``opposes`` is
    carried as ``opposes_key``. Misconceptions never carry a ``symbolic``.
    Their ontology node type is ``definition`` (a misconception is negative
    knowledge phrased as a belief statement) so the type-compat constraint
    treats them like the definitions/conditions they oppose."""
    out: list[Candidate] = []
    for entry in misc.get("misconceptions", []):
        key = entry["key"]
        out.append(
            Candidate(
                canonical_key=key,
                canon_key=canon_key_by_canonical_key.get(key, -1),
                node_type="definition",
                is_misconception=True,
                symbolic=None,
                aliases=tuple(entry.get("trigger_phrases", ())),
                display_name=entry.get("display_name", key),
                opposes_key=entry.get("opposes"),
            )
        )
    return tuple(out)


def build_candidate_set(
    *,
    reference_nodes: tuple[Candidate, ...] | list[Candidate],
    misconception_entities: tuple[Candidate, ...] | list[Candidate],
) -> tuple[Candidate, ...]:
    """Closed candidate set = this problem's reference nodes + course ``misc.*``.

    Returns an immutable tuple; misconceptions are always appended so they
    compete in every resolution (§5). No dedup — variants stay distinct."""
    return tuple(reference_nodes) + tuple(misconception_entities)
