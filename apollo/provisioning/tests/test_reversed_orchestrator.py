"""Reversed-provisioning authored-set pipeline (match -> derive -> mint) and
the transactional mint+promote savepoint.

The reversed path activates when the course has registered concepts and
``APOLLO_REVERSED_PROVISIONING`` is not "0". Tier-1: matcher/derivation are
module-level stubs; the label-match retrieval machinery runs real.
"""

from __future__ import annotations

import sys

import pytest
from sqlalchemy import func, select

from apollo.persistence.models import Concept, ConceptProblem, KGEntity, Subject
from apollo.provisioning.authored_sets.graph_derivation import DerivationError, DerivedGraph
from apollo.provisioning.authored_sets.orchestrator import run_authored_set_provisioning
from apollo.provisioning.concept_match import ConceptMatch
from apollo.provisioning.pairing_gate import PairingVerdict
from apollo.provisioning.promote import PromoteResult
from apollo.provisioning.scrape import CandidateQuestion, ScrapeResult
from database.models import AITAChunk, AITADocument, SearchSpace

orch = sys.modules["apollo.provisioning.authored_sets.orchestrator"]


class _FakeMC:
    def scrape_chat_fn(self, _system_prompt):
        return lambda _content: "[]"

    def main(self, **_k):
        return "{}"

    def cheap(self, **_k):
        return '{"equivalent": true, "reason": "same"}'


async def _seed_space(db, *, slug: str) -> int:
    space = SearchSpace(name=f"Rev {slug}", slug=slug, subject_name="Calc")
    db.add(space)
    await db.flush()
    return int(space.id)


async def _seed_registered_concept(
    db, *, search_space_id: int, slug: str = "integration-by-parts"
) -> int:
    subject = Subject(
        slug=f"calc2-{slug}", display_name="Calculus 2", search_space_id=search_space_id
    )
    db.add(subject)
    await db.flush()
    concept = Concept(
        subject_id=subject.id,
        slug=slug,
        display_name="Integration by Parts",
        description="u dv = uv - v du",
        canonical_symbols={"symbols": ["x", "u", "v", "F", "C"]},
        normalization_map={},
    )
    db.add(concept)
    await db.flush()
    return int(concept.id)


async def _seed_doc(db, search_space_id: int, content: str, *, title: str) -> int:
    doc = AITADocument(
        title=title,
        content=content,
        content_hash=f"{title}-{search_space_id}",
        search_space_id=search_space_id,
        document_metadata={"page_debug": [{"page": 1, "ocr_confidence": 0.95}]},
    )
    db.add(doc)
    await db.flush()
    db.add(AITAChunk(document_id=doc.id, content=content, page_number=1))
    await db.flush()
    return int(doc.id)


def _candidate(*, document_id: int, chash: str) -> CandidateQuestion:
    return CandidateQuestion(
        problem_text="1. Evaluate integral x e^x dx.",
        given_values={},
        target_unknown="F",
        difficulty="standard",
        document_id=document_id,
        page=1,
        chunk_content_hash=chash,
        concept_slug="provisional.inventory",
        label="1",
    )


def _derived() -> DerivedGraph:
    return DerivedGraph(
        reference_solution=[
            {
                "step": 1,
                "entry_type": "equation",
                "id": "ibp_formula",
                "content": {
                    "label": "Integration by parts",
                    "symbolic": "integral u dv = u*v - integral v du",
                    "display": True,
                },
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "definition",
                "id": "parts_assignment",
                "content": {"concept": "u = x, dv = e^x dx", "meaning": "split"},
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "procedure_step",
                "id": "apply_parts",
                "content": {
                    "action": "apply the parts formula",
                    "purpose": "reduce",
                    "order": 1,
                    "uses_equations": ["ibp_formula"],
                },
                "depends_on": ["ibp_formula", "parts_assignment"],
            },
        ],
        target_unknown="F",
        symbolic_mappings={"u": "x"},
        bound_variables=["x"],
    )


def _match(concept_id: int) -> ConceptMatch:
    return ConceptMatch(
        concept_id=concept_id,
        slug="integration-by-parts",
        secondary=[],
        confidence=0.97,
        rationale="product form",
        no_match=False,
    )


