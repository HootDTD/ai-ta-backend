"""WU-6A2 — the PURE selection + coverage + difficulty algorithm.

The pure scoring brain of the v1 Apollo session-personalization wedge (the second
of three sub-units: 6A1 read / **6A2 pure-selection** / 6A3 wiring). It owns EVERY
number and the cold-start branch; it touches NO IO. Mirrors the sibling pure module
``belief.py``: stdlib ``set``/``dict``/``sorted``/``max`` only — NO DB, NO LLM, NO
Neo4j, NO containers, NO migration.

The dependency arrow is strictly one-way: **6A2 imports the frozen 6A1 dataclasses;
6A1 imports nothing from 6A2** (no import cycle). The inputs are an in-memory
``LearnerProfile`` (WU-6A1) + a candidate ``Problem`` pool; the output is the
``Problem`` whose reference graph best covers the student's weak-and-teachable
entities. On a cold-start / empty-weak profile the choice is ``candidates[0]`` —
byte-identical to today's ``apollo/overseer/problem_selector.py:select_problem``.

The single non-obvious design fact, EMPIRICALLY PROVEN against the interpreter:
``Problem.model_validate`` DROPS the seeded per-step ``entity_key`` (pydantic v2
``extra`` ignores it), BUT each problem's canonical-key set is a PURE deterministic
function of ``(entry_type, id)`` via the FROZEN ``_ENTRY_TYPE_TO_KIND_PREFIX`` map
and the same ``f"{prefix}.{id}"`` rule the WU-3B seeder used. So
``reference_entity_keys`` reconstructs each candidate's entity-key set IN-MEMORY
from the already-loaded pool — ZERO per-problem DB/Neo4j round-trip; the N+1 hazard
does not exist (proven 3-way in the test suite across all 5 seed problems).

All constants here are orchestrator-ADJUDICATED (split proposal §4); this module
does not re-open them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apollo.errors import PoolExhaustedError
from apollo.learner_model.personalization_read import EntityProfile, LearnerProfile
from apollo.persistence.learner_model_seed import _ENTRY_TYPE_TO_KIND_PREFIX
from apollo.schemas.problem import Problem

__all__ = [
    # LOCKED constants (6A2 owns every number)
    "TEACHABLE_BAND_LO",
    "TEACHABLE_BAND_HI",
    "MASTERED_THRESHOLD",
    "UNSEEN_MASTERY",
    "REPROBE_CONFIDENCE",
    # pure functions
    "reference_entity_keys",
    "prereqs_mastered",
    "weak_teachable",
    "coverage_score",
    "personalize_selection",
]

# --- LOCKED constants (orchestrator-adjudicated, split proposal §4) ------------

TEACHABLE_BAND_LO = 0.3  # spec §6 L470 "mastery 0.3-0.7" teachable band (inclusive)
TEACHABLE_BAND_HI = 0.7
MASTERED_THRESHOLD = 0.7  # prereq "mastered" cut (reuses the band top)
UNSEEN_MASTERY = 0.50  # readout for an entity with no learner_state row
REPROBE_CONFIDENCE = 0.4  # low-confidence re-probe threshold (soft-hold, never a hard negative)


def reference_entity_keys(problem: Problem) -> frozenset[str]:
    """Reconstruct a problem's canonical-key set IN-MEMORY from its reference steps.

    The PROVEN round-trip: element ``[1]`` of the frozen prefix tuple is the prefix
    string; ``f"{prefix}.{step.id}"`` is the same rule the WU-3B seeder used, so the
    result equals the seeded ``canonical_key``s with NO per-problem IO. The frozen
    map is imported and never mutated.
    """
    return frozenset(
        f"{_ENTRY_TYPE_TO_KIND_PREFIX[s.entry_type][1]}.{s.id}" for s in problem.reference_solution
    )


def _mastery_of_key(profile: LearnerProfile, key: str) -> float:
    """The SINGLE place the 'unseen = 0.50' rule lives.

    Returns the stored mastery for an entity with a learner_state row, else
    ``UNSEEN_MASTERY`` (matches the belief cold-start readout). Uses ``.get`` for
    immutability/clarity (no in-place lookup-or-raise).
    """
    ep: EntityProfile | None = profile.by_canonical_key.get(key)
    return ep.mastery if ep is not None else UNSEEN_MASTERY


def prereqs_mastered(profile: LearnerProfile, key: str) -> bool:
    """True iff every within-concept prerequisite of ``key`` is mastered.

    ``apollo_entity_prereqs`` is "FROM depends on TO", so ``key``'s prerequisites are
    the TO-endpoints of edges whose FROM is ``key``. An entity with NO prereq edges
    is trivially mastered (``all([]) == True``). An UNSEEN prereq reads 0.50 < 0.70
    and BLOCKS (conservative, adjudication #3). A ``key`` absent from the id map has
    no resolvable prereqs and is treated as no-prereqs -> True (defensive; should not
    happen for a present key).
    """
    entity_id = profile.entity_id_by_key.get(key)
    if entity_id is None:
        return True
    prereq_ids = [to for (frm, to) in profile.prereq_edges if frm == entity_id]
    # Every prereq endpoint resolves: WU-6A1 emits only WITHIN-CONCEPT edges (both
    # endpoints among this concept's entities), so a missing pid would mean a broken
    # 6A1 contract — let the subscript raise loudly rather than silently mask it.
    prereq_keys = [profile.key_by_entity_id[pid] for pid in prereq_ids]
    return all(_mastery_of_key(profile, pk) >= MASTERED_THRESHOLD for pk in prereq_keys)


def weak_teachable(profile: LearnerProfile) -> dict[str, float]:
    """The student's weak-and-teachable entities -> deficit (``1 - mastery``).

    Only PRESENT entities can be weak (absent entities carry no signal). The band is
    INCLUSIVE at both ends. Prereq-blocked entities are excluded. SOFT-HOLD re-probe:
    low ``confidence`` does NOT exclude (adjudication #5a — never a hard negative).
    On a cold-start profile (``by_canonical_key == {}``) this returns ``{}``.
    """
    return {
        ep.canonical_key: 1.0 - ep.mastery
        for ep in profile.by_canonical_key.values()
        if TEACHABLE_BAND_LO <= ep.mastery <= TEACHABLE_BAND_HI
        and prereqs_mastered(profile, ep.canonical_key)
    }


def coverage_score(problem: Problem, weak: Mapping[str, float]) -> float:
    """Deficit-weighted coverage of ``weak`` by a problem's reference graph.

    The sum of deficits over the weak entities the problem covers (adjudication #4).
    An empty intersection scores ``0.0``.
    """
    return sum(weak[k] for k in reference_entity_keys(problem) if k in weak)


def personalize_selection(
    profile: LearnerProfile,
    pool: Sequence[Problem],
    *,
    concept_id: int,
    difficulty: str,
    attempted_ids: Sequence[str | int],
) -> Problem:
    """Pick the in-difficulty, unattempted ``Problem`` that best covers the student's
    weak-and-teachable entities; cold-start / empty-weak -> ``candidates[0]``.

    STEP 1 (byte-identical filter): clamp-within-choice — re-rank only inside the
    chosen difficulty; the anti-starvation not-attempted filter is PRESERVED.
    STEP 2 (byte-identical raise): no candidate -> ``PoolExhaustedError`` with the
    same ``concept_cluster_id=str(concept_id)`` argument ``select_problem`` uses
    (``concept_id`` is in the signature SOLELY for this reconstruction; it never
    filters or scores).
    STEP 3 (non-regression anchor): on a cold-start / empty-weak profile return
    ``candidates[0]`` — exactly today's ``select_problem`` behaviour.
    STEP 4 (deficit-weighted pick): the max-coverage candidate, ties broken to the
    LOWEST ``Problem.id`` EXPLICITLY (independent of pool order).
    """
    attempted = set(attempted_ids)
    candidates = [
        p
        for p in pool
        if p.difficulty == difficulty and p.id not in attempted and p.database_id not in attempted
    ]
    if not candidates:
        raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)

    weak = weak_teachable(profile)
    if profile.is_empty or not weak:
        return candidates[0]

    scored = {p.id: coverage_score(p, weak) for p in candidates}
    best = max(scored.values())
    return min((p for p in candidates if scored[p.id] == best), key=lambda p: p.id)
