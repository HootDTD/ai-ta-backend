import json
import logging
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apollo.persistence.models import Concept, ConceptProblem, DedupDecision, KGEntity, Subject
from apollo.provisioning.authored_sets.orchestrator import run_authored_set_provisioning
from apollo.provisioning.authored_sets.structure_pass import (
    BlockSpan,
    StructurePair,
    StructurePassResult,
    StructureUnit,
)
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


class _StructureShadowMC:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.tokens = 0
        self.failure = failure
        self.structure_calls = 0

    def cumulative_tokens(self) -> int:
        return self.tokens

    def main(self, **_kwargs):  # noqa: ANN001 - patched stages never invoke it,
        # but the orchestrator evaluates metered_chat.main eagerly at call sites
        return "{}"

    def scrape_chat_fn(self, _system_prompt):
        return lambda _content: "[]"

    def cheap(self, *, purpose, **_kwargs):  # noqa: ANN001
        assert purpose == "structure_pass"
        self.structure_calls += 1
        if self.failure is not None:
            raise self.failure
        self.tokens += 10
        return '{"units": []}'


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


def test_augmented_hold_payload_transform_is_conditional_and_preserves_originals():
    original = {"problem_text": "Define X.", "target_unknown": "X", "other": 1}
    plain = _draft(source="generated")
    assert orch._augmented_hold_payload(original, plain) is original

    augmented = plain.model_copy(
        update={
            "augmented_problem_text": "Define X and explain why it occurs.",
            "augmented_target_unknown": "why X occurs",
        }
    )
    assert orch._augmented_hold_payload(original, augmented) == {
        "problem_text": "Define X and explain why it occurs.",
        "problem_text_original": "Define X.",
        "target_unknown": "why X occurs",
        "target_unknown_original": "X",
        "augmented": "explain_why",
        "other": 1,
    }

    no_target = augmented.model_copy(update={"augmented_target_unknown": None})
    assert orch._augmented_hold_payload(None, no_target) == {
        "problem_text": "Define X and explain why it occurs.",
        "problem_text_original": None,
        "augmented": "explain_why",
    }


def _patch_common_stages(monkeypatch, *, candidate: CandidateQuestion, concept_id: int):
    # These tests pin the LEGACY (LLM-tag-draft) path; the seeded concept would
    # otherwise flip the run into reversed mode. Reversed-mode behavior has its
    # own tests below.
    monkeypatch.setenv("APOLLO_REVERSED_PROVISIONING", "0")

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
    monkeypatch.delenv("APOLLO_STRUCTURE_PAIRING", raising=False)
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

    async def _find_or_generate(  # noqa: ANN001
        db, question, *, retrieve_fn, chat_fn, augment_recall=False
    ):
        assert augment_recall is True
        spans = await retrieve_fn(question)
        assert len(spans) == 1
        assert spans[0].carries_solution is True
        draft = _draft(source="extracted")
        return draft.model_copy(update={"grounding": spans})

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn):  # noqa: ANN001
        return _mint_plan(concept_id)

    promote_kwargs: dict = {}

    async def _promote(db, neo, **kwargs):  # noqa: ANN001
        promote_kwargs.update(kwargs)
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
    # The default-off flag does not even read a token accessor (the fake has no
    # such method) and preserves the pre-PR1 serialized report shape.
    assert "structure_pass" not in report.model_dump()
    assert len(report.problems) == 1
    result = report.problems[0]
    assert result.outcome == "promoted"
    assert result.solution_source == "extracted"
    # The orchestrator threads the paired-EXTRACTED provenance into promote (so the
    # persisted apollo_concept_problems row records "extracted", not the generic
    # "generated" default). promote's own persistence is covered in test_promote.py.
    assert promote_kwargs["solution_source"] == "extracted"
    assert result.match_method == "label"
    assert result.ocr_confidence == 0.95
    assert result.concept_problem_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "on"])
async def test_structure_modes_run_pass_once_and_stash_bounded_summary(
    db_session, monkeypatch, mode
):
    space = await _seed_search_space(db_session, slug=f"aas-structure-{mode}")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title=f"Structure {mode}",
    )
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", mode)

    async def _unchanged_candidate(*_args, **_kwargs):
        return orch.ProblemResult(label="1", outcome="held_for_review", review_required=True)

    monkeypatch.setattr(orch, "_process_authored_candidate", _unchanged_candidate)
    metered = _StructureShadowMC()

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=metered,
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    assert report.problems[0].outcome == "held_for_review"
    assert report.model_dump()["structure_pass"] == {
        "unit_count": 0,
        "kind_counts": {"question": 0, "answer": 0, "other": 0},
        "paired_label_count": 0,
        "paired_labels": (),
    }
    assert metered.structure_calls == 1


def _one_structure_result(*, budget_exhausted: bool = False) -> StructurePassResult:
    question = StructureUnit(
        kind="question",
        label="1",
        document_role="problem",
        start_chunk=1,
        end_chunk=1,
        start_char=0,
        end_char=10,
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=1, start_char=0, end_char=10),),
    )
    answer = StructureUnit(
        kind="answer",
        label="1",
        document_role="solution",
        start_chunk=2,
        end_chunk=2,
        start_char=0,
        end_char=10,
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=2, start_char=0, end_char=10),),
    )
    pair = StructurePair(label="1", question=question, answer=answer)
    return StructurePassResult(
        units=(question, answer),
        pairs=(pair,),
        tokens_spent=10,
        budget_exhausted=budget_exhausted,
    )


