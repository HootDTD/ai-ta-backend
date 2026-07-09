"""Misconception detector orchestrator (T9).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.8.

``detect_misconceptions`` is PURE AGGREGATION across the three detection
tiers -- it loads the concept's misconception bank, then runs
``sympy_veto`` + ``bank_pattern`` + ``judge_concepts`` and collects every
``ConceptFinding`` they produce into one ``DetectionResult``. It does NOT
gate (``gate.py``) or merge (``merge.py``) -- those run downstream in
``done.py`` so this orchestrator stays reusable by the graph grader too
(design invariant #6 in the plan: many small, single-responsibility files).

Every external touchpoint -- the bank load, the judge call, the embedding
call -- is wrapped in its own try/except. A failure in ANY ONE of them
contributes zero findings from that source and is logged; it never
propagates and never prevents the other tiers from contributing (soft-fail,
design invariant #5 -- a detector error must never break grading).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.ontology import KGGraph
from apollo.overseer.misconception_bank import MisconceptionEntry, load_for_concept
from apollo.overseer.misconception_detector.bank_pattern import detect_bank_pattern
from apollo.overseer.misconception_detector.judge import judge_concepts
from apollo.overseer.misconception_detector.sympy_veto import detect_sign_veto
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
    EmbedFn,
    JudgeConceptInput,
    JudgeFn,
)
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)


async def _load_bank(db: AsyncSession, *, concept_id: int | None) -> tuple[MisconceptionEntry, ...]:
    """Soft-failing bank load. No ``concept_id`` -> empty bank (a valid,
    common case -- not every attempt is concept-scoped). Any load error
    (transient DB failure) also degrades to an empty bank rather than
    raising -- the sympy_veto/bank_pattern tiers already treat an empty
    bank as "abstain", so this failure mode is fully covered downstream."""
    if concept_id is None:
        return ()
    try:
        entries = await load_for_concept(db, concept_id=concept_id)
    except Exception:  # noqa: BLE001
        _LOG.exception(
            "misconception_detector_bank_load_failed concept_id=%s", concept_id
        )
        return ()
    return tuple(entries)


def _run_sympy_veto(
    student_graph: KGGraph,
    reference_graph: KGGraph,
    *,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> tuple[ConceptFinding, ...]:
    """Soft-failing wrapper -- ``detect_sign_veto`` is documented as never
    raising on its own, but this orchestrator treats every tier as
    untrusted so a future change to that contract can't crash a grade."""
    try:
        return detect_sign_veto(student_graph, reference_graph, bank_entries=bank_entries)
    except Exception:  # noqa: BLE001
        _LOG.exception("misconception_detector_sympy_veto_failed")
        return ()


