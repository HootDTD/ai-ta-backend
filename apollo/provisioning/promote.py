"""WU-3B2g — the promotion step (Tier-1 -> Tier-2 + ``:Canon`` projection).

``promote`` is the LAST of the six per-document stages the orchestrator drives.
It REUSES three frozen primitives and re-implements none of their logic:

  * ``annotate_reference_solution`` (§8 seed converter) — stamps each
    reference-solution step with its ``entity_key`` + a top-level
    ``declared_paths`` (the §6.1 annotated-graph contract the lint's gate 2
    requires);
  * ``run_promotion_lint`` (3B2b) — the eight §8B.4 gates, reading the concept's
    AUTHORED ``canonical_symbols`` / ``normalization_map`` (gate 4 non-vacuity)
    plus the caller-supplied ``existing_problem_hashes`` (gate 8 dedup);
  * ``project_canon`` (3C1) — the idempotent ``:Canon`` MERGE for the concept's
    Layer-1 entities.

On a PASS it stores the annotated reference solution + ``solution_source`` into
the ``apollo_concept_problems.payload``, flips ``tier`` 1->2 (keyed on the
existing row id — NEVER an insert), flushes, then projects ``:Canon``. The tier
flip is idempotent (a re-run flips an already-``tier=2`` row to ``2``, a no-op)
and the ``:Canon`` MERGE is idempotent, so a re-claimed job's re-promote is
replay-safe (§2c).

On a FAIL it returns ``PromoteResult(promoted=False, failed_gate, diagnostic)``
WITHOUT touching the row. ``promote`` does NOT write the
``apollo_rejected_problems`` row — the orchestrator is the SINGLE rejection-write
owner (so the gate->reject mapping lives in one place).

``promote`` never commits or rolls back the caller's session: the orchestrator
owns the transaction. A ``CanonProjectionError`` from ``project_canon`` is
re-raised (the orchestrator maps it to an ``apollo_ingest_errors`` row + a failed
run); the already-flushed tier flip is left in place — it is idempotently
re-projected on the next attempt.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.canon_projection import project_canon
from apollo.persistence.learner_model_seed import (
    _entity_key_for_step,
    annotate_reference_solution,
)
from apollo.persistence.models import Concept, ConceptProblem
from apollo.provisioning.promotion_lint import run_promotion_lint
from apollo.provisioning.subject_profile import resolve_profile
from apollo.provisioning.tag_mint import MintPlan

__all__ = ["promote", "PromoteResult"]

_LOG = logging.getLogger(__name__)

_SOLUTION_SOURCE_DEFAULT = "generated"


class PromoteResult(BaseModel):
    """The promote outcome (the 3B2g orchestrator handoff). ``failed_gate`` is the
    1..8 gate number on a lint failure, ``None`` on a pass."""

    promoted: bool
    failed_gate: int | None = None
    diagnostic: str = ""


def _annotate(problem: dict, mint_plan: MintPlan) -> dict:
    """Return the annotated reference graph the lint consumes: each step carries
    its ``entity_key`` (derived from the step's ``entry_type``/``id`` via the
    frozen §8 D5 mapping, the SAME key ``tag_and_mint`` minted under) + a
    top-level ``declared_paths``. Immutable — ``annotate_reference_solution``
    returns a NEW dict and never mutates ``problem``.

    ``mint_plan`` is accepted so the key resolution can be tied to the minted
    entities; v1 derives the per-node key deterministically from the step shape
    (identical to what ``reference_solution_to_entities`` minted), so the lint's
    gate-2 closure sees the SAME keys that were written to the graph.
    """
    steps_by_id = {step["id"]: step for step in problem.get("reference_solution", [])}

    def _key_for_node(node_id: str) -> str:
        return _entity_key_for_step(steps_by_id[node_id])

    return annotate_reference_solution(problem, _key_for_node)


async def promote(
    db: AsyncSession,
    neo,
    *,
    problem: dict,
    mint_plan: MintPlan,
    search_space_id: int,
    concept_problem_id: int,
    existing_problem_hashes: set[str] | frozenset[str],
) -> PromoteResult:
    """Annotate -> lint -> (on PASS) flip tier 1->2 + store payload +
    ``project_canon``. See the module docstring for the full contract.

    Returns ``PromoteResult(promoted=True)`` on a pass (the orchestrator counts
    it + the row is now teachable), or ``PromoteResult(promoted=False,
    failed_gate, diagnostic)`` on a lint failure (the orchestrator writes the
    rejection row; the row stays Tier-1). Raises ``CanonProjectionError`` when the
    ``:Canon`` projection fails (the orchestrator maps it to a failed run)."""
    try:
        annotated = _annotate(problem, mint_plan)
    except (KeyError, TypeError) as exc:
        # _annotate runs BEFORE run_promotion_lint's gate-1 schema validation, so
        # a malformed problem (a step missing id/entry_type, or an entry_type
        # outside the frozen mint map) would KeyError here and surface to the
        # orchestrator as an unexpected-exception WHOLE-DOCUMENT abort. Convert it
        # to the clean gate-1 rejection the lint produces for the cases that reach
        # it — one bad candidate must not sink the document.
        _LOG.info(
            "provisioning_promote_rejected",
            extra={
                "event": "provisioning_promote_rejected",
                "concept_problem_id": concept_problem_id,
                "failed_gate": 1,
            },
        )
        return PromoteResult(
            promoted=False,
            failed_gate=1,
            diagnostic=f"gate 1: malformed problem rejected before annotation: {exc}",
        )

    # Read the concept's AUTHORED symbol set (gate-4 non-vacuity). The shape is
    # {"symbols": [...], ...} (author_concept_symbols, 3B2d); a vacuous set makes
    # gate 4 reject every foreign symbol.
    concept = await db.get(Concept, mint_plan.concept_id)
    if concept is None:
        raise RuntimeError(f"promote: concept {mint_plan.concept_id} not found")
    canonical_symbols = set(dict(concept.canonical_symbols or {}).get("symbols") or [])
    normalization_map = dict(concept.normalization_map or {})

    # Subject-fluid Apollo: resolve the subject's PERSISTED profile (the gates and
    # node vocab the lint runs under). resolve_profile FAILS OPEN to
    # quantitative_symbolic (all 8 gates — today's fluid behavior) when the subject
    # is un-detected, so a pre-031 / freshly-backfilled subject promotes exactly as
    # before. No LLM in this control path: the profile_kind was detected once at
    # ingest and read deterministically here.
    profile = await resolve_profile(db, concept.subject_id)

    result = run_promotion_lint(
        annotated,
        canonical_symbols=canonical_symbols,
        normalization_map=normalization_map,
        existing_problem_hashes=set(existing_problem_hashes),
        active_gates=profile.active_gates,
    )
    if not result.ok:
        _LOG.info(
            "provisioning_promote_rejected",
            extra={
                "event": "provisioning_promote_rejected",
                "concept_problem_id": concept_problem_id,
                "failed_gate": result.failed_gate,
            },
        )
        return PromoteResult(
            promoted=False,
            failed_gate=result.failed_gate,
            diagnostic=result.diagnostic,
        )

    # --- PASS: flip tier 1->2, RE-HOME to the tagged concept, store solution --- #
    row = await db.get(ConceptProblem, concept_problem_id)
    if row is None:
        raise RuntimeError(f"promote: concept_problem {concept_problem_id} not found")
    # Store the COMPLETE annotated problem as the payload: the student selector
    # (``list_problems_for_concept``) and the gate-8 dup-hash both validate the
    # payload through ``Problem.model_validate``, so the promoted row's payload MUST
    # be a full Problem (id/concept_id/difficulty/problem_text/given_values/
    # target_unknown + the annotated reference_solution + declared_paths), not a
    # Tier-1 inventory stub. ``annotated`` carries every original problem field
    # (annotate_reference_solution returns a NEW dict of the whole problem) plus the
    # entity_key per step + declared_paths. The Tier-1 row's existing keys (its
    # content-derived ``id``) are preserved where the annotated dict does not set
    # them. Immutable assign (a NEW dict — never an in-place mutate).
    new_payload = {**(row.payload or {}), **annotated}
    row.payload = new_payload  # type: ignore[assignment]
    # RE-HOME the row from the provisional-inventory concept (scrape.py's seam home)
    # onto the REAL tagged concept (``tag_and_mint`` resolved). The student selector
    # ``list_problems_for_concept`` filters ``concept_id == <session concept> AND
    # tier == 2`` (problem_selector.py); without this re-home a promoted Tier-2 row
    # stranded on ``provisional.inventory`` is permanently UNREACHABLE. Idempotent: a
    # re-run re-assigns the SAME tagged concept_id (no-op). (scrape.py:18.)
    row.concept_id = mint_plan.concept_id  # type: ignore[assignment]
    row.tier = 2  # type: ignore[assignment]
    if not row.solution_source:
        row.solution_source = _SOLUTION_SOURCE_DEFAULT  # type: ignore[assignment]
    await db.flush()

    # --- :Canon projection (idempotent MERGE) -------------------------------- #
    await project_canon(db, neo, search_space_id=search_space_id, concept_id=mint_plan.concept_id)

    _LOG.info(
        "provisioning_promote_ok",
        extra={
            "event": "provisioning_promote_ok",
            "concept_problem_id": concept_problem_id,
            "concept_id": mint_plan.concept_id,
        },
    )
    return PromoteResult(promoted=True, failed_gate=None, diagnostic="")
