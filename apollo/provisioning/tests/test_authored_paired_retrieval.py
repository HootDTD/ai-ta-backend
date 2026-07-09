from types import SimpleNamespace

import pytest

import indexing.document_embedder as embedder
from apollo.provisioning.authored_sets.label_match import build_solution_label_index
from apollo.provisioning.authored_sets.paired_retrieval import make_paired_solution_retrieve_fn


@pytest.mark.asyncio
async def test_chunk_ocr_confidence_skips_malformed_entries(db_session):
    from apollo.provisioning.authored_sets.paired_retrieval import chunk_ocr_confidence
    from database.models import AITADocument, SearchSpace

    sp = SearchSpace(name="Conf", slug="conf-malformed", subject_name="P")
    db_session.add(sp)
    await db_session.flush()
    doc = AITADocument(
        title="t",
        content="c",
        content_hash="h-malformed",
        search_space_id=sp.id,
        document_metadata={
            "page_debug": [
                "not-a-dict",  # non-dict entry -> skipped
                {"page": "NaN", "ocr_confidence": 0.5},  # unparseable page -> skipped
                {"page": 2, "ocr_confidence": 0.7},  # kept
            ]
        },
    )
    db_session.add(doc)
    await db_session.flush()

    conf = await chunk_ocr_confidence(db_session, document_id=int(doc.id))
    assert conf == {2: 0.7}


@pytest.mark.asyncio
async def test_label_branch_returns_carries_solution_span():
    chunks = [(10, "Solution 3\nSum moments: M = wL^2/8", 2)]
    index = build_solution_label_index(chunks)
    page_conf = {2: 0.91}
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index=index,
        page_conf=page_conf,
    )
    q = SimpleNamespace(label="Problem 3", problem_text="3. beam ...")
    spans = await retrieve(q)
    assert len(spans) == 1
    assert spans[0].carries_solution is True
    assert spans[0].document_id == 55
    assert spans[0].page == 2
    assert retrieve.last_min_conf == 0.91


@pytest.mark.asyncio
async def test_no_label_no_retrieval_hits_returns_empty(monkeypatch):
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index={},
        page_conf={},
    )

    async def _empty(_db, _doc, _q, _k):
        return []

    monkeypatch.setattr(
        "apollo.provisioning.authored_sets.paired_retrieval._doc_scoped_semantic",
        _empty,
    )
    q = SimpleNamespace(label=None, problem_text="unlabelled problem")
    spans = await retrieve(q)
    assert spans == ()
    assert retrieve.last_min_conf is None


@pytest.mark.asyncio
async def test_retrieval_branch_is_solution_document_scoped(db_session, monkeypatch):
    from database.models import EMBEDDING_DIM, AITAChunk, AITADocument, SearchSpace

    db_session.add(
        SearchSpace(
            id=44,
            name="Authored Retrieval Scope",
            slug="authored-retrieval-scope",
            subject_name="Physics",
        )
    )
    solution_doc = AITADocument(
        id=4401,
        title="Solutions",
        content="solutions",
        content_hash="authored-retrieval-solutions",
        search_space_id=44,
    )
    distractor_doc = AITADocument(
        id=4402,
        title="Distractor",
        content="distractor",
        content_hash="authored-retrieval-distractor",
        search_space_id=44,
    )
    db_session.add_all([solution_doc, distractor_doc])
    await db_session.flush()

    query_vec = [0.0] * EMBEDDING_DIM
    query_vec[0] = 1.0
    off_vec = [0.0] * EMBEDDING_DIM
    off_vec[1] = 1.0

    db_session.add_all(
        [
            AITAChunk(
                document_id=solution_doc.id,
                content="Use the paired solution: M = wL^2/8",
                page_number=7,
                embedding=query_vec,
            ),
            AITAChunk(
                document_id=distractor_doc.id,
                content="Wrong unpaired solution should never appear",
                page_number=8,
                embedding=query_vec,
            ),
            AITAChunk(
                document_id=solution_doc.id,
                content="Less relevant paired solution note",
                page_number=9,
                embedding=off_vec,
            ),
        ]
    )
    await db_session.flush()
    monkeypatch.setattr(embedder, "embed_text", lambda _text: query_vec)

    retrieve = make_paired_solution_retrieve_fn(
        db_session,
        solution_document_id=solution_doc.id,
        label_index={},
        page_conf={7: 0.82, 9: 0.76},
        top_k=3,
    )
    spans = await retrieve(SimpleNamespace(label=None, problem_text="beam moment"))

    assert spans
    assert {span.document_id for span in spans} == {solution_doc.id}
    assert all("unpaired" not in span.text for span in spans)
    assert all(span.carries_solution is True for span in spans)
    assert retrieve.last_match_method == "retrieval"
    assert retrieve.last_min_conf == 0.76
