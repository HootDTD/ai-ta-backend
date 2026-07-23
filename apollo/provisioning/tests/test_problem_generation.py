"""GEN-2 problem-generation authoring-stage tests (no network, fake DB)."""

from __future__ import annotations

import json

import pytest

from apollo.persistence.models import Problem as ProblemRecord
from apollo.provisioning.metered_chat import CostBudgetExceeded
from apollo.provisioning.problem_generation import (
    VARIATION_OPERATORS,
    ProblemGenerationDisabled,
    generate_problem_variants,
    generation_max_variants,
    generation_token_ceiling,
    problem_generation_enabled,
)
from apollo.provisioning.problem_leak_guard import ProblemLeakVerdict
from apollo.provisioning.solution import ReferenceSolutionDraft, SolutionDraftError
from apollo.schemas.problem import Problem


def _payload(
    *,
    code: str = "seed-1",
    text: str = "A tank holds 10 L. Find the mass.",
    givens: dict[str, float] | None = None,
    target: str = "m",
    concept_slug: str = "mass_balance",
) -> dict:
    return {
        "id": code,
        "concept_id": concept_slug,
        "difficulty": "standard",
        "problem_text": text,
        "given_values": {"V": 10.0} if givens is None else givens,
        "target_unknown": target,
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "definition",
                "id": "mass_relation",
                "content": {"term": "mass", "definition": "amount of matter"},
                "depends_on": [],
            }
        ],
    }


def _seed(row_id: int, **payload_kwargs) -> ProblemRecord:
    row = ProblemRecord.from_pydantic_payload(
        _payload(code=f"seed-{row_id}", **payload_kwargs),
        course_id=7,
        concept_id=41,
        id=row_id,
        tier=2,
        solution_source="authored",
        provenance={},
    )
    return row


def _draft(*, provenance: dict | None = None) -> ReferenceSolutionDraft:
    return ReferenceSolutionDraft(
        solution_source="generated",
        reference_solution=[
            {
                "step": 1,
                "entry_type": "definition",
                "id": "variant_relation",
                "content": {"term": "result", "definition": "derived result"},
                "depends_on": [],
            }
        ],
        provenance=provenance or {},
    )


def _quantitative_draft(*, governing: str = "Q = A*v", stated: str = "0.06"):
    return ReferenceSolutionDraft(
        solution_source="generated",
        reference_solution=[
            {
                "step": 1,
                "entry_type": "equation",
                "id": "governing",
                "content": {"label": "Flow relation", "symbolic": governing},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "equation",
                "id": "answer_key",
                "content": {"label": "Stated answer", "symbolic": f"Q = {stated}"},
                "depends_on": ["governing"],
            },
        ],
    )


def _candidate(text: str, *, givens: dict[str, float] | None = None, target: str = "m") -> str:
    return json.dumps(
        {
            "problem_text": text,
            "given_values": {"V": 12.0} if givens is None else givens,
            "target_unknown": target,
            "difficulty": "standard",
        }
    )


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)

    def all(self):
        return list(self._values)


class _FakeDB:
    def __init__(self, seeds: list[ProblemRecord], existing_payloads: list[str] | None = None):
        self._results = [
            [(seed, "mass_balance") for seed in seeds],
            existing_payloads or [str(seed.problem_text) for seed in seeds],
        ]
        self.added: list[ProblemRecord] = []
        self.flushes = 0

    async def execute(self, _statement):
        return _Result(self._results.pop(0))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushes += 1
        for index, row in enumerate(self.added, start=1001):
            if row.id is None:
                row.id = index


class _Chat:
    def __init__(self, variants: list[str | Exception]):
        self.variants = list(variants)
        self.main_calls: list[dict] = []
        self.cheap_calls: list[dict] = []

    def main(self, **kwargs):
        self.main_calls.append(kwargs)
        response = self.variants.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def cheap(self, **kwargs):
        self.cheap_calls.append(kwargs)
        return json.dumps({"leaked": False, "confidence": 1.0, "quoted_span": None})


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("APOLLO_PROBLEM_GENERATION", "1")
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION_MAX_VARIANTS", raising=False)
    monkeypatch.delenv("MAIN_MODEL", raising=False)


