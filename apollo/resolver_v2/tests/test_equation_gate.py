"""Symbolic-blindness gate smoke tests (T8 anomaly #2 fix).

NLI entailment cannot see symbolic coefficients — the F1c calibration
credited the student's ``x = v0*t + a*t^2`` against the half-factor
reference equation at entailment 0.98
(``campaign/out/resolver-v2/REPORT.md`` §3/§5, misc.forgets_half_factor).

These tests pin the gate:

* an EQUATION-type node can never earn full credit from text evidence alone
  (``nli``/``lexical_skip`` credit capped at
  ``params.equation_text_credit_cap``, ``source="equation_cap"``);
* full credit stays reachable ONLY via the v1 symbolic floor
  (``v1_resolved_keys`` — the SymPy resolution path);
* non-equation nodes are byte-identical to the pre-gate behavior;
* the engine holds capped nodes gray-zone-eligible, and a verified grayzone
  quote upgrades to at most ``grayzone_credit`` (0.7) — never 1.0 (quotes
  are text too);
* the T8 wrong-coefficient anomaly is a locked regression fixture.

Same fake-injection idiom as ``test_scoring.py`` — zero model loads.
"""

from __future__ import annotations

import pytest

from apollo.graph_compare.canonical import (
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult
from apollo.resolver_v2.config import ResolverV2Params, load_params
from apollo.resolver_v2.engine import run_resolver_v2
from apollo.resolver_v2.grayzone import GrayzoneVerdict
from apollo.resolver_v2.scoring import score_nodes
from apollo.resolver_v2.types import RefNode, SelectFn, Window

# Same pinned scoring-math surface as test_scoring.py (the class-field
# defaults are T8's fitted values, asserted in test_config_types.py).
_PARAMS = ResolverV2Params(t_low=0.40, t_mid=0.75, t_high=0.90, alpha=0.85)

_W0 = Window(
    index=0,
    turn_index=0,
    text="The position formula is x = v0*t + a*t^2 for constant acceleration.",
)


def _nli(entailment: float = 0.0, contradiction: float = 0.0) -> NLIResult:
    neutral = max(0.0, 1.0 - entailment - contradiction)
    label = max(
        ("entailment", entailment), ("neutral", neutral), ("contradiction", contradiction),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, entailment, contradiction, neutral, "fake-nli")


def _select_by_lex(lex_by_window: dict[int, float]) -> SelectFn:
    def select(windows, view_text, k):  # noqa: ANN001 - SelectFn shape
        ranked = sorted(
            ((w.index, lex_by_window.get(w.index, 0.0)) for w in windows),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return tuple(ranked[:k])

    return select


def _eq_node(key: str = "eq.kinematics_position") -> RefNode:
    return RefNode(
        canonical_key=key,
        node_type="equation",
        label="Position under constant acceleration",
        views=(
            "Position under constant acceleration",
            "x = v0*t + 1/2*a*t^2",
        ),
    )


def _score_one(
    node: RefNode,
    *,
    entailment: float,
    lex: float = 0.5,
    params: ResolverV2Params = _PARAMS,
    v1_resolved_keys: frozenset[str] = frozenset(),
):
    scripted = {(_W0.text, view): _nli(entailment, 0.01) for view in node.views}
    scores, _ = score_nodes(
        (_W0,), (node,),
        nli=FakeNLIAdjudicator(scripted), params=params,
        v1_resolved_keys=v1_resolved_keys, select_fn=_select_by_lex({0: lex}),
    )
    return scores[0]


# --- the cap: text evidence alone never fully credits an equation node ---------


@pytest.mark.parametrize("entailment", [0.98, 0.95, 0.85])
def test_equation_high_nli_capped_at_gray(entailment: float):
    """Full- and mid-tier fused scores on an equation node are capped at the
    gray-band credit; the raw score and argmax pair stay on the trace."""
    result = _score_one(_eq_node(), entailment=entailment)
    assert result.credit == pytest.approx(_PARAMS.equation_text_credit_cap)
    assert result.credit < 0.7  # never mid, never full
    assert result.source == "equation_cap"
    expected_fused = 0.85 * entailment + 0.15 * 0.5
    assert result.score == pytest.approx(expected_fused)  # score is NOT capped
    assert result.best is not None  # argmax pair kept for the trace


def test_equation_gray_band_score_passes_through_uncapped():
    """A fused score already in the gray band earns the same 0.3 as before —
    the cap only BINDS above it, so source stays 'nli'."""
    result = _score_one(_eq_node(), entailment=0.50)
    assert result.credit == pytest.approx(0.3)
    assert result.source == "nli"


def test_equation_v1_symbolic_floor_keeps_full_credit():
    """The v1 SymPy-resolved path is the ONLY route to full equation credit:
    the floor is applied after the cap and restores 1.0."""
    result = _score_one(
        _eq_node(), entailment=0.98,
        v1_resolved_keys=frozenset({"eq.kinematics_position"}),
    )
    assert (result.credit, result.source) == (1.0, "v1_floor")


def test_non_equation_node_unaffected():
    """The same high entailment on a non-equation node still earns full
    credit via NLI — the gate is equation-type-scoped."""
    node = RefNode(
        canonical_key="proc.compute_distance",
        node_type="procedure_step",
        label="Substitute v0, a, and t into the position formula",
        views=("Substitute v0, a, and t into the position formula",),
    )
    result = _score_one(node, entailment=0.98)
    assert result.credit == 1.0
    assert result.source == "nli"


def test_equation_lexical_skip_also_capped():
    """The lexical-only degrade path (nli=None) is text evidence too: a high
    lexical score on an equation node is capped identically."""
    node = _eq_node()
    scores, pair_count = score_nodes(
        (_W0,), (node,),
        nli=None, params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.95}),
    )
    (result,) = scores
    assert result.credit == pytest.approx(_PARAMS.equation_text_credit_cap)
    assert result.source == "equation_cap"
    assert result.best is None
    assert pair_count == 0