async def _drive(
    db,
    monkeypatch,
    *,
    slug: str,
    match_result,
    derive_result,
    tag_and_mint=None,
    promote=None,
):
    """Seed a course with one registered concept + a labeled problem/solution
    pair, stub match/derive at the orchestrator seam, run provisioning."""
    space = await _seed_space(db, slug=slug)
    concept_id = await _seed_registered_concept(db, search_space_id=space)
    prob_doc = await _seed_doc(db, space, "1. Evaluate integral x e^x dx.", title=f"P-{slug}")
    sol_doc = await _seed_doc(
        db,
        space,
        "Solution 1\nLet u = x, dv = e^x dx ... ANSWER: x e^x - e^x + C",
        title=f"S-{slug}",
    )
    candidate = _candidate(document_id=prob_doc, chash=f"{slug}-chash")
    provisional_id = await orch.resolve_or_create_provisional_concept(db, search_space_id=space)

    async def _scrape(chunk_rows, **_k):
        return ScrapeResult(candidates=(candidate,), scraped_count=1, parse_failures=0)

    async def _validate_pair(question, draft, *, retrieve_fn, judge_fn):
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    match_calls: list[str] = []

    async def _match_concept(problem_text, registered, *, chat_fn):
        match_calls.append(problem_text)
        if isinstance(match_result, Exception):
            raise match_result
        return match_result(concept_id) if callable(match_result) else match_result

    derive_calls: list[dict] = []

    async def _derive(cand, spans, **kwargs):
        derive_calls.append({"spans": spans, **kwargs})
        if isinstance(derive_result, Exception):
            raise derive_result
        return derive_result

    monkeypatch.setattr(orch, "scrape_document", _scrape)
    monkeypatch.setattr(orch, "validate_pair", _validate_pair)
    monkeypatch.setattr(orch, "match_concept", _match_concept)
    monkeypatch.setattr(orch, "derive_reference_graph", _derive)
    if tag_and_mint is not None:
        monkeypatch.setattr(orch, "tag_and_mint", tag_and_mint)
    if promote is not None:
        monkeypatch.setattr(orch, "promote", promote)

    report = await run_authored_set_provisioning(
        db,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeMC(),
        embed_fn=lambda _t: [0.0],
    )
    return report, {
        "space": space,
        "concept_id": concept_id,
        "provisional_id": provisional_id,
        "match_calls": match_calls,
        "derive_calls": derive_calls,
    }


@pytest.mark.asyncio
async def test_reversed_promotes_with_matched_concept_and_derived_graph(db_session, monkeypatch):
    mint_kwargs: dict = {}

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn, resolved_concept=None):
        mint_kwargs["resolved_concept"] = resolved_concept
        mint_kwargs["problem"] = pair.problem
        from apollo.provisioning.tag_mint import MintPlan

        return MintPlan(
            concept_id=resolved_concept.concept_id,
            concept_slug=resolved_concept.slug,
            authored_symbols=[],
            minted_entity_ids={},
            merged_entity_keys=[],
            prereq_pairs=[],
            misconception_keys=[],
        )

    async def _promote(db, neo, **kwargs):
        row = await db.get(ConceptProblem, kwargs["concept_problem_id"])
        row.tier = 2
        return PromoteResult(promoted=True)

    report, ctx = await _drive(
        db_session,
        monkeypatch,
        slug="rev-ok",
        match_result=_match,
        derive_result=_derived(),
        tag_and_mint=_tag_and_mint,
        promote=_promote,
    )
    assert report.counts == {"promoted": 1, "rejected": 0, "held_for_review": 0}
    assert report.problems[0].solution_source == "extracted"
    # the matched concept was threaded as resolved_concept (no tag draft)
    assert mint_kwargs["resolved_concept"].concept_id == ctx["concept_id"]
    # the derived extras ride the problem dict into promote/lint
    assert mint_kwargs["problem"]["bound_variables"] == ["x"]
    assert mint_kwargs["problem"]["symbolic_mappings"] == {"u": "x"}
    assert mint_kwargs["problem"]["concept_id"] == "integration-by-parts"
    # derivation saw ONLY carries_solution spans (the leak guard input scoping)
    spans = ctx["derive_calls"][0]["spans"]
    assert spans and all(s.carries_solution for s in spans)


