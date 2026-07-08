"""Per-turn clarification orchestration, factored out of handle_chat so it is
unit-testable without the DB/HTTP stack. Fail-safe throughout: any failure
returns no hints and persists nothing — teaching never blocks (spec §12)."""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.clarification import v2_selection
from apollo.clarification.detector import detect_ambiguous_nodes
from apollo.clarification.embedding import CandidateEmbeddingCache, Embedder
from apollo.clarification.pacing import select_probes
from apollo.clarification.probe import build_probe_hint
from apollo.clarification.store import write_asked_waiting
from apollo.clarification.v2_config import clarification_v2_ranker_enabled
from apollo.resolution import find_residual_nodes
from apollo.resolution.candidates import Candidate
from apollo.resolution.nli_resolution import NLIContext
from apollo.resolution.tiers import student_surface_text
from apollo.resolver_v2.config import resolver_v2_enabled
from apollo.resolver_v2.incremental_types import IncrementalSnapshot

_LOG = logging.getLogger(__name__)

# Mirrors apollo.handlers.chat._clarification_enabled exactly (same env var,
# same truthy parsing). Duplicated rather than imported: chat.py imports this
# module (run_clarification_detection), so importing chat.py here would be
# circular. Single source of truth for the env var NAME lives in chat.py's
# docstring/flag constant; behavior here must stay identical.
_CLARIFICATION_ENABLED_FLAG = "APOLLO_CLARIFICATION_ENABLED"


def _clarification_enabled() -> bool:
    return os.environ.get(_CLARIFICATION_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


def _v2_ranker_active() -> bool:
    """Shared gating predicate (spec §8.2 H1): the V2 ranker only runs when
    ALL THREE flags are ON. Every flag is re-read fresh (no caching)."""
    return (
        clarification_v2_ranker_enabled()
        and resolver_v2_enabled()
        and _clarification_enabled()
    )


async def _v1_select(
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
    nli_ctx: NLIContext | None,
) -> list[str]:
    """The pre-V2 clarification selection pipeline, byte-identical to the
    original body of run_clarification_detection: find_residual_nodes ->
    detect_ambiguous_nodes -> select_probes -> persist + build hints."""
    try:
        # Conditional offload: when NLI is active, find_residual_nodes runs via
        # a thread executor so the CPU-bound model never blocks the async event
        # loop.  When nli_ctx is None (the default off-path) the call is
        # synchronous — byte-identical to the pre-NLI code.
        if nli_ctx is not None and nli_ctx.nli is not None:
            loop = asyncio.get_running_loop()
            residual = await loop.run_in_executor(
                None,
                lambda: find_residual_nodes(
                    parsed_nodes,
                    candidates,
                    symbolic_mappings=symbolic_mappings,
                    nli_ctx=nli_ctx,
                ),
            )
        else:
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
    nli_ctx: NLIContext | None = None,
    snapshot: IncrementalSnapshot | None = None,
    reference_graph=None,
    problem_payload=None,
    resolved_candidate_keys: frozenset[str] = frozenset(),
    snapshot_source: str = "prior_turn",
    pair_count_total: int = 0,
    seeded_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """Detect ambiguous residual nodes, persist asked_waiting rows, and return
    the answer-blind probe hints for draft_reply. Returns [] on any failure or
    when no candidates exist.

    ``snapshot``/``reference_graph``/``problem_payload``/
    ``resolved_candidate_keys`` are new, keyword-only, and all default so
    every existing v1 caller/test is unchanged (integration spec §2.2/§10).
    When the V2 ranker is active (``_v2_ranker_active()`` — all three flags
    ON, spec §8.2) AND a completed ``snapshot`` is available, selection runs
    through ``v2_selection.select`` and falls back to the v1 pipeline on any
    exception (fail-open, spec §8.3). ``reference_graph``/``problem_payload``/
    ``resolved_candidate_keys`` are accepted here for forward wiring by the
    caller (building/reusing the incremental snapshot) and are not consumed
    directly by this function.

    ``snapshot_source``/``pair_count_total``/``seeded_keys`` are trace-only
    (spec §7, task T13), forwarded to ``v2_selection.select`` for observability.
    """
    del reference_graph, problem_payload, resolved_candidate_keys
    if not parsed_nodes or not candidates:
        return []

    if snapshot is not None and _v2_ranker_active():
        try:
            return await v2_selection.select(
                snapshot,
                candidates,
                db,
                attempt_id,
                session_id=session_id,
                user_id=user_id,
                search_space_id=search_space_id,
                concept_id=concept_id,
                asked_turn=asked_turn,
                snapshot_source=snapshot_source,
                pair_count_total=pair_count_total,
                seeded_keys=seeded_keys,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open to v1, spec §8.3
            _LOG.warning(
                "clarification_v2_ranker_failed_falling_back_to_v1 "
                "attempt_id=%s exception_class=%s",
                attempt_id,
                type(exc).__name__,
            )
            # fall through to the v1 path below

    elif clarification_v2_ranker_enabled() and _clarification_enabled() and not resolver_v2_enabled():
        # Row 3 of the §8.2 matrix: the ranker flag is on but there is no V2
        # scoring to rank (APOLLO_RESOLVER_V2 is OFF, so no snapshot was ever
        # produced) -- fall back to v1, no force-enabling V2 from here.
        _LOG.info("clarification_v2_no_resolver_v2 attempt_id=%s", attempt_id)
        v2_selection.emit_trace(
            v2_selection.build_empty_trace(snapshot_source="none_v1_fallback"),
            attempt_id=attempt_id,
        )

    return await _v1_select(
        db=db,
        parsed_nodes=parsed_nodes,
        candidates=candidates,
        symbolic_mappings=symbolic_mappings,
        embedder=embedder,
        cache=cache,
        attempt_id=attempt_id,
        session_id=session_id,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        asked_turn=asked_turn,
        nli_ctx=nli_ctx,
    )
