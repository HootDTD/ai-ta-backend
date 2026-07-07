"""Resolver V2 node scoring — fusion, graded credit, max-aggregation (§5.3-§5.5, T4).

Per reference node the score is the MAX fused value over (window × view)
pairs: the R1 inverted loop. Window selection is INJECTED as a ``SelectFn``
callable (T3 builds the real lexical prefilter in parallel — this module never
imports it), and the NLI adjudicator is injected too (``None`` = deterministic
lexical-only degrade), so every test runs with fakes and zero model loads.

Per-pair screens (§5.4, the polarity lesson):

* **polarity screen** — ``polarity_allows_match(window_text, view)``;
  a disallowed pair contributes 0 and NEVER reaches NLI (free, no budget).
* **contradiction veto** — ``P(contradiction) > max_contradiction`` zeroes the
  pair's fused score (the raw entailment/contradiction are still recorded on
  the :class:`PairScore` for the trace).

Budget (§5.4): ``max_nli_pairs`` real NLI calls per attempt, nodes consumed in
``ref_nodes`` (declared-path) order. An in-attempt memo cache on
``(window_index, view_text)`` means a view shared across nodes is never
re-scored and cache hits are budget-free. A node reached after exhaustion
falls back to lexical-only (``source="lexical_skip"``, score = its max lexical
score through ``g`` — the design's recall-first literal reading; calibration
owns the thresholds).

Floors are applied AFTER ``g`` (max wins, ``source`` records which): the v1
floor here (resolved-in-S_norm keys keep credit 1.0, guaranteeing V2 >= V1);
grayzone upgrade and edge pull-up happen in the engine (T7).

Symbolic-blindness gate (T8 anomaly #2): NLI entailment cannot see symbolic
coefficients — the F1c calibration credited ``x = v0*t + a*t^2`` against the
half-factor reference at entailment 0.98. So EQUATION-type nodes
(ontology ``node_type == "equation"``) can never earn full credit from text
evidence alone: on the ``nli`` and ``lexical_skip`` paths their credit is
capped at ``params.equation_text_credit_cap`` (default = the gray-band
credit, ``source="equation_cap"``), applied BEFORE the v1 floor so the
symbolic path (SymPy resolution in v1, surfaced as ``v1_resolved_keys``)
remains the ONLY route to credit 1.0. Capped nodes stay eligible for the
engine's gray-zone confirmation, which itself tops out at
``grayzone_credit`` (0.7) — a verified quote is still text, never full
credit for an equation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from apollo.resolution.nli_adjudicator import NLIAdjudicator
from apollo.resolution.polarity import polarity_allows_match
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.types import NodeScore, PairScore, RefNode, SelectFn, Window

_LOG = logging.getLogger(__name__)

#: Graded credits per §5.5 tier (gray gets the deterministic default here —
#: the grayzone upgrade to 0.7 is the engine's job, T7).
_CREDIT_HIGH: float = 1.0
_CREDIT_MID: float = 0.7
_CREDIT_GRAY: float = 0.3
_CREDIT_ZERO: float = 0.0

#: Ontology node types whose full credit requires a SYMBOLIC check (v1's
#: SymPy resolution path) — paraphrase entailment is blind to coefficient
#: errors, so text-only credit for these is capped at
#: ``params.equation_text_credit_cap`` (the symbolic-blindness gate).
#: ``"equation"`` is the only equation type in ``apollo.ontology.nodes``.
_EQUATION_NODE_TYPES: frozenset[str] = frozenset({"equation"})


def fuse(entailment: float, lexical: float, params: ResolverV2Params) -> float:
    """§5.4 MENLI-style linear fusion: ``alpha * P(entailment) +
    (1 - alpha) * lex``. Both inputs live in [0, 1], so the result does too."""
    return params.alpha * entailment + (1.0 - params.alpha) * lexical


def credit_for_score(score: float, params: ResolverV2Params) -> tuple[float, bool]:
    """§5.5 graded credit ``g(s)`` WITHOUT grayzone/floors.

    Returns ``(credit, is_gray)`` — the gray band gets the deterministic 0.3
    default here; the engine may upgrade it to ``grayzone_credit`` (T7).
    """
    if score >= params.t_high:
        return _CREDIT_HIGH, False
    if score >= params.t_mid:
        return _CREDIT_MID, False
    if score >= params.t_low:
        return _CREDIT_GRAY, True
    return _CREDIT_ZERO, False


def _cap_equation_text_credit(
    node: RefNode,
    credit: float,
    source: str,
    params: ResolverV2Params,
) -> tuple[float, str]:
    """The symbolic-blindness gate (T8 anomaly #2): text-derived credit for an
    EQUATION-type node is capped at ``params.equation_text_credit_cap``.

    Returns the (possibly capped) ``(credit, source)`` pair — a binding cap
    records ``source="equation_cap"`` so the engine can hold the node
    gray-zone-eligible and the trace shows why full credit was withheld.
    Non-equation nodes and credits already at/below the cap pass through
    untouched. Runs BEFORE :func:`_with_v1_floor`, so the v1 symbolic path
    still restores full credit.
    """
    if node.node_type not in _EQUATION_NODE_TYPES:
        return credit, source
    if credit <= params.equation_text_credit_cap:
        return credit, source
    return params.equation_text_credit_cap, "equation_cap"


def _with_v1_floor(
    canonical_key: str,
    score: float,
    credit: float,
    source: str,
    best: PairScore | None,
    v1_resolved_keys: frozenset[str],
) -> NodeScore:
    """Apply the §5.5 v1 floor (max wins; ``source`` records the winner).

    A key v1 already resolved keeps credit 1.0 — V2 coverage is >= v1's by
    construction. A tie at 1.0 keeps the earned source (e.g. ``"nli"``).
    """
    if canonical_key in v1_resolved_keys and credit < _CREDIT_HIGH:
        return NodeScore(canonical_key, score, _CREDIT_HIGH, "v1_floor", best)
    return NodeScore(canonical_key, score, credit, source, best)


def _select_view_pairs(
    windows: Sequence[Window],
    views: tuple[str, ...],
    params: ResolverV2Params,
    select_fn: SelectFn,
) -> tuple[tuple[int, str, tuple[tuple[int, float], ...]], ...]:
    """Run the injected prefilter per view: ``(view_index, view_text,
    ((window_index, lex), ...))`` for every view with a non-empty selection."""
    selections: list[tuple[int, str, tuple[tuple[int, float], ...]]] = []
    for view_index, view_text in enumerate(views):
        selected = tuple(select_fn(windows, view_text, params.top_k_windows))
        if selected:
            selections.append((view_index, view_text, selected))
    return tuple(selections)


def score_nodes(
    windows: Sequence[Window],
    ref_nodes: Sequence[RefNode],
    *,
    nli: NLIAdjudicator | None,
    params: ResolverV2Params,
    v1_resolved_keys: frozenset[str],
    select_fn: SelectFn,
) -> tuple[tuple[NodeScore, ...], int]:
    """Score every reference node against the student windows (§5.3-§5.5).

    Returns ``(node_scores, pair_count)`` — one :class:`NodeScore` per input
    node, in input order; ``pair_count`` = real NLI classifications run
    (cache hits and polarity-screened pairs are free — the budget audit).

    Per-node outcome ladder:

    * no (window, view) pair at all → ``source="zero"`` (score 0.0);
    * ``nli is None`` / max lex < ``lex_floor`` / budget exhausted at node
      start → ``source="lexical_skip"``, score = max lex, ``best=None``;
    * otherwise → ``source="nli"``, score = max fused over evaluated pairs,
      ``best`` = the argmax pair (first-evaluated wins ties). Pairs that hit
      budget exhaustion MID-node are simply not evaluated.

    Equation-type nodes then pass the symbolic-blindness gate: on both text
    paths (``nli``/``lexical_skip``) their credit is capped at
    ``params.equation_text_credit_cap`` (``source="equation_cap"`` when the
    cap binds — see module docstring).

    The v1 floor is applied last on every path (``source="v1_floor"``).
    """
    window_by_index = {window.index: window for window in windows}
    nli_cache: dict[tuple[int, str], tuple[float, float]] = {}
    pairs_run = 0
    scores: list[NodeScore] = []

    for node in ref_nodes:
        selections = _select_view_pairs(windows, node.views, params, select_fn)
        if not selections:
            scores.append(
                _with_v1_floor(node.canonical_key, 0.0, _CREDIT_ZERO, "zero", None, v1_resolved_keys)
            )
            continue

        max_lex = max(lex for _, _, selected in selections for _, lex in selected)
        if nli is None or max_lex < params.lex_floor or pairs_run >= params.max_nli_pairs:
            # §5.3 skip rule / §5.4 budget exhaustion / no-NLI degrade: score
            # is the raw max lexical signal through g (v1 floor still applies).
            credit, _is_gray = credit_for_score(max_lex, params)
            credit, source = _cap_equation_text_credit(node, credit, "lexical_skip", params)
            scores.append(
                _with_v1_floor(
                    node.canonical_key, max_lex, credit, source, None, v1_resolved_keys
                )
            )
            continue

        best_pair: PairScore | None = None
        for view_index, view_text, selected in selections:
            for window_index, lex in selected:
                window = window_by_index.get(window_index)
                if window is None:  # defensive: selector returned an unknown index
                    continue
                pair = _score_pair(
                    window=window,
                    view_index=view_index,
                    view_text=view_text,
                    lex=lex,
                    nli=nli,
                    params=params,
                    nli_cache=nli_cache,
                    pairs_run=pairs_run,
                )
                if pair is None:  # budget exhausted mid-node: pair not evaluated
                    continue
                pair_score, ran_nli = pair
                if ran_nli:
                    pairs_run += 1
                if best_pair is None or pair_score.fused > best_pair.fused:
                    best_pair = pair_score

        node_score = best_pair.fused if best_pair is not None else 0.0
        credit, _is_gray = credit_for_score(node_score, params)
        credit, source = _cap_equation_text_credit(node, credit, "nli", params)
        scores.append(
            _with_v1_floor(
                node.canonical_key, node_score, credit, source, best_pair, v1_resolved_keys
            )
        )

    return tuple(scores), pairs_run


def _score_pair(
    *,
    window: Window,
    view_index: int,
    view_text: str,
    lex: float,
    nli: NLIAdjudicator,
    params: ResolverV2Params,
    nli_cache: dict[tuple[int, str], tuple[float, float]],
    pairs_run: int,
) -> tuple[PairScore, bool] | None:
    """Score one (window, view) pair per the §5.4 screens.

    Returns ``(pair_score, ran_nli)`` — ``ran_nli`` is True only for a real
    (uncached) NLI classification, the unit the budget counts. Returns ``None``
    when the pair needs NLI but the budget is exhausted (pair skipped).

    Screen order: polarity first (free, deterministic — a disallowed pair
    contributes 0 and never costs an NLI call), then the contradiction veto on
    the NLI output (fused zeroed, raw scores kept for the trace).
    """
    if not polarity_allows_match(window.text, view_text).allowed:
        return PairScore(window.index, view_index, lex, 0.0, 0.0, 0.0), False

    cache_key = (window.index, view_text)
    cached = nli_cache.get(cache_key)
    ran_nli = False
    if cached is None:
        if pairs_run >= params.max_nli_pairs:
            return None
        result = nli.classify(window.text, view_text)
        cached = (float(result.entailment), float(result.contradiction))
        nli_cache[cache_key] = cached
        ran_nli = True
    entailment, contradiction = cached

    if contradiction > params.max_contradiction:
        fused = 0.0  # §5.4 contradiction veto: the pair contributes nothing
    else:
        fused = fuse(entailment, lex, params)
    return PairScore(window.index, view_index, lex, entailment, contradiction, fused), ran_nli
