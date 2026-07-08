"""Offline selection-quality + calibration gate (integration spec §11.3/§8.1,
task T16).

**Pre-flag-ON gate, NOT a runtime dependency.** Nothing here is imported by
``turn.py``/``chat.py``/``v2_selection.py`` — it exists purely so a labeled,
deterministic, fixtures-only harness can answer: "on the SAME snapshots, does
the VoI ranker (:func:`apollo.clarification.v2_ranker.rank_by_voi`) put more
genuinely-weak reference nodes in the first 3 clarification questions than
the v1 ranking it replaces (:func:`apollo.clarification.pacing
.rubric_weight_for` + detector cosine)?"

The replay corpus passes ``clarification_trace=[]`` (spec §11 caveat), so it
CANNOT exercise the clarification loop -- this module's fixtures are the
only coverage this comparison gets. Nothing here claims replay coverage.

**Calibration pin (§8.1 M3).** ``rank_by_voi``'s uncertainty band reads
``t_low``/``t_mid`` fresh from ``resolver_v2.config.load_params()`` and its
``p_*``/``voi_*`` constants come from ``clarification.v2_config
.load_clarification_v2_params()``. Both are heuristic and were reasoned
against a *specific* band. :func:`record_calibration_band` snapshots the
exact values this comparison ran against; :data:`PINNED_CALIBRATION_BAND` is
that recorded snapshot (as of the T16 build, 2026-07-07) baked into the test
suite so a silent recalibration of either config module's defaults makes the
pinned-band assertion fail loudly -- the recorded signal that "flag-ON is
only valid while the live band matches the recorded one, or the comparison
must be re-run" (spec §8.1/§11.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from apollo.clarification.pacing import rubric_weight_for
from apollo.clarification.v2_config import ClarificationV2Params, load_clarification_v2_params
from apollo.clarification.v2_ranker import VoICandidate, rank_by_voi
from apollo.grading.composite import CompositeWeights, load_weights
from apollo.resolver_v2.config import ResolverV2Params, load_params
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore

#: Top-N clarification questions/topics compared per turn (spec §11.3
#: "precision@3" -- also the ranker's own default ``max_questions`` bound,
#: §8.1, so this mirrors the real packing width without importing packing).
TOP_K: int = 3


@dataclass(frozen=True)
class LabeledNode:
    """One reference node in a turn's clarification-candidate pool, carrying
    both the v1 ranking inputs (``node_type``/``cosine``) and the v2 ranking
    inputs (``node_credit``/``incident_edges``), plus the oracle label this
    gate scores against."""

    canonical_key: str
    node_type: str
    node_credit: float  # current running V2 credit, in [0, 1]
    cosine: float  # v1 detector cosine (pacing.select_probes tie-break)
    is_gray: bool
    incident_edges: tuple[EdgeScore, ...] = ()
    is_genuinely_weak: bool = False  # human/oracle label: this node needed a
    # clarification question (the ground truth the gate scores against).


@dataclass(frozen=True)
class LabeledTurn:
    """One turn of a labeled multi-turn transcript: the closed candidate
    pool visible to clarification selection at that turn, oracle-labeled.

    ``extra_node_credits`` carries running credits for reference nodes that
    are NOT clarification candidates this turn (e.g. an already-resolved
    neighbor an incident edge points at) -- a real ``IncrementalSnapshot``'s
    ``node_credits`` covers every reference node, not just the gray/missing
    pool, so edge endpoints outside the labeled pool still need a credit for
    ``edge_gain``'s ``c_v`` term to be evaluated realistically."""

    turn_id: str
    nodes: tuple[LabeledNode, ...]
    extra_node_credits: Mapping[str, float] = field(default_factory=dict)

    @property
    def weak_keys(self) -> frozenset[str]:
        return frozenset(n.canonical_key for n in self.nodes if n.is_genuinely_weak)


@dataclass(frozen=True)
class CalibrationBand:
    """The exact param values a T16 comparison ran against (§8.1 pin)."""

    t_low: float
    t_mid: float
    t_high: float
    voi_target_credit: float
    p_missing: float
    p_near_resolved: float
    p_gray_min: float
    p_gray_max: float
    p_equation_floor: float


