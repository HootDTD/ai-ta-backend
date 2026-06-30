import sys

import pytest

from apollo.persistence.models import Concept, ConceptProblem, Subject
from apollo.provisioning.authored_sets.orchestrator import run_authored_set_provisioning
from apollo.provisioning.pairing_gate import PairingVerdict
from apollo.provisioning.promote import PromoteResult
from apollo.provisioning.scrape import CandidateQuestion, ScrapeResult
from apollo.provisioning.solution import ReferenceSolutionDraft
from apollo.provisioning.tag_mint import MintPlan
from database.models import AITAChunk, AITADocument, SearchSpace

orch = sys.modules["apollo.provisioning.authored_sets.orchestrator"]


class _FakeAuthoredMC:
    def scrape_chat_fn(self, _system_prompt):
        return lambda _content: "[]"

    def main(self, **_k):
        return "{}"

    def cheap(self, **_k):
        return '{"equivalent": false, "reason": "different answer"}'


async def _seed_search_space(db, *, slug: str) -> int:
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    return int(space.id)


async def _seed_concept(db, *, search_space_id: int, slug: str = "authored") -> int:
    subject = Subject(
        slug=f"subject-{slug}", display_name="Physics", search_space_id=search_space_id
    )
    db.add(subject)
    await db.flush()
    concept = Concept(
        subject_id=subject.id,
        slug=slug,
        display_name="Authored",
        canonical_symbols={"symbols": ["M"]},
        normalization_map={},
    )
    db.add(concept)
    await db.flush()
    return int(concept.id)


async def _seed_doc_with_chunk(
    db,
    search_space_id: int,
    content: str,
    *,
    title: str,
    ocr_conf: float = 0.95,
) -> int:
    doc = AITADocument(
        title=title,
        content=content,
        content_hash=f"{title}-{search_space_id}",
        search_space_id=search_space_id,
        document_metadata={"page_debug": [{"page": 1, "ocr_confidence": ocr_conf}]},
    )
    db.add(doc)
    await db.flush()
    db.add(AITAChunk(document_id=doc.id, content=content, page_number=1))
    await db.flush()
    return int(doc.id)


def _candidate(*, document_id: int, chash: str = "aas-chunk") -> CandidateQuestion:
    return CandidateQuestion(
        problem_text="1. A beam length L, load w. Find max moment M.",
        given_values={"L": 2.0, "w": 3.0},
        target_unknown="M",
        difficulty="intro",
        document_id=document_id,
        page=1,
        chunk_content_hash=chash,
        concept_slug="provisional.inventory",
        label="1",
    )


def _draft(*, source: str = "extracted", symbolic: str = "M = w*L^2/8") -> ReferenceSolutionDraft:
    return ReferenceSolutionDraft(
        solution_source=source,  # type: ignore[arg-type]
        reference_solution=[
            {
                "step": 1,
                "id": "eq1",
                "entry_type": "equation",
                "content": {"symbolic": symbolic},
                "depends_on": [],
            }
        ],
        grounding=(),
        provenance={},
    )


def _mint_plan(concept_id: int) -> MintPlan:
    return MintPlan(
        concept_id=concept_id,
        concept_slug="authored",
        authored_symbols=["M"],
        minted_entity_ids={},
        merged_entity_keys=[],
        prereq_pairs=[],
        misconception_keys=[],
    )


def _patch_common_stages(monkeypatch, *, candidate: CandidateQuestion, concept_id: int):
    async def _resolve_prov(db, *, search_space_id):  # noqa: ANN001
        return concept_id

    async def _scrape_document(chunk_rows, **_kwargs):  # noqa: ANN001
        assert chunk_rows
        return ScrapeResult(candidates=(candidate,), scraped_count=1, parse_failures=0)

    async def _validate_pair(question, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve_prov)
    monkeypatch.setattr(orch, "scrape_document", _scrape_document)
    monkeypatch.setattr(orch, "validate_pair", _validate_pair)


@pytest.mark.asyncio
async def test_authored_set_label_extract_promotes(db_session, monkeypatch):
    space = await _seed_search_space(db_session, slug="aas1")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title="Problems",
    )
    sol_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "Solution 1\nM = w*L^2/8 by summing moments.",
        title="Solutions",
        ocr_conf=0.95,
    )
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)

    async def _find_or_generate(db, question, *, retrieve_fn, chat_fn):  # noqa: ANN001
        spans = await retrieve_fn(question)
        assert len(spans) == 1
        assert spans[0].carries_solution is True
        draft = _draft(source="extracted")
        return draft.model_copy(update={"grounding": spans})

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        return _mint_plan(concept_id)

    async def _promote(db, neo, **kwargs):  # noqa: ANN001
        row = await db.get(ConceptProblem, kwargs["concept_problem_id"])
        row.tier = 2
        return PromoteResult(promoted=True)

    monkeypatch.setattr(orch, "find_or_generate", _find_or_generate)
    monkeypatch.setattr(orch, "tag_and_mint", _tag_and_mint)
    monkeypatch.setattr(orch, "promote", _promote)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeAuthoredMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 1, "rejected": 0, "held_for_review": 0}
    assert len(report.problems) == 1
    result = report.problems[0]
    assert result.outcome == "promoted"
    assert result.solution_source == "extracted"
    assert result.match_method == "label"
    assert result.ocr_confidence == 0.95
    assert result.concept_problem_id is not None


@pytest.mark.asyncio
async def test_authored_set_low_confidence_divergence_holds_without_minting(
    db_session, monkeypatch
):
    import apollo.provisioning.authored_sets.verification as verification

    space = await _seed_search_space(db_session, slug="aas2")
    concept_id = await _seed_concept(db_session, search_space_id=space, slug="authored2")
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title="Problems Low",
    )
    sol_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "Solution 1\nM = w*L^2/9 by summing moments.",
        title="Solutions Low",
        ocr_conf=0.20,
    )
    candidate = _candidate(document_id=prob_doc, chash="aas-low")
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    minted = False
    promoted = False

    async def _find_or_generate(db, question, *, retrieve_fn, chat_fn):  # noqa: ANN001
        spans = await retrieve_fn(question)
        return _draft(source="extracted", symbolic="M = w*L^2/9").model_copy(
            update={"grounding": spans}
        )

    async def _generated_alt(*_args, **_kwargs):
        return _draft(source="generated", symbolic="M = w*L^2/8")

    async def _tag_and_mint(*_args, **_kwargs):
        nonlocal minted
        minted = True
        return _mint_plan(concept_id)

    async def _promote(*_args, **_kwargs):
        nonlocal promoted
        promoted = True
        return PromoteResult(promoted=True)

    monkeypatch.setattr(orch, "find_or_generate", _find_or_generate)
    monkeypatch.setattr(verification, "_independent_generate", _generated_alt)
    monkeypatch.setattr(orch, "tag_and_mint", _tag_and_mint)
    monkeypatch.setattr(orch, "promote", _promote)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeAuthoredMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    result = report.problems[0]
    assert result.outcome == "held_for_review"
    assert result.review_required is True
    assert result.reason == "ocr_divergence"
    assert result.ocr_confidence == 0.20
    assert minted is False
    assert promoted is False
    tier1 = await db_session.get(ConceptProblem, result.concept_problem_id)
    assert tier1.tier == 1
    review = tier1.provenance["authored_review"]
    assert review["required"] is True
    assert review["reason"] == "ocr_divergence"
