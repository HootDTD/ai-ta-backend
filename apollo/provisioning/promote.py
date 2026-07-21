"""WU-3B2g — the promotion step (Tier-1 -> Tier-2 + ``:Canon`` projection).

``promote`` is the LAST of the six per-document stages the orchestrator drives.
It REUSES three frozen primitives and re-implements none of their logic:

  * ``annotate_reference_solution`` (§8 seed converter) — stamps each
    reference-solution step with its ``entity_key`` + a top-level
    ``declared_paths`` (the §6.1 annotated-graph contract the lint's gate 2
    requires);
  * ``run_promotion_lint`` (3B2b) — the nine §8B.4 gates, reading the concept's
    AUTHORED ``canonical_symbols`` / ``normalization_map`` (gate 4 non-vacuity)
    plus the caller-supplied ``existing_problem_hashes`` (gate 8 dedup);
  * ``project_canon`` (3C1) — the idempotent ``:Canon`` MERGE for the concept's
    Layer-1 entities.

On a PASS it stores the annotated reference solution + ``solution_source`` into
the promoted ``app.problems`` columns, flips ``tier`` 1->2 (keyed on the
existing row id — NEVER an insert), flushes, then projects ``:Canon``. The tier
flip is idempotent (a re-run flips an already-``tier=2`` row to ``2``, a no-op)
and the ``:Canon`` MERGE is idempotent, so a re-claimed job's re-promote is
replay-safe (§2c).

With the default-OFF multi-path flag enabled, promotion replaces the legacy
all-node ``declared_paths`` value with the enumerated object paths only when the
entire replacement set passes ``validate_reference_graph``. That validation
includes joint graph coverage and the object-path sink-milestone floor. Empty,
malformed, invalid, or failed enumeration leaves the legacy single path intact.

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
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.canon_projection import project_canon
from apollo.persistence.learner_model_seed import (
    _entity_key_for_step,
    annotate_reference_solution,
    validate_reference_graph,
)
from apollo.persistence.models import Concept
from apollo.persistence.models import Problem as ProblemRecord
from apollo.provisioning.path_enumeration import multi_path_enabled
from apollo.provisioning.promotion_lint import (
    PromotionUnresolved,
    content_active_gates,
    run_promotion_lint,
)
from apollo.provisioning.tag_mint import MintPlan

__all__ = ["promote", "PromoteHeldForReview", "PromoteResult"]

_LOG = logging.getLogger(__name__)

_SOLUTION_SOURCE_DEFAULT = "generated"


class PromoteResult(BaseModel):
    """The promote outcome (the 3B2g orchestrator handoff). ``failed_gate`` is the
    1..9 gate number on a lint failure, ``None`` on a pass."""

    promoted: bool
    failed_gate: int | None = None
    diagnostic: str = ""


class PromoteHeldForReview(PromoteResult):
    """Distinguished gate-9 unresolved outcome; still a non-pass to old callers."""

    verdict: Literal["unresolved"] = "unresolved"


def _annotate(
    problem: dict,
    mint_plan: MintPlan,
) -> dict:
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


def _with_enumerated_paths(
    annotated: dict,
    path_enumerator: Callable[[dict], list[dict]] | None,
    *,
    concept_problem_id: int,
) -> dict:
    """Replace the legacy path with a valid object-path set, or return it unchanged."""
    if not multi_path_enabled() or path_enumerator is None:
        return annotated
    try:
        enumerated_paths = path_enumerator(annotated)
        if (
            not isinstance(enumerated_paths, list)
            or len(enumerated_paths) < 2
            or not all(isinstance(path, dict) for path in enumerated_paths)
        ):
            raise ValueError("enumeration must return at least two object paths")
        candidate = {**annotated, "declared_paths": enumerated_paths}
        validation = validate_reference_graph(candidate)
        if not validation.ok:
            raise ValueError("; ".join(validation.errors))
        return candidate
    except Exception:  # noqa: BLE001 - enumeration must never block promotion
        _LOG.warning(
            "provisioning_path_enumeration_fallback",
            exc_info=True,
            extra={
                "event": "provisioning_path_enumeration_fallback",
                "concept_problem_id": concept_problem_id,
            },
        )
        return annotated


async def promote(
    db: AsyncSession,
    neo,
    *,
    problem: dict,
    mint_plan: MintPlan,
    search_space_id: int,
    concept_problem_id: int,
    existing_problem_hashes: set[str] | frozenset[str],
    solution_source: str | None = None,
    path_enumerator: Callable[[dict], list[dict]] | None = None,
) -> PromoteResult:
    """Annotate -> lint -> (on PASS) flip tier 1->2 + store payload +
    ``project_canon``. See the module docstring for the full contract.

    ``solution_source`` is the true per-problem provenance
    (``"extracted"``/``"generated"``/``"authored"``) the caller knows; it is
    written ONLY when the row has none yet (default ``"generated"``), so callers
    that don't thread it keep the old behavior and a pre-stamped row is preserved.

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

    annotated = _with_enumerated_paths(
        annotated,
        path_enumerator,
        concept_problem_id=concept_problem_id,
    )

    # Read the concept's AUTHORED symbol array (gate-4 non-vacuity); a vacuous
    # set makes gate 4 reject every foreign symbol.
    concept = await db.get(Concept, mint_plan.concept_id)
    if concept is None:
        raise RuntimeError(f"promote: concept {mint_plan.concept_id} not found")
    canonical_symbols = set(concept.canonical_symbols or [])
    normalization_map = dict(concept.normalization_map or {})

    # Subject-AGNOSTIC Apollo (spec §3): gate applicability is CONTENT-DERIVED, not
    # read from a stored subject profile. The structural core {1,2,3,5,8} always
    # runs; the symbolic rigor gates {4,6,7} self-activate ONLY when the problem
    # carries a parseable equation — so a rigor gate can never block a subject it
    # does not apply to. No DB read, no LLM in this control path; the lint stays
    # pure (the active set is computed here and passed in).
    active_gates = content_active_gates(annotated)

    result = run_promotion_lint(
        annotated,
        canonical_symbols=canonical_symbols,
        normalization_map=normalization_map,
        existing_problem_hashes=set(existing_problem_hashes),
        active_gates=active_gates,
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
        result_type = (
            PromoteHeldForReview if isinstance(result, PromotionUnresolved) else PromoteResult
        )
        return result_type(
            promoted=False,
            failed_gate=result.failed_gate,
            diagnostic=result.diagnostic,
        )

    # --- PASS: flip tier 1->2, RE-HOME to the tagged concept, store solution --- #
    row = await db.get(ProblemRecord, concept_problem_id)
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
    #
    # Provenance stamp (spec §5 honesty handle): "mechanically_verified" iff a
    # mechanical (symbolic) oracle was APPLICABLE and passed — i.e. the symbolic
    # rigor gates {4,6,7} were in the content-derived active set (we only reach this
    # PASS branch when every active gate passed). Else "faithfulness_only": the
    # problem rode the structural core + the LLM faithfulness judge with no
    # mechanical oracle. Distinct from ``solution_source`` (where the solution came
    # from); this is HOW HARD it was checked — a future higher human-review bar for
    # faithfulness-only content (intent decision #4).
    mechanically_verified = bool({4, 6, 7} & set(active_gates))
    annotated_with_stamp = {
        **annotated,
        "verification": "mechanically_verified" if mechanically_verified else "faithfulness_only",
    }
    new_payload = {**dict(row.payload_extra or {}), **annotated_with_stamp}
    row.apply_pydantic_payload(new_payload)
    # RE-HOME the row from the provisional-inventory concept (scrape.py's seam home)
    # onto the REAL tagged concept (``tag_and_mint`` resolved). The student selector
    # ``list_problems_for_concept`` filters ``concept_id == <session concept> AND
    # tier == 2`` (problem_selector.py); without this re-home a promoted Tier-2 row
    # stranded on ``provisional.inventory`` is permanently UNREACHABLE. Idempotent: a
    # re-run re-assigns the SAME tagged concept_id (no-op). (scrape.py:18.)
    row.concept_id = mint_plan.concept_id  # type: ignore[assignment]
    row.course_id = search_space_id  # type: ignore[assignment]
    row.tier = 2  # type: ignore[assignment]
    # Persist the true per-problem provenance the caller threaded (e.g. an authored
    # set's paired-EXTRACTED solution), falling back to the generic default. The
    # ``if not`` guard keeps promote idempotent and preserves a Tier-1 row already
    # stamped by ingest (single-authored -> "authored"): a (re-)promote never
    # downgrades or overwrites an existing source.
    if not row.solution_source:
        row.solution_source = solution_source or _SOLUTION_SOURCE_DEFAULT  # type: ignore[assignment]
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
