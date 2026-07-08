"""Tier-1 bank_pattern misconception detector.

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.3.

Embeds each raw student utterance via the injected ``EmbedFn`` (DI seam —
production wires a real embedding call, tests inject an offline stub) and
compares it against the concept's misconception bank. On Postgres this
would delegate to ``misconception_bank.match_by_embedding`` (pgvector-only,
per that module's own docstring); on SQLite (and in this unit-test suite)
there is no pgvector extension available, so this module falls back to a
pure in-memory cosine similarity computed directly against the supplied
``bank_entries`` tuple — mirroring the dialect-branch pattern already used
in ``apollo/emergent/store.py::record_observations_from_canonical``.

A hit >= ``BANK_SIM_FLOOR`` yields a ``misconception`` finding with
``source='bank_pattern'``, ``confidence=similarity``, ``corroborated=False``
(this tier alone never docks a misconception — it needs a second
corroborating signal, per ``gate.py``'s contract). No match, an empty bank,
or an embedding failure all abstain silently (soft-fail — a detector error
must never break grading).
"""
from __future__ import annotations

import logging
import math

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.config import BANK_SIM_FLOOR
from apollo.overseer.misconception_detector.types import ConceptFinding, EmbedFn

_LOG = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure cosine similarity in [-1, 1]. Returns 0.0 for a zero-norm vector
    (degenerate embedding) instead of raising a ZeroDivisionError."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _dialect_name(db: AsyncSession) -> str:
    """Mirrors ``apollo/emergent/store.py``'s dialect-detection pattern:
    defaults to 'postgresql' when the bind is unavailable (e.g. a mock)."""
    try:
        return db.bind.dialect.name if db.bind is not None else "postgresql"
    except Exception:
        return "postgresql"


def _best_bank_match(
    utterance_embedding: list[float],
    bank_embeddings: tuple[tuple[MisconceptionEntry, list[float]], ...],
) -> tuple[MisconceptionEntry, float] | None:
    """Pure. Returns the (entry, similarity) with the highest cosine
    similarity to ``utterance_embedding``, or None if the bank is empty."""
    best: tuple[MisconceptionEntry, float] | None = None
    for entry, entry_embedding in bank_embeddings:
        similarity = _cosine_similarity(utterance_embedding, entry_embedding)
        if best is None or similarity > best[1]:
            best = (entry, similarity)
    return best


async def _in_memory_cosine_match(
    utterance: str,
    *,
    embed_fn: EmbedFn,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> tuple[MisconceptionEntry, float] | None:
    """SQLite/offline fallback: embed the utterance and every bank entry's
    description via the injected ``embed_fn``, then pick the best cosine
    match. Any embedding failure is swallowed (soft-fail) and treated as
    "no match" for that utterance."""
    try:
        utterance_embedding = embed_fn(utterance)
    except Exception:
        _LOG.exception("bank_pattern_embed_failed utterance_len=%d", len(utterance))
        return None

    bank_embeddings: list[tuple[MisconceptionEntry, list[float]]] = []
    for entry in bank_entries:
        try:
            bank_embeddings.append((entry, embed_fn(entry.description)))
        except Exception:
            _LOG.exception(
                "bank_pattern_bank_embed_failed code=%s", entry.code
            )
            continue

    if not bank_embeddings:
        return None
    return _best_bank_match(utterance_embedding, tuple(bank_embeddings))


async def _postgres_match(
    db: AsyncSession,
    utterance: str,
    *,
    concept_id: int | None,
    embed_fn: EmbedFn,
) -> tuple[MisconceptionEntry, float] | None:
    """Postgres/pgvector path — delegates to
    ``misconception_bank.match_by_embedding``. Soft-fails to "no match" on
    any error (embedding failure, missing concept_id, DB error)."""
    if concept_id is None:
        return None
    try:
        utterance_embedding = embed_fn(utterance)
    except Exception:
        _LOG.exception("bank_pattern_embed_failed utterance_len=%d", len(utterance))
        return None

    from apollo.overseer.misconception_bank import match_by_embedding

    try:
        matches = await match_by_embedding(
            db, concept_id=concept_id, query_embedding=utterance_embedding, k=1
        )
    except Exception:
        _LOG.exception("bank_pattern_match_by_embedding_failed concept_id=%s", concept_id)
        return None

    if not matches:
        return None
    return matches[0]


def _finding_for_match(
    utterance: str, entry: MisconceptionEntry, similarity: float
) -> ConceptFinding:
    return ConceptFinding(
        concept_key=str(entry.concept_id),
        verdict="misconception",
        confidence=similarity,
        severity=0.0,
        evidence_span=utterance,
        signature=f"misc.{entry.code}",
        source="bank_pattern",
        corroborated=False,
    )


async def detect_bank_pattern(
    db: AsyncSession,
    *,
    concept_id: int | None,
    student_utterances: tuple[str, ...],
    embed_fn: EmbedFn,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> tuple[ConceptFinding, ...]:
    """Tier-1 precision second opinion.

    Embeds each raw student utterance and runs
    ``misconception_bank.match_by_embedding`` (Postgres) or an in-memory
    cosine over ``bank_entries`` (SQLite/test — ``match_by_embedding`` is
    pgvector-only and bypassed on SQLite per its own docstring). A hit
    ``>= BANK_SIM_FLOOR`` yields a ``misconception`` finding,
    ``source='bank_pattern'``, ``signature='misc.<code>'``,
    ``confidence=similarity``, ``evidence_span=utterance``,
    ``corroborated=False`` (this tier alone never docks — it needs a 2nd
    signal, per ``gate.py``). Abstains (returns no finding for that
    utterance) on no match, an empty bank, or any internal error
    (soft-fail — a detector error must never break grading).
    """
    if not student_utterances or not bank_entries:
        return ()

    dialect = _dialect_name(db)
    findings: list[ConceptFinding] = []

    for utterance in student_utterances:
        try:
            if dialect == "sqlite":
                match = await _in_memory_cosine_match(
                    utterance, embed_fn=embed_fn, bank_entries=bank_entries
                )
            else:
                match = await _postgres_match(
                    db, utterance, concept_id=concept_id, embed_fn=embed_fn
                )
        except Exception:
            _LOG.exception("bank_pattern_detect_failed")
            match = None

        if match is None:
            continue

        entry, similarity = match
        if similarity >= BANK_SIM_FLOOR:
            findings.append(_finding_for_match(utterance, entry, similarity))

    return tuple(findings)


__all__ = ["detect_bank_pattern"]