def _combined_structure_result(chunks) -> StructurePassResult:  # noqa: ANN001
    units: list[StructureUnit] = []
    pairs: list[StructurePair] = []
    for index, chunk in enumerate(chunks, start=1):
        content = str(chunk.content)
        answer_start = content.index("Answer")
        question = StructureUnit(
            kind="question",
            label=str(index),
            document_role="problem",
            start_chunk=int(chunk.id),
            end_chunk=int(chunk.id),
            start_char=0,
            end_char=answer_start,
            confidence=0.99,
            block_spans=(BlockSpan(chunk_id=int(chunk.id), start_char=0, end_char=answer_start),),
        )
        answer = StructureUnit(
            kind="answer",
            label=str(index),
            document_role="problem",
            start_chunk=int(chunk.id),
            end_chunk=int(chunk.id),
            start_char=answer_start,
            end_char=len(content),
            confidence=0.99,
            block_spans=(
                BlockSpan(
                    chunk_id=int(chunk.id),
                    start_char=answer_start,
                    end_char=len(content),
                ),
            ),
        )
        units.extend((question, answer))
        pairs.append(StructurePair(label=str(index), question=question, answer=answer))
    return StructurePassResult(
        units=tuple(units),
        pairs=tuple(pairs),
        tokens_spent=200,
    )


def test_question_mask_subtracts_explicit_answer_overlap_and_preserves_provenance():
    content = "Question 1: Which force applies?\nAnswer 1: PORTER_SECRET"
    answer_start = content.index("Answer")
    chunk = SimpleNamespace(
        id=17,
        content=content,
        document_id=9,
        page_number=3,
        section_path="Question 1",
        chunk_type="body",
    )
    question = StructureUnit(
        kind="question",
        label="1",
        document_role="problem",
        start_chunk=17,
        end_chunk=17,
        start_char=0,
        end_char=len(content),
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=17, start_char=0, end_char=len(content)),),
    )
    answer = StructureUnit(
        kind="answer",
        label="1",
        document_role="problem",
        start_chunk=17,
        end_chunk=17,
        start_char=answer_start,
        end_char=len(content),
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=17, start_char=answer_start, end_char=len(content)),),
    )

    masked = orch._question_only_chunks((chunk,), (question, answer))

    assert len(masked) == 1
    assert masked[0].content == content[:answer_start]
    assert (masked[0].id, masked[0].document_id, masked[0].page_number) == (17, 9, 3)
    assert "PORTER_SECRET" not in masked[0].content


def test_question_mask_does_not_truncate_midline_answer_prose():
    content = (
        "Question 1: Review the choices below.\n"
        "Explain which answer best describes competitive rivalry."
    )
    chunk = SimpleNamespace(id=18, content=content, document_id=9)
    question = StructureUnit(
        kind="question",
        label="1",
        document_role="problem",
        start_chunk=18,
        end_chunk=18,
        start_char=0,
        end_char=len(content),
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=18, start_char=0, end_char=len(content)),),
    )

    masked = orch._question_only_chunks((chunk,), (question,))

    assert len(masked) == 1
    assert masked[0].content == content


@pytest.mark.asyncio
async def test_answer_line_backstop_does_not_break_proper_structure_pairing(caplog):
    oversized_content = "Question 1: Which force applies?\nAnswer: LEAKED_TAIL"
    proper_content = "Question 2: When is rivalry strongest?\nAnswer: when offers converge"
    answer_start = proper_content.index("Answer:")
    chunks = (
        SimpleNamespace(id=31, content=oversized_content, document_id=9, page_number=1),
        SimpleNamespace(id=32, content=proper_content, document_id=9, page_number=2),
    )
    oversized_question = StructureUnit(
        kind="question",
        label="1",
        document_role="problem",
        start_chunk=31,
        end_chunk=31,
        start_char=0,
        end_char=len(oversized_content),
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=31, start_char=0, end_char=len(oversized_content)),),
    )
    proper_question = StructureUnit(
        kind="question",
        label="2",
        document_role="problem",
        start_chunk=32,
        end_chunk=32,
        start_char=0,
        end_char=answer_start,
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=32, start_char=0, end_char=answer_start),),
    )
    proper_answer = StructureUnit(
        kind="answer",
        label="2",
        document_role="problem",
        start_chunk=32,
        end_chunk=32,
        start_char=answer_start,
        end_char=len(proper_content),
        confidence=0.9,
        block_spans=(
            BlockSpan(chunk_id=32, start_char=answer_start, end_char=len(proper_content)),
        ),
    )
    pair = StructurePair(label="2", question=proper_question, answer=proper_answer)
    caplog.set_level(logging.WARNING, logger=orch.__name__)

    masked = orch._question_only_chunks(
        chunks, (oversized_question, proper_question, proper_answer)
    )
    retrieve = orch.make_paired_solution_retrieve_fn(
        None,
        solution_document_id=9,
        label_index={},
        page_conf={2: 0.95},
        solution_chunks=((32, proper_content, 2),),
        structure_pairs=(pair,),
        structure_only=True,
    )
    candidate = CandidateQuestion(
        problem_text=masked[1].content,
        given_values={},
        target_unknown="competitive rivalry",
        difficulty="intro",
        document_id=9,
        page=2,
        chunk_content_hash="proper-pair",
        concept_slug="provisional.inventory",
        label="2",
    )

    spans = await retrieve(candidate)

    assert masked[0].content.strip() == "Question 1: Which force applies?"
    assert "LEAKED_TAIL" not in masked[0].content
    assert masked[1].content == proper_content[:answer_start]
    assert len(spans) == 1
    assert spans[0].text == proper_content[answer_start:]
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "authored_set_combined_answer_line_backstop"
    ]
    assert len(records) == 1
    assert records[0].count == 1


