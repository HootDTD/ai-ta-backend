"""S2′ + the ladder — the table-driven truth table for `overseer/wrongness.py`.

S2′ (spec 2026-08-12 §2.2) is the ONLY consequence predicate in P3.2, and it
exists because the naive rule it replaces ("final state `conflicting` ⇒ dock")
scored 0/2 on the only two high-credit prod cases it fired on. The three named
regression attempts are encoded here as PREDICATE cases (the committed
transcript fixtures are W0's; these tests pin the semantics, not the data):

* **86** — zero student transcript: no evidence, therefore no finding at all.
* **167** — the student self-corrects after two probes: `corrected_later`, so it
  is reported, never corroborated, and it is the one that earns the XP bonus.
* **124** — a graded node whose tally state went sticky `conflicting`: state
  alone never corroborates; S2′ keys on evidence recency + the second reader.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from apollo.overseer import topic_score, wrongness

pytestmark = pytest.mark.unit

NODE = "eq.momentum"
OTHER = "eq.energy"


@dataclass
class _Row:
    """A duck-typed `QuestionOpportunity` — `ledger_findings` never touches the ORM.

    Every field the reader coerces is annotated `Any` ON PURPOSE: the column it
    stands in for is free-form JSONB (or an ORM attribute that can hold whatever
    a prior write left there), and `test_non_string_field_types_coerce` proves
    the reader is total by handing it garbage. A narrower annotation here would
    make the type-checker reject the very cases the module promises to survive.
    """

    reference_node_id: Any = NODE
    state: str = "understood"
    times_asked: Any = 1
    last_asked_turn: Any = 1
    evidence: Any = field(default_factory=list)


def _entry(
    *,
    turn_id: int = 2,
    quote: str = "Momentum is conserved only when energy is.",
    wrongness_label: str | None = wrongness.WRONGNESS_MATERIAL,
    contradicts: str | None = "Momentum is conserved when net external force is zero.",
    kind: str | None = "wrong-condition",
) -> dict[str, Any]:
    """A persisted evidence entry (S2). `wrongness_label=None` ⇒ the untagged
    two-key entry every pre-feature and level-0 turn writes."""
    entry: dict[str, Any] = {"turn_id": turn_id, "quote": quote}
    if wrongness_label is not None:
        entry["wrongness"] = wrongness_label
        entry["contradicts"] = contradicts
        entry["kind"] = kind
    return entry


def _select(
    rows: list[_Row],
    *,
    credits: dict[str, float] | None = None,
    second_reader: dict[str, dict[str, bool]] | None = None,
    graded: set[str] | None = None,
    raw_score: int = 90,
) -> tuple[wrongness.WrongnessFinding, ...]:
    reader: Mapping[str, Mapping[str, bool]] = {
        NODE: {"contradicted": True, "corrected_later": False, "prompted": True}
    }
    if second_reader is not None:
        reader = second_reader
    return wrongness.select_findings(
        findings=wrongness.ledger_findings(rows),
        credits={NODE: 1.0} if credits is None else credits,
        second_reader=reader,
        graded_node_ids={NODE} if graded is None else graded,
        raw_score=raw_score,
    )


def _one(rows: list[_Row], **kwargs: Any) -> wrongness.WrongnessFinding:
    selected = _select(rows, **kwargs)
    assert len(selected) == 1
    return selected[0]


# ---------------------------------------------------------------------------
# The truth table
# ---------------------------------------------------------------------------

# (label, wrongness, credit, latest?, reader, graded?, expected corroborated)
_TRUTH_TABLE: list[tuple[str, str, float, bool, dict[str, bool] | None, bool, bool]] = [
    (
        "all five clauses hold",
        wrongness.WRONGNESS_MATERIAL,
        1.0,
        True,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        True,
        True,
    ),
    (
        "credit exactly at the 0.6 anchor",
        wrongness.WRONGNESS_MATERIAL,
        0.6,
        True,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        True,
        True,
    ),
    (
        "self-contradiction is a dialogue artifact, not material wrongness",
        wrongness.WRONGNESS_SELF,
        1.0,
        True,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        True,
        False,
    ),
    (
        "uncredited node has no unearned credit to contest",
        wrongness.WRONGNESS_MATERIAL,
        0.0,
        True,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        True,
        False,
    ),
    (
        "INTERACTION5 aside cap (0.5) sits below the floor — levers cannot stack",
        wrongness.WRONGNESS_MATERIAL,
        0.5,
        True,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        True,
        False,
    ),
    (
        "superseded by a later evidence entry",
        wrongness.WRONGNESS_MATERIAL,
        1.0,
        False,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        True,
        False,
    ),
    (
        "attempt 167: second reader says the student fixed it",
        wrongness.WRONGNESS_MATERIAL,
        1.0,
        True,
        {"contradicted": True, "corrected_later": True, "prompted": True},
        True,
        False,
    ),
    (
        "second reader denies the tally's claim",
        wrongness.WRONGNESS_MATERIAL,
        1.0,
        True,
        {"contradicted": False, "corrected_later": False, "prompted": True},
        True,
        False,
    ),
    (
        "second reader abstained (fail-safe = miss)",
        wrongness.WRONGNESS_MATERIAL,
        1.0,
        True,
        None,
        True,
        False,
    ),
    (
        "definition-type node: reported for the narrative, never scoreable",
        wrongness.WRONGNESS_MATERIAL,
        1.0,
        True,
        {"contradicted": True, "corrected_later": False, "prompted": True},
        False,
        False,
    ),
]


@pytest.mark.parametrize(
    ("label", "wrongness_label", "credit", "latest", "reader", "graded", "expected"),
    _TRUTH_TABLE,
    ids=[case[0] for case in _TRUTH_TABLE],
)
def test_s2_prime_truth_table(
    label: str,
    wrongness_label: str,
    credit: float,
    latest: bool,
    reader: dict[str, bool] | None,
    graded: bool,
    expected: bool,
) -> None:
    evidence = [_entry(turn_id=2, wrongness_label=wrongness_label)]
    if not latest:
        evidence.append(
            _entry(turn_id=4, quote="Actually, only net external force.", wrongness_label=None)
        )

    finding = _one(
        [_Row(evidence=evidence)],
        credits={NODE: credit},
        second_reader={NODE: reader} if reader is not None else {},
        graded={NODE} if graded else set(),
    )

    assert finding.corroborated is expected, label
    assert finding.node_id == NODE
    assert finding.quote == evidence[0]["quote"]


def test_non_latest_evidence_never_corroborates() -> None:
    """`conflicting` is a STICKY final state: "the state was never upgraded"
    mistakes the tally's failure to relabel for the student's failure to revise.
    S2′ therefore keys on evidence recency."""
    rows = [
        _Row(
            state="conflicting",
            evidence=[
                _entry(turn_id=2),
                _entry(
                    turn_id=5,
                    quote="Net external force zero is the condition.",
                    wrongness_label=None,
                ),
            ],
        )
    ]
    findings = _select(rows)

    assert [f.corroborated for f in findings] == [False]
    assert findings[0].quote == "Momentum is conserved only when energy is."


def test_corrected_later_never_corroborates() -> None:
    """Attempt 167 — 'So I guess I was wrong about governance…' after two probes.
    The student is celebrated, not demoted: reported, resolved, never corroborated."""
    finding = _one(
        [_Row(evidence=[_entry()])],
        second_reader={NODE: {"contradicted": True, "corrected_later": True, "prompted": True}},
    )

    assert finding.corroborated is False
    assert finding.resolved is True
    assert finding.would_ceiling is False


def test_ungraded_type_never_corroborates_but_still_reports() -> None:
    """4 of the 6 clean prod detections land on `definition`-type nodes, which
    `topic_score._GRADED_NODE_TYPES` excludes. They may carry wrongness for the
    narrative and the teacher surfaces and may never move the score — widening
    the graded denominator is P1.4's decision."""
    finding = _one([_Row(evidence=[_entry()])], graded=set())

    assert finding.corroborated is False
    assert finding.quote


