"""Shared harness for the P3.2 at-Done wrongness wiring tests (W2-B).

One place builds the level-N Done run so the shadow / container / rubric-axis /
XP suites all exercise the SAME inputs and can only differ in what they assert.
Everything is driven through the real ``handle_done`` — the wiring under test is
an ordering claim (ledger -> corroborator -> S2' -> containers -> XP), and a
unit test of the helpers alone would not catch a mis-ordered call site.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from apollo.handlers.done import handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.ontology import KGGraph, build_node

# The concept slug on the fixture problem. Named because
# `effective_wrongness_level` pairs the ladder flag with
# `interaction_allowed_for_concept`, and an empty allowlist (the default) admits
# it — the tests that pin the pairing set INTERACTION_CONCEPTS themselves.
CONCEPT_SLUG = "bernoulli_principle"

# The wrongness-bearing evidence entry the questioning controller writes (seam
# S2). Two-key entries are the untagged, level-0 shape.
MATERIAL_QUOTE = "Pressure rises wherever the flow speeds up."
CONTRADICTS = "P + 0.5*rho*v**2 = const"


class LedgerRow:
    """Minimal stand-in for a ``QuestionOpportunity`` row.

    Duck-typed exactly like the ORM row `wrongness.ledger_findings` and
    `done._probed_node_ids` read — `last_asked_turn` included, because the
    decision-7 Apollo-elicited guard is `last_asked_turn < turn_id`.
    """

    def __init__(
        self,
        node_id: str,
        *,
        state: str = "understood",
        times_asked: int = 1,
        last_asked_turn: int | None = 2,
        evidence: Any = None,
    ) -> None:
        self.reference_node_id = node_id
        self.state = state
        self.times_asked = times_asked
        self.last_asked_turn = last_asked_turn
        self.evidence = [] if evidence is None else evidence


def tagged_evidence(
    *,
    turn_id: int = 4,
    quote: str = MATERIAL_QUOTE,
    wrongness: str = "contradicts_material",
) -> list[dict[str, Any]]:
    """One tagged S2 entry — the five keys the controller writes."""
    return [
        {
            "turn_id": turn_id,
            "quote": quote,
            "wrongness": wrongness,
            "contradicts": CONTRADICTS,
            "kind": "reversal",
        }
    ]


def contested_ledger(**row_kwargs: Any) -> tuple[LedgerRow, ...]:
    """The default ledger: `eq1` carries a material contradiction, `c1` is clean."""
    return (
        LedgerRow("eq1", evidence=tagged_evidence(), **row_kwargs),
        LedgerRow("c1", evidence=[{"turn_id": 5, "quote": "Steady flow only."}]),
    )


def second_reader(
    *,
    contradicted: bool = True,
    corrected_later: bool = False,
    node_id: str = "eq1",
) -> dict[str, dict[str, bool]]:
    """The corroborator's answer map, as it rides back on ``coverage``."""
    return {
        node_id: {
            "contradicted": contradicted,
            "corrected_later": corrected_later,
            "prompted": True,
        }
    }


def reference_graph() -> KGGraph:
    """Two GRADED nodes so the topic score is non-trivial and both are credited
    at 1.0 — above S2''s 0.6 floor, and a raw 100 so `would_ceiling` is True."""
    return KGGraph(
        nodes=[
            build_node(
                node_type="equation",
                node_id="eq1",
                attempt_id=99,
                source="reference",
                content={"symbolic": CONTRADICTS, "label": "Bernoulli"},
            ),
            build_node(
                node_type="condition",
                node_id="c1",
                attempt_id=99,
                source="reference",
                content={"applies_when": "steady flow", "label": ""},
            ),
        ],
        edges=[],
    )


def coverage_dict(wrongness_map: dict[str, dict[str, bool]] | None) -> dict[str, Any]:
    """A both-covered adjudication verdict, optionally carrying the S4 map."""
    coverage: dict[str, Any] = {
        "per_step": {"eq1": "covered", "c1": "covered"},
        "procedure_scores": {},
        "confidences": {"eq1": 0.9, "c1": 0.9},
    }
    if wrongness_map is not None:
        coverage["wrongness"] = wrongness_map
    return coverage


DEFAULT: Any = object()
"""Sentinel: "use the harness default", so ``None`` stays a meaningful value
(a failed ledger read; an adjudication that returned no wrongness map)."""


async def run_done(
    monkeypatch,
    *,
    level: int,
    ledger: Any = DEFAULT,
    wrongness_map: Any = DEFAULT,
    real_rubric: bool = False,
    real_artifact: bool = False,
    prior_findings: tuple[dict[str, Any], ...] = (),
    extra_patches: list[Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive one Done at ``APOLLO_WRONGNESS_LEVEL=level``.

    Returns ``(student_response, started)`` where ``started`` maps each patched
    attribute to its started mock, so a caller can assert on the kwargs the
    adjudicator / scorer / artifact writer actually received.
    """
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", str(level))
    if ledger is DEFAULT:
        ledger = contested_ledger()
    if wrongness_map is DEFAULT:
        wrongness_map = second_reader()

    db, sess, _attempt, patches = _old_path_patches()
    graph = reference_graph()

    async def _find_problem_with_graph(_db, _cid, _code, *, course_id):
        problem = MagicMock()
        problem.id = "p_code"
        problem.database_id = 42
        problem.concept_id = CONCEPT_SLUG
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    drop = {
        "_find_problem",
        "compute_transcript_coverage_with_spans",
        "compute_topic_score",
        "_question_ledger",
    }
    if real_rubric:
        drop.add("compute_rubric")
    if real_artifact:
        drop.add("write_artifacts")
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch("apollo.handlers.done._question_ledger", new=AsyncMock(return_value=ledger)),
        patch(
            "apollo.handlers.done.compute_transcript_coverage_with_spans",
            new=AsyncMock(return_value=(coverage_dict(wrongness_map), {})),
        ),
        patch(
            "apollo.handlers.done.prior_wrongness_findings",
            new=AsyncMock(return_value=prior_findings),
        ),
    ]
    if real_artifact:
        patches += [
            patch("apollo.handlers.done.write_artifacts", new=AsyncMock(side_effect=_artifact)),
            # A non-None canonical payload also triggers the mastery projection,
            # which is unrelated telemetry with its own failure domain; stub it
            # so the scorecard assertions are not reading through a swallowed
            # AttributeError on the MagicMock session.
            patch("apollo.handlers.done._project_mastery", new=AsyncMock()),
        ]
    patches += extra_patches or []

    started: dict[str, Any] = {}
    for p in patches:
        started[getattr(p, "attribute", repr(p))] = p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=int(sess.id))
    finally:
        for p in reversed(patches):
            p.stop()
    return out, started


async def _artifact(_db, **kwargs: Any) -> dict[str, Any]:
    """Stand in for `write_artifacts` with the REAL builder, minus the INSERT.

    The point of the substitution is that the canonical payload — and therefore
    the scorecard rendered from it — is the genuine
    `artifact_build.build_llm_artifact` output for the topic score this Done
    produced, not a hand-written dict that could agree with a fiction.
    """
    from apollo.grading.artifact_build import build_llm_artifact

    return build_llm_artifact(
        coverage=kwargs["coverage"],
        rubric=kwargs["rubric"],
        latency_ms=kwargs["latency_ms"],
        clarification_trace=[],
        topic_score=kwargs["topic_score"],
    )
