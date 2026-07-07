"""Resolver V2 integration — DB turn loader, ``GradeResult`` substitution,
trace dump (task T7).

This is the ONLY resolver_v2 module ``done_grading.py`` touches (lazily,
inside the ``resolver_v2_enabled()`` branch — flag-OFF never imports it).
:func:`apply_resolver_v2` loads the attempt's student turns, derives the v1
floors from the already-built ``S_norm``, runs the engine off the event loop
(CPU-bound NLI), and substitutes EXACTLY three ``GradeResult`` numbers
(``coverage_score``, ``node_coverage_score``, ``edge_coverage_score`` — §2
scope guards). Substitution happens BEFORE ``build_audited_grade``, so the
§10 composite gate, the artifact, and replay all read V2 numbers with zero
further changes (design §4/§11).

Failure semantics — DELIBERATE DEVIATION from the design card: the card said
a V2 failure follows the chain's broad-except NO-FALLBACK contract, but the
integration mandate pins the opposite for the prototype: ANY engine/loader
exception under flag-ON is logged (``resolver_v2_failed_falling_back_to_v1``)
and grading proceeds on the UNTOUCHED v1 scores — a shadow experiment must
never crash (or re-arm the retry loop for) the real grading chain. The
flag-OFF path never reaches this module at all.

Trace dump: when ``APOLLO_RESOLVER_V2_TRACE_DIR`` is set, the full per-attempt
trace JSON is written to ``<dir>/attempt_<id>.json`` — NEW FILES ONLY (an
existing file is never overwritten; calibration reads these) and a dump
failure is logged, never raised.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.graph_compare.canonical import CanonicalGraph, ReferenceGraph
from apollo.graph_compare.core import GradeResult
from apollo.graph_compare.soundness import is_misconception_key
from apollo.persistence.models import Message
from apollo.resolver_v2.config import grayzone_enabled, load_params
from apollo.resolver_v2.engine import run_resolver_v2
from apollo.resolver_v2.grayzone import main_chat_grayzone
from apollo.resolver_v2.nli_provider import get_adjudicator
from apollo.resolver_v2.types import ResolverV2Result

_LOG = logging.getLogger(__name__)

#: ``apollo_messages.role`` value for student turns (writer: ``chat.py``;
#: same literal ``done_turn_order.py`` filters on).
STUDENT_ROLE: str = "student"

#: v1 edge triple shape shared with ``edges.score_edges``: ``(edge_type,
#: from_key, to_key)`` with the ``EdgeType`` StrEnum collapsed to its plain
#: string value (mirrors v1 ``scores.py`` match keying).
_Triple = tuple[str, str, str]


async def load_student_turns(db: AsyncSession, attempt_id: int) -> tuple[str, ...]:
    """The attempt's student-turn texts, in ``turn_index`` order (§5.1 input:
    student turns ONLY — Apollo's replies are never premise text)."""
    rows = (
        (
            await db.execute(
                select(Message.content)
                .where(Message.attempt_id == attempt_id, Message.role == STUDENT_ROLE)
                .order_by(Message.turn_index)
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def v1_inputs_from_canonical(
    student_canonical: CanonicalGraph,
) -> tuple[frozenset[str], frozenset[_Triple], frozenset[_Triple]]:
    """Derive the engine's v1 floors from the already-built ``S_norm``:

    - resolved keys (EXCLUDING ``misc.*`` — a resolved misconception must
      never floor a reference node's credit),
    - explicit edge triples (v1 edge credit 1.0),
    - inferred edge triples (v1 edge credit 0.5).

    Pure reshaping; the guarantees ("V2 >= V1 by construction", §2) hang off
    these three sets.
    """
    resolved_keys = frozenset(
        node.canonical_key
        for node in student_canonical.nodes
        if not is_misconception_key(node.canonical_key)
    )
    explicit = frozenset(
        (str(edge.edge_type), edge.from_key, edge.to_key)
        for edge in student_canonical.edges
        if edge.provenance == "explicit"
    )
    inferred = frozenset(
        (str(edge.edge_type), edge.from_key, edge.to_key)
        for edge in student_canonical.edges
        if edge.provenance == "inferred"
    )
    return resolved_keys, explicit, inferred


def substitute_scores(grade: GradeResult, v2: ResolverV2Result) -> GradeResult:
    """Write V2's two coverages into the frozen ``GradeResult`` — the §2 scope
    guard: EXACTLY three fields change (``coverage_score`` and
    ``node_coverage_score`` both take the winning-path node coverage;
    ``edge_coverage_score`` takes the graded edge coverage). Findings,
    soundness, sub-scores, confidence — everything else — stay v1."""
    return replace(
        grade,
        coverage_score=v2.node_coverage,
        node_coverage_score=v2.node_coverage,
        edge_coverage_score=v2.edge_coverage,
    )


def _dump_trace(trace: dict, *, attempt_id: int, trace_dir: str) -> None:
    """Write the full trace JSON to ``<trace_dir>/attempt_<id>.json`` — new
    files only, failure logged and swallowed (a trace dump must never affect
    grading)."""
    try:
        directory = Path(trace_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"attempt_{attempt_id}.json"
        if path.exists():
            _LOG.info("resolver_v2_trace_exists_skipping path=%s", path)
            return
        path.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        _LOG.warning(
            "resolver_v2_trace_dump_failed attempt_id=%s dir=%s",
            attempt_id,
            trace_dir,
            exc_info=True,
        )


async def apply_resolver_v2(
    db: AsyncSession,
    *,
    attempt_id: int,
    grade: GradeResult,
    student_canonical: CanonicalGraph,
    reference_graph: ReferenceGraph,
    problem_payload: dict,
) -> tuple[GradeResult, dict | None]:
    """Step 8b (flag ON only): run the V2 engine for this attempt and return
    ``(grade with V2 scores substituted, trace dict)``.

    - params are read fresh (env-tunable per attempt, §7);
    - NLI comes from V2's own lazy singleton (works even when the v1 tier's
      ``APOLLO_NLI_ENABLED`` is off; ``None`` = lexical-only degrade);
    - the gray-zone LLM check runs only when ``grayzone_enabled()``
      (default OFF = deterministic);
    - the engine runs in ``asyncio.to_thread`` (CPU-bound transformer
      inference must not block the event loop — same offload the v1 NLI tier
      uses);
    - ANY exception: log + return the ORIGINAL grade with ``None`` trace (the
      prototype never crashes grading — see module docstring for why this
      deviates from the design card's NO-FALLBACK line).
    """
    try:
        params = load_params()
        student_turns = await load_student_turns(db, attempt_id)
        v1_keys, v1_explicit, v1_inferred = v1_inputs_from_canonical(student_canonical)
        nli = get_adjudicator()
        grayzone_fn = main_chat_grayzone if grayzone_enabled() else None
        result: ResolverV2Result = await asyncio.to_thread(
            run_resolver_v2,
            student_turns=student_turns,
            reference_graph=reference_graph,
            problem_payload=problem_payload,
            v1_resolved_keys=v1_keys,
            v1_explicit_triples=v1_explicit,
            v1_inferred_triples=v1_inferred,
            nli=nli,
            grayzone_fn=grayzone_fn,
            params=params,
        )
        trace = result.trace()
        if params.trace_dir:
            _dump_trace(trace, attempt_id=attempt_id, trace_dir=params.trace_dir)
        _LOG.info(
            "resolver_v2_applied attempt_id=%s node_coverage=%.4f edge_coverage=%.4f "
            "pair_count=%d grayzone_used=%s",
            attempt_id,
            result.node_coverage,
            result.edge_coverage,
            result.pair_count,
            result.grayzone_used,
        )
        return substitute_scores(grade, result), trace
    except Exception:  # noqa: BLE001 — prototype fallback contract (see docstring)
        _LOG.warning(
            "resolver_v2_failed_falling_back_to_v1 attempt_id=%s", attempt_id, exc_info=True
        )
        return grade, None
