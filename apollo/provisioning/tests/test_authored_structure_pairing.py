"""PR2 trust-spine tests for structure-paired authored candidates.

These drive the real ``_process_authored_candidate`` boundary with frozen
Porter-shaped structure units and injected stage doubles; no network or vendor
service is involved.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import apollo.provisioning.authored_sets.orchestrator as orch
from apollo.provisioning.authored_sets.graph_derivation import DerivedGraph
from apollo.provisioning.authored_sets.structure_pass import BlockSpan, StructurePair, StructureUnit
from apollo.provisioning.authored_sets.verification import VerificationVerdict
from apollo.provisioning.concept_match import ConceptMatch
from apollo.provisioning.pairing_gate import PairingVerdict
from apollo.provisioning.promote import PromoteResult
from apollo.provisioning.scrape import CandidateQuestion
from apollo.provisioning.tag_mint import MintPlan

_ANSWER = "Answer: Competitive rivalry is strongest when competitors converge."
_REFERENCE = [
    {
        "step": 1,
        "id": "rivalry_definition",
        "entry_type": "definition",
        "content": {
            "concept": "competitive rivalry",
            "meaning": "pressure from firms competing for the same customers",
        },
        "depends_on": [],
    },
    {
        "step": 2,
        "id": "identify_rivalry",
        "entry_type": "procedure_step",
        "content": {
            "order": 1,
            "action": "identify competitors with converging offers",
            "purpose": "determine when rivalry is strongest",
            "uses_equations": [],
        },
        "depends_on": ["rivalry_definition"],
    },
]


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _DB:
    def begin_nested(self):
        return _Nested()

    async def get(self, _model, _row_id):
        return SimpleNamespace(
            display_name="Porter's Five Forces",
            canonical_symbols={},
            normalization_map={},
        )

    async def flush(self):
        return None


class _Metered:
    def main(self, **_kwargs):
        return json.dumps(
            {
                "reference_solution": _REFERENCE,
                "augmented_problem_text": None,
                "augmented_target_unknown": None,
            }
        )

    def cheap(self, **_kwargs):
        return "{}"


def _candidate() -> CandidateQuestion:
    return CandidateQuestion(
        problem_text="1. (MC) In Porter's model, when is competitive rivalry strongest?",
        given_values={},
        target_unknown="competitive rivalry",
        difficulty="intro",
        document_id=10,
        page=1,
        chunk_content_hash="porter-1",
        concept_slug="provisional.inventory",
        label="1",
    )


def _pair() -> StructurePair:
    return StructurePair(
        label="1",
        question=StructureUnit(
            kind="question",
            label="1",
            document_role="problem",
            start_chunk=10,
            end_chunk=10,
            start_char=0,
            end_char=70,
            confidence=0.98,
            block_spans=(BlockSpan(chunk_id=10, start_char=0, end_char=70),),
        ),
        answer=StructureUnit(
            kind="answer",
            label="1",
            document_role="solution",
            start_chunk=20,
            end_chunk=20,
            start_char=0,
            end_char=len(_ANSWER),
            confidence=0.97,
            block_spans=(BlockSpan(chunk_id=20, start_char=0, end_char=len(_ANSWER)),),
        ),
    )


def _mint_plan() -> MintPlan:
    return MintPlan(
        concept_id=7,
        concept_slug="porters-five-forces",
        authored_symbols=[],
        minted_entity_ids={},
        merged_entity_keys=[],
        prereq_pairs=[],
        misconception_keys=[],
    )


def _patch_downstream(monkeypatch, *, pair_passes: bool = True):
    calls = {"verify": 0, "validate": 0, "mint": 0, "promote": []}
    tier1 = SimpleNamespace(id=99, payload={}, provenance={})

    async def _find_tier1(*_args, **_kwargs):
        return tier1

    async def _verify(*_args, **_kwargs):
        calls["verify"] += 1
        return VerificationVerdict(review_required=False)

    async def _validate(*_args, **_kwargs):
        calls["validate"] += 1
        return PairingVerdict(paired=pair_passes, faithful=pair_passes, confidence=1.0)

    async def _tag(*_args, **_kwargs):
        calls["mint"] += 1
        return _mint_plan()

    async def _hashes(*_args, **_kwargs):
        return set()

    async def _promote(*_args, **kwargs):
        calls["promote"].append(kwargs)
        return PromoteResult(promoted=True)

    async def _match(*_args, **_kwargs):
        return ConceptMatch(
            concept_id=7,
            slug="porters-five-forces",
            confidence=0.99,
            rationale="explicit force vocabulary",
            no_match=False,
        )

    async def _derive(*_args, **_kwargs):
        return DerivedGraph(reference_solution=_REFERENCE, target_unknown="competitive rivalry")

    monkeypatch.setattr(orch, "_find_tier1_row", _find_tier1)
    monkeypatch.setattr(orch, "verify_against_generated", _verify)
    monkeypatch.setattr(orch, "validate_pair", _validate)
    monkeypatch.setattr(orch, "tag_and_mint", _tag)
    monkeypatch.setattr(orch, "_authored_concept_dup_hashes", _hashes)
    monkeypatch.setattr(orch, "promote", _promote)
    monkeypatch.setattr(orch, "match_concept", _match)
    monkeypatch.setattr(orch, "derive_reference_graph", _derive)
    return calls, tier1


async def _process(*, reversed_mode: bool, structure_pairs, monkeypatch):
    return await orch._process_authored_candidate(
        _DB(),
        neo=None,
        candidate=_candidate(),
        concept_id=3,
        search_space_id=5,
        solution_document_id=55,
        label_index={},
        page_conf={2: 0.95},
        problem_low_conf=False,
        metered_chat=_Metered(),
        embed_fn=lambda _text: [0.0],
        conf_threshold=0.6,
        registered=(),
        reversed_mode=reversed_mode,
        solution_chunks=((20, _ANSWER, 2),),
        structure_pairs=structure_pairs,
    )


@pytest.mark.parametrize("reversed_mode", [False, True], ids=["legacy", "reversed"])
@pytest.mark.asyncio
async def test_structure_pair_promotes_as_llm_paired_and_executes_both_gates(
    monkeypatch, reversed_mode
):
    calls, _tier1 = _patch_downstream(monkeypatch)

    result = await _process(
        reversed_mode=reversed_mode, structure_pairs=(_pair(),), monkeypatch=monkeypatch
    )

    assert result.outcome == "promoted"
    assert result.solution_source == "llm_paired"
    assert result.match_method == "structure"
    assert calls["verify"] == 1
    assert calls["validate"] == 1
    assert calls["mint"] == 1
    assert calls["promote"][0]["solution_source"] == "llm_paired"


@pytest.mark.parametrize("reversed_mode", [False, True], ids=["legacy", "reversed"])
@pytest.mark.asyncio
async def test_structure_pair_gate_failure_rejects(monkeypatch, reversed_mode):
    calls, _tier1 = _patch_downstream(monkeypatch, pair_passes=False)

    result = await _process(
        reversed_mode=reversed_mode, structure_pairs=(_pair(),), monkeypatch=monkeypatch
    )

    assert result.outcome == "rejected"
    assert result.solution_source == "llm_paired"
    assert calls["verify"] == 1
    assert calls["validate"] == 1
    assert calls["mint"] == 0
    assert calls["promote"] == []


@pytest.mark.parametrize("reversed_mode", [False, True], ids=["legacy", "reversed"])
@pytest.mark.asyncio
async def test_no_structure_pair_generates_and_holds(monkeypatch, reversed_mode):
    calls, tier1 = _patch_downstream(monkeypatch)

    async def _empty_semantic(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "apollo.provisioning.authored_sets.paired_retrieval._doc_scoped_semantic",
        _empty_semantic,
    )
    result = await _process(
        reversed_mode=reversed_mode, structure_pairs=(), monkeypatch=monkeypatch
    )

    assert result.outcome == "held_for_review"
    assert result.solution_source == "generated"
    assert result.reason == "generated_no_match"
    assert calls["verify"] == 0
    assert calls["validate"] == 0
    assert calls["mint"] == 0
    assert tier1.provenance["authored_review"]["ocr_draft"]["solution_source"] == "generated"
