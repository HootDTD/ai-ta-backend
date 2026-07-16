"""WU-3C2 — resolve_attempt: the §5 resolver orchestration.

Maps every student evidence node onto the closed candidate set with content
tiers (exact -> SymPy-symbolic -> derived-equation-alignment -> alias -> fuzzy
>= 0.9), the structural type-compat HARD constraint, misconception competition
(on RAW lexical proximity) + a per-winning-alias polarity screen, bounded
greedy global assignment, and authoritative clarification resolutions. Produces
a :class:`ResolutionResult` with per-node ``{resolution, resolved_key, method,
confidence}`` and confidence caps by method.

v1 wires the type-compat HARD constraint + misconception competition as the
structural / anti-over-normalization signals. Neighborhood corroboration (§5
steps 2-3 — boosting confidence from agreeing graph-neighbors) is DEFERRED to a
WU-4A-era refinement; the live resolver performs NO neighborhood corroboration.

The one-LLM-adjudication path has been RETIRED (Task 4 / §5): a post-tier,
non-clarified node stays ``unresolved`` DATA (no LLM guess, no exception). The
clarification loop (Task 5+) occupies the band the silent guess used to.

Pure + synchronous + deterministic: re-running on the same
``(student_graph, candidates)`` yields the same result. A non-match is
``unresolved`` DATA (no edge, no exception); over
:data:`~apollo.resolution.assignment.MAX_STUDENT_NODES` the whole attempt
abstains (``llm_calls == 0``).
"""

from __future__ import annotations

import logging

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import Node
from apollo.resolution.assignment import (
    MAX_STUDENT_NODES,
    greedy_global_assignment,
)
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, Candidate
from apollo.resolution.competition import (
    apply_misconception_competition,
    polarity_screen,
)
from apollo.resolution.equation_alignment import match_equation_alignment
from apollo.resolution.result import ResolutionResult, ResolvedNode
from apollo.resolution.structural import ScoredMatch, type_compatible
from apollo.resolution.tiers import (
    match_alias_all,
    match_exact,
    match_fuzzy_all,
    match_symbolic,
    student_surface_text,
)

_LOG = logging.getLogger(__name__)


def _content_match(
    node: Node,
    candidates: tuple[Candidate, ...],
    *,
    fuzzy_threshold: float,
    symbolic_mappings: dict[str, str],
) -> ScoredMatch | None:
    """Run the content tiers in priority order for one node and return the
    winning :class:`ScoredMatch`, applying the type-compat HARD constraint, the
    per-winning-alias polarity screen (lexical only), and misconception
    competition.

    Tier precedence: exact -> symbolic -> alias -> fuzzy. Within the lexical
    (alias/fuzzy) tiers a misconception may out-compete a lexically-close
    reference node (§5 / §6.11): ALL above-threshold alias + fuzzy hits compete,
    ranked on their RAW lexical proximity (1.0 for an exact alias hit, the
    ``token_set_ratio`` for a fuzzy hit). The winner's reported confidence is
    re-derived from ``METHOD_CONFIDENCE_CAP[method]`` downstream in
    :func:`_resolved` (the §3 cap is the confidence; the raw score is only the
    competition/assignment ranking signal)."""
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

    # Derived tier (Phase 1a): a solved/rearranged form of a reference equation
    # aligns deterministically under the DECLARED symbolic_mappings only. Placed
    # AFTER the symbolic tier so substitution-collapsible forms still report
    # symbolic@0.98; only genuine solve-for-variable forms report derived@0.95.
    derived = match_equation_alignment(node, type_ok, mappings=symbolic_mappings)
    if derived is not None:
        cand, method, _ = derived
        return ScoredMatch(node.node_id, cand, method, METHOD_CONFIDENCE_CAP[method])

    # Lexical tiers: collect EVERY above-threshold alias + fuzzy hit (raw scores)
    # so misconceptions can compete, screen polarity per winning alias, then pick
    # the winner. score = RAW proximity (NOT the flat method cap) — the cap is the
    # reported confidence, re-applied in _resolved; the raw score ranks competition.
    #
    # One entry PER CANDIDATE: a candidate that exact-alias-matches (raw 1.0) also
    # fuzzy-matches itself (token_set_ratio of identical strings == 1.0), so it
    # would otherwise be enqueued TWICE. We dedupe by candidate and keep the
    # higher tier EXPLICITLY (alias 0.92 > fuzzy 0.80) so the reported method does
    # not depend on enqueue order or max()'s tie-stability — same input, same
    # method, every run.
    surface = student_surface_text(node)
    by_candidate: dict[str, ScoredMatch] = {}
    for hit in match_alias_all(node, type_ok):
        by_candidate[hit.candidate.canonical_key] = ScoredMatch(
            node.node_id, hit.candidate, "alias", hit.score
        )
    for hit in match_fuzzy_all(node, type_ok, threshold=fuzzy_threshold):
        # Polarity screen ONLY the alias that produced this fuzzy hit — an
        # UNRELATED inverted alias on the same candidate must not false-reject a
        # valid match. A misconception is SUPPOSED to be polar, so it is exempt.
        keep = hit.candidate.is_misconception or polarity_screen(surface, hit.winning_alias)
        if not keep:
            continue
        # Prefer the alias tier when this candidate already produced an alias hit
        # (alias > fuzzy); only register the fuzzy hit for candidates with no
        # alias hit. Never downgrade an alias entry to fuzzy.
        if hit.candidate.canonical_key not in by_candidate:
            by_candidate[hit.candidate.canonical_key] = ScoredMatch(
                node.node_id, hit.candidate, "fuzzy", hit.score
            )

    lexical = list(by_candidate.values())
    if lexical:
        winner = apply_misconception_competition(surface, lexical)
        return winner
    return None


