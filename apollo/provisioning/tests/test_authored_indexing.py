import types

import pytest

import apollo.provisioning.authored_sets.indexing as idx


@pytest.mark.asyncio
async def test_index_authored_doc_sets_hidden_status(db_session, monkeypatch):
    from database.models import SearchSpace

    db_session.add(
        SearchSpace(
            id=4,
            name="AAE 333 E2E Test",
            slug="aae-333-e2e-test",
            subject_name="AAE",
        )
    )
    await db_session.flush()

    fake_ing = types.SimpleNamespace(
        items=[types.SimpleNamespace(id="i1")],
        source_markdown="Problem 1 ...",
        page_count=2,
        pages=[],
        artifact_manifest={"pages": []},
        ocr_provider="openai",
        ocr_summary={"openai_pages": 2},
        warning_count=0,
        warnings=[],
    )
    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: fake_ing)
    monkeypatch.setattr(
        idx,
        "items_to_chunk_texts",
        lambda items: [("Problem 1 ...", {"page_number": 1, "chunk_type": "body"})],
    )

    async def fake_prepare(self, docs):
        from database.models import AITADocument

        doc = AITADocument(
            title=docs[0].title,
            search_space_id=docs[0].search_space_id,
            content="c",
            content_hash="h",
            unique_identifier_hash="u",
        )
        db_session.add(doc)
        await db_session.flush()
        return [doc]

    monkeypatch.setattr(idx.AITAIndexingService, "prepare_for_indexing", fake_prepare)

    async def fake_embed(**k):
        return 1

    monkeypatch.setattr(idx, "embed_and_persist_chunks", fake_embed)

    async def fake_finalize(session, **k):
        return None

    monkeypatch.setattr(idx, "finalize_document", fake_finalize)
    monkeypatch.setattr(idx, "embed_text", lambda t: [0.0] * 8)

    doc_id = await idx.index_authored_doc(
        db_session,
        search_space_id=4,
        file_bytes=b"%PDF-1.4 fake",
        title="HW7 Problems",
        set_index=1,
        role="problem",
    )

    from database.models import AITADocument

    doc = await db_session.get(AITADocument, doc_id)
    assert doc.status == {"state": "apollo_reference"}
