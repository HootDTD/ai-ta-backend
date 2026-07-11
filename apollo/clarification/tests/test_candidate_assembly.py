from apollo.clarification.candidate_assembly import (
    _misconceptions_dict,
    load_problem_candidates,
    load_problem_candidates_with_soundness,
    misconception_bank_applicable,
)

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class _Spec:
    def __init__(self, ck, k):
        self.canonical_key, self.key = ck, k


class _Entry:
    # The bank seeder strips the ``misc.`` prefix into the DB ``code`` column
    # (misconception_bank_seed.misconception_entry_to_bank_spec ->
    # ``key.removeprefix("misc.")``), so a REAL ``apollo_misconceptions`` row
    # carries a stripped code. This fixture must mirror that reality — an
    # already-prefixed code here would mask the re-prefix seam under test.
    code = "density_ignored"
    trigger_phrases = ["density doesn't matter"]
    description = "Student ignored density"
    # migration 039: a real ``MisconceptionEntry`` row always carries this
    # field (default None). Mirrored here so the byte-identity case (no
    # opposes authored) and the opposes-carried case (below) are both real.
    opposes: str | None = None


class _EntryWithOpposes(_Entry):
    """A bank entry with an authored ``opposes`` link (migration 039)."""

    opposes = "def.real_basis"


_PROBLEM = {
    "reference_solution": [
        {
            "entry_type": "condition",
            "entity_key": "cond.bernoulli",
            "content": {"applies_when": "flow is faster", "aliases": []},
        },
    ]
}


def _patch_loaders(monkeypatch, *, entries):
    async def fake_load_for_concept(db, *, concept_id):
        return entries

    async def fake_load_entity_specs(db, *, search_space_id, concept_id):
        return [_Spec("cond.bernoulli", 7)]

    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_for_concept", fake_load_for_concept
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_entity_specs", fake_load_entity_specs
    )


# ---------------------------------------------------------------------------
# Existing test (preserved)
# ---------------------------------------------------------------------------


async def test_assembles_candidates_from_problem_and_bank(monkeypatch):
    # Stub the three async loaders so no DB/LLM is touched.
    async def fake_load_for_concept(db, *, concept_id):
        return []  # empty bank -> only reference candidates

    async def fake_load_entity_specs(db, *, search_space_id, concept_id):
        return [_Spec("cond.bernoulli", 7)]

    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_for_concept", fake_load_for_concept
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_entity_specs", fake_load_entity_specs
    )

    problem = {
        "reference_solution": [
            {
                "entry_type": "condition",
                "entity_key": "cond.bernoulli",
                "content": {"applies_when": "flow is faster", "aliases": []},
            },
        ]
    }
    inputs = await load_problem_candidates(
        object(), search_space_id=1, concept_id=2, problem_payload=problem
    )
    keys = {c.canonical_key for c in inputs.candidates}
    assert "cond.bernoulli" in keys


# ---------------------------------------------------------------------------
# _misconceptions_dict tests
# ---------------------------------------------------------------------------


def test_misconceptions_dict_maps_code_to_key():
    # The DB ``code`` is stored WITHOUT the ``misc.`` prefix (the seeder strips
    # it); the candidate key must carry the canonical ``misc.``-prefixed form so
    # contradiction detection (``is_misconception_key``) and the KG entity lookup
    # (keyed on the prefixed ``entity_key``) both see a misconception. Lane B4/Q1.
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["key"] == "misc.density_ignored"


def test_misconceptions_dict_reprefixes_stripped_code():
    """The seam under test: a stripped DB ``code`` gains the ``misc.`` prefix so
    it becomes a canonical misconception key. Without the prefix the candidate's
    canonical_key never satisfies ``is_misconception_key`` and every misconception
    is silently routed to ``unsupported_extra`` (the course-wide dead path)."""

    class _Stripped:
        code = "pressure_velocity_same_direction"
        trigger_phrases: list[str] = []
        description = "P and v point the same way"
        opposes: str | None = None

    m = _misconceptions_dict([_Stripped()])["misconceptions"][0]
    assert m["key"] == "misc.pressure_velocity_same_direction"