@pytest.mark.asyncio
async def test_oversized_question_without_answer_unit_is_masked_before_tier1(
    db_session, monkeypatch, caplog
):
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", "on")
    monkeypatch.setenv("APOLLO_REVERSED_PROVISIONING", "0")
    space = await _seed_search_space(db_session, slug="combined-answer-line-backstop")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    leaked_tail = "PORTER_SECRET_WITHOUT_ANSWER_UNIT"
    raw_text = (
        "Question 1: Which competitive force applies?\n"
        f"Answer: {leaked_tail}\n"
        "Explanation: this tail must also be removed."
    )
    prob_doc = await _seed_doc_with_chunk(
        db_session, space, raw_text, title="Oversized question unit"
    )
    chunk = (await orch._load_chunks(db_session, document_id=prob_doc))[0]
    oversized_question = StructureUnit(
        kind="question",
        label="1",
        document_role="problem",
        start_chunk=int(chunk.id),
        end_chunk=int(chunk.id),
        start_char=0,
        end_char=len(raw_text),
        confidence=0.9,
        block_spans=(BlockSpan(chunk_id=int(chunk.id), start_char=0, end_char=len(raw_text)),),
    )
    structure_result = StructurePassResult(units=(oversized_question,), pairs=(), tokens_spent=10)
    scraped_candidates: tuple[CandidateQuestion, ...] = ()

    async def _resolve(*_args, **_kwargs):
        return concept_id

    async def _scrape_document(chunk_rows, **_kwargs):  # noqa: ANN001
        nonlocal scraped_candidates
        assert len(chunk_rows) == 1
        assert leaked_tail not in chunk_rows[0].content
        assert "Explanation:" not in chunk_rows[0].content
        scraped_candidates = (
            CandidateQuestion(
                problem_text=chunk_rows[0].content.strip(),
                given_values={},
                target_unknown="competitive force",
                difficulty="intro",
                document_id=prob_doc,
                page=1,
                chunk_content_hash="answer-line-backstop",
                concept_slug="provisional.inventory",
                label="1",
            ),
        )
        return ScrapeResult(candidates=scraped_candidates, scraped_count=1, parse_failures=0)

    async def _process_candidate(*_args, **kwargs):  # noqa: ANN001
        assert kwargs["solution_document_id"] is None
        assert kwargs["structure_pairs"] == ()
        assert kwargs["structure_only"] is False
        return orch.ProblemResult(label="1", outcome="held_for_review", solution_source="generated")

    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve)
    monkeypatch.setattr(orch, "run_structure_pass", lambda **_kwargs: structure_result)
    monkeypatch.setattr(orch, "scrape_document", _scrape_document)
    monkeypatch.setattr(orch, "_process_authored_candidate", _process_candidate)
    caplog.set_level(logging.WARNING, logger=orch.__name__)

    await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=_StructureShadowMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert len(scraped_candidates) == 1
    assert leaked_tail not in scraped_candidates[0].problem_text
    payload = (
        await db_session.execute(
            select(ConceptProblem.payload).where(ConceptProblem.concept_id == concept_id)
        )
    ).scalar_one()
    assert leaked_tail not in payload["problem_text"]
    assert "Explanation:" not in payload["problem_text"]
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "authored_set_combined_answer_line_backstop"
    ]
    assert len(records) == 1
    assert records[0].count == 1