def test_t8_anomaly_wrong_coefficient_regression():
    """THE T8 anomaly, locked: the persona writes x = v0*t + a*t^2 (missing
    the 1/2), NLI scores it 0.98 against the half-factor reference — that
    paraphrase must never reach full (or even mid) credit again."""
    result = _score_one(_eq_node(), entailment=0.98, lex=0.9)
    assert result.score >= _PARAMS.t_high  # fused says "taught" — the blindness
    assert result.credit == pytest.approx(_PARAMS.equation_text_credit_cap)
    assert result.credit < 1.0
    assert result.credit < 0.7
    assert result.source == "equation_cap"


# --- engine: capped nodes stay grayzone-eligible, upgrade tops out at 0.7 -------


class _VerifyingGrayzone:
    """Stub GrayzoneFn: confirms every queried key with a verbatim quote."""

    def __init__(self, quote: str):
        self.quote = quote
        self.queried_keys: list[str] = []

    def __call__(self, queries, transcript):  # noqa: ANN001 - GrayzoneFn shape
        self.queried_keys.extend(q.canonical_key for q in queries)
        return tuple(
            GrayzoneVerdict(
                canonical_key=q.canonical_key, taught=True,
                quote=self.quote, verified=True,
            )
            for q in queries
        )


def test_engine_capped_equation_grayzone_upgrade_never_full_credit():
    """End-to-end: entailment 1.0 on an equation node -> capped at gray ->
    the engine queries grayzone for it -> verified upgrade lands at
    grayzone_credit (0.7), NEVER 1.0."""
    turn = "The position formula is x = v0*t + a*t^2 for constant acceleration."
    label = "Position under constant acceleration"
    reference = ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="eq.kinematics_position",
                node_type="equation",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
        ),
        edges=(),
        paths=(ReferencePathView(canonical_keys=("eq.kinematics_position",)),),
    )
    payload = {
        "concept_id": "equation_gate_test",  # not in the committed views cache
        "id": "t8_anomaly",
        "reference_solution": [
            {"id": "s1", "entity_key": "eq.kinematics_position", "content": {"label": label}},
        ],
    }
    grayzone = _VerifyingGrayzone(quote=turn)
    result = run_resolver_v2(
        student_turns=(turn,),
        reference_graph=reference,
        problem_payload=payload,
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=FakeNLIAdjudicator({(turn, label): _nli(1.0, 0.0)}),
        grayzone_fn=grayzone,
        params=ResolverV2Params(),  # fitted defaults, grayzone_credit=0.7
        select_fn=lambda windows, view_text, k: ((0, 1.0),),
    )
    (node,) = result.node_scores
    assert grayzone.queried_keys == ["eq.kinematics_position"]  # gray-eligible
    assert node.credit == pytest.approx(ResolverV2Params().grayzone_credit)
    assert node.credit < 1.0  # a verified quote is text too
    assert node.source == "grayzone"
    assert result.grayzone_used is True
    assert result.node_coverage == pytest.approx(0.7)


def test_engine_capped_equation_without_grayzone_stays_at_cap():
    """Deterministic replay mode (grayzone_fn=None): the capped equation node
    keeps the cap value all the way into node_coverage."""
    turn = "The position formula is x = v0*t + a*t^2 for constant acceleration."
    label = "Position under constant acceleration"
    reference = ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="eq.kinematics_position",
                node_type="equation",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
        ),
        edges=(),
        paths=(ReferencePathView(canonical_keys=("eq.kinematics_position",)),),
    )
    payload = {
        "concept_id": "equation_gate_test",
        "id": "t8_anomaly_no_grayzone",
        "reference_solution": [
            {"id": "s1", "entity_key": "eq.kinematics_position", "content": {"label": label}},
        ],
    }
    params = ResolverV2Params()
    result = run_resolver_v2(
        student_turns=(turn,),
        reference_graph=reference,
        problem_payload=payload,
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=FakeNLIAdjudicator({(turn, label): _nli(1.0, 0.0)}),
        grayzone_fn=None,
        params=params,
        select_fn=lambda windows, view_text, k: ((0, 1.0),),
    )
    (node,) = result.node_scores
    assert node.credit == pytest.approx(params.equation_text_credit_cap)
    assert node.source == "equation_cap"
    assert result.node_coverage == pytest.approx(params.equation_text_credit_cap)


# --- config surface ---------------------------------------------------------------


def test_cap_param_default_is_gray_band_credit(monkeypatch):
    monkeypatch.delenv("APOLLO_RESOLVER_V2_EQUATION_TEXT_CREDIT_CAP", raising=False)
    assert ResolverV2Params().equation_text_credit_cap == 0.3  # == scoring._CREDIT_GRAY
    assert load_params().equation_text_credit_cap == 0.3


def test_cap_param_env_override_and_malformed_fallback(monkeypatch):
    monkeypatch.setenv("APOLLO_RESOLVER_V2_EQUATION_TEXT_CREDIT_CAP", "0.5")
    assert load_params().equation_text_credit_cap == 0.5
    monkeypatch.setenv("APOLLO_RESOLVER_V2_EQUATION_TEXT_CREDIT_CAP", "not-a-float")
    assert load_params().equation_text_credit_cap == 0.3
