"""Document-structure pass for authored problem/solution sets.

Retrieval chunks are intentionally tiny and may split a printed label from the
block it introduces. This module therefore presents each document to one
structured-output call as a stable, id-ordered character stream, then maps the
returned half-open offsets back to real chunk ids and chunk-local spans. The
model identifies structure; pairing remains deterministic: one normalized
question label must align with exactly one normalized answer label, otherwise
the label is left unpaired rather than guessed.

Calls use only the injected metered cheap tier and never log document or response
bodies. Separate problem/solution documents run after scrape and stop before
another call once pass-local spend exceeds ``max(scrape_spend, 30_000)``. A
combined document must run before scrape so answer blocks can be excluded from
student-facing problem text; scrape spend is not known yet, so that call uses
the 30k floor as its budget. A final call may overshoot because the guard is
deliberately pre-flight.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apollo.provisioning.authored_sets.label_match import normalize_label
from apollo.provisioning.cost_constants import PER_DOCUMENT_TOKEN_CEILING

__all__ = [
    "BlockSpan",
    "StructurePair",
    "StructurePassResult",
    "StructurePassSummary",
    "StructureUnit",
    "run_structure_pass",
]

_LOG = logging.getLogger(__name__)

_MIN_STRUCTURE_BUDGET = 30_000
_MAX_SUMMARY_LABELS = 200
_GENERIC_LABEL_RE = re.compile(r"^\s*([a-z]+|\d{1,3}[a-z]?)\s*[.):\-]?\s*$", re.IGNORECASE)
_UNIT_KINDS: tuple[Literal["question", "answer", "other"], ...] = (
    "question",
    "answer",
    "other",
)

_SYSTEM_PROMPT = """You segment exam-style documents into structural blocks.
Return only JSON matching the supplied schema. A unit is a complete block from
its printed or inferred label through the character before the next unit; it may
cross chunk boundaries. Identify questions, worked answers, and other material.
For an answer introduced only by 'Answer:' after a numbered question, inherit
that question's label. Preserve enough label syntax to identify it (for example
'1.' or 'Question 1'). Use half-open document-global character offsets. Use the
provided chunk table to report the first and last overlapping chunk ids. Do not
invent a pairing when a label is ambiguous."""


class BlockSpan(BaseModel):
    """The portion of one unit that overlaps one source chunk."""

    model_config = ConfigDict(frozen=True)

    chunk_id: int
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)


class StructureUnit(BaseModel):
    """One validated structural block mapped back to source chunks."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["question", "answer", "other"]
    label: str | None = None
    document_role: Literal["problem", "solution"]
    start_chunk: int
    end_chunk: int
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    block_spans: tuple[BlockSpan, ...]


class StructurePair(BaseModel):
    """An unambiguous deterministic question-to-answer label alignment."""

    model_config = ConfigDict(frozen=True)

    label: str
    question: StructureUnit
    answer: StructureUnit


class StructurePassSummary(BaseModel):
    """Bounded result-summary projection; deliberately contains no document text."""

    model_config = ConfigDict(frozen=True)

    unit_count: int = Field(ge=0)
    kind_counts: dict[str, int]
    paired_label_count: int = Field(ge=0)
    paired_labels: tuple[str, ...]


class StructurePassResult(BaseModel):
    """Full in-memory shadow result. PR1 persists only ``summary``."""

    model_config = ConfigDict(frozen=True)

    units: tuple[StructureUnit, ...]
    pairs: tuple[StructurePair, ...]
    tokens_spent: int = Field(ge=0)
    budget_exhausted: bool = False

    def summary(self) -> StructurePassSummary:
        counts = Counter(unit.kind for unit in self.units)
        labels = tuple(pair.label for pair in self.pairs)
        return StructurePassSummary(
            unit_count=len(self.units),
            kind_counts={kind: counts.get(kind, 0) for kind in _UNIT_KINDS},
            paired_label_count=len(labels),
            paired_labels=labels[:_MAX_SUMMARY_LABELS],
        )


class _RawUnit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["question", "answer", "other"]
    label: str | None
    start_chunk: int
    end_chunk: int
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)


class _RawResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    units: tuple[_RawUnit, ...]


class _ChunkOffset(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: int
    start: int
    end: int


def _chunk_parts(chunk: Any) -> tuple[int, str]:
    if isinstance(chunk, tuple):
        return int(chunk[0]), str(chunk[1] or "")
    return int(chunk.id), str(getattr(chunk, "content", "") or "")


def _assemble(chunks: Sequence[Any]) -> tuple[str, tuple[_ChunkOffset, ...]]:
    """Concatenate chunks in id order and record their document-global offsets."""
    parts: list[str] = []
    offsets: list[_ChunkOffset] = []
    cursor = 0
    for chunk_id, content in sorted((_chunk_parts(chunk) for chunk in chunks), key=lambda x: x[0]):
        if parts:
            parts.append("\n")
            cursor += 1
        start = cursor
        parts.append(content)
        cursor += len(content)
        offsets.append(_ChunkOffset(chunk_id=chunk_id, start=start, end=cursor))
    return "".join(parts), tuple(offsets)


def _normalized_label(raw: str | None) -> str | None:
    normalized = normalize_label(raw)
    if normalized is not None:
        return normalized
    match = _GENERIC_LABEL_RE.fullmatch(raw or "")
    if match is None:
        return None
    token = match.group(1).lower()
    # Re-enter the shared normalizer for bare numeric labels (the structured
    # model commonly emits "1" even when the source marker was "1.").
    return normalize_label(f"Question {token}") or token


def _map_unit(
    raw: _RawUnit,
    *,
    role: Literal["problem", "solution"],
    text_length: int,
    offsets: tuple[_ChunkOffset, ...],
) -> StructureUnit | None:
    if raw.start_char >= raw.end_char or raw.end_char > text_length:
        return None
    overlaps = [
        offset for offset in offsets if offset.end > raw.start_char and offset.start < raw.end_char
    ]
    if not overlaps:
        return None
    spans = tuple(
        BlockSpan(
            chunk_id=offset.chunk_id,
            start_char=max(raw.start_char, offset.start) - offset.start,
            end_char=min(raw.end_char, offset.end) - offset.start,
        )
        for offset in overlaps
    )
    # Chunk ids are derived from the offset map rather than trusted from model
    # output. The raw fields remain required in the wire schema so mismatches
    # are visible to schema-aware fakes and future diagnostics.
    return StructureUnit(
        kind=raw.kind,
        label=_normalized_label(raw.label),
        document_role=role,
        start_chunk=overlaps[0].chunk_id,
        end_chunk=overlaps[-1].chunk_id,
        start_char=raw.start_char,
        end_char=raw.end_char,
        confidence=raw.confidence,
        block_spans=spans,
    )


def _response_schema() -> dict[str, Any]:
    schema = _RawResponse.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "authored_set_structure_pass",
            "strict": True,
            "schema": schema,
        },
    }


