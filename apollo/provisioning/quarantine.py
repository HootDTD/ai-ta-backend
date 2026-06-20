"""WU-3B2h — per-problem anomaly quarantine (§8B.3 / §8B.7).

Two layers, one file:

  * ``quarantine_decision`` — the PURE miss-concentration statistic over a
    hand-buildable coverage matrix. No DB, no IO, no LLM. Never mutates the
    input, never divides by zero. This is the §8B.3 backstop: if the class-wide
    coverage concentrates "missing" on ONE reference node (most students miss
    the SAME node), that is the signature of a wrong/mispaired reference
    solution. The conjunction is THREE clauses (``N >= n_min``,
    ``m(top) >= theta_miss``, concentration ``>= margin``); each is independently
    mutation-proven by a discriminating fixture in the test suite.

  * ``sweep_quarantine`` — the async real-PG sweep. It runs the §9 OPS-3
    aggregation (findings -> runs -> attempts, resolving the TEXT
    ``problem_id`` == ``problem_code`` to the BIGINT ``apollo_concept_problems``
    row, course-scoped by ``runs.search_space_id``), calls the pure decision per
    ``(problem_code, search_space_id)`` group, and sets/clears
    ``quarantined_at`` REVERSIBLY (re-cleared as N grows and the concentration no
    longer fires). It emits one structured audit log row per fire/clear (the
    §9 OPS-4 mitigation b: a human can audit false positives), commits once, and
    returns a frozen ``SweepReport``. It is best-effort observability infra: a
    per-problem resolution miss is logged and skipped, never raised.

stdlib ``statistics`` ONLY — NO numpy import, NO scipy. The point-biserial
discrimination refinement is v1.1 (ADJ #1 / ADJ #8). The quarantine is ADVISORY
until the ADJ #12 calibration step passes (see ``docs/architecture/apollo.md``).
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import (
    ConceptProblem,
    GraphComparisonFinding,
    GraphComparisonRun,
    ProblemAttempt,
)
from apollo.provisioning.quarantine_constants import (
    CONCENTRATION_MARGIN,
    N_MIN,
    THETA_MISS,
)

_LOG = logging.getLogger(__name__)

# A per-problem coverage matrix: reference-node key -> per-attempt "missing?"
# flags. All sequences share length N (the graded-attempt count). Keyed by the
# node's stable id (``str(entity_id)`` at the sweep boundary; arbitrary hashable
# in fixtures).
CoverageMatrix = Mapping[str, Sequence[bool]]

# Only an explicit missing-node finding counts as a miss. A node absent from an
# attempt's findings is NOT a miss for that attempt.
_MISSING_NODE = "missing_node"


def _now() -> datetime:
    """Python-side wall clock (test-controllable; NOT SQL ``now()``)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class QuarantineVerdict:
    """The frozen decision DTO. The audit fields are exactly what the sweep's
    per-fire log emits so a human can reconstruct WHY a problem quarantined."""

    quarantine: bool
    n_attempts: int
    top_node_key: str | None
    top_miss_rate: float
    mean_miss_rate: float
    concentration: float  # top_miss_rate - mean_miss_rate
    reason: str  # "fired"|"below_n_min"|"below_theta"|"below_margin"|"empty"


@dataclass(frozen=True)
class SweepReport:
    """The frozen sweep summary returned to an ops caller (janitor/cron)."""

    n_evaluated: int
    n_quarantined: int
    n_cleared: int
    fired_problem_codes: tuple[str, ...]
    cleared_problem_codes: tuple[str, ...]


def _empty_verdict() -> QuarantineVerdict:
    return QuarantineVerdict(
        quarantine=False,
        n_attempts=0,
        top_node_key=None,
        top_miss_rate=0.0,
        mean_miss_rate=0.0,
        concentration=0.0,
        reason="empty",
    )