@pytest.mark.asyncio
async def test_problem_only_combined_porter_masks_answers_before_tier1_write(
    db_session, monkeypatch
):
    """The 20-question combined-doc safety boundary: answer text never reaches
    scrape input or the persisted student-facing Tier-1 payloads."""
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", "on")
    monkeypatch.setenv("APOLLO_REVERSED_PROVISIONING", "0")
    space = await _seed_search_space(db_session, slug="combined-porter")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    doc = AITADocument(
        title="Combined Porter Q&A",
        content="20 Porter questions and answers",
        content_hash="combined-porter-hash",
        search_space_id=space,
        document_metadata={"page_debug": [{"page": 1, "ocr_confidence": 0.95}]},
    )
    db_session.add(doc)
    await db_session.flush()
    chunks = []
    answer_texts = []
    for index in range(1, 21):
        answer = f"Answer {index}: PORTER_SECRET_{index}"
        answer_texts.append(answer)
        chunk = AITAChunk(
            document_id=doc.id,
            content=f"Question {index}: Which force applies?\n{answer}",
            page_number=index,
            section_path=f"Question {index}",
        )
        db_session.add(chunk)
        chunks.append(chunk)
    await db_session.flush()
    structure_result = _combined_structure_result(chunks)
    pass_calls = 0

    async def _resolve(*_args, **_kwargs):
        return concept_id

    def _run_structure_pass(**kwargs):  # noqa: ANN001
        nonlocal pass_calls
        pass_calls += 1
        assert kwargs["scrape_spend"] == 0
        assert "solution_chunks" not in kwargs
        return structure_result

    scraped_candidates: tuple[CandidateQuestion, ...] = ()

    async def _scrape_document(chunk_rows, **_kwargs):  # noqa: ANN001
        nonlocal scraped_candidates
        assert [row.id for row in chunk_rows] == [chunk.id for chunk in chunks]
        assert [row.page_number for row in chunk_rows] == list(range(1, 21))
        assert all("PORTER_SECRET" not in row.content for row in chunk_rows)
        scraped_candidates = tuple(
            CandidateQuestion(
                problem_text=row.content.strip(),
                given_values={},
                target_unknown="competitive force",
                difficulty="intro",
                document_id=int(doc.id),
                page=row.page_number,
                chunk_content_hash=f"combined-{index}",
                concept_slug="provisional.inventory",
                label=str(index),
            )
            for index, row in enumerate(chunk_rows, start=1)
        )
        return ScrapeResult(
            candidates=scraped_candidates,
            scraped_count=len(scraped_candidates),
            parse_failures=0,
        )

    async def _process_candidate(*_args, candidate, **kwargs):  # noqa: ANN001
        assert kwargs["solution_document_id"] == int(doc.id)
        assert kwargs["structure_only"] is True
        assert kwargs["label_index"] == {}
        retrieve = orch.make_paired_solution_retrieve_fn(
            None,
            solution_document_id=kwargs["solution_document_id"],
            label_index=kwargs["label_index"],
            page_conf=kwargs["page_conf"],
            solution_chunks=kwargs["solution_chunks"],
            structure_pairs=kwargs["structure_pairs"],
            structure_only=kwargs["structure_only"],
        )
        spans = await retrieve(candidate)
        assert len(spans) == 1
        assert spans[0].text == answer_texts[int(candidate.label) - 1]
        assert "Question" not in spans[0].text
        return orch.ProblemResult(
            label=candidate.label,
            outcome="held_for_review",
            solution_source="llm_paired",
            review_required=True,
        )

    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve)
    monkeypatch.setattr(orch, "run_structure_pass", _run_structure_pass)
    monkeypatch.setattr(orch, "scrape_document", _scrape_document)
    monkeypatch.setattr(orch, "_process_authored_candidate", _process_candidate)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=int(doc.id),
        solution_document_id=None,
        metered_chat=_StructureShadowMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert pass_calls == 1
    assert len(scraped_candidates) == 20
    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 20}
    payloads = (
        (
            await db_session.execute(
                select(ConceptProblem.payload).where(ConceptProblem.concept_id == concept_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(payloads) == 20
    for candidate in scraped_candidates:
        assert all(answer not in candidate.problem_text for answer in answer_texts)
    for payload in payloads:
        assert all(answer not in payload["problem_text"] for answer in answer_texts)


@pytest.mark.asyncio
async def test_problem_only_zero_answer_units_keeps_generate_and_hold_flow(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", "on")
    space = await _seed_search_space(db_session, slug="combined-zero-answer")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    problem_text = "1. A beam length L, load w. Find max moment M."
    prob_doc = await _seed_doc_with_chunk(db_session, space, problem_text, title="Questions only")
    candidate = _candidate(document_id=prob_doc, chash="zero-answer")
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    only_question = _one_structure_result().model_copy(
        update={"units": (_one_structure_result().pairs[0].question,), "pairs": ()}
    )
    pass_calls = 0

    def _run_structure_pass(**_kwargs):
        nonlocal pass_calls
        pass_calls += 1
        return only_question

    async def _scrape_full(chunk_rows, **_kwargs):  # noqa: ANN001
        assert len(chunk_rows) == 1
        assert chunk_rows[0].content == problem_text
        return ScrapeResult(candidates=(candidate,), scraped_count=1, parse_failures=0)

    async def _find_or_generate(
        _db,
        question,
        *,
        retrieve_fn,
        chat_fn,
        augment_recall=False,  # noqa: ANN001
    ):
        assert await retrieve_fn(question) == ()
        return _draft(source="generated")

    monkeypatch.setattr(orch, "run_structure_pass", _run_structure_pass)
    monkeypatch.setattr(orch, "scrape_document", _scrape_full)
    monkeypatch.setattr(orch, "find_or_generate", _find_or_generate)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=_StructureShadowMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert pass_calls == 1
    assert report.problems[0].outcome == "held_for_review"
    assert report.problems[0].solution_source == "generated"
    assert report.problems[0].reason == "generated_no_match"


@pytest.mark.asyncio
async def test_combined_segmentation_failure_falls_back_to_full_scrape_without_pairing(
    db_session, monkeypatch
):
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", "on")
    space = await _seed_search_space(db_session, slug="combined-segment-failure")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    raw_text = "Question 1: Which force?\nAnswer 1: rivalry"
    prob_doc = await _seed_doc_with_chunk(db_session, space, raw_text, title="Combined failure")
    candidate = _candidate(document_id=prob_doc, chash="segment-failure")
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)

    def _segment_failure(**_kwargs):
        raise RuntimeError("segmentation unavailable")

    async def _scrape_full(chunk_rows, **_kwargs):  # noqa: ANN001
        assert len(chunk_rows) == 1
        assert chunk_rows[0].content == raw_text
        return ScrapeResult(candidates=(candidate,), scraped_count=1, parse_failures=0)

    async def _find_or_generate(
        _db,
        question,
        *,
        retrieve_fn,
        chat_fn,
        augment_recall=False,  # noqa: ANN001
    ):
        assert await retrieve_fn(question) == ()
        return _draft(source="generated")

    monkeypatch.setattr(orch, "run_structure_pass", _segment_failure)
    monkeypatch.setattr(orch, "scrape_document", _scrape_full)
    monkeypatch.setattr(orch, "find_or_generate", _find_or_generate)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=prob_doc,
        combined_document=True,
        metered_chat=_StructureShadowMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert report.problems[0].outcome == "held_for_review"
    assert report.problems[0].solution_source == "generated"
    assert "structure_pass" not in report.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "budget_exhausted", "expected_pairs"),
    [("shadow", False, 0), ("on", True, 0), ("on", False, 1)],
)
async def test_structure_pairs_consumed_only_when_on_and_complete(
    db_session, monkeypatch, mode, budget_exhausted, expected_pairs
):
    space = await _seed_search_space(
        db_session, slug=f"structure-consume-{mode}-{budget_exhausted}"
    )
    concept_id = await _seed_concept(db_session, search_space_id=space)
    prob_doc = await _seed_doc_with_chunk(db_session, space, "1. Porter question", title="P")
    sol_doc = await _seed_doc_with_chunk(db_session, space, "Answer: rivalry", title="S")
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", mode)
    monkeypatch.setattr(
        orch,
        "run_structure_pass",
        lambda **_kwargs: _one_structure_result(budget_exhausted=budget_exhausted),
    )
    received: list = []

    async def _capture_candidate(*_args, structure_pairs=(), **_kwargs):
        received.extend(structure_pairs)
        return orch.ProblemResult(label="1", outcome="held_for_review", review_required=True)

    monkeypatch.setattr(orch, "_process_authored_candidate", _capture_candidate)

    await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_StructureShadowMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert len(received) == expected_pairs


@pytest.mark.asyncio
async def test_structure_failure_never_escapes_or_changes_candidate_result(db_session, monkeypatch):
    space = await _seed_search_space(db_session, slug="aas-structure-failure")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title="Structure Failure",
    )
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", "shadow")

    async def _unchanged_candidate(*_args, **_kwargs):
        return orch.ProblemResult(label="1", outcome="held_for_review", review_required=True)

    monkeypatch.setattr(orch, "_process_authored_candidate", _unchanged_candidate)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=_StructureShadowMC(failure=RuntimeError("shadow failed")),
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    assert "structure_pass" not in report.model_dump()


@pytest.mark.asyncio
async def test_unaugmented_hold_payload_untouched(db_session, monkeypatch):
    """B1: no paired solution document (``solution_document_id=None``, e.g. the
    teacher never uploaded one) must retrieve NO spans and fall through to
    ``find_or_generate``'s generate branch — which is ALWAYS held for review
    (the product policy: a generated reference solution needs teacher
    approval before students see it) regardless of the reversed/legacy mode."""
    space = await _seed_search_space(db_session, slug="aas-nosol")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title="Problems",
    )
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)

    async def _find_or_generate(  # noqa: ANN001
        db, question, *, retrieve_fn, chat_fn, augment_recall=False
    ):
        assert augment_recall is True
        spans = await retrieve_fn(question)
        assert spans == ()
        return _draft(source="generated")

    monkeypatch.setattr(orch, "find_or_generate", _find_or_generate)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=_FakeAuthoredMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    assert len(report.problems) == 1
    result = report.problems[0]
    assert result.outcome == "held_for_review"
    assert result.solution_source == "generated"
    assert result.review_required is True
    assert result.reason == "generated_no_match"
    row = await db_session.get(ConceptProblem, result.concept_problem_id)
    assert row.payload == {
        "id": f"scrape.{candidate.chunk_content_hash}",
        "concept_id": candidate.concept_slug,
        "difficulty": candidate.difficulty,
        "problem_text": candidate.problem_text,
        "given_values": candidate.given_values,
        "target_unknown": candidate.target_unknown,
    }


