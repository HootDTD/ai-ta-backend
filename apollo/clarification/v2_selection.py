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

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.clarification.probe import build_probe_hint
from apollo.clarification.store import load_asked_candidate_keys, write_asked_waiting
from apollo.clarification.v2_config import load_clarification_v2_params
from apollo.clarification.v2_ranker import pack_questions, rank_by_voi, v2_gray_candidates
from apollo.grading.composite import load_weights
from apollo.ontology.nodes import Node, build_node
from apollo.resolution.candidates import Candidate
from apollo.resolver_v2.incremental_types import IncrementalSnapshot

_LOG = logging.getLogger(__name__)

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
) -> list[str]:
    """V2 selection pipeline (§3.1). Returns the same ``list[str]``
    answer-blind hints the v1 path returns.

    ``candidates`` is the attempt's closed candidate set (the same tuple the
    v1 path receives) -- used only to look up each pooled reference node's
    ``node_type``/``display_name`` for ``build_probe_hint``; V2 candidate
    identity is otherwise entirely canonical-key based (the snapshot).
    """
    params = load_clarification_v2_params()

    pool = v2_gray_candidates(snapshot, params)
    if not pool:
        return []

    asked_keys = await load_asked_candidate_keys(db, attempt_id=attempt_id)
    pool = [c for c in pool if c.canonical_key not in asked_keys]
    if not pool:
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
        return []

    candidate_by_key: dict[str, Candidate] = {c.canonical_key: c for c in candidates}

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
    return hints