@pytest.fixture
def clean_pipeline(monkeypatch):
    import apollo.provisioning.problem_generation.generator as generator

    async def find(_db, _candidate, **_kwargs):
        return _draft(
            provenance={
                "generation_defects": ["dependency_completeness: review"],
                "symbol_table": {"m": {"role": "mass"}},
            }
        )

    monkeypatch.setattr(generator, "find_or_generate", find)
    monkeypatch.setattr(
        generator,
        "check_problem_leak",
        lambda _problem, **_kwargs: ProblemLeakVerdict(False, 1.0, ["clean"], "judge"),
    )
    return generator


def test_flag_defaults_off_and_accepts_only_settled_truthy_values(monkeypatch):
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION", raising=False)
    assert problem_generation_enabled() is False
    for value in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("APOLLO_PROBLEM_GENERATION", value)
        assert problem_generation_enabled() is True
    for value in ("", "0", "false", "on"):
        monkeypatch.setenv("APOLLO_PROBLEM_GENERATION", value)
        assert problem_generation_enabled() is False


async def test_flag_off_raises_before_db_or_chat(monkeypatch):
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION", raising=False)
    with pytest.raises(ProblemGenerationDisabled):
        await generate_problem_variants(
            object(),
            concept_id=41,
            seed_problem_ids=[1],
            count=1,
            metered_chat=object(),
            search_space_id=7,
        )


def test_ceiling_and_max_variants_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION_TOKEN_CEILING", raising=False)
    monkeypatch.delenv("APOLLO_PROBLEM_GENERATION_MAX_VARIANTS", raising=False)
    assert generation_token_ceiling() == 200_000
    assert generation_max_variants() == 10
    monkeypatch.setenv("APOLLO_PROBLEM_GENERATION_TOKEN_CEILING", "12345")
    monkeypatch.setenv("APOLLO_PROBLEM_GENERATION_MAX_VARIANTS", "4")
    assert generation_token_ceiling() == 12_345
    assert generation_max_variants() == 4


def test_operator_contract_and_structure_only_dag_prompt():
    seed = Problem.model_validate(_payload())
    dag_operator = next(op for op in VARIATION_OPERATORS if op.name == "isomorphic_dag_shape")
    messages = dag_operator.build_messages(seed)
    user = json.loads(messages[1]["content"])
    assert user["dependency_shape"] == [{"entry_type": "definition", "depends_on": []}]
    assert "content" not in json.dumps(user["dependency_shape"])
    for operator in VARIATION_OPERATORS:
        system = operator.build_messages(seed)[0]["content"]
        assert "NEVER include the solution" in system
        assert "Return the JSON object ONLY" in system


async def test_happy_path_writes_held_tier1_rows_with_exact_provenance(
    enabled, clean_pipeline, monkeypatch
):
    monkeypatch.setenv("MAIN_MODEL", "test-main-model")
    seed = _seed(11)
    db = _FakeDB([seed])
    chat = _Chat([_candidate(f"Variant statement {n}") for n in range(3)])

    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[11],
        count=3,
        metered_chat=chat,
        search_space_id=7,
    )

    assert result.requested == 3
    assert result.written == [1001, 1002, 1003]
    assert [record.concept_problem_id for record in result.records] == result.written
    assert db.flushes == 1
    assert [row.tier for row in db.added] == [1, 1, 1]
    assert [row.solution_source for row in db.added] == ["generated"] * 3
    assert [row.provenance["variation_operator"] for row in db.added] == [
        "parameter_perturbation",
        "context_reskin",
        "isomorphic_dag_shape",
    ]
    for row in db.added:
        assert row.provenance["source"] == "generated"
        assert row.provenance["aig_seed_id"] == 11
        assert row.provenance["model"] == "test-main-model"
        assert row.provenance["authored_review"]["required"] is True
        assert row.provenance["authored_review"]["reason"] == "generated_variant"
        assert row.provenance["generation_defects"]
        assert row.provenance["symbol_table"]["m"]["role"] == "mass"
        ReferenceSolutionDraft.model_validate(row.provenance["authored_review"]["ocr_draft"])
    assert all(call["purpose"] == "problem_generation_variant" for call in chat.main_calls)
    assert all(call["temperature"] == 0.0 for call in chat.main_calls)
    assert all(call["response_format"]["type"] == "json_schema" for call in chat.main_calls)


