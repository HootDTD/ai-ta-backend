"""Per-turn clarification orchestration, factored out of handle_chat so it is
unit-testable without the DB/HTTP stack. Fail-safe throughout: any failure
returns no hints and persists nothing — teaching never blocks (spec §12)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.clarification.detector import detect_ambiguous_nodes
from apollo.clarification.embedding import CandidateEmbeddingCache, Embedder
from apollo.clarification.pacing import select_probes
from apollo.clarification.probe import build_probe_hint
from apollo.clarification.store import write_asked_waiting
from apollo.resolution import find_residual_nodes
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)


async def run_clarification_detection(
    *,
    db: AsyncSession,
    parsed_nodes: list,
    candidates: tuple[Candidate, ...],
    symbolic_mappings: dict[str, str],
    embedder: Embedder,
    cache: CandidateEmbeddingCache,
    attempt_id: int,
    session_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int | None,
    asked_turn: int,
) -> list[str]:
    """Detect ambiguous residual nodes, persist asked_waiting rows, and return
    the answer-blind probe hints for draft_reply. Returns [] on any failure or
    when no candidates exist."""
    if not parsed_nodes or not candidates:
        return []
    try:
        residual = find_residual_nodes(
            parsed_nodes, candidates, symbolic_mappings=symbolic_mappings
        )
        if not residual:
            return []
        flagged = detect_ambiguous_nodes(residual, candidates, embedder=embedder, cache=cache)
        chosen = select_probes(flagged)
        hints: list[str] = []
        for f in chosen:
            await write_asked_waiting(
                db,
                attempt_id=attempt_id,
                session_id=session_id,
                user_id=user_id,
                search_space_id=search_space_id,
                concept_id=concept_id,
                node_id=f.node.node_id,
                candidate_key=f.candidate.canonical_key,
                probe_question="",
                original_statement=student_surface_text(f.node),
                asked_turn=asked_turn,
            )
            hints.append(build_probe_hint(f.node, f.candidate))
        return hints
    except Exception as exc:  # noqa: BLE001 - never block teaching
        _LOG.warning("clarification_detection_failed attempt_id=%s error=%s", attempt_id, exc)
        return []