@pytest.mark.asyncio
async def test_hold_arm_applies_augmentation_to_tier1_payload(db_session, monkeypatch):
    """A generated draft has no pair to validate (no grounding spans): the
    pairing-gate judge must NEVER be invoked for it, and the candidate must
    flow straight to held_for_review with the authored_review provenance
    written. Before the fix, ``validate_pair`` ran unconditionally and its
    Phase-B faithfulness check failed-closed on the empty grounding, rejecting
    every generated candidate outright."""
    space = await _seed_search_space(db_session, slug="aas-genskip")
    concept_id = await _seed_concept(db_session, search_space_id=space, slug="genskip")
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title="Problems",
    )
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)

    validate_pair_calls = {"n": 0}

    async def _validate_pair_spy(question, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        validate_pair_calls["n"] += 1
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    # Overrides _patch_common_stages' blanket-approve mock with a call-counting one.
    monkeypatch.setattr(orch, "validate_pair", _validate_pair_spy)

    async def _find_or_generate(  # noqa: ANN001
        db, question, *, retrieve_fn, chat_fn, augment_recall=False
    ):
        assert augment_recall is True
        spans = await retrieve_fn(question)
        assert spans == ()
        return _draft(source="generated").model_copy(
            update={
                "augmented_problem_text": "Define beam moment and explain why it peaks.",
                "augmented_target_unknown": "why beam moment peaks",
                "provenance": {"augmented": "explain_why"},
            }
        )

    monkeypatch.setattr(orch, "find_or_generate", _find_or_generate)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=_FakeAuthoredMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert validate_pair_calls["n"] == 0
    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    result = report.problems[0]
    assert result.outcome == "held_for_review"
    assert result.review_required is True
    assert result.reason == "generated_no_match"
    assert result.concept_problem_id is not None

    row = await db_session.get(ConceptProblem, result.concept_problem_id)
    authored_review = row.provenance["authored_review"]
    assert row.payload["problem_text"] == "Define beam moment and explain why it peaks."
    assert row.payload["target_unknown"] == "why beam moment peaks"
    assert row.payload["problem_text_original"] == candidate.problem_text
    assert row.payload["target_unknown_original"] == candidate.target_unknown
    assert row.payload["augmented"] == "explain_why"
    assert authored_review["required"] is True
    assert authored_review["reason"] == "generated_no_match"
    assert authored_review["augmented"] == "explain_why"
    assert authored_review["ocr_draft"]["solution_source"] == "generated"


def test_doc_is_low_conf_helper():
    from apollo.provisioning.authored_sets.orchestrator import _doc_is_low_conf

    assert _doc_is_low_conf({1: 0.3, 2: None}, 0.6) is True
    assert _doc_is_low_conf({1: 0.9}, 0.6) is False
    assert _doc_is_low_conf({1: None}, 0.6) is False  # no usable values


@pytest.mark.asyncio
async def test_authored_concept_dup_hashes_skips_invalid_payload(db_session):
    from apollo.provisioning.authored_sets.orchestrator import _authored_concept_dup_hashes

    space = await _seed_search_space(db_session, slug="dup")
    concept_id = await _seed_concept(db_session, search_space_id=space, slug="dup")
    db_session.add(
        ConceptProblem(
            concept_id=concept_id,
            problem_code="bad-payload",
            difficulty="intro",
            tier=2,
            payload={"not": "a valid problem"},
            search_space_id=space,
        )
    )
    await db_session.flush()
    hashes = await _authored_concept_dup_hashes(db_session, concept_id=concept_id)
    assert hashes == set()


@pytest.mark.asyncio
async def test_provisioning_defaults_embed_fn_when_omitted(db_session, monkeypatch):
    space = await _seed_search_space(db_session, slug="noembed")
    concept_id = await _seed_concept(db_session, search_space_id=space, slug="noembed")
    prob_doc = await _seed_doc_with_chunk(db_session, space, "nothing here", title="P")
    sol_doc = await _seed_doc_with_chunk(db_session, space, "nothing", title="S")

    async def _resolve(db, *, search_space_id):
        return concept_id

    async def _scrape(chunk_rows, **_k):
        return ScrapeResult(candidates=(), scraped_count=0, parse_failures=0)

    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve)
    monkeypatch.setattr(orch, "scrape_document", _scrape)
    # embed_fn omitted -> exercises the default-import branch.
    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeAuthoredMC(),
    )
    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 0}