async def test_count_is_clamped_to_per_call_max(enabled, clean_pipeline, monkeypatch):
    monkeypatch.setenv("APOLLO_PROBLEM_GENERATION_MAX_VARIANTS", "2")
    db = _FakeDB([_seed(12)])
    chat = _Chat([_candidate("One"), _candidate("Two")])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[12],
        count=50,
        metered_chat=chat,
        search_space_id=7,
    )
    assert result.requested == 2
    assert len(result.written) == 2


async def test_prose_seed_skips_parameter_operator_without_forcing_numbers(enabled, clean_pipeline):
    seed = _seed(
        13,
        text="Explain why institutional trust matters.",
        givens={},
        target="institutional trust",
        concept_slug="institutional_trust",
    )
    db = _FakeDB([seed])
    chat = _Chat(
        [
            _candidate("Explain trust using a municipal scenario.", givens={}),
            _candidate("Analyze trust in a different institution.", givens={}),
        ]
    )
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[13],
        count=2,
        metered_chat=chat,
        search_space_id=7,
    )
    assert result.written == [1001, 1002]
    assert [row.provenance["variation_operator"] for row in db.added] == [
        "context_reskin",
        "isomorphic_dag_shape",
    ]
    assert all(row.given_values == {} for row in db.added)
    assert all(
        "do not force equations or numeric values" in c["messages"][0]["content"]
        for c in chat.main_calls
    )


async def test_leaked_variant_is_dropped_with_reasons(enabled, clean_pipeline, monkeypatch):
    monkeypatch.setattr(
        clean_pipeline,
        "check_problem_leak",
        lambda _problem, **_kwargs: ProblemLeakVerdict(
            True, 0.99, ["problem_text contains final answer"], "judge"
        ),
    )
    db = _FakeDB([_seed(14)])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[14],
        count=1,
        metered_chat=_Chat([_candidate("The answer is embedded here.")]),
        search_space_id=7,
    )
    assert result.written == []
    assert result.dropped["leaked"] == 1
    assert result.records[-1].reasons == ("problem_text contains final answer",)
    assert db.added == []


async def test_refuted_variant_is_dropped_before_leak_guard(enabled, clean_pipeline, monkeypatch):
    async def find(_db, _candidate, **_kwargs):
        return _quantitative_draft(governing="Q = A*v", stated="0.07")

    def unexpected_leak_check(*_args, **_kwargs):
        raise AssertionError("leak guard must not run for a refuted variant")

    monkeypatch.setattr(clean_pipeline, "find_or_generate", find)
    monkeypatch.setattr(clean_pipeline, "check_problem_leak", unexpected_leak_check)
    db = _FakeDB([_seed(140, givens={"A": 0.015, "v": 4.0}, target="Q")])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[140],
        count=1,
        metered_chat=_Chat(
            [
                _candidate(
                    "Find flow Q for a new section.",
                    givens={"A": 0.015, "v": 4.0},
                    target="Q",
                )
            ]
        ),
        search_space_id=7,
    )

    assert result.written == []
    assert result.dropped["refuted"] == 1
    assert "all solution branches contradict" in result.records[-1].reasons[0]
    assert db.added == []


async def test_quantitative_round_trip_verdict_is_stamped(enabled, clean_pipeline, monkeypatch):
    async def find(_db, _candidate, **_kwargs):
        return _quantitative_draft()

    monkeypatch.setattr(clean_pipeline, "find_or_generate", find)
    db = _FakeDB([_seed(141, givens={"A": 0.015, "v": 4.0}, target="Q")])
    chat = _Chat(
        [
            _candidate(
                "Compute Q for the changed section.",
                givens={"A": 0.015, "v": 4.0},
                target="Q",
            )
        ]
    )
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[141],
        count=1,
        metered_chat=chat,
        search_space_id=7,
    )

    assert result.written == [1001]
    assert db.added[0].provenance["round_trip"]["verdict"] == "verified"
    assert "all solution branches match" in db.added[0].provenance["round_trip"]["diagnostic"]
    assert "qualitative_rubric" not in db.added[0].provenance
    assert chat.cheap_calls == []


