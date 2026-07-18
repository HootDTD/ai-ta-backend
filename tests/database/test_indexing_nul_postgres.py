"""Real-Postgres regression test for NUL bytes in extracted document text.

Staging upload id=2 (873-page scanned textbook): PyMuPDF/Mathpix extraction
produced text containing ``\\x00``, Postgres rejected the ``aita_documents``
INSERT with ``CharacterNotInRepertoireError``, ``prepare_for_indexing``
swallowed it and returned ``[]``, and the worker raised "Failed to resolve
indexed document for teacher upload". SQLite-backed tests can never catch
this — only real Postgres rejects NUL — hence the ``db_session`` pgvector
harness.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from database.models import Document, Course
from indexing.connector_document import AITAConnectorDocument
from indexing.indexing_service import AITAIndexingService

pytestmark = pytest.mark.integration

NUL = "\x00"


async def _new_search_space(db_session) -> Course:
    space = Course(
        name="Fluids E2E (NUL regression)",
        slug="fluids-e2e-nul-regression",
        subject_name="Mechanical Engineering",
    )
    db_session.add(space)
    await db_session.commit()
    return space


async def test_prepare_for_indexing_persists_document_with_nul_laden_extraction(db_session):
    """The exact failure shape from staging: NUL in markdown, title, and JSONB metadata."""
    space = await _new_search_space(db_session)

    connector_doc = AITAConnectorDocument(
        title=f"fluid{NUL}Mechanics.pdf",
        source_markdown=f"Bernoulli{NUL} equation: p + \\rho g h{NUL} = const",
        unique_id="teacher-upload:nul-regression",
        search_space_id=space.id,
        material_kind="textbook",
        week=None,
        metadata={
            "page_debug": [{"latex_text": f"p_1 + {NUL}\\rho g h_1", "warnings": [f"w{NUL}"]}],
        },
    )

    docs = await AITAIndexingService(db_session).prepare_for_indexing([connector_doc])

    assert len(docs) == 1, "NUL-laden extraction must still persist a document row"

    row = (
        (
            await db_session.execute(
                select(Document).where(Document.search_space_id == space.id)
            )
        )
        .scalars()
        .one()
    )
    assert NUL not in row.title
    assert NUL not in (row.source_markdown or "")
    assert NUL not in json.dumps(row.metadata_)
    assert row.material_kind == "textbook"
    assert row.week is None