async def _run_single_candidate(db, monkeypatch, *, slug, find_or_generate, **overrides):
    """Drive one extracted, high-confidence candidate through provisioning,
    letting the caller override find_or_generate / validate_pair / promote /
    tag_and_mint / _find_tier1_row to exercise a specific branch."""
    space = await _seed_search_space(db, slug=slug)
    concept_id = await _seed_concept(db, search_space_id=space, slug=slug)
    prob_doc = await _seed_doc_with_chunk(db, space, "1. A beam, find M.", title="P")
    sol_doc = await _seed_doc_with_chunk(
        db, space, "Solution 1\nM = w*L^2/8", title="S", ocr_conf=0.95
    )
    candidate = _candidate(document_id=prob_doc, chash=f"{slug}-chash")
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    monkeypatch.setattr(orch, "find_or_generate", find_or_generate)
    for name, fn in overrides.items():
        monkeypatch.setattr(orch, name, fn)
    return await run_authored_set_provisioning(
        db,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeAuthoredMC(),
        embed_fn=lambda _t: [0.0],
    )


@pytest.mark.asyncio
async def test_candidate_solution_draft_error_is_rejected(db_session, monkeypatch):
    from apollo.provisioning.solution import SolutionDraftError

    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        await retrieve_fn(question)
        raise SolutionDraftError("boom")

    report = await _run_single_candidate(db_session, monkeypatch, slug="sde", find_or_generate=_fog)
    assert report.counts["rejected"] == 1
    assert report.problems[0].diagnostic.startswith("solution_draft_error")


@pytest.mark.asyncio
async def test_candidate_tag_mint_error_is_rejected(db_session, monkeypatch):
    """A TagMintError for ONE candidate (e.g. the LLM prereq draft naming an
    unminted entity key like 'pressure_box_3') must reject just that candidate —
    not propagate out and fail the entire authored set."""
    from apollo.provisioning.tag_mint import TagMintError

    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        spans = await retrieve_fn(question)
        draft = _draft(source="extracted")
        return draft.model_copy(update={"grounding": spans})

    async def _raise_tag_mint(db, pair, *, chat_fn, embed_fn):
        raise TagMintError("prereq draft references an unminted entity key 'pressure_box_3'")

    report = await _run_single_candidate(
        db_session,
        monkeypatch,
        slug="tme",
        find_or_generate=_fog,
        tag_and_mint=_raise_tag_mint,
    )
    assert report.counts["rejected"] == 1
    result = report.problems[0]
    assert result.outcome == "rejected"
    assert result.diagnostic.startswith("tag_mint_error")
    assert "pressure_box_3" in result.diagnostic
    assert result.concept_problem_id is not None


