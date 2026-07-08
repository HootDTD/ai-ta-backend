"""V2 selection pipeline (integration spec §2.1/§3.1, task T9).

Thin orchestrator that the flag-gated branch of ``run_clarification_detection``
(turn.py, task T10) calls when the V2 VoI ranker is active. Wires together the
already-built T5-T8 pieces:

    pool   = v2_gray_candidates(snapshot)                     # T6
    pool  -= load_asked_candidate_keys(db, attempt_id)         # T8 dedup + M4 cap
    ranked = rank_by_voi(pool, snapshot, weights)               # T5
    packed = pack_questions(ranked, max_q=3, max_topics=3)      # T5/T7
    for each selected topic: build_probe_hint(...) + write_asked_waiting(...)

Returns the SAME ``list[str]`` answer-blind hints the v1 path
(``run_clarification_detection``) returns -- NOT ``Probe``/``PackedQuestion``
objects. Answer-blindness is inherited, not re-implemented (§10.2): every hint
comes from the existing ``build_probe_hint(node, candidate)``, which names
only the dimension (direction/variable/relationship) and never renders the
candidate's content. Hints then flow through the existing
``draft_reply``/``guard_clarification_reply`` machinery unchanged -- no new
LLM prompt surface is introduced here.

An empty pool (nothing gray/missing, everything already asked, or the
per-attempt cap already spent) returns ``[]`` -- a valid outcome, not an
error (§8.3).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.clarification.probe import build_probe_hint
from apollo.clarification.store import load_asked_candidate_keys, write_asked_waiting
from apollo.clarification.v2_config import ClarificationV2Params, load_clarification_v2_params
from apollo.clarification.v2_ranker import (
    PackedQuestion,
    VoICandidate,
    VoIScore,
    pack_questions,
    rank_by_voi,
    v2_gray_candidates,
)
from apollo.grading.composite import load_weights
from apollo.ontology.nodes import Node, build_node
from apollo.resolution.candidates import Candidate
from apollo.resolver_v2.incremental_types import IncrementalSnapshot

_LOG = logging.getLogger(__name__)

#: Reuses the SAME sink the resolver_v2 per-attempt trace dump uses (spec
#: §7: "no new sink") -- when set, the clarification-v2 trace is APPENDED
#: (one JSON line per turn) to ``<dir>/attempt_<id>_clarification_v2.jsonl``,
#: alongside the (separately, new-file-only) ``attempt_<id>.json`` Done dump
#: ``apollo.resolver_v2.integration`` writes.
_TRACE_DIR_ENV = "APOLLO_RESOLVER_V2_TRACE_DIR"

#: Per node type, the hint DIMENSION TYPE (never the rendered hint string --
#: L1 fix, spec §7). Mirrors ``probe.py``'s ``_HINT_BY_TYPE`` keys, one level
#: more abstract: a category name, not the phrased instruction.
_HINT_DIM_BY_TYPE: dict[str, str] = {
    "condition": "direction",
    "equation": "variable",
    "simplification": "condition",
    "definition": "definition",
    "procedure_step": "action",
    "variable_mapping": "relationship",
}
_HINT_DIM_FALLBACK = "general"


def _hint_dim(node_type: str) -> str:
    return _HINT_DIM_BY_TYPE.get(node_type, _HINT_DIM_FALLBACK)


@dataclass(frozen=True)
class TracePoolEntry:
    canonical_key: str
    node_type: str
    node_credit: float
    is_gray: bool
    source: str


@dataclass(frozen=True)
class TraceRankedEntry:
    canonical_key: str
    importance: float
    uncertainty: float
    voi: float


@dataclass(frozen=True)
class TraceQuestion:
    question_index: int
    topic_keys: tuple[str, ...]
    hint_dims: tuple[str, ...]


@dataclass(frozen=True)
class TraceBudget:
    pair_count_this_turn: int
    pair_count_total: int
    budget_truncated: bool


@dataclass(frozen=True)
class ClarificationV2Trace:
    """Spec §7 observability payload. Frozen + plain-JSON-safe fields only
    (str/float/int/bool/tuple-of-those); :meth:`as_dict` is the sole
    serialization entry point so every caller round-trips ``json.dumps`` the
    same way."""

    enabled: bool
    snapshot_source: str  # "this_turn" | "prior_turn" | "none_v1_fallback"
    pool: tuple[TracePoolEntry, ...]
    ranked: tuple[TraceRankedEntry, ...]  # top params.trace_top_n only
    questions: tuple[TraceQuestion, ...]
    asked_dedup_skipped: tuple[str, ...]
    budget: TraceBudget
    seeded: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "clarification_v2": {
                "enabled": self.enabled,
                "snapshot_source": self.snapshot_source,
                "pool": [asdict(p) for p in self.pool],
                "ranked": [asdict(r) for r in self.ranked],
                "questions": [
                    {
                        "question_index": q.question_index,
                        "topic_keys": list(q.topic_keys),
                        "hint_dims": list(q.hint_dims),
                    }
                    for q in self.questions
                ],
                "asked_dedup_skipped": list(self.asked_dedup_skipped),
                "budget": asdict(self.budget),
                "seeded": list(self.seeded),
            }
        }


def build_empty_trace(*, snapshot_source: str, enabled: bool = True) -> ClarificationV2Trace:
    """The trace for a turn that never reached the V2 pool -- either no
    completed snapshot exists yet (``snapshot_source="none_v1_fallback"``)
    or the ranker itself failed before building a pool. All list-shaped
    fields are empty; budget is all-zero."""
    return ClarificationV2Trace(
        enabled=enabled,
        snapshot_source=snapshot_source,
        pool=(),
        ranked=(),
        questions=(),
        asked_dedup_skipped=(),
        budget=TraceBudget(pair_count_this_turn=0, pair_count_total=0, budget_truncated=False),
        seeded=(),
    )


def emit_trace(trace: ClarificationV2Trace, *, attempt_id: int) -> None:
    """Log the trace (chat-turn clarification log) and, when
    ``APOLLO_RESOLVER_V2_TRACE_DIR`` is set, append it to this attempt's
    per-attempt trace file (spec §7: extend the existing sink, no new one).
    A JSON-safety failure or a dump I/O failure is logged and swallowed --
    tracing must never affect selection or teaching."""
    payload = trace.as_dict()
    try:
        serialized = json.dumps(payload)
    except (TypeError, ValueError):
        _LOG.warning("clarification_v2_trace_not_json_safe attempt_id=%s", attempt_id)
        return

    _LOG.info("clarification_v2_trace attempt_id=%s trace=%s", attempt_id, serialized)

    trace_dir = os.environ.get(_TRACE_DIR_ENV)
    if not trace_dir:
        return
    try:
        directory = Path(trace_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"attempt_{attempt_id}_clarification_v2.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(serialized + "\n")
    except OSError:
        _LOG.warning(
            "clarification_v2_trace_dump_failed attempt_id=%s dir=%s",
            attempt_id,
            trace_dir,
            exc_info=True,
        )


def _build_trace(
    *,
    snapshot_source: str,
    pool: list[VoICandidate],
    ranked: list[VoIScore],
    packed: list[PackedQuestion],
    candidate_by_key: dict[str, Candidate],
    asked_dedup_skipped: tuple[str, ...],
    pair_count_this_turn: int,
    pair_count_total: int,
    budget_truncated: bool,
    seeded_keys: frozenset[str],
    params: ClarificationV2Params,
) -> ClarificationV2Trace:
    def _node_type(key: str, fallback: str) -> str:
        candidate = candidate_by_key.get(key)
        return candidate.node_type if candidate is not None else fallback

    pool_entries = tuple(
        TracePoolEntry(
            canonical_key=c.canonical_key,
            node_type=_node_type(c.canonical_key, c.node_type),
            node_credit=c.node_credit,
            is_gray=c.is_gray,
            source="gray" if c.is_gray else "missing",
        )
        for c in pool
    )
    ranked_entries = tuple(
        TraceRankedEntry(
            canonical_key=s.candidate.canonical_key,
            importance=s.importance,
            uncertainty=s.uncertainty,
            voi=s.voi,
        )
        for s in ranked[: max(0, params.trace_top_n)]
    )
    question_entries = tuple(
        TraceQuestion(
            question_index=index,
            topic_keys=q.topic_keys,
            hint_dims=tuple(_hint_dim(_node_type(key, "")) for key in q.topic_keys),
        )
        for index, q in enumerate(packed)
    )
    return ClarificationV2Trace(
        enabled=True,
        snapshot_source=snapshot_source,
        pool=pool_entries,
        ranked=ranked_entries,
        questions=question_entries,
        asked_dedup_skipped=asked_dedup_skipped,
        budget=TraceBudget(
            pair_count_this_turn=pair_count_this_turn,
            pair_count_total=pair_count_total,
            budget_truncated=budget_truncated,
        ),
        seeded=tuple(seeded_keys),
    )

# Minimal, schema-valid placeholder content per node type -- used only to
# construct a Node good enough for build_probe_hint (which reads ONLY
# node.node_type, never node.content) to run against. Never rendered to the
# student (§10.2 answer-blindness is entirely a function of node_type here).
_PLACEHOLDER_CONTENT: dict[str, dict] = {
    "equation": {"symbolic": "x", "label": ""},
    "condition": {"applies_when": "x", "label": ""},
    "simplification": {"applies_when": "x", "transformation": "x"},
    "definition": {"concept": "x", "meaning": "x"},
    "variable_mapping": {"term": "x", "symbol": "x"},
    "procedure_step": {"action": "x", "purpose": ""},
}


def _reference_probe_node(candidate: Candidate, *, attempt_id: int) -> Node | None:
    """Build a placeholder ``Node`` carrying only ``candidate.node_type`` --
    the sole field ``build_probe_hint`` reads. Returns ``None`` for a
    ``node_type`` this ontology does not know (defensive; should not happen
    for a real closed candidate set)."""
    content = _PLACEHOLDER_CONTENT.get(candidate.node_type)
    if content is None:
        return None
    return build_node(
        node_type=candidate.node_type,
        node_id=candidate.canonical_key,
        attempt_id=attempt_id,
        source="system",
        content=content,
    )


async def select(
    snapshot: IncrementalSnapshot,
    candidates: tuple[Candidate, ...],
    db: AsyncSession,
    attempt_id: int,
    *,
    session_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int | None,
    asked_turn: int,
    snapshot_source: str = "prior_turn",
    pair_count_total: int = 0,
    seeded_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """V2 selection pipeline (§3.1). Returns the same ``list[str]``
    answer-blind hints the v1 path returns.

    ``candidates`` is the attempt's closed candidate set (the same tuple the
    v1 path receives) -- used only to look up each pooled reference node's
    ``node_type``/``display_name`` for ``build_probe_hint``; V2 candidate
    identity is otherwise entirely canonical-key based (the snapshot).

    ``snapshot_source``/``pair_count_total``/``seeded_keys`` are trace-only
    (spec §7, task T13) -- passed through by the caller for observability,
    never consulted by the selection logic itself. A :class:`ClarificationV2Trace`
    is always built and emitted (:func:`emit_trace`) before returning, at
    every exit point, including the empty-pool/empty-packed early returns.
    """
    params = load_clarification_v2_params()

    pool_all = v2_gray_candidates(snapshot, params)
    candidate_by_key: dict[str, Candidate] = {c.canonical_key: c for c in candidates}

    def _emit(
        pool: list[VoICandidate],
        ranked: list[VoIScore],
        packed: list[PackedQuestion],
        asked_dedup_skipped: tuple[str, ...],
    ) -> None:
        trace = _build_trace(
            snapshot_source=snapshot_source,
            pool=pool,
            ranked=ranked,
            packed=packed,
            candidate_by_key=candidate_by_key,
            asked_dedup_skipped=asked_dedup_skipped,
            pair_count_this_turn=snapshot.pair_count_this_turn,
            pair_count_total=pair_count_total,
            budget_truncated=snapshot.budget_truncated,
            seeded_keys=seeded_keys,
            params=params,
        )
        emit_trace(trace, attempt_id=attempt_id)

    if not pool_all:
        _emit([], [], [], ())
        return []

    asked_keys = await load_asked_candidate_keys(db, attempt_id=attempt_id)
    pool = [c for c in pool_all if c.canonical_key not in asked_keys]
    dedup_skipped = tuple(
        sorted(c.canonical_key for c in pool_all if c.canonical_key in asked_keys)
    )
    if not pool:
        _emit(pool_all, [], [], dedup_skipped)
        return []

    weights = load_weights()
    ranked = rank_by_voi(pool, snapshot, weights, params)

    remaining_budget = max(0, params.max_questions_per_attempt - len(asked_keys))
    packed = pack_questions(
        ranked,
        params.max_questions,
        params.max_topics_per_question,
        remaining_budget,
    )
    if not packed:
        _emit(pool_all, ranked, [], dedup_skipped)
        return []

    hints: list[str] = []
    for question in packed:
        for topic_key in question.topic_keys:
            candidate = candidate_by_key.get(topic_key)
            if candidate is None:
                # Defensive: a snapshot node with no matching entry in this
                # attempt's closed candidate set. Should not happen for a
                # correctly-built pool (§2.1), but never crash selection over
                # a single stale/foreign key -- skip it (§8.3 fail-open spirit).
                _LOG.warning(
                    "clarification_v2_candidate_missing attempt_id=%s canonical_key=%s",
                    attempt_id,
                    topic_key,
                )
                continue
            node = _reference_probe_node(candidate, attempt_id=attempt_id)
            if node is None:
                _LOG.warning(
                    "clarification_v2_unknown_node_type attempt_id=%s canonical_key=%s "
                    "node_type=%s",
                    attempt_id,
                    topic_key,
                    candidate.node_type,
                )
                continue
            await write_asked_waiting(
                db,
                attempt_id=attempt_id,
                session_id=session_id,
                user_id=user_id,
                search_space_id=search_space_id,
                concept_id=concept_id,
                node_id=candidate.canonical_key,
                candidate_key=candidate.canonical_key,
                probe_question="",
                original_statement=candidate.display_name,
                asked_turn=asked_turn,
            )
            hints.append(build_probe_hint(node, candidate))

    _emit(pool_all, ranked, packed, dedup_skipped)
    return hints
