from types import SimpleNamespace

import pytest

import indexing.document_embedder as embedder
from apollo.provisioning.authored_sets.label_match import build_solution_label_index
from apollo.provisioning.authored_sets.paired_retrieval import make_paired_solution_retrieve_fn
from apollo.provisioning.authored_sets.structure_pass import BlockSpan, StructurePair, StructureUnit
from apollo.provisioning.scrape import chunk_content_hash


def _structure_pair(*, answer_spans: tuple[BlockSpan, ...]) -> StructurePair:
    return StructurePair(
        label="1",
        question=StructureUnit(
            kind="question",
            label="1",
            document_role="problem",
            start_chunk=1,
            end_chunk=1,
            start_char=0,
            end_char=10,
            confidence=0.99,
            block_spans=(BlockSpan(chunk_id=1, start_char=0, end_char=10),),
        ),
        answer=StructureUnit(
            kind="answer",
            label="1",
            document_role="solution",
            start_chunk=20,
            end_chunk=21,
            start_char=0,
            end_char=30,
            confidence=0.98,
            block_spans=answer_spans,
        ),
    )


@pytest.mark.asyncio
async def test_no_solution_document_returns_no_spans():
    """B1: no paired solution doc (``solution_document_id=None``) must always
    yield no spans, regardless of label match, so the caller falls through to
    generation instead of querying a document that doesn't exist."""
    chunks = [(10, "Solution 1\nM = wL^2/8", 2)]
    index = build_solution_label_index(chunks)
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=None,
        label_index=index,
        page_conf={2: 0.9},
    )
    q = SimpleNamespace(label="Problem 1", problem_text="1. beam ...")
    spans = await retrieve(q)
    assert spans == ()
    assert retrieve.last_min_conf is None
    assert retrieve.last_match_method is None


@pytest.mark.asyncio
async def test_load_solution_chunks_none_returns_empty_without_query():
    from apollo.provisioning.authored_sets.paired_retrieval import load_solution_chunks

    assert await load_solution_chunks(None, solution_document_id=None) == []


@pytest.mark.asyncio
async def test_chunk_ocr_confidence_none_returns_empty_without_query():
    from apollo.provisioning.authored_sets.paired_retrieval import chunk_ocr_confidence

    assert await chunk_ocr_confidence(None, document_id=None) == {}


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
async def test_no_structure_pairs_preserves_regex_label_fast_path():
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
    assert spans[0].text == chunks[0][1]
    assert retrieve.last_min_conf == 0.91
    assert retrieve.last_match_method == "label"


@pytest.mark.asyncio
async def test_structure_branch_returns_ordered_multichunk_solution_spans():
    chunks = [
        (20, "preface Answer: rivalry is strongest", 2),
        (21, "when firms and offers converge. trailer", 3),
    ]
    pair = _structure_pair(
        answer_spans=(
            BlockSpan(chunk_id=20, start_char=8, end_char=len(chunks[0][1])),
            BlockSpan(chunk_id=21, start_char=0, end_char=31),
        )
    )
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index={},
        page_conf={2: 0.91, 3: 0.84},
        solution_chunks=chunks,
        structure_pairs=(pair,),
    )

    spans = await retrieve(SimpleNamespace(label="1", problem_text="1. (MC) Which force?"))

    assert [span.text for span in spans] == [
        "Answer: rivalry is strongest",
        "when firms and offers converge.",
    ]
    assert [span.chunk_content_hash for span in spans] == [
        chunk_content_hash(chunks[0][1]),
        chunk_content_hash(chunks[1][1]),
    ]
    assert all(span.carries_solution for span in spans)
    assert retrieve.last_match_method == "structure"
    assert retrieve.last_min_conf == 0.84


@pytest.mark.asyncio
async def test_structure_pair_wins_over_answerless_regex_label_chunk():
    label_chunk = (10, "1. (MC) In Porter's model, when is rivalry strongest?", 2)
    option_chunk = (20, "A. When firms are highly differentiated\nB. When firms converge", 3)
    answer_chunk = (21, "Answer: B. Rivalry is strongest when firms and offers converge.", 3)
    continuation_chunk = (22, "Low switching costs intensify that rivalry.", 4)
    solution_chunks = (label_chunk, option_chunk, answer_chunk, continuation_chunk)
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index=build_solution_label_index((label_chunk,)),
        page_conf={2: 0.93, 3: 0.81, 4: 0.79},
        solution_chunks=solution_chunks,
        structure_pairs=(
            _structure_pair(
                answer_spans=(
                    BlockSpan(chunk_id=20, start_char=0, end_char=len(option_chunk[1])),
                    BlockSpan(chunk_id=21, start_char=0, end_char=len(answer_chunk[1])),
                    BlockSpan(
                        chunk_id=22,
                        start_char=0,
                        end_char=len(continuation_chunk[1]),
                    ),
                )
            ),
        ),
    )

    spans = await retrieve(SimpleNamespace(label="1", problem_text="1. Which force?"))

    assert [span.text for span in spans] == [
        option_chunk[1],
        answer_chunk[1],
        continuation_chunk[1],
    ]
    assert label_chunk[1] not in [span.text for span in spans]
    assert all(span.carries_solution for span in spans)
    assert retrieve.last_match_method == "structure"
    assert retrieve.last_min_conf == 0.79