def find_residual_nodes(
    nodes: list[Node],
    candidates: tuple[Candidate, ...],
    *,
    fuzzy_threshold: float = 0.9,
    symbolic_mappings: dict[str, str] | None = None,
) -> list[Node]:
    """Nodes no deterministic tier confidently matched (the clarification
    detector's input). Pure: reuses ``_content_match`` so banding stays in
    lockstep with grading-time resolution."""
    maps = symbolic_mappings if symbolic_mappings is not None else {}
    residual: list[Node] = []
    for n in nodes:
        if (
            _content_match(
                n,
                candidates,
                fuzzy_threshold=fuzzy_threshold,
                symbolic_mappings=maps,
            )
            is None
        ):
            residual.append(n)
    return residual


def resolve_attempt(
    student_graph: KGGraph,
    candidates: tuple[Candidate, ...],
    *,
    confirmed_resolutions: dict[str, str] | None = None,
    fuzzy_threshold: float = 0.9,
    symbolic_mappings: dict[str, str] | None = None,
) -> ResolutionResult:
    """Resolve every student evidence node against the closed candidate set.

    ``confirmed_resolutions`` maps ``node_id -> candidate_key`` for nodes whose
    mapping was authorised by a clarification exchange (Task 3 / §5.3). For each
    entry whose key exists in the candidate set AND is type-compatible with the
    node, the node resolves via method ``"clarification"`` (confidence 0.90),
    applied BEFORE the content tiers and AUTHORITATIVE (overrides any tier hit).
    Nodes not in the map, or with an unknown/type-incompatible key, follow the
    normal resolution path unchanged.

    A post-tier, non-clarified node stays ``unresolved`` (no LLM guess).
    ``llm_calls`` in the result is always 0 — the silent LLM adjudication path
    was retired in Task 4; the clarification loop (Task 5+) now occupies
    that band."""
    nodes = list(student_graph.nodes)
    # Symbolic mappings are PER-PROBLEM declared data (§5), never a global
    # default: with none supplied the symbolic tier applies NO substitution (a
    # global d=2r default produced false symbolic matches). WU-4A passes the
    # per-problem table.
    maps = symbolic_mappings if symbolic_mappings is not None else {}

    # Over the cap -> the whole attempt abstains (no unbounded solve, no LLM).
    if len(nodes) > MAX_STUDENT_NODES:
        resolved = tuple(_unresolved(n) for n in nodes)
        return ResolutionResult(
            resolved=resolved,
            tier_counts=_histogram(resolved),
            llm_calls=0,
        )

    confirmed = confirmed_resolutions or {}
    by_key = {c.canonical_key: c for c in candidates}

    # Authoritative clarification resolutions (the student committed an answer to
    # a pointed question). Applied BEFORE the tiers; type-compat still enforced.
    clarified: dict[str, Candidate] = {}
    for node in nodes:
        key = confirmed.get(node.node_id)
        if key is None:
            continue
        cand = by_key.get(key)
        if cand is not None and type_compatible(node.node_type, cand):
            clarified[node.node_id] = cand

    # 1) Content tiers — skip nodes already clarified.
    matches_by_node: dict[str, list[ScoredMatch]] = {}
    for n in nodes:
        if n.node_id in clarified:
            continue
        hit = _content_match(
            n,
            candidates,
            fuzzy_threshold=fuzzy_threshold,
            symbolic_mappings=maps,
        )
        if hit is not None:
            matches_by_node[n.node_id] = [hit]

    # 2) Bounded greedy global assignment.
    outcome = greedy_global_assignment(matches_by_node)
    if outcome.abstained:  # pragma: no cover - cap already short-circuited above
        resolved = tuple(_unresolved(n) for n in nodes)
        return ResolutionResult(resolved=resolved, tier_counts=_histogram(resolved), llm_calls=0)
    assigned = outcome.assignment

    # No live LLM adjudication: a post-tier, non-clarified node stays unresolved
    # (the clarification loop now occupies the band the silent guess used to).
    llm_calls = 0

    # 3) Build the per-node result with confidence caps by method.
    resolved_nodes: list[ResolvedNode] = []
    for n in nodes:
        if n.node_id in clarified:
            resolved_nodes.append(_resolved(n.node_id, clarified[n.node_id], "clarification"))
        elif n.node_id in assigned:
            m = assigned[n.node_id]
            resolved_nodes.append(_resolved(n.node_id, m.candidate, m.method))
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
