"""WU-3C2 — resolve_attempt: the §5 resolver orchestration.

Maps every student evidence node onto the closed candidate set with content
tiers (exact -> SymPy-symbolic -> alias -> fuzzy >= 0.9), structural type-compat
veto + neighborhood corroboration, misconception competition + polarity screen,
bounded greedy global assignment, and ONE LLM adjudication for the remainder.
Produces a :class:`ResolutionResult` with per-node ``{resolution, resolved_key,
method, confidence}`` and confidence caps by method.

Pure + synchronous + deterministic given a deterministic ``llm_adjudicator``:
re-running on the same ``(student_graph, candidates)`` yields the same result.
A non-match is ``unresolved`` DATA (no edge, no exception); over
:data:`~apollo.resolution.assignment.MAX_STUDENT_NODES` the whole attempt
abstains; a hallucinated LLM key raises ``ResolutionInvalidOutputError``; an
infra failure of the one LLM call raises ``ResolutionUnavailableError``.
"""

from __future__ import annotations

import logging
from typing import Callable

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import Node
from apollo.resolution.adjudication import (
    Adjudicator,
    ResolutionLLMReply,
    ResolutionLLMRequest,
    adjudicate,
)
from apollo.resolution.assignment import (
    MAX_STUDENT_NODES,
    greedy_global_assignment,
)
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, Candidate
from apollo.resolution.competition import (
    apply_misconception_competition,
    polarity_screen,
)
from apollo.resolution.result import ResolutionResult, ResolvedNode
from apollo.resolution.structural import ScoredMatch, type_compatible
from apollo.resolution.tiers import (
    match_alias,
    match_exact,
    match_fuzzy,
    match_symbolic,
    student_surface_text,
)

_LOG = logging.getLogger(__name__)

# The §6.9 declared variable mapping (circle radius/diameter) the symbolic tier
# applies before the sign-exact comparison. Authored once here so the solver
# (sympy_exec.py) is never edited.
_DEFAULT_SYMBOLIC_MAPPINGS: dict[str, str] = {"d": "2*r"}


def _content_match(
    node: Node,
    candidates: tuple[Candidate, ...],
    *,
    fuzzy_threshold: float,
    symbolic_mappings: dict[str, str],
) -> ScoredMatch | None:
    """Run the content tiers in priority order for one node and return the
    winning :class:`ScoredMatch`, applying the type-compat HARD constraint, the
    polarity screen (fuzzy only), and misconception competition.

    Tier precedence: exact -> symbolic -> alias -> fuzzy. Within the lexical
    (alias/fuzzy) tiers a misconception may out-compete a lexically-close
    reference node (§5)."""
    type_ok = tuple(c for c in candidates if type_compatible(node.node_type, c))
    if not type_ok:
        return None

    # Deterministic, higher-confidence tiers first; first hit wins outright.
    exact = match_exact(node, type_ok)
    if exact is not None:
        cand, method, _ = exact
        return ScoredMatch(node.node_id, cand, method, METHOD_CONFIDENCE_CAP[method])

    symbolic = match_symbolic(node, type_ok, mappings=symbolic_mappings)
    if symbolic is not None:
        cand, method, _ = symbolic
        return ScoredMatch(node.node_id, cand, method, METHOD_CONFIDENCE_CAP[method])

    # Lexical tiers: collect alias + fuzzy candidate matches so misconceptions
    # can compete, then screen polarity and pick the winner.
    lexical: list[ScoredMatch] = []
    surface = student_surface_text(node)
    alias = match_alias(node, type_ok)
    if alias is not None:
        cand, _method, _ = alias
        lexical.append(ScoredMatch(node.node_id, cand, "alias", METHOD_CONFIDENCE_CAP["alias"]))
    fuzzy = match_fuzzy(node, type_ok, threshold=fuzzy_threshold)
    if fuzzy is not None:
        cand, _method, raw = fuzzy
        # Polarity screen: reject a direction-inverted fuzzy match against a
        # non-misconception target (a misconception is SUPPOSED to be polar).
        keep = cand.is_misconception or all(
            polarity_screen(surface, a) for a in cand.aliases
        )
        if keep:
            lexical.append(
                ScoredMatch(node.node_id, cand, "fuzzy", METHOD_CONFIDENCE_CAP["fuzzy"])
            )

    if not lexical:
        return None
    return apply_misconception_competition(surface, lexical)