def _segment_document(
    chunks: Sequence[Any],
    *,
    role: Literal["problem", "solution"],
    metered_chat: Any,
) -> tuple[StructureUnit, ...]:
    document, offsets = _assemble(chunks)
    if not document.strip():
        return ()
    chunk_table = [
        {"chunk_id": item.chunk_id, "start": item.start, "end": item.end} for item in offsets
    ]
    response = metered_chat.cheap(
        purpose="structure_pass",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"document_role": role, "chunk_offsets": chunk_table, "document": document},
                    ensure_ascii=False,
                ),
            },
        ],
        response_format=_response_schema(),
    )
    try:
        parsed = _RawResponse.model_validate_json(response)
    except ValidationError:
        _LOG.warning(
            "authored_set_structure_pass_invalid",
            extra={"event": "authored_set_structure_pass_invalid", "document_role": role},
        )
        return ()
    return tuple(
        unit
        for raw in parsed.units
        if (unit := _map_unit(raw, role=role, text_length=len(document), offsets=offsets))
        is not None
    )


def _align(units: Sequence[StructureUnit], *, paired_document: bool) -> tuple[StructurePair, ...]:
    questions: dict[str, list[StructureUnit]] = {}
    answers: dict[str, list[StructureUnit]] = {}
    for unit in units:
        if unit.label is None:
            continue
        if unit.kind == "question" and unit.document_role == "problem":
            questions.setdefault(unit.label, []).append(unit)
        answer_role = "solution" if paired_document else "problem"
        if unit.kind == "answer" and unit.document_role == answer_role:
            answers.setdefault(unit.label, []).append(unit)
    return tuple(
        StructurePair(label=label, question=questions[label][0], answer=answers[label][0])
        for label in sorted(questions)
        if len(questions[label]) == 1 and len(answers.get(label, ())) == 1
    )


def run_structure_pass(
    *,
    problem_chunks: Sequence[Any],
    solution_chunks: Sequence[Any] = (),
    metered_chat: Any,
    scrape_spend: int,
) -> StructurePassResult:
    """Segment problem and optional solution documents within a pass-local budget."""
    start_tokens = metered_chat.cumulative_tokens()
    budget = max(int(scrape_spend), _MIN_STRUCTURE_BUDGET)
    # Ceiling headroom: the pass shares the run's MeteredChat ledger, so its
    # spend counts toward PER_DOCUMENT_TOKEN_CEILING. A run near the ceiling
    # must NOT be tipped over by an observability pass (flag=off would have
    # survived) — refuse to spend once cumulative crosses half the ceiling,
    # leaving the other half (~1M tokens) as headroom the pass can never eat.
    headroom_limit = PER_DOCUMENT_TOKEN_CEILING // 2
    units: list[StructureUnit] = []
    budget_exhausted = False

    documents: tuple[tuple[Literal["problem", "solution"], Sequence[Any]], ...] = (
        ("problem", problem_chunks),
        ("solution", solution_chunks),
    )
    for role, chunks in documents:
        if not chunks:
            continue
        spent = metered_chat.cumulative_tokens() - start_tokens
        if spent > budget or metered_chat.cumulative_tokens() >= headroom_limit:
            budget_exhausted = True
            _LOG.warning(
                "authored_set_structure_pass_budget",
                extra={
                    "event": "authored_set_structure_pass_budget",
                    "tokens_spent": spent,
                    "budget": budget,
                    "document_role": role,
                },
            )
            break
        units.extend(_segment_document(chunks, role=role, metered_chat=metered_chat))

    tokens_spent = max(0, metered_chat.cumulative_tokens() - start_tokens)
    pairs = _align(units, paired_document=bool(solution_chunks))
    counts = Counter(unit.kind for unit in units)
    _LOG.info(
        "authored_set_structure_pass",
        extra={
            "event": "authored_set_structure_pass",
            "unit_count": len(units),
            "kind_counts": dict(counts),
            "paired_label_count": len(pairs),
            "tokens_spent": tokens_spent,
        },
    )
    return StructurePassResult(
        units=tuple(units),
        pairs=pairs,
        tokens_spent=tokens_spent,
        budget_exhausted=budget_exhausted,
    )