def test_misconceptions_dict_maps_description_to_display_name():
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["display_name"] == "Student ignored density"


def test_misconceptions_dict_opposes_is_none():
    """Byte-identity: an entry with no authored opposes stays None."""
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["opposes"] is None


def test_misconceptions_dict_carries_authored_opposes():
    """T8(a) — the spec-named bug fix: an entry WITH an authored ``opposes``
    (migration 039) must reach the candidate dict instead of being discarded
    as a hardcoded None, so F-struct's ``build_opposes_map`` can see it."""
    result = _misconceptions_dict([_EntryWithOpposes()])
    m = result["misconceptions"][0]
    assert m["opposes"] == "def.real_basis"


def test_misconceptions_dict_trigger_phrases():
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["trigger_phrases"] == ["density doesn't matter"]


def test_misconceptions_dict_empty_entries():
    result = _misconceptions_dict([])
    assert result["misconceptions"] == []


# ---------------------------------------------------------------------------
# load_problem_candidates_with_soundness — bank_applicable flag
# ---------------------------------------------------------------------------


async def test_soundness_false_when_entries_empty(monkeypatch):
    """Empty misconception bank → bank_applicable=False."""
    _patch_loaders(monkeypatch, entries=[])
    _, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert bank_applicable is False


async def test_soundness_false_when_concept_id_none(monkeypatch):
    """Non-empty bank + concept_id=None → bank_applicable=False."""
    _patch_loaders(monkeypatch, entries=[_Entry()])
    _, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=None, problem_payload=_PROBLEM
    )
    assert bank_applicable is False


async def test_soundness_true_when_nonempty_and_concept_id_set(monkeypatch):
    """Non-empty bank + concept_id=<int> → bank_applicable=True."""
    _patch_loaders(monkeypatch, entries=[_Entry()])
    _, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert bank_applicable is True


# ---------------------------------------------------------------------------
# misconception_bank_applicable — the isolated flag (lane B3a/D1 LLM path)
# ---------------------------------------------------------------------------


async def test_bank_applicable_isolated_false_when_empty(monkeypatch):
    """Empty bank → not applicable, WITHOUT building the candidate set (the LLM
    artifact path has no problem_payload)."""
    _patch_loaders(monkeypatch, entries=[])
    assert await misconception_bank_applicable(object(), concept_id=2) is False


async def test_bank_applicable_isolated_true_when_nonempty(monkeypatch):
    _patch_loaders(monkeypatch, entries=[_Entry()])
    assert await misconception_bank_applicable(object(), concept_id=2) is True


async def test_bank_applicable_isolated_none_concept_short_circuits(monkeypatch):
    """concept_id=None short-circuits to False WITHOUT touching load_for_concept
    (a NULL concept can never own a bank)."""

    async def _boom(db, *, concept_id):
        raise AssertionError("load_for_concept must not run for a NULL concept")

    monkeypatch.setattr("apollo.clarification.candidate_assembly.load_for_concept", _boom)
    assert await misconception_bank_applicable(object(), concept_id=None) is False


async def test_bank_applicable_isolated_matches_soundness_recipe(monkeypatch):
    """The isolated helper and the candidate-recipe's ``bank_applicable`` share
    ONE predicate — they can never disagree about whether the bank was empty."""
    _patch_loaders(monkeypatch, entries=[_Entry()])
    isolated = await misconception_bank_applicable(object(), concept_id=2)
    _, recipe = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert isolated == recipe is True


# ---------------------------------------------------------------------------
# Equivalence test — chat-path and grading-path share the same candidate set
# ---------------------------------------------------------------------------