@pytest.mark.parametrize("credit", [0.0, 0.1, 0.5, 0.59])
def test_credit_below_0_6_never_corroborates(credit: float) -> None:
    assert _one([_Row(evidence=[_entry()])], credits={NODE: credit}).corroborated is False


def test_absent_second_reader_row_never_corroborates() -> None:
    """P0.5 abstain-not-zero, carried into wrongness: the corroborator's SILENCE
    can never create a penalty."""
    assert _one([_Row(evidence=[_entry()])], second_reader={}).corroborated is False
    assert (
        _one(
            [_Row(evidence=[_entry()])], second_reader={OTHER: {"contradicted": True}}
        ).corroborated
        is False
    )


def test_non_bool_second_reader_values_never_corroborate() -> None:
    """Mirrors `_verdict_bool`: only a genuine `True` counts."""
    reader = {NODE: {"contradicted": "true", "corrected_later": 0, "prompted": 1}}
    assert _one([_Row(evidence=[_entry()])], second_reader=reader).corroborated is False


def test_malformed_second_reader_row_never_corroborates() -> None:
    assert _one([_Row(evidence=[_entry()])], second_reader={NODE: None}).corroborated is False


def test_unparseable_credit_reads_as_zero() -> None:
    assert _one([_Row(evidence=[_entry()])], credits={NODE: "high"}).corroborated is False