class _TagMintFakeMC:
    """A metered_chat double whose ``cheap`` returns a valid tag/mint concept-tag
    payload (unlike ``_FakeAuthoredMC.cheap``, which returns a pairing-judge
    shape) — required so the REAL ``tag_and_mint`` (not mocked) can parse it."""

    def scrape_chat_fn(self, _system_prompt):
        return lambda _content: "[]"

    def main(self, **_k):
        return "{}"

    def cheap(self, *, purpose=None, **_k):
        return json.dumps({"concept_slug": "savepoint", "display_name": "Savepoint", "prereqs": []})


def _savepoint_candidate(*, document_id: int, label: str, chash: str) -> CandidateQuestion:
    return CandidateQuestion(
        problem_text=f"{label}. A beam length L, load w. Find max moment M.",
        given_values={"L": 2.0, "w": 3.0},
        target_unknown="M",
        difficulty="intro",
        document_id=document_id,
        page=1,
        chunk_content_hash=chash,
        concept_slug="provisional.inventory",
        label=label,
    )


def _savepoint_draft(*, step_id: str) -> ReferenceSolutionDraft:
    return ReferenceSolutionDraft(
        solution_source="extracted",
        reference_solution=[
            {
                "step": 1,
                "id": step_id,
                "entry_type": "equation",
                "content": {"symbolic": "M - w*L**2/8"},
                "depends_on": [],
            }
        ],
        grounding=(),
        provenance={},
    )


@pytest.mark.asyncio
async def test_tag_mint_partial_failure_rolls_back_via_savepoint(db_session, monkeypatch):
    """M1: a fail-closed TagMintError raised INSIDE the REAL ``tag_and_mint`` (at
    step 5a, ``link_opposes``) after concept/KGEntity/apollo_dedup_decisions rows
    for THAT candidate have already been flushed must not leave those rows
    orphaned once the caller's (simulated ``_run_set_background``) commit lands.
    The per-candidate ``begin_nested`` savepoint rolls back exactly the failed
    candidate's partial writes; a SIBLING candidate in the same run still mints
    and promotes. DISCRIMINATING: without the savepoint wrap, the failed
    candidate's ``eq.eq-fail`` KGEntity + its DedupDecision row would survive the
    outer commit even though no ConceptProblem was ever promoted for it."""
    from apollo.provisioning import tag_mint as tm
    from apollo.provisioning.tag_mint_persist import link_opposes as real_link_opposes

    monkeypatch.setenv("APOLLO_REVERSED_PROVISIONING", "0")
    space = await _seed_search_space(db_session, slug="savepoint")
    concept_id = await _seed_concept(db_session, search_space_id=space, slug="savepoint")
    prob_doc = await _seed_doc_with_chunk(
        db_session, space, "1. beam.\n2. beam.", title="P-savepoint"
    )
    sol_doc = await _seed_doc_with_chunk(
        db_session, space, "Solution 1\nM = w*L^2/8\nSolution 2\nM = w*L^2/8", title="S-savepoint"
    )
    cand_fail = _savepoint_candidate(document_id=prob_doc, label="1", chash="sp-fail")
    cand_ok = _savepoint_candidate(document_id=prob_doc, label="2", chash="sp-ok")

    async def _resolve_prov(db, *, search_space_id):
        return concept_id

    async def _scrape_document(chunk_rows, **_kwargs):
        return ScrapeResult(candidates=(cand_fail, cand_ok), scraped_count=2, parse_failures=0)

    async def _validate_pair(question, draft, *, retrieve_fn, judge_fn):
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _verify_ok(*_a, **_k):
        from apollo.provisioning.authored_sets.verification import VerificationVerdict

        return VerificationVerdict(review_required=False)

    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        step_id = "eq-fail" if question.label == "1" else "eq-ok"
        return _savepoint_draft(step_id=step_id)

    call_count = {"n": 0}

    async def _link_opposes_first_fails(db, *, concept_id, key_to_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise KeyError("misc.forced-failure")
        return await real_link_opposes(db, concept_id=concept_id, key_to_id=key_to_id)

    async def _promote(db, neo, **kwargs):
        row = await db.get(ConceptProblem, kwargs["concept_problem_id"])
        row.tier = 2
        return PromoteResult(promoted=True)

    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve_prov)
    monkeypatch.setattr(orch, "scrape_document", _scrape_document)
    monkeypatch.setattr(orch, "validate_pair", _validate_pair)
    monkeypatch.setattr(orch, "verify_against_generated", _verify_ok)
    monkeypatch.setattr(orch, "find_or_generate", _fog)
    monkeypatch.setattr(orch, "promote", _promote)
    monkeypatch.setattr(tm, "link_opposes", _link_opposes_first_fails)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_TagMintFakeMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 1, "rejected": 1, "held_for_review": 0}
    by_label = {r.label: r for r in report.problems}
    assert by_label["1"].outcome == "rejected"
    assert by_label["1"].diagnostic.startswith("tag_mint_error")
    assert by_label["2"].outcome == "promoted"

    # Simulate `_run_set_background`'s end-of-run commit over the WHOLE session.
    await db_session.commit()

    # The failed candidate's entity must NOT have survived the savepoint rollback
    # (orphaned KG rows unreachable by any promoted ConceptProblem, yet a live
    # dedup target for a later mint, is exactly the defect M1 fixes).
    fail_rows = (
        (await db_session.execute(select(KGEntity).where(KGEntity.canonical_key == "eq.eq-fail")))
        .scalars()
        .all()
    )
    assert fail_rows == []

    # The sibling candidate's mint DOES survive the commit.
    ok_rows = (
        (await db_session.execute(select(KGEntity).where(KGEntity.canonical_key == "eq.eq-ok")))
        .scalars()
        .all()
    )
    assert len(ok_rows) == 1

    # No apollo_dedup_decisions row from the rolled-back candidate leaks through
    # either (it would otherwise become a live dedup target for a future mint).
    dedup_rows = (
        (
            await db_session.execute(
                select(DedupDecision).where(DedupDecision.candidate_key == "eq.eq-fail")
            )
        )
        .scalars()
        .all()
    )
    assert dedup_rows == []