def record_calibration_band(
    resolver_params: ResolverV2Params | None = None,
    clarification_params: ClarificationV2Params | None = None,
) -> CalibrationBand:
    """Snapshot the exact ``t_*``/``p_*``/``voi_*`` values a comparison ran
    against (§8.1 calibration pin). Reads fresh via the SAME entry points the
    live ranker uses (``load_params()``/``load_clarification_v2_params()``)
    unless explicit params are injected (deterministic fixture harnesses
    should inject to avoid any ambient-env dependency)."""
    rp = resolver_params if resolver_params is not None else load_params()
    cp = clarification_params if clarification_params is not None else load_clarification_v2_params()
    return CalibrationBand(
        t_low=rp.t_low,
        t_mid=rp.t_mid,
        t_high=rp.t_high,
        voi_target_credit=cp.voi_target_credit,
        p_missing=cp.p_missing,
        p_near_resolved=cp.p_near_resolved,
        p_gray_min=cp.p_gray_min,
        p_gray_max=cp.p_gray_max,
        p_equation_floor=cp.p_equation_floor,
    )


#: The band the T16 offline comparison was run and PASSED against (2026-07-07
#: build). These are the ``ResolverV2Params``/``ClarificationV2Params`` class
#: DEFAULTS at build time -- not forked/re-tuned here (§8.1: this module
#: never hardcodes the gray band, it only records it). If either config
#: module's defaults drift, :func:`test_pinned_calibration_band_matches_live_defaults`
#: (apollo/clarification/tests/test_v2_efficacy_gate.py) fails, signaling the
#: comparison must be re-run before flag-ON is valid again.
PINNED_CALIBRATION_BAND: CalibrationBand = CalibrationBand(
    t_low=0.30,
    t_mid=0.70,
    t_high=0.90,
    voi_target_credit=1.0,
    p_missing=0.6,
    p_near_resolved=0.2,
    p_gray_min=0.3,
    p_gray_max=0.8,
    p_equation_floor=0.7,
)


def _build_snapshot(turn: LabeledTurn) -> IncrementalSnapshot:
    """Assemble the ``IncrementalSnapshot`` both rankings score against --
    the "SAME snapshots" the spec requires (§11.3)."""
    node_credits = {n.canonical_key: n.node_credit for n in turn.nodes}
    node_credits.update(turn.extra_node_credits)
    gray = frozenset(n.canonical_key for n in turn.nodes if n.is_gray)
    edge_scores: list[EdgeScore] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for node in turn.nodes:
        for edge in node.incident_edges:
            key = (edge.edge_type, edge.from_key, edge.to_key)
            if key not in seen_edges:
                seen_edges.add(key)
                edge_scores.append(edge)
    return IncrementalSnapshot(
        node_credits=node_credits,
        edge_scores=tuple(edge_scores),
        node_cov=0.0,
        edge_cov=0.0,
        winning_path_index=0,
        gray=gray,
        pair_count_this_turn=0,
    )


def v1_ranking(turn: LabeledTurn) -> list[str]:
    """The v1 ranking this feature replaces: ``pacing.select_probes``'s own
    ordering key, ``(rubric_weight_for(node_type), cosine)`` desc, applied
    to the full turn pool (no per-idea dedup needed -- fixture nodes are
    already one-per-topic). Ties broken by ``canonical_key`` asc for
    determinism (``select_probes`` itself has no explicit tie-break beyond
    dict/sort stability, which this pins down for a reproducible gate)."""
    # Two stable passes: sort ascending by canonical_key first, then
    # descending by (rubric_weight, cosine) -- Python's sort stability means
    # ties on the second pass keep the first pass's ascending key order, so
    # the ranking is deterministic independent of fixture/dict insertion
    # order (`sorted(..., reverse=True)` on a 3-tuple would instead reverse
    # the string tiebreak too, which is not the intended determinism rule).
    ordered = sorted(turn.nodes, key=lambda n: n.canonical_key)
    ordered.sort(key=lambda n: (rubric_weight_for(n.node_type), n.cosine), reverse=True)
    return [n.canonical_key for n in ordered]