async def _run_bank_pattern(
    db: AsyncSession,
    *,
    concept_id: int | None,
    student_utterances: tuple[str, ...],
    embed_fn: EmbedFn,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> tuple[ConceptFinding, ...]:
    """Soft-failing wrapper around the bank_pattern tier. ``detect_bank_pattern``
    already soft-fails internally per-utterance; this outer guard catches any
    failure that escapes it (e.g. a bad ``db``/dialect lookup) so a bank_pattern
    defect can never take down the other tiers."""
    try:
        return await detect_bank_pattern(
            db,
            concept_id=concept_id,
            student_utterances=student_utterances,
            embed_fn=embed_fn,
            bank_entries=bank_entries,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("misconception_detector_bank_pattern_failed")
        return ()


def _judge_concept_inputs(
    reference_graph: KGGraph,
    *,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> tuple[JudgeConceptInput, ...]:
    """One ``JudgeConceptInput`` per reference-graph node with usable surface
    text, keyed by ``node_id`` (matching ``centrality.py``'s and
    ``sympy_veto.py``'s node_id-keyed convention). ``correct_belief`` is
    derived via the shared ``student_surface_text`` helper so every node type
    (equation/condition/definition/...) yields a sensible belief string
    without this module re-implementing that per-type switch. Every concept
    gets the FULL bank (the judge prompt is a single batched call across all
    concepts; per-concept bank filtering is a future refinement, not required
    by this task's frozen contract)."""
    inputs: list[JudgeConceptInput] = []
    for node in reference_graph.nodes:
        belief = student_surface_text(node)
        if not belief:  # pragma: no cover - every current node content type
            # has a min_length=1 Pydantic field, so student_surface_text()
            # cannot return "" for any of the six known node types today;
            # kept as a defensive guard against a future node type that
            # legitimately yields no surface text (never a crash, per the
            # helper's own documented contract).
            continue
        inputs.append(
            JudgeConceptInput(
                concept_key=node.node_id,
                correct_belief=belief,
                bank_entries=bank_entries,
            )
        )
    return tuple(inputs)


def _run_judge(
    *,
    problem_text: str,
    reference_graph: KGGraph,
    bank_entries: tuple[MisconceptionEntry, ...],
    student_utterances: tuple[str, ...],
    judge_fn: JudgeFn,
) -> tuple[ConceptFinding, ...]:
    """Soft-failing wrapper around the judge tier. ``judge_concepts`` already
    soft-fails internally (a raising ``judge_fn`` or malformed JSON both
    degrade to all-`clear`); this outer guard exists so a defect in prompt
    construction itself (e.g. a malformed ``JudgeConceptInput``) can't
    propagate either. ``student_utterances`` is forwarded so the judge's
    prompt is actually grounded in what the student said (previously this
    tier never saw the student at all -- the structural-blindness defect)."""
    concepts = _judge_concept_inputs(reference_graph, bank_entries=bank_entries)
    if not concepts:
        return ()
    try:
        return judge_concepts(
            problem_text=problem_text,
            concepts=concepts,
            judge_fn=judge_fn,
            student_utterances=student_utterances,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("misconception_detector_judge_failed")
        return ()


async def detect_misconceptions(
    db: AsyncSession,
    *,
    attempt_id: int,
    concept_id: int | None,
    student_graph: KGGraph,
    reference_graph: KGGraph,
    problem_text: str,
    student_utterances: tuple[str, ...],
    judge_fn: JudgeFn,
    embed_fn: EmbedFn,
) -> DetectionResult:
    """Orchestrator: load the bank, run all three detection tiers, aggregate.

    Loads ``bank_entries`` via ``load_for_concept`` (empty on ``concept_id is
    None`` or any load failure), then runs ``sympy_veto`` + ``bank_pattern`` +
    ``judge_concepts`` and concatenates every ``ConceptFinding`` they produce
    into one ``DetectionResult``. Gate/merge run downstream in ``done.py``.

    Every tier is individually soft-failed: a raising tier contributes zero
    findings from that tier while the others still run and contribute theirs
    (this function itself never raises). An empty bank does not stop the
    judge tier from running -- it just means ``sympy_veto``/``bank_pattern``
    have nothing bank-specific to match against and abstain by their own
    contracts. ``attempt_id`` is accepted for logging/future ledger-keying
    parity with the rest of the pipeline; this pure-aggregation stage does
    not itself write anything.
    """
    bank_entries = await _load_bank(db, concept_id=concept_id)

    sympy_findings = _run_sympy_veto(
        student_graph, reference_graph, bank_entries=bank_entries
    )
    bank_pattern_findings = await _run_bank_pattern(
        db,
        concept_id=concept_id,
        student_utterances=student_utterances,
        embed_fn=embed_fn,
        bank_entries=bank_entries,
    )
    judge_findings = _run_judge(
        problem_text=problem_text,
        reference_graph=reference_graph,
        bank_entries=bank_entries,
        student_utterances=student_utterances,
        judge_fn=judge_fn,
    )

    per_concept = sympy_findings + bank_pattern_findings + judge_findings
    return DetectionResult(per_concept=per_concept)


__all__ = ["detect_misconceptions"]
