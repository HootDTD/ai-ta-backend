"""Emergent-store read-path wiring in candidate_assembly.

2026-07-10 emergent misconception map plan, T8(b)/R1: the promoted-emergent
read is gated on the NEW ``APOLLO_EMERGENT_MAP_ASSERT`` flag (independent of
the legacy ``APOLLO_EMERGENT_MISCONCEPTIONS`` flag, which still gates ONLY
the artifact-derived ledger WRITE feed in ``artifact_writer.py`` — untouched
by this module). Fixtures use the REAL promoted-signature shape emitted by
``load_promoted_misconceptions_dict`` in production: ``key="emergent.<entity_
key>"`` (``apollo/emergent/capture.py``'s ``_SIGNATURE_PREFIX``), never
``misc.*`` — a promoted emergent misconception is never re-keyed, it is
asserted under its own ``emergent.*`` signature and must still dock
structurally (R1: ``is_misconception_key`` accepts both prefixes).

Proves: ASSERT-flag-OFF dormancy (byte-identical candidate set +
bank_applicable), ASSERT-flag-ON exposure (promoted emergent misconceptions
become candidates carrying their opposes, deduped against authored keys),
the legacy flag no longer controls this read path, and the R1 end-to-end
dock: a promoted emergent candidate routes a matching student node to
CONTRADICTION (never unsupported_extra), and does NOT dock an unrelated
node's correct statement (negative/precision test).
"""

from __future__ import annotations

import pytest

from apollo.clarification.candidate_assembly import load_problem_candidates_with_soundness


class _Spec:
    def __init__(self, ck, k):
        self.canonical_key, self.key = ck, k


class _Entry:
    # DB `code` column is UNPREFIXED (the seeder strips `misc.`; PR #94):
    # candidate assembly emits the key as f"misc.{code}".
    code = "density_ignored"
    trigger_phrases = ["density doesn't matter"]
    description = "Student ignored density"
    opposes: str | None = None


_PROBLEM = {
    "reference_solution": [
        {
            "entry_type": "condition",
            "entity_key": "cond.bernoulli",
            "content": {"applies_when": "flow is faster", "aliases": []},
        },
        {
            "entry_type": "equation",
            "entity_key": "eq.newton2",
            "content": {"symbolic": "F = m*a", "aliases": []},
        },
    ]
}

# Real production shape (apollo/emergent/store.py::load_promoted_misconceptions_dict):
# key is ALWAYS "emergent.<entity_key>" (apollo/emergent/capture.py::_SIGNATURE_PREFIX),
# never "misc.*" — a promoted emergent misconception is asserted under its own
# signature, not re-keyed into the hand-authored namespace.
_EMERGENT_PROMOTED = {
    "misconceptions": [
        {
            "key": "emergent.eq.newton2",
            "trigger_phrases": ["flipped the sign"],
            "opposes": "eq.newton2",
            "display_name": "emergent.eq.newton2",
        }
    ]
}


def _patch(monkeypatch, *, entries, promoted):
    async def fake_load_for_concept(db, *, concept_id):
        return entries

    async def fake_load_entity_specs(db, *, search_space_id, concept_id):
        return [_Spec("cond.bernoulli", 7), _Spec("eq.newton2", 9)]

    async def fake_load_promoted(db, *, search_space_id, concept_id):
        return promoted

    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_for_concept", fake_load_for_concept
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_entity_specs", fake_load_entity_specs
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_promoted_misconceptions_dict",
        fake_load_promoted,
    )


# ---------------------------------------------------------------------------
# ASSERT flag OFF -> byte-identical (the read-side byte-identical-off proof)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_flag_off_ignores_promoted_store(monkeypatch):
    """ASSERT flag OFF: even with promoted emergent entries available, the
    candidate set + bank_applicable are byte-identical to the hand-authored-
    only behavior."""
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_ASSERT", raising=False)
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    keys = {c.canonical_key for c in inputs.candidates}
    assert "emergent.eq.newton2" not in keys  # emergent key NOT present
    assert bank_applicable is False  # empty hand-authored bank, flag off


@pytest.mark.asyncio
async def test_legacy_flag_no_longer_controls_promoted_read(monkeypatch):
    """The OLD `APOLLO_EMERGENT_MISCONCEPTIONS` flag no longer gates THIS read
    path (T8(b)): turning it ON alone (ASSERT left OFF) must NOT expose the
    promoted emergent candidate. Confirms the investigated split: the legacy
    flag now controls only the artifact_writer.py ledger WRITE feed."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_ASSERT", raising=False)
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    keys = {c.canonical_key for c in inputs.candidates}
    assert "emergent.eq.newton2" not in keys
    assert bank_applicable is False


# ---------------------------------------------------------------------------
# ASSERT flag ON -> promoted emergent candidates appear, carrying opposes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_flag_on_exposes_promoted_as_candidates(monkeypatch):
    """ASSERT flag ON + empty hand-authored bank: the promoted emergent
    misconception becomes a candidate carrying its opposes, and flips
    bank_applicable True."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", "1")
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    misc = {c.canonical_key: c for c in inputs.candidates if c.is_misconception}
    assert set(misc) == {"emergent.eq.newton2"}
    assert misc["emergent.eq.newton2"].opposes_key == "eq.newton2"
    assert bank_applicable is True