async def test_unresolved_quantitative_variant_is_written_with_verdict(
    enabled, clean_pipeline, monkeypatch
):
    async def find(_db, _candidate, **_kwargs):
        return _quantitative_draft(governing="Q + cos(Q)", stated="0")

    monkeypatch.setattr(clean_pipeline, "find_or_generate", find)
    db = _FakeDB([_seed(146, givens={}, target="Q")])
    chat = _Chat([_candidate("Solve the transcendental relation for Q.", givens={}, target="Q")])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[146],
        count=1,
        metered_chat=chat,
        search_space_id=7,
    )

    assert result.written == [1001]
    diagnostic = db.added[0].provenance["round_trip"]["diagnostic"]
    assert db.added[0].provenance["round_trip"]["verdict"] == "unresolved"
    assert "NotImplementedError" in diagnostic or "timeout" in diagnostic
    assert chat.cheap_calls == []


async def test_qualitative_judge_runs_only_after_inapplicable_variant_passes_leak(
    enabled, clean_pipeline
):
    db = _FakeDB([_seed(142, givens={}, target="institutional trust")])

    class _RubricChat(_Chat):
        def cheap(self, **kwargs):
            self.cheap_calls.append(kwargs)
            return json.dumps(
                {
                    "claims": [
                        {
                            "claim": "Transparency may support trust.",
                            "supported": True,
                            "note": "Supported by the scenario.",
                        },
                        {
                            "claim": "Trust is guaranteed.",
                            "supported": False,
                            "note": "The statement gives no guarantee.",
                        },
                    ]
                }
            )

    chat = _RubricChat([_candidate("Analyze transparency and trust.", givens={})])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[142],
        count=1,
        metered_chat=chat,
        search_space_id=7,
    )

    assert result.written == [1001]
    assert len(chat.cheap_calls) == 1
    rubric = db.added[0].provenance["qualitative_rubric"]
    assert rubric["unsupported_count"] == 1
    assert rubric["ceiling"] == "faithfulness_only"


async def test_qualitative_judge_is_not_called_when_leak_guard_drops_variant(
    enabled, clean_pipeline, monkeypatch
):
    monkeypatch.setattr(
        clean_pipeline,
        "check_problem_leak",
        lambda _problem, **_kwargs: ProblemLeakVerdict(True, 1.0, ["leaked"], "judge"),
    )
    db = _FakeDB([_seed(143, givens={}, target="institutional trust")])
    chat = _Chat([_candidate("The leaked prose answer.", givens={})])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[143],
        count=1,
        metered_chat=chat,
        search_space_id=7,
    )

    assert result.dropped["leaked"] == 1
    assert chat.cheap_calls == []


async def test_malformed_qualitative_judge_output_still_writes_row(enabled, clean_pipeline):
    db = _FakeDB([_seed(144, givens={}, target="institutional trust")])
    chat = _Chat([_candidate("Discuss trust without unsupported conclusions.", givens={})])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[144],
        count=1,
        metered_chat=chat,
        search_space_id=7,
    )

    assert result.written == [1001]
    assert db.added[0].provenance["round_trip"]["verdict"] == "inapplicable"
    assert "qualitative_rubric" not in db.added[0].provenance


async def test_qualitative_judge_budget_breach_reaches_partial_run_seam(enabled, clean_pipeline):
    class _RubricBudgetChat(_Chat):
        def cheap(self, **_kwargs):
            raise CostBudgetExceeded(tokens=401, ceiling=400, document_id=None)

    db = _FakeDB([_seed(145, givens={}, target="institutional trust")])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[145],
        count=1,
        metered_chat=_RubricBudgetChat([_candidate("Explain institutional trust.", givens={})]),
        search_space_id=7,
    )

    assert result.written == []
    assert result.dropped["budget_exceeded"] == 1
    assert "401 > 400" in result.records[-1].reasons[0]


async def test_solution_failure_drops_one_and_run_continues(enabled, clean_pipeline, monkeypatch):
    calls = 0

    async def find(_db, _candidate, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SolutionDraftError("unusable draft")
        return _draft()

    monkeypatch.setattr(clean_pipeline, "find_or_generate", find)
    db = _FakeDB([_seed(15)])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[15],
        count=2,
        metered_chat=_Chat([_candidate("First variant"), _candidate("Second variant")]),
        search_space_id=7,
    )
    assert result.dropped["solution_failed"] == 1
    assert result.written == [1001]
    assert result.records[0].reasons == ("unusable draft",)