@pytest.mark.parametrize(
    ("raw_score", "expected"),
    [(100, True), (85, True), (84, False), (60, False), (0, False)],
)
def test_would_ceiling_only_above_84(raw_score: int, expected: bool) -> None:
    """`would_ceiling` is the level-1 shadow counterfactual and moves nothing.
    At or below the ceiling `min(raw, 84)` is a no-op, so it must not fire."""
    assert _one([_Row(evidence=[_entry()])], raw_score=raw_score).would_ceiling is expected


def test_would_ceiling_requires_corroboration() -> None:
    finding = _one([_Row(evidence=[_entry()])], second_reader={}, raw_score=100)
    assert (finding.corroborated, finding.would_ceiling) == (False, False)


@pytest.mark.parametrize(
    ("last_asked_turn", "claim_turn", "expected"),
    [(1, 2, True), (0, 2, True), (2, 2, True), (4, 2, True), (None, 2, False)],
)
def test_apollo_elicited_requires_apollo_to_have_asked_about_the_node(
    last_asked_turn: int | None, claim_turn: int, expected: bool
) -> None:
    """Decision 7 amendment: the XP bonus only ever rewards a contradiction
    APOLLO drew out. 'Assert something wrong on a node Apollo never asked about,
    then fix it' is a farmable path and earns nothing — the population that would
    farm it is demonstrably present in the prod export.

    The guard is a PRESENCE test, not `last_asked_turn < claim_turn`; see
    `select_findings`' docstring for why the turn comparison is both
    unimplementable as spec'd (no correction turn on the ledger) and
    self-defeating (a later probe flips it off). Only `None` — Apollo never
    asked — is not elicited."""
    finding = _one(
        [_Row(last_asked_turn=last_asked_turn, evidence=[_entry(turn_id=claim_turn)])],
    )
    assert finding.apollo_elicited is expected


def test_a_probe_AFTER_the_wrong_claim_still_counts_as_elicited() -> None:
    """THE regression this exists for. The ordinary challenge loop is: student
    errs at turn 2, L2a sorts the contested node to the front, Apollo probes it
    at turn 3, the student fixes it. `last_asked_turn` (3) is then GREATER than
    the claim turn (2).

    Under the old `last_asked_turn < turn_id` reading that made the finding
    NOT elicited, so the decision-7 bonus never paid in exactly the population
    level 2's probe priority exists to create — the more Apollo elicited, the
    less likely the flag was true. The bonus must fire here."""
    finding = _one(
        [_Row(last_asked_turn=3, evidence=[_entry(turn_id=2)])],
        second_reader={NODE: {"contradicted": True, "corrected_later": True}},
    )

    assert (finding.resolved, finding.apollo_elicited) == (True, True)


