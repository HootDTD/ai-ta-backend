import json
import sys

import pytest
from sqlalchemy import select

from apollo.persistence.models import Concept, ConceptProblem, DedupDecision, KGEntity, Subject
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
async def test_authored_set_scrape_is_exhaustive(monkeypatch):
    """Teacher-declared problem sets bypass the autoprovision min-candidate guard."""
    scrape_kwargs = {}

    async def _resolve(db, *, search_space_id):
        return 1

    async def _registered(db, *, search_space_id):
        return []

    async def _solution_chunks(db, *, solution_document_id):
        return []

    async def _confidence(db, *, document_id):
        return {}

    async def _problem_chunks(db, *, document_id):
        return [object()]

    async def _scrape(chunk_rows, **kwargs):
        assert chunk_rows
        scrape_kwargs.update(kwargs)
        return ScrapeResult(candidates=(), scraped_count=0, parse_failures=0)

    async def _write(db, candidates, *, concept_id, search_space_id):
        assert candidates == ()
        return 0

    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve)
    monkeypatch.setattr(orch, "list_registered_concepts", _registered)
    monkeypatch.setattr(orch, "load_solution_chunks", _solution_chunks)
    monkeypatch.setattr(orch, "chunk_ocr_confidence", _confidence)
    monkeypatch.setattr(orch, "_load_chunks", _problem_chunks)
    monkeypatch.setattr(orch, "scrape_document", _scrape)
    monkeypatch.setattr(orch, "write_tier1_problems", _write)

    await run_authored_set_provisioning(
        object(),
        neo=None,
        search_space_id=1,
        problem_document_id=2,
        solution_document_id=None,
        metered_chat=_FakeAuthoredMC(),
        embed_fn=lambda _text: [0.0],
    )

    assert scrape_kwargs["exhaustive"] is True


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