def resolve_attempt(
    student_graph: KGGraph,
    candidates: tuple[Candidate, ...],
    *,
    llm_adjudicator: Adjudicator | None = None,
    fuzzy_threshold: float = 0.9,
    symbolic_mappings: dict[str, str] | None = None,
) -> ResolutionResult:
    """Resolve every student evidence node against the closed candidate set.

    ``llm_adjudicator`` defaults to ``None`` — when None and nodes remain after
    the content tiers, those nodes stay ``unresolved`` and NO live call is made
    (CI-safe). A caller wanting the real one-call path passes
    :func:`apollo.resolution.adjudication.main_chat_adjudicator`."""
    nodes = list(student_graph.nodes)
    maps = symbolic_mappings if symbolic_mappings is not None else dict(_DEFAULT_SYMBOLIC_MAPPINGS)

    # Over the cap -> the whole attempt abstains (no unbounded solve, no LLM).
    if len(nodes) > MAX_STUDENT_NODES:
        resolved = tuple(_unresolved(n) for n in nodes)
        return ResolutionResult(
            resolved=resolved,
            tier_counts=_histogram(resolved),
            llm_calls=0,
        )

    # 1) Content tiers + structural/competition per node.
    matches_by_node: dict[str, list[ScoredMatch]] = {}
    for n in nodes:
        hit = _content_match(
            n, candidates,
            fuzzy_threshold=fuzzy_threshold,
            symbolic_mappings=maps,
        )
        if hit is not None:
            matches_by_node[n.node_id] = [hit]

    # 2) Bounded greedy global assignment.
    outcome = greedy_global_assignment(matches_by_node)
    if outcome.abstained:  # pragma: no cover - cap already short-circuited above
        resolved = tuple(_unresolved(n) for n in nodes)
        return ResolutionResult(
            resolved=resolved, tier_counts=_histogram(resolved), llm_calls=0
        )
    assigned = outcome.assignment

    # 3) ONE LLM adjudication for the remainder (only if an adjudicator is given).
    llm_calls = 0
    llm_resolved: dict[str, Candidate] = {}
    remaining = [n for n in nodes if n.node_id not in assigned]
    if remaining and llm_adjudicator is not None:
        llm_resolved = adjudicate(remaining, candidates, adjudicator=llm_adjudicator)
        llm_calls = 1

    # 4) Build the per-node result with confidence caps by method.
    resolved_nodes: list[ResolvedNode] = []
    for n in nodes:
        if n.node_id in assigned:
            m = assigned[n.node_id]
            resolved_nodes.append(_resolved(n.node_id, m.candidate, m.method))
        elif n.node_id in llm_resolved:
            resolved_nodes.append(_resolved(n.node_id, llm_resolved[n.node_id], "llm"))
        else:
            resolved_nodes.append(_unresolved(n))

    resolved = tuple(resolved_nodes)
    return ResolutionResult(
        resolved=resolved,
        tier_counts=_histogram(resolved),
        llm_calls=llm_calls,
    )


def _resolved(node_id: str, candidate: Candidate, method: str) -> ResolvedNode:
    return ResolvedNode(
        node_id=node_id,
        resolution="resolved",
        resolved_key=candidate.canonical_key,
        resolved_canon_key=candidate.canon_key if candidate.canon_key >= 0 else None,
        method=method,
        confidence=METHOD_CONFIDENCE_CAP[method],
    )


def _unresolved(node: Node) -> ResolvedNode:
    _LOG.debug("resolution_unresolved node_id=%s", node.node_id)
    return ResolvedNode(
        node_id=node.node_id,
        resolution="unresolved",
        resolved_key=None,
        resolved_canon_key=None,
        method="unresolved",
        confidence=METHOD_CONFIDENCE_CAP["unresolved"],
    )


def _histogram(resolved: tuple[ResolvedNode, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rn in resolved:
        counts[rn.method] = counts.get(rn.method, 0) + 1
    return counts