async def test_normalized_duplicate_vs_seed_and_sibling_is_dropped(enabled, clean_pipeline):
    seed = _seed(16, text="A tank holds 10 L. Find the mass!")
    db = _FakeDB([seed])
    chat = _Chat(
        [
            _candidate("a TANK holds 10 L find the mass"),
            _candidate("A fresh sibling statement."),
            _candidate("a fresh sibling statement"),
        ]
    )
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[16],
        count=3,
        metered_chat=chat,
        search_space_id=7,
    )
    assert result.dropped["duplicate"] == 2
    assert result.written == [1001]


async def test_duplicate_against_existing_nonquarantined_problem(enabled, clean_pipeline):
    db = _FakeDB(
        [_seed(17)],
        existing_payloads=[
            _seed(17).problem_text,
            "Existing concept statement.",
        ],
    )
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[17],
        count=1,
        metered_chat=_Chat([_candidate("existing concept statement")]),
        search_space_id=7,
    )
    assert result.dropped["duplicate"] == 1
    assert result.written == []


async def test_invalid_and_missing_seeds_are_skipped_while_valid_seed_proceeds(
    enabled, clean_pipeline
):
    invalid = _seed(18)
    invalid.problem_text = ""
    invalid.reference_solution = {"version": 1, "steps": []}
    valid = _seed(19)
    db = _FakeDB([invalid, valid])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[18, 999, 19],
        count=1,
        metered_chat=_Chat([_candidate("Valid seed variant")]),
        search_space_id=7,
    )
    assert result.dropped["invalid_seed"] == 2
    assert result.written == [1001]
    assert db.added[0].provenance["aig_seed_id"] == 19


async def test_invalid_variant_is_fail_soft(enabled, clean_pipeline):
    db = _FakeDB([_seed(20)])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[20],
        count=1,
        metered_chat=_Chat([json.dumps({"problem_text": "missing fields"})]),
        search_space_id=7,
    )
    assert result.dropped["invalid_variant"] == 1
    assert result.written == []


async def test_cost_budget_breach_returns_partial_result_and_flushes_prior_write(
    enabled, clean_pipeline
):
    breach = CostBudgetExceeded(tokens=201, ceiling=200, document_id=None)
    db = _FakeDB([_seed(21)])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[21],
        count=3,
        metered_chat=_Chat([_candidate("Written before breach"), breach]),
        search_space_id=7,
    )
    assert result.written == [1001]
    assert result.dropped["budget_exceeded"] == 1
    assert db.flushes == 1
    assert len(db.added) == 1


async def test_cost_breach_from_fail_open_leak_judge_is_still_recorded(
    enabled, clean_pipeline, monkeypatch
):
    from apollo.provisioning.problem_leak_guard import check_problem_leak

    monkeypatch.setattr(clean_pipeline, "check_problem_leak", check_problem_leak)

    class _CheapBreachChat(_Chat):
        def cheap(self, **kwargs):
            raise CostBudgetExceeded(tokens=301, ceiling=300, document_id=None)

    db = _FakeDB([_seed(22)])
    result = await generate_problem_variants(
        db,
        concept_id=41,
        seed_problem_ids=[22],
        count=1,
        metered_chat=_CheapBreachChat([_candidate("Leak judge budget candidate")]),
        search_space_id=7,
    )
    assert result.dropped["budget_exceeded"] == 1
    assert result.written == []
    assert db.added == []


def test_generated_provenance_stamp_satisfies_orm_invariant():
    row = ProblemRecord.from_inventory_payload(
        {
            "id": "gen-1-context_reskin-abcdef",
            "concept_id": "mass_balance",
            "difficulty": "standard",
            "problem_text": "x",
            "given_values": {},
            "target_unknown": "m",
        },
        course_id=7,
        concept_id=41,
        tier=1,
        solution_source="generated",
        provenance={
            "source": "generated",
            "aig_seed_id": 1,
            "variation_operator": "context_reskin",
            "model": "gpt-4o",
        },
    )
    assert row.solution_source == "generated"
    assert row.provenance["source"] == "generated"
