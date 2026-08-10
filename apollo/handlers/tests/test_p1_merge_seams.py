"""Integration seams of the 2026-08-07 bimodal-fix P1+P2 build.

Every assertion here spans a boundary that NO single build slice owned, so a
regression on either side passes that slice's own suite and only breaks in
production. Each slice tested its own half:

* S1 (`overseer/transcript_coverage`) owns the adjudicator and its
  `tally_context` normalizer; S3 (`handlers/done`) owns the producer of those
  rows. Both are tested against synthetic inputs — nobody tested that the
  producer's output survives the consumer unchanged.
* S4 (`overseer/diagnostic`) owns `generate_diagnostic`; S3 owns its call site.
* S3 owns the one serializer; the artifact writer and the served payload are two
  different call paths through it.
* S2 (`smart_questions/selection` + `controller`) derives
  `graded_topic_total` / `open_graded_topics` from the tally, and S3 derives the
  SAME two served fields independently in `handlers/chat`. Two live derivations
  of one cross-repo contract must agree, forever.
* S2 caps asks per node in CODE (`MAX_ASKS_PER_NODE`) and reserves budget for
  graded nodes; S1 hands the grader the same `times_asked` as prompt data. The
  cap must exist in exactly one of those two places.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from apollo.grading.artifact_build import LEDGER_STATUS_UNPROBED, build_llm_artifact
from apollo.handlers import done as done_module
from apollo.handlers.chat import _graded_topic_counts
from apollo.handlers.done import _tally_context
from apollo.ontology import KGGraph, NodeType, build_node
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import (
    REFERENCE_TEXT_CREDIT_THRESHOLD,
    compute_centrality,
    compute_topic_score,
)
from apollo.overseer.topic_score_serialize import serialize_topics
from apollo.overseer.transcript_coverage import (
    TallyContextEntry,
    _build_rubric_items,
    _normalize_tally_context,
    build_system_prompt,
    build_user_message,
)
from apollo.smart_questions import selection
from apollo.smart_questions.controller import CoveredTopic
from apollo.smart_questions.unified import TallyState

pytestmark = pytest.mark.unit


# --- shared fixtures -------------------------------------------------------


_CONTENT_BY_TYPE: dict[str, dict[str, Any]] = {
    "definition": {"concept": "concept text", "meaning": "meaning text"},
    "variable_mapping": {"term": "term text", "symbol": "v"},
    "procedure_step": {"action": "action text", "purpose": "purpose text"},
    "condition": {"applies_when": "condition text"},
    "simplification": {"transformation": "transform text", "applies_when": "always"},
    "equation": {"symbolic": "p + q = c"},
}


def _node(node_id: str, node_type: NodeType):
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content=_CONTENT_BY_TYPE[node_type],
    )


class _LedgerRow:
    """Minimal stand-in for a ``QuestionOpportunity`` ORM row."""

    def __init__(
        self,
        node_id: str,
        *,
        state: str = "understood",
        times_asked: int = 1,
        evidence: Any = None,
    ) -> None:
        self.reference_node_id = node_id
        self.state = state
        self.times_asked = times_asked
        self.evidence = evidence


# --- seam (a): done.py produces exactly what transcript_coverage accepts ----


def test_done_tally_rows_survive_the_adjudicator_normalizer_unchanged():
    """`done._tally_context` -> `transcript_coverage._normalize_tally_context`
    must be lossless.

    S1's normalizer is untrusted-by-construction: a row it cannot read is
    dropped and a field it dislikes is replaced by a safe default, SILENTLY. So
    a producer/consumer drift (a renamed key, an int-as-str `times_asked`, a
    state the grader's vocabulary no longer contains) does not raise — it
    quietly strips the whole point of P1.3 and the grade goes back to ignoring
    the live tally. Only a round-trip pins that.
    """
    rows = [
        _LedgerRow(
            "eq1",
            state="understood",
            times_asked=2,
            evidence=[{"turn_id": 3, "quote": "the student's own words"}],
        ),
        _LedgerRow("st2", state="tentative", times_asked=1, evidence=[]),
        _LedgerRow("cd3", state="conflicting", times_asked=0, evidence=None),
        _LedgerRow("sm4", state="missing", times_asked=0, evidence=[]),
    ]
    produced = _tally_context(rows)

    assert _normalize_tally_context(produced, {"eq1", "st2", "cd3", "sm4"}) == produced


def test_producer_emits_exactly_the_keys_the_contract_declares():
    produced = _tally_context([_LedgerRow("eq1")])

    assert set(produced[0]) == set(TallyContextEntry.__annotations__)


def test_every_state_done_can_emit_is_in_the_graders_vocabulary():
    """`done._tally_context` copies `QuestionOpportunity.state` through verbatim;
    the normalizer rewrites anything outside its own `_VALID_STATES` to
    "missing". A new tally state added on the questioning side would therefore
    reach the grader as "missing" — the WORST reading of it — instead of failing
    loudly. Pin the two vocabularies together."""
    from apollo.smart_questions.controller import _VALID_STATES

    rows = [_LedgerRow(f"n{i}", state=state) for i, state in enumerate(sorted(_VALID_STATES))]
    produced = _tally_context(rows)
    normalized = _normalize_tally_context(produced, {row["node_id"] for row in produced})

    assert [row["state"] for row in normalized] == [row["state"] for row in produced]


def test_tally_context_default_is_none_and_none_is_byte_identical():
    """The contract's "default None = byte-identical" half, asserted across the
    S1/S3 boundary rather than inside S1's own prompt tests."""
    graph = KGGraph(nodes=[_node("eq1", "equation")], edges=[])
    items = _build_rubric_items(graph)
    problem = MagicMock()

    assert build_system_prompt(problem, reference_items=items) == build_system_prompt(
        problem, tally_context=None, reference_items=items
    )
    assert build_user_message(problem, items, ()) == build_user_message(
        problem, items, (), tally_context=None
    )
    # An EMPTY ledger (a real signal, deliberately not None) is byte-identical too.
    assert build_user_message(problem, items, (), tally_context=_tally_context([])) == (
        build_user_message(problem, items, ())
    )


# --- seam (b): done.py's call site vs S4's generate_diagnostic signature ----


def _call_site_kwargs(module: Any, func_name: str) -> set[str]:
    """The keyword names `module`'s source passes to `func_name`.

    Read from the SOURCE, not from a mock: every `done.py` test patches
    `generate_diagnostic`, so a signature change on S4's side and a call-site
    change on S3's side can disagree without a single test going red.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func if isinstance(node.func, ast.Name) else None
        # `asyncio.to_thread(generate_diagnostic, **kwargs)` passes the callee
        # as the first positional argument.
        if target is None and node.args and isinstance(node.args[0], ast.Name):
            target = node.args[0]
        if target is not None and target.id == func_name:
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError(f"no call to {func_name} found in {module.__name__}")


def test_done_calls_generate_diagnostic_with_a_signature_it_accepts():
    passed = _call_site_kwargs(done_module, "generate_diagnostic")
    parameters = inspect.signature(generate_diagnostic).parameters

    assert passed, "done.py must still call generate_diagnostic by keyword"
    # Nothing passed that the callee would reject...
    assert passed <= set(parameters)
    # ...and nothing the callee requires left unpassed.
    required = {
        name
        for name, param in parameters.items()
        if param.default is inspect.Parameter.empty and param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert required <= passed
    # P2.1: the narrative is generated FROM the final per-node verdicts.
    assert "topic_score" in passed


# --- seam (c): one serializer, artifact and served payload agree ------------


def _scored_result():
    """A topic score containing BOTH P1.2b shapes: an unprobed node (excluded
    from the grade) and a probed node below the D2 reveal threshold."""
    graph = KGGraph(
        nodes=[
            _node("eq1", "equation"),
            _node("st2", "procedure_step"),
            _node("cd3", "condition"),
            _node("sm4", "simplification"),
        ],
        edges=[],
    )
    coverage = {
        "per_step": {"eq1": "covered", "st2": "missing", "cd3": "missing", "sm4": "covered"},
        "procedure_scores": {},
    }
    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=graph.nodes,
        centrality=compute_centrality(graph),
        asked_node_ids=frozenset({"eq1", "st2", "sm4"}),
    )
    return result


def test_artifact_and_served_payload_share_one_serializer_output():
    result = _scored_result()

    artifact = build_llm_artifact(
        coverage={
            "per_step": {"eq1": "covered", "st2": "missing", "cd3": "missing", "sm4": "covered"},
            "confidences": {},
        },
        rubric={"overall": {"score": result.score}},
        latency_ms=1,
        clarification_trace=[],
        topic_score=result,
    )
    served = serialize_topics(result)

    assert artifact["scores"]["topic_score"]["topics"] == served


def test_unprobed_and_reference_text_are_coherent_on_both_surfaces():
    result = _scored_result()
    served = {topic["canonical_key"]: topic for topic in serialize_topics(result)}
    artifact = build_llm_artifact(
        coverage={
            "per_step": {"eq1": "covered", "st2": "missing", "cd3": "missing", "sm4": "covered"},
            "confidences": {},
        },
        rubric={"overall": {"score": result.score}},
        latency_ms=1,
        clarification_trace=[],
        topic_score=result,
    )
    ledger = {row["canonical_key"]: row["status"] for row in artifact["node_ledger"]}

    # The unprobed node leaves the grade on BOTH surfaces, under the same name.
    assert served["cd3"]["status"] == LEDGER_STATUS_UNPROBED == "unprobed"
    assert served["cd3"]["weight"] == 0.0
    assert ledger["cd3"] == LEDGER_STATUS_UNPROBED
    # ...and is never handed the answer it was excluded from being graded on.
    assert served["cd3"]["reference_text"] is None

    # A graded topic below the D2 threshold reveals its statement; one at or
    # above it never does. Both facts read off the SERVED payload, which is the
    # artifact's `topics` list by the test above.
    assert served["st2"]["credit"] < REFERENCE_TEXT_CREDIT_THRESHOLD
    assert served["st2"]["reference_text"]
    assert served["eq1"]["credit"] >= REFERENCE_TEXT_CREDIT_THRESHOLD
    assert served["eq1"]["reference_text"] is None


# --- seam (e): two live derivations of graded_topic_total/open_graded_topics


def _spec_to_graph_problem_and_state(
    spec: list[tuple[str, NodeType, str, int]],
) -> tuple[KGGraph, Any, tuple[TallyState, ...], tuple[CoveredTopic, ...]]:
    """One spec -> the four views the two derivations read.

    `handlers/chat` walks `problem.reference_solution` + the controller's
    covered snapshot; `smart_questions/selection` walks the reference graph +
    the tally state. `Problem.to_kg_graph` builds one node per reference step
    (`node_id = step.id`, `node_type = step.entry_type`), so the two walks are
    only ever equivalent if BOTH sides keep reading the same fields.
    """
    graph = KGGraph(nodes=[_node(nid, ntype) for nid, ntype, _, _ in spec], edges=[])
    problem = SimpleNamespace(
        id=1,
        reference_solution=[SimpleNamespace(id=nid, entry_type=ntype) for nid, ntype, _, _ in spec],
    )
    state = tuple(
        TallyState(node_id=nid, label=nid, status=status, times_asked=asked)  # type: ignore[arg-type]
        for nid, _, status, asked in spec
    )
    covered = tuple(
        CoveredTopic(node_id=nid, display_name=nid)
        for nid, _, status, _ in spec
        if status == "understood"
    )
    return graph, problem, state, covered


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(
            [
                ("eq1", "equation", "missing", 0),
                ("st2", "procedure_step", "missing", 0),
                ("df3", "definition", "missing", 0),
            ],
            id="nothing-understood",
        ),
        pytest.param(
            [
                ("eq1", "equation", "understood", 1),
                ("st2", "procedure_step", "tentative", 1),
                ("df3", "definition", "understood", 1),
                ("vm4", "variable_mapping", "missing", 0),
            ],
            id="ungraded-understood-does-not-close-the-meter",
        ),
        pytest.param(
            [
                ("eq1", "equation", "understood", 2),
                ("cd2", "condition", "understood", 1),
            ],
            id="all-graded-understood",
        ),
        pytest.param(
            [
                # At the ask cap but never understood: still OPEN on both sides.
                ("eq1", "equation", "tentative", selection.MAX_ASKS_PER_NODE),
                ("sm2", "simplification", "conflicting", selection.MAX_ASKS_PER_NODE),
            ],
            id="exhausted-but-open",
        ),
        pytest.param([("df1", "definition", "understood", 1)], id="no-graded-nodes"),
    ],
)
def test_chat_meter_counts_match_the_questioning_engines_own_policy(spec):
    """`handlers/chat._graded_topic_counts` (S3) and `SelectionPolicy` (S2) are
    two independent derivations of the SAME two served fields — the controller
    already returns `decision.graded_topic_total` / `open_graded_topics` from
    the policy, and chat.py recomputes them from the problem + the covered
    snapshot. They are equal today; this makes them stay equal, because the
    student-UI's Done guard and the questioning loop's graded-first reserve must
    never disagree about how much of the problem is still open.
    """
    graph, problem, state, covered = _spec_to_graph_problem_and_state(spec)
    policy = selection.build_selection_policy(
        reference_graph=graph,
        tally_state=state,
        questions_asked=sum(item.times_asked for item in state),
        cap=8,
    )

    assert _graded_topic_counts(problem, covered) == (
        policy.graded_topic_total,
        policy.open_graded_topics,
    )


# --- seam (f): the 2-asks cap lives in exactly one place -------------------


def test_graded_reserve_holds_one_slot_per_node_not_one_per_remaining_ask():
    """P1.2a reserves budget so every graded node is PROBED before ungraded
    territory. The reserve is one question per still-probeable graded node — if
    it were `MAX_ASKS_PER_NODE` per node it would double-count the cap, strand
    the ungraded nodes permanently, and (once `askable_ids` empties) auto-done
    the session early."""
    graph = KGGraph(
        nodes=[
            _node("eq1", "equation"),
            _node("st2", "procedure_step"),
            _node("df3", "definition"),
        ],
        edges=[],
    )
    state = (
        # Already at the code cap and NOT understood: unaskable, still open.
        TallyState(node_id="eq1", label="eq1", status="tentative", times_asked=2),
        TallyState(node_id="st2", label="st2", status="missing", times_asked=0),
        TallyState(node_id="df3", label="df3", status="missing", times_asked=0),
    )
    policy = selection.build_selection_policy(
        reference_graph=graph, tally_state=state, questions_asked=2, cap=8
    )

    assert selection.MAX_ASKS_PER_NODE == 2
    assert policy.reserved_for_graded == 1  # st2 only — never 2, never 2*2
    assert "eq1" not in policy.askable_ids  # the cap is enforced in code
    assert policy.open_graded_topics == 2  # ...but the node is still unaddressed
    assert policy.askable_ids == ("st2", "df3")  # budget is not starved yet


def test_the_adjudication_prompt_rule_does_not_depend_on_times_asked():
    """The grader is given `times_asked` as DATA (P1.3) but must not re-derive a
    cap from it: the ask cap is S2's code-level rail. If a future prompt edit
    made the RULE vary with `times_asked`, the two systems would contradict —
    e.g. the grader penalising a node the controller refused to ask again."""
    graph = KGGraph(nodes=[_node("eq1", "equation")], edges=[])
    items = _build_rubric_items(graph)
    problem = MagicMock()
    fresh = [{"node_id": "eq1", "state": "understood", "times_asked": 0, "student_quote": "q"}]
    at_cap = [
        {
            "node_id": "eq1",
            "state": "understood",
            "times_asked": selection.MAX_ASKS_PER_NODE,
            "student_quote": "q",
        }
    ]

    assert build_system_prompt(
        problem, tally_context=fresh, reference_items=items
    ) == build_system_prompt(problem, tally_context=at_cap, reference_items=items)
    # The data block, by contrast, does carry the real count.
    assert build_user_message(problem, items, (), tally_context=fresh) != build_user_message(
        problem, items, (), tally_context=at_cap
    )
