"""Per-concept misconception bank loader (Class 2 Phase 2).

Subject-agnostic by design: every public function takes a `concept_id`
(the FK into apollo_concepts) and never sees a subject or concept slug
string. The runtime resolves concept_id from the session row
(apollo_sessions.concept_id), so adding a new class is one row in
apollo_subjects + N rows in apollo_concepts + M rows in
apollo_misconceptions — no code change.

Storage is migration 019. Embeddings are vector(3072) in Postgres,
HNSW-indexed via halfvec(3072) per CLAUDE.md. Queries cast at read
time so the precision/recall tradeoff lives at index level, not in
Python.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Misconception

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MisconceptionEntry:
    """In-memory row from apollo_misconceptions. Frozen — read-only at
    runtime. Authoring goes through INSERT/UPDATE on the table directly."""

    id: int
    concept_id: int
    code: str
    description: str
    confusion_pair: tuple[str, str] | None
    trigger_phrases: tuple[str, ...]
    probe_question: str
    rt_steps: tuple[str, ...]


def _from_row(row: Misconception) -> MisconceptionEntry:
    pair: tuple[str, str] | None
    if row.confusion_pair_a and row.confusion_pair_b:
        pair = (row.confusion_pair_a, row.confusion_pair_b)
    else:
        pair = None

    triggers = row.trigger_phrases or []
    if isinstance(triggers, str):
        triggers = json.loads(triggers)
    rt = row.rt_steps or []
    if isinstance(rt, str):
        rt = json.loads(rt)

    return MisconceptionEntry(
        id=int(row.id),
        concept_id=int(row.concept_id),
        code=row.code,
        description=row.description,
        confusion_pair=pair,
        trigger_phrases=tuple(triggers),
        probe_question=row.probe_question,
        rt_steps=tuple(rt),
    )


async def load_for_concept(
    db: AsyncSession,
    *,
    concept_id: int,
) -> list[MisconceptionEntry]:
    """Return every authored misconception for a concept.

    Used by the inference pipeline as the candidate set for retrieval.
    Pure read; safe to cache per-concept at the caller's discretion
    (signal: bank changes when an author edits the table, which is
    rare relative to chat-turn frequency).
    """
    rows = (
        await db.execute(
            select(Misconception).where(Misconception.concept_id == concept_id)
        )
    ).scalars().all()
    return [_from_row(r) for r in rows]


async def match_by_embedding(
    db: AsyncSession,
    *,
    concept_id: int,
    query_embedding: list[float],
    k: int = 3,
) -> list[tuple[MisconceptionEntry, float]]:
    """Top-k nearest authored misconceptions to `query_embedding` for
    a concept, using the HNSW halfvec cosine index from migration 019.

    Returns [(entry, similarity)] where similarity is in [-1, 1] (1 =
    identical). Uses pgvector's `<=>` cosine-distance operator and
    converts: similarity = 1 - distance.

    Bypassed entirely on SQLite (test mode) — that backend has no
    pgvector. Callers should fall back to in-memory cosine on the
    `load_for_concept` result when running against SQLite.
    """
    if not query_embedding:
        return []

    # Stringify the embedding into pgvector's expected text form.
    embedding_str = "[" + ",".join(f"{x:.7f}" for x in query_embedding) + "]"

    # Cast both sides to halfvec(3072) so the HNSW expression index from
    # migration 019 is used.
    sql = text(
        """
        SELECT
            m.id,
            m.concept_id,
            m.code,
            m.description,
            m.confusion_pair_a,
            m.confusion_pair_b,
            m.trigger_phrases,
            m.probe_question,
            m.rt_steps,
            (m.description_embedding::halfvec(3072))
                <=> (:emb)::halfvec(3072) AS distance
        FROM apollo_misconceptions m
        WHERE m.concept_id = :concept_id
          AND m.description_embedding IS NOT NULL
        ORDER BY (m.description_embedding::halfvec(3072))
                 <=> (:emb)::halfvec(3072)
        LIMIT :k
        """
    )
    rows = (
        await db.execute(
            sql,
            {"concept_id": concept_id, "emb": embedding_str, "k": k},
        )
    ).mappings().all()

    out: list[tuple[MisconceptionEntry, float]] = []
    for r in rows:
        triggers = r["trigger_phrases"] or []
        if isinstance(triggers, str):
            triggers = json.loads(triggers)
        rt = r["rt_steps"] or []
        if isinstance(rt, str):
            rt = json.loads(rt)

        entry = MisconceptionEntry(
            id=int(r["id"]),
            concept_id=int(r["concept_id"]),
            code=r["code"],
            description=r["description"],
            confusion_pair=(
                (r["confusion_pair_a"], r["confusion_pair_b"])
                if r["confusion_pair_a"] and r["confusion_pair_b"]
                else None
            ),
            trigger_phrases=tuple(triggers),
            probe_question=r["probe_question"],
            rt_steps=tuple(rt),
        )
        similarity = 1.0 - float(r["distance"])
        out.append((entry, similarity))
    return out


async def upsert_entry(
    db: AsyncSession,
    *,
    concept_id: int,
    code: str,
    description: str,
    description_embedding: list[float] | None,
    confusion_pair_a: str | None,
    confusion_pair_b: str | None,
    trigger_phrases: list[str],
    probe_question: str,
    rt_steps: list[str],
) -> int:
    """Curriculum-author-facing INSERT-or-UPDATE. Returns the row id.

    Used by the seeder script and (later) by the teacher UI authoring
    flow. The runtime never calls this — it's read-only on chat turns.
    """
    embedding_str: Any = None
    if description_embedding:
        embedding_str = (
            "[" + ",".join(f"{x:.7f}" for x in description_embedding) + "]"
        )

    sql = text(
        """
        INSERT INTO apollo_misconceptions (
            concept_id, code, description, description_embedding,
            confusion_pair_a, confusion_pair_b,
            trigger_phrases, probe_question, rt_steps
        )
        VALUES (
            :concept_id, :code, :description,
            CAST(:embedding AS vector(3072)),
            :pair_a, :pair_b,
            CAST(:triggers AS jsonb), :probe, CAST(:rt AS jsonb)
        )
        ON CONFLICT (concept_id, code) DO UPDATE SET
            description = EXCLUDED.description,
            description_embedding = EXCLUDED.description_embedding,
            confusion_pair_a = EXCLUDED.confusion_pair_a,
            confusion_pair_b = EXCLUDED.confusion_pair_b,
            trigger_phrases = EXCLUDED.trigger_phrases,
            probe_question = EXCLUDED.probe_question,
            rt_steps = EXCLUDED.rt_steps,
            updated_at = now()
        RETURNING id
        """
    )
    result = await db.execute(
        sql,
        {
            "concept_id": concept_id,
            "code": code,
            "description": description,
            "embedding": embedding_str,
            "pair_a": confusion_pair_a,
            "pair_b": confusion_pair_b,
            "triggers": json.dumps(trigger_phrases),
            "probe": probe_question,
            "rt": json.dumps(rt_steps),
        },
    )
    return int(result.scalar_one())


__all__ = [
    "MisconceptionEntry",
    "load_for_concept",
    "match_by_embedding",
    "upsert_entry",
]
