"""WU-5B2 §3 — the negotiation-move likelihood multiplier (PURE + one tiny read).

Mostly PURE (named module constants, returns NEW objects, no IO) except the one
small async read ``load_student_moves`` (plain indexed SQL over the already-
existing ``apollo_kg_negotiations`` table — NO Neo4j read; the bridge to the KG
is the events' ``evidence_node_ids``, already in memory). Mirrors ``decay.py`` /
``belief.py``.

§3 Step-1 L428 (the ``negotiation move | — | x1.2 | x1.1`` row): a qualifying
move multiplies the COMBINED §3 likelihood ONCE per entity BEFORE the damper.
The blank ``L_misc`` cell is the identity 1.0 (L430 "never 0"). The RATIFIED
suppression (adjudication #4b): a MISCONCEPTION (or a PARTIAL carrying a
misconception_code — the ambiguous-order conflict event) entity forces identity
x1.0 so a negotiation move can NEVER dilute a real misconception (restores the
-0.044 p_misc cancel). The boost IS allowed on CORRECTED/COVERED (genuine
metacognition — the student fixed it).

LOCKED — do NOT re-derive or re-parameterize: ``NEGOTIATION_LIKELIHOOD``,
``MULTIPLIER_MOVES``, the ``student``-actor filter, latest-wins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.learner_model.belief import NO_OP_LIKELIHOOD
from apollo.persistence.models import KGNegotiation

# §3 L428 negotiation row, order (p_misc, p_shaky, p_mastered). The blank L_misc
# cell = 1.0 (L430 "never 0"). LOCKED — do NOT re-derive or re-parameterize.
NEGOTIATION_LIKELIHOOD: tuple[float, float, float] = (1.0, 1.2, 1.1)

# Which moves count as a metacognition (confidence) signal. ``skip`` ("I won't
# fight about it") is NOT a confidence signal -> no-op identity (adjudication #4).
# LOCKED.
MULTIPLIER_MOVES: frozenset[str] = frozenset({"challenge", "paraphrase"})

# The actor whose rows are metacognitive evidence. The 021 CHECK allows
# student|parser|system; ``store._log_negotiation`` always writes ``student``, so
# the read MUST filter to it (parser/system rows are NOT student metacognition).
# LOCKED.
STUDENT_ACTOR: str = "student"


async def load_student_moves(db: AsyncSession, *, attempt_id: int) -> dict[str, str]:
    """``{entry_id (Neo4j node_id) -> latest student move}`` for this attempt.

    Reads ``apollo_kg_negotiations`` WHERE ``attempt_id = :id`` AND
    ``actor = 'student'``, ordered by ``(created_at ASC, id ASC)`` so the LAST
    (latest) row per ``entry_id`` wins in the dict comprehension. parser/system
    rows are EXCLUDED. NO Neo4j read. The ``id`` tiebreak makes a same-instant
    ``created_at`` deterministic (the higher id == later insert wins)."""
    rows = (
        await db.execute(
            select(KGNegotiation.entry_id, KGNegotiation.move)
            .where(
                KGNegotiation.attempt_id == attempt_id,
                KGNegotiation.actor == STUDENT_ACTOR,
            )
            .order_by(KGNegotiation.created_at.asc(), KGNegotiation.id.asc())
        )
    ).all()
    return {entry_id: move for entry_id, move in rows}  # last write wins -> latest


def entity_negotiation_move(
    entity_events: Sequence[LearnerEvent], moves: Mapping[str, str]
) -> str | None:
    """The representative move for ONE entity = the move on the LAST (by the
    deterministic converter order) of the entity's ``evidence_node_ids`` that has
    a move; ``None`` when none of the entity's nodes were negotiated.

    EXACTLY ONE move per entity (a student spamming moves cannot stack/inflate —
    the belief multiplier is applied once per entity regardless). The exact
    tie-break among multiple qualifying moves on one entity is not load-bearing
    for the belief (any qualifying move yields the same ``NEGOTIATION_LIKELIHOOD``)
    — only for the persisted provenance string; "latest in converter order" is a
    stable, documented choice. PURE — never mutates inputs."""
    representative: str | None = None
    for event in entity_events:
        for node_id in event.evidence_node_ids:
            move = moves.get(node_id)
            if move is not None:
                representative = move
    return representative


def negotiation_multiplier(move: str | None) -> tuple[float, float, float]:
    """``NEGOTIATION_LIKELIHOOD`` when ``move in MULTIPLIER_MOVES``, else
    ``NO_OP_LIKELIHOOD`` (skip / None / unknown -> identity x1.0). Never returns a
    0 component (both return values are floored constants from ``belief.py`` / the
    LOCKED row, all >= 1.0)."""
    if move in MULTIPLIER_MOVES:
        return NEGOTIATION_LIKELIHOOD
    return NO_OP_LIKELIHOOD


def suppresses_hedge(entity_events: Sequence[LearnerEvent]) -> bool:
    """RATIFIED adjudication #4b: ``True`` when any event is a MISCONCEPTION, OR a
    PARTIAL carrying a non-``None`` ``misconception_code`` (the ambiguous-order
    conflict event). CORRECTED is ALLOWED (the student fixed it — genuine
    metacognition). When ``True`` the caller forces the multiplier to identity
    x1.0 — a negotiation move must NOT dilute a misconception (restores the
    -0.044 p_misc cancel). PURE."""
    for event in entity_events:
        if event.event_kind == LearnerEventKind.MISCONCEPTION:
            return True
        if event.event_kind == LearnerEventKind.PARTIAL and event.misconception_code is not None:
            return True
    return False