def test_a_non_mapping_second_reader_never_raises() -> None:
    """`second_reader` is `coverage["wrongness"]` — LLM-shaped data that
    `validate_coverage_verdict` admits as an optional key without checking its
    type. A raise here lands on the grade path AFTER the Done claim is taken,
    so the module's totality contract has to cover its own argument."""
    garbage: object
    for garbage in ([], "none", 7, None):
        findings = wrongness.select_findings(
            findings=wrongness.ledger_findings([_Row(evidence=[_entry()])]),
            credits={NODE: 1.0},
            second_reader=cast(Any, garbage),
            graded_node_ids={NODE},
            raw_score=100,
        )
        assert [f.corroborated for f in findings] == [False]


def test_resolved_and_elicited_is_the_xp_bonus_shape() -> None:
    """The decision-7 population: `resolved AND apollo_elicited`. It is NEVER
    also `corroborated` — S2′ requires `NOT corrected_later` — so the XP caller
    must key on `resolved`, not on `corroborated AND resolved` (vacuous)."""
    finding = _one(
        [_Row(last_asked_turn=1, evidence=[_entry(turn_id=3)])],
        second_reader={NODE: {"contradicted": True, "corrected_later": True, "prompted": True}},
    )
    assert (finding.resolved, finding.apollo_elicited, finding.corroborated) == (True, True, False)


def test_ceiling_constant_agrees_with_topic_score() -> None:
    """`topic_score` is the authority; `wrongness` keeps an import-light copy to
    avoid the `unified → selection → topic_score → wrongness` cycle. Since W1-D
    landed the authority, this is an UNCONDITIONAL equality — it was written
    tolerantly (`getattr(..., None)`) while the two slices were building in
    parallel, and a tolerant duplicate-constant pin is worth nothing: it would
    also pass if the authority were renamed or deleted, which is precisely the
    drift it exists to catch. `84` is B+ on today's letter bands
    (`test_ceiling_letter_bands.py` pins that half)."""
    assert wrongness.CEILING_UNCORRECTED == 84
    assert wrongness.CEILING_UNCORRECTED == topic_score.CEILING_UNCORRECTED