@pytest.mark.asyncio
async def test_label_without_structure_pair_uses_regex_gap_filler():
    label_chunk = (10, "Solution 2\nThe threat of entry is low.", 2)
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index=build_solution_label_index((label_chunk,)),
        page_conf={2: 0.93},
        solution_chunks=((20, "Answer: unrelated structure pair", 3),),
        structure_pairs=(
            _structure_pair(answer_spans=(BlockSpan(chunk_id=20, start_char=0, end_char=32),)),
        ),
    )

    spans = await retrieve(SimpleNamespace(label="2", problem_text="2. Which force?"))

    assert [span.text for span in spans] == [label_chunk[1]]
    assert retrieve.last_match_method == "label"


@pytest.mark.asyncio
async def test_ambiguous_structure_label_falls_through_regex_then_semantic(monkeypatch):
    label_chunk = (10, "Solution 1\nRegex gap-filler answer", 2)
    structure_chunk = (20, "Answer: ambiguous structure candidate", 3)
    duplicate_pairs = (
        _structure_pair(
            answer_spans=(BlockSpan(chunk_id=20, start_char=0, end_char=len(structure_chunk[1])),)
        ),
        _structure_pair(answer_spans=(BlockSpan(chunk_id=20, start_char=8, end_char=20),)),
    )
    semantic_hit = (30, "Semantic fallback context", 5)

    async def _semantic(_db, _doc, _query, _top_k):
        return [semantic_hit]

    monkeypatch.setattr(
        "apollo.provisioning.authored_sets.paired_retrieval._doc_scoped_semantic",
        _semantic,
    )
    regex_retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index=build_solution_label_index((label_chunk,)),
        page_conf={2: 0.93, 5: 0.77},
        solution_chunks=(structure_chunk,),
        structure_pairs=duplicate_pairs,
    )
    semantic_retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=55,
        label_index={},
        page_conf={5: 0.77},
        solution_chunks=(structure_chunk,),
        structure_pairs=duplicate_pairs,
    )

    regex_spans = await regex_retrieve(SimpleNamespace(label="1", problem_text="1. Which force?"))
    semantic_spans = await semantic_retrieve(
        SimpleNamespace(label="1", problem_text="1. Which force?")
    )

    assert [span.text for span in regex_spans] == [label_chunk[1]]
    assert regex_retrieve.last_match_method == "label"
    assert [span.text for span in semantic_spans] == [semantic_hit[1]]
    assert all(span.carries_solution is False for span in semantic_spans)
    assert semantic_retrieve.last_match_method == "retrieval"


@pytest.mark.asyncio
async def test_structure_only_uses_answer_slice_not_regex_whole_chunk(monkeypatch):
    combined = "Question 1: Which force?\nAnswer 1: rivalry"
    answer_start = combined.index("Answer")
    pair = _structure_pair(
        answer_spans=(BlockSpan(chunk_id=20, start_char=answer_start, end_char=len(combined)),)
    )

    async def _semantic_must_not_run(*_args, **_kwargs):
        raise AssertionError("combined mode must not retrieve whole chunks")

    monkeypatch.setattr(
        "apollo.provisioning.authored_sets.paired_retrieval._doc_scoped_semantic",
        _semantic_must_not_run,
    )
    retrieve = make_paired_solution_retrieve_fn(
        db=None,
        solution_document_id=10,
        label_index=build_solution_label_index(((20, combined, 1),)),
        page_conf={1: 0.95},
        solution_chunks=((20, combined, 1),),
        structure_pairs=(pair,),
        structure_only=True,
    )

    spans = await retrieve(SimpleNamespace(label="1", problem_text="Question 1"))

    assert [span.text for span in spans] == ["Answer 1: rivalry"]
    assert retrieve.last_match_method == "structure"

    unmatched = await retrieve(SimpleNamespace(label="2", problem_text="Question 2"))
    assert unmatched == ()
    assert retrieve.last_match_method is None


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
    # B3: the semantic top-k fallback is an unconfirmed guess, not a label match,
    # so it must NOT be flagged as a printed/worked solution (find_or_generate's
    # extract branch is reserved for confirmed label-matched spans).
    assert all(span.carries_solution is False for span in spans)
    assert retrieve.last_match_method == "retrieval"
    assert retrieve.last_min_conf == 0.76