def v2_ranking(
    turn: LabeledTurn,
    snapshot: IncrementalSnapshot,
    weights: CompositeWeights,
    params: ClarificationV2Params,
) -> list[str]:
    """The VoI ranking (task T5) on the SAME turn/snapshot. Builds
    ``VoICandidate`` directly from the labeled fixture (rather than routing
    through ``v2_gray_candidates``, which -- per its own docstring -- cannot
    recover real ``node_type`` from a bare snapshot and would zero it out;
    this harness wants the true node types so ``p_equation_floor`` and any
    future type-conditioned VoI logic are exercised faithfully)."""
    pool = [
        VoICandidate(
            canonical_key=n.canonical_key,
            node_type=n.node_type,
            node_credit=n.node_credit,
            is_gray=n.is_gray,
            incident_edges=n.incident_edges,
            best_window_index=None,
        )
        for n in turn.nodes
    ]
    ranked = rank_by_voi(pool, snapshot, weights, params)
    return [scored.candidate.canonical_key for scored in ranked]


def precision_at_k(ranked_keys: Sequence[str], weak_keys: frozenset[str], k: int) -> float:
    """Fraction of the top-``k`` ranked keys that are genuinely weak (§11.3
    "precision@3" / "questions spent on genuinely-weak nodes" -- the top-k
    ranked topics are exactly what the real 3x1-topic-per-question packing
    would ask about first, so this is the questions-spent metric expressed
    as a fraction of the fixed question budget). ``k<=0`` or an empty
    ranking returns 0.0 (defensive; never divides by zero)."""
    if k <= 0 or not ranked_keys:
        return 0.0
    top = ranked_keys[:k]
    hits = sum(1 for key in top if key in weak_keys)
    return hits / len(top)


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    v1_precision_at_k: float
    voi_precision_at_k: float


@dataclass(frozen=True)
class EfficacyGateResult:
    """The full T16 comparison output. ``passed`` is the gate itself:
    flag-ON in ANY environment is blocked unless this is True (spec §11.3/
    §12 T16 acceptance criteria)."""

    per_turn: tuple[TurnResult, ...]
    v1_mean_precision_at_k: float
    voi_mean_precision_at_k: float
    calibration_band: CalibrationBand
    passed: bool


def run_efficacy_gate(
    turns: Sequence[LabeledTurn],
    *,
    weights: CompositeWeights | None = None,
    params: ClarificationV2Params | None = None,
    resolver_params: ResolverV2Params | None = None,
    k: int = TOP_K,
) -> EfficacyGateResult:
    """Run the §11.3 offline selection-quality comparison over a labeled
    multi-turn transcript fixture. Deterministic: fixtures only, no network,
    no NLI/model calls -- every input is either a caller-supplied param
    object or a hand-built ``LabeledTurn``.

    ``weights``/``params``/``resolver_params`` default to the SAME fresh
    ``load_*()`` entry points the live ranker uses when omitted, so a caller
    validating "does the live-configured ranker still beat v1" can call this
    with no overrides; a caller pinning a specific historical band (as the
    T16 test suite does, to stay deterministic across config-default drift)
    passes explicit params."""
    live_resolver_params = resolver_params if resolver_params is not None else load_params()
    live_params = params if params is not None else load_clarification_v2_params()
    live_weights = weights if weights is not None else load_weights()

    per_turn: list[TurnResult] = []
    for turn in turns:
        snapshot = _build_snapshot(turn)
        weak_keys = turn.weak_keys
        v1_keys = v1_ranking(turn)
        voi_keys = v2_ranking(turn, snapshot, live_weights, live_params)
        per_turn.append(
            TurnResult(
                turn_id=turn.turn_id,
                v1_precision_at_k=precision_at_k(v1_keys, weak_keys, k),
                voi_precision_at_k=precision_at_k(voi_keys, weak_keys, k),
            )
        )

    v1_mean = sum(r.v1_precision_at_k for r in per_turn) / len(per_turn) if per_turn else 0.0
    voi_mean = sum(r.voi_precision_at_k for r in per_turn) / len(per_turn) if per_turn else 0.0

    band = record_calibration_band(live_resolver_params, live_params)
    return EfficacyGateResult(
        per_turn=tuple(per_turn),
        v1_mean_precision_at_k=v1_mean,
        voi_mean_precision_at_k=voi_mean,
        calibration_band=band,
        passed=voi_mean >= v1_mean,
    )
