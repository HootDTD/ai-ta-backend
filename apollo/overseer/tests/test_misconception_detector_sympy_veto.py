"""RED->GREEN tests for the deterministic Tier-1 equation sign-veto.

Contract: docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md
section 5.2 (``sympy_veto.py``), amended by A5 (bare ``misc.<code>`` in
``canonical_key`` downstream — this module only sets ``signature``).

``detect_sign_veto`` compares each STUDENT equation node against the
REFERENCE graph's equation nodes using ``apollo.resolution.tiers._symbolic_equiv``
(sign-EXACT structural equivalence):

  * sign-exact match to a reference equation -> the student got it right ->
    no finding.
  * NOT sign-exact to any reference equation, but sign-exact to a
    pre-authored sign/direction MUTANT of a bank equation (an ``eq:``-prefixed
    entry in ``MisconceptionEntry.trigger_phrases``) -> a deterministic
    ``misconception`` finding: confidence=1.0, corroborated=True,
    source="sympy_veto", signature="misc.<code>".
  * A genuine same-sign algebraic rearrangement of the reference equation is
    still sign-exact under ``_symbolic_equiv`` -> no finding (no
    over-correction; this is the whole point of reusing the resolver's
    equivalence check rather than a textual diff).
  * Malformed/unparseable symbolic text -> no finding, no crash (soft-fail,
    mirrors ``_symbolic_equiv``'s own try/except contract).
"""

from __future__ import annotations

from types import SimpleNamespace

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.sympy_veto import (
    _attach_concept_key,
    detect_sign_veto,
)


def _eq_node(node_id: str, symbolic: str, label: str = "", *, attempt_id: int = 1):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": symbolic, "label": label},
    )


def _bank_entry(
    *,
    code: str = "misc.sign_flip_continuity",
    trigger_phrases: tuple[str, ...] = (),
    concept_id: int = 1,
) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=1,
        concept_id=concept_id,
        code=code,
        description="Flips the sign of the continuity equation.",
        confusion_pair=None,
        trigger_phrases=trigger_phrases,
        probe_question="Which direction does the flow go?",
        rt_steps=(),
    )


def test_sign_flipped_mutant_of_bank_equation_fires_misconception():
    """A student equation matching an eq:-prefixed sign-mutant trigger phrase
    -> one deterministic, fully-corroborated misconception finding."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1", "some non-eq trigger phrase"),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert len(result) == 1
    finding = result[0]
    assert finding.verdict == "misconception"
    assert finding.confidence == 1.0
    assert finding.corroborated is True
    assert finding.source == "sympy_veto"
    assert finding.signature == "misc.sign_flip_continuity"
    assert finding.concept_key == "ref_continuity"


def test_sign_exact_match_to_reference_yields_no_finding():
    """The student wrote the reference equation verbatim (sign-exact) ->
    correct, no finding."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A1*v1 - A2*v2")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert result == ()


def test_genuine_same_sign_rearrangement_yields_no_finding():
    """A student who rearranges the SAME-sign equation algebraically must NOT
    be flagged (no over-correction) -- _symbolic_equiv is sign-exact but
    rearrangement-tolerant."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A1*v1 - A2*v2 + 0")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert result == ()


def test_malformed_symbolic_text_yields_no_finding_no_crash():
    """Unparseable student symbolic text is a non-match, never a crash."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A1*v1 -- === v2 ((")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert result == ()


def test_malformed_mutant_trigger_phrase_is_skipped_not_crashed():
    """A malformed eq: mutant string in the bank must not crash the veto --
    it's simply never matchable."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:((( malformed",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert result == ()


def test_no_reference_match_and_no_bank_yields_no_finding():
    """An unrelated student equation with an empty bank -> no finding
    (honest non-detection, not a false positive)."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "x + y")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])

    result = detect_sign_veto(student, reference, bank_entries=())

    assert result == ()


def test_non_eq_prefixed_trigger_phrases_are_ignored():
    """Trigger phrases without the eq: prefix are plain-text CBM triggers
    (bank_pattern's territory) and must never be parsed as equations here."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("A2*v2 - A1*v1",),  # no eq: prefix
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert result == ()


def test_concept_key_falls_back_to_node_id_when_label_missing():
    """concept_key attaches to the matched reference node -- node_id when the
    reference equation carries no label."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2")])  # no label
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert len(result) == 1
    assert result[0].concept_key == "ref_continuity"