async def test_load_problem_candidates_equals_with_soundness_first_element(monkeypatch):
    """load_problem_candidates returns the same ProblemInputs as the first
    element of load_problem_candidates_with_soundness — the candidate set is
    byte-identical by construction (no recipe duplication)."""
    _patch_loaders(monkeypatch, entries=[])

    inputs_chat = await load_problem_candidates(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    inputs_grading, _ = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert inputs_chat.candidates == inputs_grading.candidates
    assert inputs_chat.symbolic_mappings == inputs_grading.symbolic_mappings


# ---------------------------------------------------------------------------
# Lane B4/Q1 — the dead-path regression: a stripped-code bank entry must yield a
# misconception candidate whose canonical_key satisfies contradiction detection
# (is_misconception_key) and lands on the real KG entity (canon_key != -1), and
# a student node matching it must produce a CONTRADICTION (never unsupported),
# so build_misconceptions is non-empty. Before the re-prefix fix EVERY assertion
# below fails: the candidate key is the bare `density_ignored`.
# ---------------------------------------------------------------------------


def _patch_loaders_with_misc_entity(monkeypatch, *, entries):
    """Like ``_patch_loaders`` but the entity-spec projection ALSO carries the
    prefixed ``misc.density_ignored`` :Canon key — so the candidate's canon_key
    resolves to the real KG entity (id 42) instead of the -1 miss."""

    async def fake_load_for_concept(db, *, concept_id):
        return entries

    async def fake_load_entity_specs(db, *, search_space_id, concept_id):
        return [_Spec("cond.bernoulli", 7), _Spec("misc.density_ignored", 42)]

    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_for_concept", fake_load_for_concept
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_entity_specs", fake_load_entity_specs
    )


async def test_misconception_candidate_is_prefixed_and_hits_kg_entity(monkeypatch):
    """The seam end-to-end through candidate assembly: the misconception
    Candidate carries the canonical ``misc.``-prefixed key, satisfies
    ``is_misconception_key``, and its canon_key resolves to the real KG entity
    (not the -1 miss the un-prefixed key produced)."""
    from apollo.graph_compare.soundness import is_misconception_key

    _patch_loaders_with_misc_entity(monkeypatch, entries=[_Entry()])
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert bank_applicable is True
    misc = [c for c in inputs.candidates if c.is_misconception]
    assert len(misc) == 1
    assert misc[0].canonical_key == "misc.density_ignored"
    assert is_misconception_key(misc[0].canonical_key) is True
    assert misc[0].canon_key == 42  # the real :Canon entity, no longer -1


async def test_student_matching_bank_misconception_is_contradiction_not_unsupported(monkeypatch):
    """The whole previously-dead path: a student node resolved to the bank
    misconception's key produces a CONTRADICTION finding (soundness penalized),
    NOT an unsupported_extra, and build_misconceptions carries the entry.

    This is the regression test for the F1b/F1c/Q1 vacuous-S5 defect: with the
    un-prefixed key the same student node routed to unsupported_extra and
    build_misconceptions returned []."""
    from apollo.grading.artifact_build import build_misconceptions
    from apollo.grading.opposes import build_opposes_map
    from apollo.graph_compare.canonical import (
        CanonicalGraph,
        CanonicalNode,
        ReferenceGraph,
        ReferencePathView,
    )
    from apollo.graph_compare.core import grade_attempt
    from apollo.graph_compare.findings import FindingKind
    from apollo.resolution.result import ResolutionResult

    _patch_loaders_with_misc_entity(monkeypatch, entries=[_Entry()])
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    misc_key = next(c.canonical_key for c in inputs.candidates if c.is_misconception)

    # Simulate the resolver having resolved a student utterance to that key.
    student = CanonicalGraph(
        nodes=(
            CanonicalNode(
                canonical_key=misc_key,
                node_type="definition",
                source_node_ids=("s1",),
                evidence_spans=("density doesn't matter here",),
                method="alias",
                confidence=0.92,
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
        ),
        edges=(),
        paths=(ReferencePathView(canonical_keys=("cond.bernoulli",)),),
    )

    result = grade_attempt(student, reference, bank_applicable=bank_applicable)

    kinds = [f.kind for f in result.findings]
    assert FindingKind.CONTRADICTION in kinds
    assert FindingKind.UNSUPPORTED_EXTRA not in kinds
    assert result.soundness_score is not None and result.soundness_score < 1.0

    misconceptions = build_misconceptions(
        result.findings,
        ResolutionResult(resolved=(), tier_counts={}, llm_calls=0),
        dict(build_opposes_map(inputs.candidates)),
    )
    assert len(misconceptions) == 1
    assert misconceptions[0]["canonical_key"] == "misc.density_ignored"