@pytest.mark.asyncio
async def test_assert_flag_on_hand_authored_wins_collision(monkeypatch):
    """ASSERT flag ON: a hand-authored key present in BOTH banks appears once,
    from the hand-authored entry (the emergent duplicate is dropped)."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", "1")
    collision = {
        "misconceptions": [
            {
                "key": "misc.density_ignored",
                "trigger_phrases": ["x"],
                "opposes": "eq.z",
                "display_name": "emergent dup",
            }
        ]
    }
    _patch(monkeypatch, entries=[_Entry()], promoted=collision)
    inputs, _ = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    dupes = [c for c in inputs.candidates if c.canonical_key == "misc.density_ignored"]
    assert len(dupes) == 1
    assert dupes[0].display_name == "Student ignored density"  # hand-authored won


@pytest.mark.asyncio
async def test_assert_flag_on_empty_store_is_cold_start(monkeypatch):
    """ASSERT flag ON but no promoted entries (cold-start class): identical to
    flag-off behavior — nothing asserted, bank stays inapplicable on an empty
    bank."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", "1")
    _patch(monkeypatch, entries=[], promoted={"misconceptions": []})
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert {c.canonical_key for c in inputs.candidates} == {"cond.bernoulli", "eq.newton2"}
    assert bank_applicable is False


# ---------------------------------------------------------------------------
# R1 — the load-bearing end-to-end dock: a promoted emergent candidate must
# route a matching student node to CONTRADICTION (never unsupported_extra),
# and must NOT dock an unrelated node's correct statement (precision).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promoted_emergent_docks_the_right_node_structurally(monkeypatch):
    """R1 end-to-end: ASSERT ON + a promoted emergent signature at eq.newton2
    -> a student node resolved to that signature is a CONTRADICTION finding,
    NOT unsupported_extra. This is the whole loop closing: is_misconception_key
    must accept the `emergent.*` prefix or the candidate is unsupported_extra
    forever (the plan's R1 risk)."""
    from apollo.graph_compare.canonical import (
        CanonicalGraph,
        CanonicalNode,
        ReferenceGraph,
        ReferencePathView,
    )
    from apollo.graph_compare.core import grade_attempt
    from apollo.graph_compare.findings import FindingKind
    from apollo.graph_compare.soundness import is_misconception_key

    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", "1")
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    emergent_key = next(
        c.canonical_key for c in inputs.candidates if c.is_misconception
    )
    assert is_misconception_key(emergent_key) is True

    # A future student's wrong statement resolves to the promoted emergent key.
    student = CanonicalGraph(
        nodes=(
            CanonicalNode(
                canonical_key=emergent_key,
                node_type="definition",
                source_node_ids=("s1",),
                evidence_spans=("force equals mass over acceleration",),
                method="alias",
                confidence=0.9,
            ),
        ),
        edges=(),
        unresolved_nodes=(),
        dropped_edge_count=0,
    )
    reference = ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="eq.newton2",
                node_type="equation",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
        ),
        edges=(),
        paths=(ReferencePathView(canonical_keys=("eq.newton2",)),),
    )

    result = grade_attempt(student, reference, bank_applicable=bank_applicable)
    kinds = [f.kind for f in result.findings]
    assert FindingKind.CONTRADICTION in kinds
    assert FindingKind.UNSUPPORTED_EXTRA not in kinds
    assert result.soundness_score is not None and result.soundness_score < 1.0


@pytest.mark.asyncio
async def test_promoted_emergent_does_not_dock_an_unrelated_correct_node(monkeypatch):
    """Negative/precision test (the user's quality bar): a DIFFERENT node's
    correct statement must NOT be docked by the presence of a promoted
    emergent candidate elsewhere in the candidate set — the dock fires for
    the right node and ONLY the right node."""
    from apollo.graph_compare.canonical import (
        CanonicalGraph,
        CanonicalNode,
        ReferenceGraph,
        ReferencePathView,
    )
    from apollo.graph_compare.core import grade_attempt
    from apollo.graph_compare.findings import FindingKind

    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", "1")
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )

    # The student correctly states the UNRELATED cond.bernoulli reference node
    # — no misconception key anywhere in this graph.
    student = CanonicalGraph(
        nodes=(
            CanonicalNode(
                canonical_key="cond.bernoulli",
                node_type="condition",
                source_node_ids=("s1",),
                evidence_spans=("flow is faster here",),
                method="alias",
                confidence=0.9,
            ),
        ),
        edges=(),
        unresolved_nodes=(),
        dropped_edge_count=0,
    )
    reference = ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="cond.bernoulli",
                node_type="condition",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
            CanonicalNode(
                canonical_key="eq.newton2",
                node_type="equation",
                source_node_ids=("r2",),
                evidence_spans=(),
            ),
        ),
        edges=(),
        paths=(ReferencePathView(canonical_keys=("cond.bernoulli", "eq.newton2")),),
    )

    result = grade_attempt(student, reference, bank_applicable=bank_applicable)
    kinds = [f.kind for f in result.findings]
    assert FindingKind.CONTRADICTION not in kinds
    assert FindingKind.UNSUPPORTED_EXTRA not in kinds
    assert result.soundness_score == 1.0
    del inputs  # candidate set built (ASSERT ON) but nothing docks — unused here