def test_result_is_a_tuple_and_multiple_students_are_independent():
    """Return type is a tuple (immutability contract); each student equation
    node is evaluated independently."""
    student = KGGraph(
        nodes=[
            _eq_node("stu_eq1", "A2*v2 - A1*v1"),  # mutant -> misconception
            _eq_node("stu_eq2", "P1 - P2"),  # unrelated -> no finding
        ]
    )
    reference = KGGraph(
        nodes=[
            _eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity"),
        ]
    )
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert isinstance(result, tuple)
    assert len(result) == 1
    assert result[0].concept_key == "ref_continuity"


def test_non_equation_student_nodes_are_ignored():
    """Only equation-typed student nodes are considered."""
    cond_node = build_node(
        node_type="condition",
        node_id="stu_cond1",
        attempt_id=1,
        source="parser",
        content={"applies_when": "steady state"},
    )
    student = KGGraph(nodes=[cond_node])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert result == ()


def test_empty_bank_entries_default_is_accepted():
    """bank_entries defaults to () -- calling without it must not crash and
    yields no misconception findings (no mutants to compare against)."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A2*v2 - A1*v1")])
    reference = KGGraph(nodes=[_eq_node("ref_continuity", "A1*v1 - A2*v2", "Continuity")])

    result = detect_sign_veto(student, reference)

    assert result == ()


# ---------------------------------------------------------------------------
# Empty-surface-text branches (lines 64, 85).
#
# The real `EquationContent.symbolic` field enforces `min_length=1` at the
# pydantic level, so an equation node with a genuinely empty `symbolic`
# cannot be constructed via `build_node`/`KGGraph(nodes=[...])` (pydantic
# would reject either the node or the list-of-Node validation). Both
# `_reference_equations` and `detect_sign_veto` only ever call
# `<graph>.by_type("equation")` on their graph argument (no isinstance check
# on KGGraph itself), so a minimal duck-typed fake graph exercises the
# skip-on-empty-surface-text branch without touching production code.
# ---------------------------------------------------------------------------


def _fake_equation_node(node_id: str, symbolic: str, label: str = ""):
    return SimpleNamespace(
        node_id=node_id,
        node_type="equation",
        content=SimpleNamespace(symbolic=symbolic, label=label),
    )


def _fake_graph(*nodes):
    """Minimal duck-typed stand-in for KGGraph: only `.by_type` is used by
    this module's functions."""
    return SimpleNamespace(
        by_type=lambda node_type: [n for n in nodes if n.node_type == node_type],
    )


def test_reference_equation_with_empty_surface_text_is_skipped():
    """Line 64: a reference equation node whose surface text is empty/falsy
    must be skipped when building the (concept_key, symbolic) pairs, rather
    than included as a blank entry. The student equation is a genuine mutant
    of the bank trigger, so if the blank reference were (incorrectly)
    included as a match candidate it would not change this assertion --
    the real proof is via `_attach_concept_key` falling back to the student's
    own node_id below, since no non-blank reference equation exists."""
    student = _fake_graph(_fake_equation_node("stu_eq1", "A2*v2 - A1*v1"))
    reference = _fake_graph(_fake_equation_node("ref_blank", ""))  # empty symbolic -> skipped
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    assert len(result) == 1
    finding = result[0]
    assert finding.signature == "misc.sign_flip_continuity"
    # No usable reference equation was on record (the blank one was skipped),
    # so concept_key falls back to the student's own node_id.
    assert finding.concept_key == "stu_eq1"


def test_student_equation_with_empty_surface_text_is_skipped():
    """Line 85: a STUDENT equation node whose surface text is empty/falsy
    must be skipped entirely (continue) -- no finding, no crash, and it must
    not interfere with a second, genuinely mutant-matching student node in
    the same graph."""
    blank_student = _fake_equation_node("stu_blank", "")
    mutant_student = _fake_equation_node("stu_eq1", "A2*v2 - A1*v1")
    student = _fake_graph(blank_student, mutant_student)
    reference = _fake_graph(_fake_equation_node("ref_continuity", "A1*v1 - A2*v2", "Continuity"))
    bank = (
        _bank_entry(
            code="misc.sign_flip_continuity",
            trigger_phrases=("eq:A2*v2 - A1*v1",),
        ),
    )

    result = detect_sign_veto(student, reference, bank_entries=bank)

    # Only the mutant-matching (non-blank) student node produces a finding.
    assert len(result) == 1
    assert result[0].concept_key == "ref_continuity"


# ---------------------------------------------------------------------------
# `_attach_concept_key` direct branch test (line 130).
# ---------------------------------------------------------------------------


def test_attach_concept_key_falls_back_to_student_node_id_when_no_reference_equations():
    """Line 130: with an empty `reference_equations` tuple (a bare mutant bank
    with no reference equation nodes at all), the concept_key attaches to the
    student node's own id rather than raising an IndexError."""
    result = _attach_concept_key((), "stu_eq1")
    assert result == "stu_eq1"