def quarantine_decision(
    coverage_matrix: CoverageMatrix,
    *,
    n_min: int,
    theta_miss: float,
    margin: float,
) -> QuarantineVerdict:
    """PURE miss-concentration decision over one problem's coverage matrix.

    ``coverage_matrix`` maps a reference-node key to a per-attempt "missing?"
    flag sequence; all sequences share length N (the graded-attempt count). The
    decision fires iff ALL THREE clauses hold:

      1. ``N >= n_min``                          (enough attempts to trust)
      2. ``m(top) >= theta_miss``                (one node missed by ~everyone)
      3. ``m(top) - mean_m >= margin``           (concentrated, not uniform-hard)

    where ``m(node) = misses(node) / N`` and ``mean_m`` is the mean per-node
    miss rate. Never mutates the input. Never divides by zero (an empty matrix
    or zero-length sequences -> a non-firing ``"empty"`` verdict).
    """
    if not coverage_matrix:
        return _empty_verdict()

    # N = number of graded attempts == the (shared) sequence length. Sequences
    # in one matrix share length N by the sweep's construction; read it from any.
    n_attempts = len(next(iter(coverage_matrix.values())))
    if n_attempts <= 0:
        return _empty_verdict()

    # Per-node miss rate (a NEW dict — the input is never mutated).
    miss_rates = {
        key: sum(1 for missing in flags if missing) / n_attempts
        for key, flags in coverage_matrix.items()
    }
    mean_m = statistics.fmean(miss_rates.values())
    top_node_key = max(miss_rates, key=lambda k: miss_rates[k])
    top_m = miss_rates[top_node_key]
    concentration = top_m - mean_m

    def _verdict(*, quarantine: bool, reason: str) -> QuarantineVerdict:
        return QuarantineVerdict(
            quarantine=quarantine,
            n_attempts=n_attempts,
            top_node_key=top_node_key,
            top_miss_rate=top_m,
            mean_miss_rate=mean_m,
            concentration=concentration,
            reason=reason,
        )

    if n_attempts < n_min:
        return _verdict(quarantine=False, reason="below_n_min")
    if top_m < theta_miss:
        return _verdict(quarantine=False, reason="below_theta")
    if concentration < margin:
        return _verdict(quarantine=False, reason="below_margin")
    return _verdict(quarantine=True, reason="fired")


async def _aggregate_misses(
    db: AsyncSession,
    *,
    search_space_id: int | None,
) -> dict[tuple[str, int], dict[str, int]]:
    """Per-(problem_code, search_space_id) -> {node_key -> miss_count}.

    The §9 OPS-3 join: findings -> runs -> attempts, filtered to
    ``missing_node`` findings with a NON-NULL ``entity_id`` (a pruned-entity
    finding carries no resolvable node identity — it must never become a phantom
    node). Optionally course-scoped by ``runs.search_space_id``.
    """
    stmt = (
        select(
            ProblemAttempt.problem_id,
            GraphComparisonRun.search_space_id,
            GraphComparisonFinding.entity_id,
            func.count().label("miss_count"),
        )
        .join(GraphComparisonRun, GraphComparisonFinding.run_id == GraphComparisonRun.id)
        .join(ProblemAttempt, GraphComparisonRun.attempt_id == ProblemAttempt.id)
        .where(
            GraphComparisonFinding.finding_kind == _MISSING_NODE,
            GraphComparisonFinding.entity_id.is_not(None),
        )
        .group_by(
            ProblemAttempt.problem_id,
            GraphComparisonRun.search_space_id,
            GraphComparisonFinding.entity_id,
        )
    )
    if search_space_id is not None:
        stmt = stmt.where(GraphComparisonRun.search_space_id == search_space_id)

    misses: dict[tuple[str, int], dict[str, int]] = {}
    for problem_code, sid, entity_id, miss_count in (await db.execute(stmt)).all():
        key = (problem_code, sid)
        misses.setdefault(key, {})[str(entity_id)] = int(miss_count)
    return misses


async def _attempt_totals(
    db: AsyncSession,
    *,
    search_space_id: int | None,
) -> dict[tuple[str, int], int]:
    """Per-(problem_code, search_space_id) -> N (COUNT DISTINCT graded attempts).

    N is the denominator — every graded attempt yields one comparison run, so
    ``COUNT(DISTINCT runs.attempt_id)`` per problem in the course is the count of
    graded attempts (NOT the count of findings)."""
    stmt = (
        select(
            ProblemAttempt.problem_id,
            GraphComparisonRun.search_space_id,
            func.count(func.distinct(GraphComparisonRun.attempt_id)).label("n"),
        )
        .join(ProblemAttempt, GraphComparisonRun.attempt_id == ProblemAttempt.id)
        .group_by(ProblemAttempt.problem_id, GraphComparisonRun.search_space_id)
    )
    if search_space_id is not None:
        stmt = stmt.where(GraphComparisonRun.search_space_id == search_space_id)

    return {
        (problem_code, sid): int(n)
        for problem_code, sid, n in (await db.execute(stmt)).all()
    }