@pytest.mark.asyncio
async def test_reversed_no_match_holds_for_review(db_session, monkeypatch):
    no_match = ConceptMatch(
        concept_id=None, slug=None, no_match=True, retried=True, rationale="nothing fits"
    )

    async def _tag_never(*_a, **_k):
        raise AssertionError("tag_and_mint must not run on NO_MATCH")

    report, _ctx = await _drive(
        db_session,
        monkeypatch,
        slug="rev-nomatch",
        match_result=no_match,
        derive_result=_derived(),
        tag_and_mint=_tag_never,
    )
    assert report.counts == {"promoted": 0, "rejected": 0, "held_for_review": 1}
    result = report.problems[0]
    assert result.reason == "no_matching_concept"
    tier1 = await db_session.get(ConceptProblem, result.concept_problem_id)
    review = tier1.provenance["authored_review"]
    assert review["required"] is True
    assert review["reason"] == "no_matching_concept"
    assert review["concept_match"]["no_match"] is True


@pytest.mark.asyncio
async def test_reversed_derivation_failure_rejects_candidate(db_session, monkeypatch):
    report, _ctx = await _drive(
        db_session,
        monkeypatch,
        slug="rev-defect",
        match_result=_match,
        derive_result=DerivationError("derivation defective after retry"),
    )
    assert report.counts["rejected"] == 1
    assert report.problems[0].diagnostic.startswith("derivation_error")


@pytest.mark.asyncio
async def test_kill_switch_falls_back_to_legacy(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_REVERSED_PROVISIONING", "0")
    from apollo.provisioning.solution import SolutionDraftError

    async def _legacy_fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        raise SolutionDraftError("legacy path exercised")

    monkeypatch.setattr(orch, "find_or_generate", _legacy_fog)
    report, ctx = await _drive(
        db_session,
        monkeypatch,
        slug="rev-kill",
        match_result=_match,
        derive_result=_derived(),
    )
    # matcher never ran; the candidate went down the legacy find_or_generate path
    assert ctx["match_calls"] == []
    assert report.problems[0].diagnostic.startswith("solution_draft_error: legacy path")


@pytest.mark.asyncio
async def test_no_registered_concepts_falls_back_to_legacy(db_session, monkeypatch):
    """A course with NO premade list (only the reserved provisional concept)
    keeps today's legacy behavior even with the flag on."""
    from apollo.provisioning.solution import SolutionDraftError

    space = await _seed_space(db_session, slug="rev-legacy")
    prob_doc = await _seed_doc(db_session, space, "1. beam", title="P-leg")
    sol_doc = await _seed_doc(db_session, space, "Solution 1\nM = 2", title="S-leg")
    candidate = _candidate(document_id=prob_doc, chash="leg-chash")

    async def _scrape(chunk_rows, **_k):
        return ScrapeResult(candidates=(candidate,), scraped_count=1, parse_failures=0)

    async def _match_never(*_a, **_k):
        raise AssertionError("match_concept must not run without registered concepts")

    async def _legacy_fog(db, question, *, retrieve_fn, chat_fn, augment_recall=False):
        raise SolutionDraftError("legacy path exercised")

    monkeypatch.setattr(orch, "scrape_document", _scrape)
    monkeypatch.setattr(orch, "match_concept", _match_never)
    monkeypatch.setattr(orch, "find_or_generate", _legacy_fog)

    report = await run_authored_set_provisioning(
        db_session,
        neo=None,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeMC(),
        embed_fn=lambda _t: [0.0],
    )
    assert report.problems[0].diagnostic.startswith("solution_draft_error: legacy path")


@pytest.mark.asyncio
async def test_gate_rejection_rolls_back_minted_entities(db_session, monkeypatch):
    """Mint is TRANSACTIONAL with promotion: a lint rejection (here a stubbed
    gate-8 duplicate) rolls back every KG row the REAL tag_and_mint flushed.
    Regression pin for the verified 17->33 entity-doubling orphan bug —
    without the savepoint wrap the three minted entities survive the commit."""

    async def _promote_gate8(db, neo, **kwargs):
        return PromoteResult(promoted=False, failed_gate=8, diagnostic="gate 8: duplicate problem")

    report, ctx = await _drive(
        db_session,
        monkeypatch,
        slug="rev-gate8",
        match_result=_match,
        derive_result=_derived(),
        promote=_promote_gate8,  # REAL tag_and_mint runs (resolved mode, no chat)
    )
    assert report.counts["rejected"] == 1
    assert report.problems[0].failed_gate == 8

    # Simulate the background task's end-of-run commit, then count.
    await db_session.commit()
    n = (
        await db_session.execute(
            select(func.count())
            .select_from(KGEntity)
            .where(KGEntity.concept_id == ctx["concept_id"])
        )
    ).scalar_one()
    assert n == 0  # no orphaned KG rows survive the gate rejection
