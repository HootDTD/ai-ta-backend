from apollo.clarification.candidate_assembly import load_problem_candidates


async def test_assembles_candidates_from_problem_and_bank(monkeypatch):
    # Stub the three async loaders so no DB/LLM is touched.
    async def fake_load_for_concept(db, *, concept_id):
        return []  # empty bank -> only reference candidates

    class _Spec:
        def __init__(self, ck, k):
            self.canonical_key, self.key = ck, k

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
