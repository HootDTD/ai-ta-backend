"""NUL bytes (``\\x00``) from PDF extraction must never reach Postgres.

Postgres TEXT/VARCHAR and JSONB reject ``\\x00`` outright
(``CharacterNotInRepertoireError``), and scanned-PDF text layers (PyMuPDF)
plus OCR output can contain them — staging upload id=2 (873-page textbook)
died exactly this way. These tests pin the two sanitization chokepoints every
ingest route passes through: the ``AITAConnectorDocument`` DTO and
``items_to_chunk_texts``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from indexing.connector_document import AITAConnectorDocument
from indexing.document_chunker import items_to_chunk_texts
from indexing.text_sanitization import sanitize_jsonable, strip_nul

pytestmark = pytest.mark.unit

NUL = "\x00"


# --------------------------------------------------------------------------- #
# strip_nul
# --------------------------------------------------------------------------- #
def test_strip_nul_removes_all_nul_bytes():
    assert strip_nul(f"flu{NUL}id Mech{NUL}{NUL}anics") == "fluid Mechanics"


def test_strip_nul_leaves_clean_text_unchanged():
    assert strip_nul("Bernoulli equation") == "Bernoulli equation"


def test_strip_nul_handles_empty_string():
    assert strip_nul("") == ""


# --------------------------------------------------------------------------- #
# sanitize_jsonable
# --------------------------------------------------------------------------- #
def test_sanitize_jsonable_cleans_nested_structures():
    dirty = {
        "page_debug": [
            {"latex_text": f"p_1 + {NUL}\\rho g h", "warnings": [f"warn{NUL}ing"]},
        ],
        f"key{NUL}": "value",
    }

    clean = sanitize_jsonable(dirty)

    assert clean == {
        "page_debug": [
            {"latex_text": "p_1 + \\rho g h", "warnings": ["warning"]},
        ],
        "key": "value",
    }


def test_sanitize_jsonable_does_not_mutate_the_input():
    dirty = {"text": f"a{NUL}b"}

    sanitize_jsonable(dirty)

    assert dirty == {"text": f"a{NUL}b"}


def test_sanitize_jsonable_passes_non_string_scalars_through():
    value = {"count": 3, "ratio": 0.5, "flag": True, "missing": None}
    assert sanitize_jsonable(value) == value


# --------------------------------------------------------------------------- #
# AITAConnectorDocument — the DTO boundary all document inserts pass through
# --------------------------------------------------------------------------- #
def _connector_doc(**overrides) -> AITAConnectorDocument:
    defaults = dict(
        title="fluidMechanics.pdf",
        source_markdown="Bernoulli equation",
        unique_id="teacher-upload:2",
        search_space_id=2,
        material_kind="textbook",
    )
    defaults.update(overrides)
    return AITAConnectorDocument(**defaults)


def test_connector_document_strips_nul_from_text_fields():
    doc = _connector_doc(
        title=f"fluid{NUL}Mechanics.pdf",
        source_markdown=f"Berno{NUL}ulli equation",
    )

    assert doc.title == "fluidMechanics.pdf"
    assert doc.source_markdown == "Bernoulli equation"


def test_connector_document_sanitizes_metadata_recursively():
    doc = _connector_doc(
        metadata={"page_debug": [{"latex_text": f"x{NUL}y", "warnings": [f"w{NUL}"]}]},
    )

    assert doc.metadata == {"page_debug": [{"latex_text": "xy", "warnings": ["w"]}]}


def test_connector_document_still_rejects_title_that_is_only_nul_bytes():
    with pytest.raises(ValueError):
        _connector_doc(title=NUL * 3)


def test_connector_document_still_rejects_non_string_text_fields():
    """The before-validator passes non-strings through to pydantic's type check."""
    with pytest.raises(ValueError):
        _connector_doc(title=123)


# --------------------------------------------------------------------------- #
# items_to_chunk_texts — the chokepoint for chunk content
# --------------------------------------------------------------------------- #
def _item(**overrides) -> SimpleNamespace:
    defaults = dict(
        id="doc:1:0",
        page=1,
        type="body",
        section_path=["Chapter 1"],
        text="Bernoulli equation",
        raw_text="Bernoulli equation",
        figure_id=None,
        source_pdf="fluidMechanics.pdf",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_chunk_texts_strip_nul_from_text_and_metadata():
    item = _item(text=f"Berno{NUL}ulli", section_path=[f"Chap{NUL}ter 1"])

    [(text, metadata)] = items_to_chunk_texts([item])

    assert text == "Bernoulli"
    assert metadata["section_path"] == "Chapter 1"


def test_chunk_texts_drop_items_that_are_only_nul_bytes():
    item = _item(text=NUL * 2, raw_text=NUL)

    assert items_to_chunk_texts([item]) == []


def test_chunk_texts_fall_back_to_raw_text_after_stripping():
    item = _item(text=NUL, raw_text=f"OCR{NUL} text")

    [(text, _)] = items_to_chunk_texts([item])

    assert text == "OCR text"