def _build_coverage_matrix(
    node_misses: Mapping[str, int], n_attempts: int
) -> dict[str, list[bool]]:
    """Reconstruct a per-node missing-flag matrix from miss COUNTS + N.

    A node missed ``c`` of N attempts becomes ``c`` True flags then ``N-c``
    False — order is irrelevant to ``quarantine_decision`` (it only counts), so
    a count-faithful reconstruction is exact for the statistic."""
    return {
        node_key: [True] * miss_count + [False] * (n_attempts - miss_count)
        for node_key, miss_count in node_misses.items()
    }


async def sweep_quarantine(
    db: AsyncSession,
    *,
    search_space_id: int | None = None,
) -> SweepReport:
    """Reconcile ``apollo_concept_problems.quarantined_at`` from the findings.

    Reads the §9 OPS-3 aggregation, calls ``quarantine_decision`` per
    ``(problem_code, course)`` with the committed constants, and REVERSIBLY
    sets/clears ``quarantined_at`` on the resolved BIGINT row. Logs one audit row
    per fire/clear, commits once, returns a frozen ``SweepReport``. Best-effort:
    a per-problem resolution miss is logged at WARNING and skipped — the sweep
    continues. Only a genuine DB failure on the final commit propagates.
    """
    node_misses_by_group = await _aggregate_misses(db, search_space_id=search_space_id)
    n_by_group = await _attempt_totals(db, search_space_id=search_space_id)

    n_evaluated = 0
    fired: list[str] = []
    cleared: list[str] = []

    for (problem_code, sid), n_attempts in n_by_group.items():
        n_evaluated += 1
        node_misses = node_misses_by_group.get((problem_code, sid), {})
        matrix = _build_coverage_matrix(node_misses, n_attempts)
        verdict = quarantine_decision(
            matrix,
            n_min=N_MIN,
            theta_miss=THETA_MISS,
            margin=CONCENTRATION_MARGIN,
        )

        row = (
            await db.execute(
                select(ConceptProblem).where(
                    ConceptProblem.search_space_id == sid,
                    ConceptProblem.problem_code == problem_code,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # Orphan finding: a problem_code with no matching concept_problems
            # row (course-scoped). Log and skip — never raise to the caller.
            _LOG.warning(
                "quarantine_resolution_miss",
                extra={
                    "event": "quarantine_resolution_miss",
                    "problem_code": problem_code,
                    "search_space_id": sid,
                },
            )
            continue

        currently_quarantined = row.quarantined_at is not None

        if verdict.quarantine and not currently_quarantined:
            row.quarantined_at = _now()
            fired.append(problem_code)
            _LOG.info(
                "quarantine_fire",
                extra={
                    "event": "quarantine_fire",
                    "problem_code": problem_code,
                    "search_space_id": sid,
                    "concept_id": int(row.concept_id),
                    "node_key": verdict.top_node_key,
                    "n_attempts": verdict.n_attempts,
                    "top_miss_rate": verdict.top_miss_rate,
                    "concentration": verdict.concentration,
                },
            )
        elif not verdict.quarantine and currently_quarantined:
            row.quarantined_at = None
            cleared.append(problem_code)
            _LOG.info(
                "quarantine_clear",
                extra={
                    "event": "quarantine_clear",
                    "problem_code": problem_code,
                    "search_space_id": sid,
                    "reason": verdict.reason,
                },
            )

    await db.commit()

    return SweepReport(
        n_evaluated=n_evaluated,
        n_quarantined=len(fired),
        n_cleared=len(cleared),
        fired_problem_codes=tuple(fired),
        cleared_problem_codes=tuple(cleared),
    )


__all__ = [
    "CoverageMatrix",
    "QuarantineVerdict",
    "quarantine_decision",
    "SweepReport",
    "sweep_quarantine",
]
