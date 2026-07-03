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
    code = "misc.density_ignored"
    trigger_phrases = ["density doesn't matter"]
    description = "Student ignored density"


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
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["key"] == "misc.density_ignored"


def test_misconceptions_dict_maps_description_to_display_name():
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["display_name"] == "Student ignored density"


def test_misconceptions_dict_opposes_is_none():
    result = _misconceptions_dict([_Entry()])
    m = result["misconceptions"][0]
    assert m["opposes"] is None


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

    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_for_concept", _boom
    )
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