@pytest.mark.asyncio
async def test_candidate_pair_rejection_is_rejected(db_session, monkeypatch):
    from apollo.provisioning.pairing_gate import PairingVerdict

    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        spans = await retrieve_fn(question)
        return _draft(source="extracted").model_copy(update={"grounding": spans})

    async def _reject_pair(question, draft, *, retrieve_fn, judge_fn):
        return PairingVerdict(paired=False, faithful=False, confidence=0.1)

    report = await _run_single_candidate(
        db_session, monkeypatch, slug="rej", find_or_generate=_fog, validate_pair=_reject_pair
    )
    assert report.counts["rejected"] == 1


@pytest.mark.asyncio
async def test_candidate_missing_tier1_row_is_rejected(db_session, monkeypatch):
    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        spans = await retrieve_fn(question)
        return _draft(source="extracted").model_copy(update={"grounding": spans})

    async def _no_tier1(db, *, concept_id, chunk_content_hash):
        return None

    report = await _run_single_candidate(
        db_session, monkeypatch, slug="notier1", find_or_generate=_fog, _find_tier1_row=_no_tier1
    )
    assert report.counts["rejected"] == 1
    assert report.problems[0].diagnostic == "missing_tier1_row"


@pytest.mark.asyncio
async def test_candidate_promote_failure_is_rejected(db_session, monkeypatch):
    from apollo.provisioning.promote import PromoteResult

    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        spans = await retrieve_fn(question)
        return _draft(source="extracted").model_copy(update={"grounding": spans})

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn):
        return _mint_plan(0)

    async def _promote(db, neo, **kwargs):
        return PromoteResult(promoted=False, failed_gate=8, diagnostic="gate8 failed")

    report = await _run_single_candidate(
        db_session,
        monkeypatch,
        slug="promfail",
        find_or_generate=_fog,
        tag_and_mint=_tag_and_mint,
        promote=_promote,
        _authored_concept_dup_hashes=_noop_hashes,
    )
    assert report.counts["rejected"] == 1
    assert report.problems[0].failed_gate == 8


@pytest.mark.asyncio
async def test_candidate_gate9_unresolved_is_held_for_review(db_session, monkeypatch):
    from apollo.provisioning.promote import PromoteHeldForReview

    async def _fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        spans = await retrieve_fn(question)
        return _draft(source="extracted").model_copy(update={"grounding": spans})

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn):
        return _mint_plan(0)

    async def _promote(db, neo, **kwargs):
        return PromoteHeldForReview(
            promoted=False,
            failed_gate=9,
            diagnostic="gate 9: unresolved: solver timeout",
        )

    report = await _run_single_candidate(
        db_session,
        monkeypatch,
        slug="gate9hold",
        find_or_generate=_fog,
        tag_and_mint=_tag_and_mint,
        promote=_promote,
        _authored_concept_dup_hashes=_noop_hashes,
    )
    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    result = report.problems[0]
    assert result.outcome == "held_for_review"
    assert result.reason == "promotion_lint_unresolved"
    row = await db_session.get(ConceptProblem, result.concept_problem_id)
    assert row.tier == 1
    assert row.provenance["authored_review"]["reason"] == "promotion_lint_unresolved"


async def _noop_hashes(db, *, concept_id):
    return set()


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

    async def _find_or_generate(  # noqa: ANN001
        db, question, *, retrieve_fn, chat_fn, augment_recall=False
    ):
        assert augment_recall is True
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


@pytest.mark.asyncio
async def test_structure_snapshot_failure_skips_pass_and_run_proceeds(db_session, monkeypatch):
    """The pre-scrape ledger snapshot is part of the shadow setup: if it raises
    (orchestrator's first defensive arm), the pass is skipped entirely and the
    run is identical to flag=off — no structure calls, no summary key."""
    space = await _seed_search_space(db_session, slug="aas-structure-snapshot")
    concept_id = await _seed_concept(db_session, search_space_id=space)
    prob_doc = await _seed_doc_with_chunk(
        db_session,
        space,
        "1. A beam length L, load w. Find max moment M.",
        title="Structure Snapshot Failure",
    )
    candidate = _candidate(document_id=prob_doc)
    _patch_common_stages(monkeypatch, candidate=candidate, concept_id=concept_id)
    monkeypatch.setenv("APOLLO_STRUCTURE_PAIRING", "shadow")

    async def _unchanged_candidate(*_args, **_kwargs):
        return orch.ProblemResult(label="1", outcome="held_for_review", review_required=True)

    monkeypatch.setattr(orch, "_process_authored_candidate", _unchanged_candidate)

    class _SnapshotFailMC(_StructureShadowMC):
        def cumulative_tokens(self) -> int:
            raise RuntimeError("ledger unavailable")

    metered = _SnapshotFailMC()
    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=None,
        metered_chat=metered,
        embed_fn=lambda _text: [0.0],
    )

    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    assert "structure_pass" not in report.model_dump()
    assert metered.structure_calls == 0