def test_wrongness_core_stays_import_light() -> None:
    """The duplicate constant is only justified while `wrongness` is import-light.

    The moment this module imports `topic_score` (or `transcript_coverage`, or
    `smart_questions.*`), the honest fix is to import the constant instead of
    duplicating it — so the duplication and the import ban are pinned together,
    in one place, rather than living as a comment.
    """
    imported = {
        node.module
        for node in ast.walk(ast.parse(Path(wrongness.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(ast.parse(Path(wrongness.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    apollo_imports = sorted(name for name in imported if name.startswith("apollo"))
    assert apollo_imports == [], apollo_imports
    assert "config.settings" in imported


# ---------------------------------------------------------------------------
# ledger_findings / candidate_quotes
# ---------------------------------------------------------------------------


def test_attempt_086_zero_transcript_yields_no_findings() -> None:
    """Attempt 86 (defect I7) has zero student messages, so no row carries a
    quote — there is nothing to contest and the empty-attempt guard refuses the
    grade upstream anyway. The naive state rule fired here; S2′ cannot."""
    rows = [_Row(state="conflicting", times_asked=0, last_asked_turn=None, evidence=[])]
    assert wrongness.ledger_findings(rows) == ()
    assert _select(rows) == ()


def test_attempt_124_sticky_conflicting_state_alone_never_corroborates() -> None:
    """A graded node left in `conflicting` whose evidence carries no wrongness
    tag produces no finding at all: the label is the producer's, never the state's."""
    rows = [_Row(state="conflicting", evidence=[_entry(wrongness_label=None)])]

    findings = wrongness.ledger_findings(rows)
    assert [f.wrongness for f in findings] == [wrongness.WRONGNESS_NONE]
    assert _select(rows) == ()


def test_untagged_and_tagged_entries_coexist_on_one_row() -> None:
    rows = [
        _Row(
            evidence=[
                _entry(
                    turn_id=1, quote="Force equals mass times acceleration.", wrongness_label=None
                ),
                _entry(turn_id=3),
            ]
        )
    ]
    findings = wrongness.ledger_findings(rows)

    assert [f.wrongness for f in findings] == [
        wrongness.WRONGNESS_NONE,
        wrongness.WRONGNESS_MATERIAL,
    ]
    assert [f.is_latest_evidence for f in findings] == [False, True]
    assert findings[1].contradicts.startswith("Momentum is conserved when")
    assert findings[1].kind == "wrong-condition"
    assert (findings[1].state, findings[1].times_asked, findings[1].last_asked_turn) == (
        "understood",
        1,
        1,
    )


def test_off_enum_wrongness_coerces_to_none() -> None:
    rows = [_Row(evidence=[_entry(wrongness_label="catastrophically_wrong")])]
    assert wrongness.ledger_findings(rows)[0].wrongness == wrongness.WRONGNESS_NONE


def test_missing_status_forces_wrongness_none() -> None:
    """The producer's verbatim gate drops every non-`missing` update without a
    span, so `missing` + a contradiction is impossible by construction."""
    rows = [_Row(state="missing", evidence=[_entry()])]
    assert wrongness.ledger_findings(rows)[0].wrongness == wrongness.WRONGNESS_NONE


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        "not-a-list",
        [],
        ["not-a-dict"],
        [{"turn_id": 1}],
        [{"turn_id": 1, "quote": "   "}],
        [{"quote": 42}],
    ],
)
def test_malformed_evidence_is_skipped_never_raised(evidence: Any) -> None:
    assert wrongness.ledger_findings([_Row(evidence=evidence)]) == ()


def test_non_string_field_types_coerce() -> None:
    rows = [
        _Row(
            reference_node_id=7,
            times_asked="not-a-number",
            last_asked_turn="never",
            evidence=[{"turn_id": "later", "quote": "ok", "wrongness": 3, "contradicts": None}],
        )
    ]
    finding = wrongness.ledger_findings(rows)[0]

    assert (finding.node_id, finding.turn_id, finding.times_asked) == ("7", 0, 0)
    assert (finding.last_asked_turn, finding.wrongness, finding.contradicts) == (
        None,
        wrongness.WRONGNESS_NONE,
        "",
    )


def test_row_without_a_node_id_is_skipped() -> None:
    assert wrongness.ledger_findings([_Row(reference_node_id=None, evidence=[_entry()])]) == ()
    assert wrongness.ledger_findings([_Row(reference_node_id="", evidence=[_entry()])]) == ()


def test_unreadable_row_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    class _Exploding:
        reference_node_id = NODE
        state = "understood"
        times_asked = 1
        last_asked_turn = 1

        @property
        def evidence(self) -> list[dict[str, Any]]:
            raise RuntimeError("column unreadable")

    with caplog.at_level("WARNING"):
        findings = wrongness.ledger_findings([_Exploding(), _Row(evidence=[_entry()])])

    assert len(findings) == 1
    assert "apollo_wrongness_ledger_row_skipped" in caplog.text


def test_candidate_quotes_are_material_latest_graded_only() -> None:
    rows = [
        _Row(evidence=[_entry(turn_id=1, quote="stale wrong claim"), _entry(turn_id=3)]),
        _Row(reference_node_id=OTHER, evidence=[_entry(wrongness_label=wrongness.WRONGNESS_SELF)]),
        _Row(reference_node_id="def.impulse", evidence=[_entry()]),
    ]

    candidates = wrongness.candidate_quotes(
        wrongness.ledger_findings(rows), graded_node_ids={NODE, OTHER}
    )

    assert candidates == {NODE: "Momentum is conserved only when energy is."}


def test_candidate_quotes_empty_when_nothing_material() -> None:
    rows = [_Row(evidence=[_entry(wrongness_label=None)])]
    assert wrongness.candidate_quotes(wrongness.ledger_findings(rows), graded_node_ids={NODE}) == {}


def test_select_findings_reports_every_non_none_finding() -> None:
    rows = [
        _Row(evidence=[_entry()]),
        _Row(reference_node_id=OTHER, evidence=[_entry(wrongness_label=wrongness.WRONGNESS_SELF)]),
        _Row(reference_node_id="def.impulse", evidence=[_entry(wrongness_label=None)]),
    ]

    selected = _select(rows, graded={NODE, OTHER})

    assert [(f.node_id, f.corroborated) for f in selected] == [(NODE, True), (OTHER, False)]
