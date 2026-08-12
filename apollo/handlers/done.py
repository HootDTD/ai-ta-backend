"""POST /apollo/sessions/{id}/done — freeze, solve, grade, narrate, award XP.

V3: KGStore.read_graph returns a typed KGGraph; reference graph is derived
from the problem via Problem.to_kg_graph(); coverage walks both graphs;
rubric consumes Node objects directly. Hardcoded `g=9.81` and per-problem
augmentations come from the concept registry, not from this file.

"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from apollo.errors import (
    EmptyAttemptError,
    GradingInProgressError,
    KGUnavailableError,
    RetentionError,
)
from apollo.handlers.artifact_writer import write_artifacts
from apollo.handlers.browse import feedback_from_report, served_overall_from_report
from apollo.hoot_bridge.reference_answer import (
    ASIDE_COUNT_SESSION_METADATA_KEY,
    ASIDE_MESSAGE_INTENT_TAG,
)
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.overseer import wrongness
from apollo.overseer.aside_penalty import apply_aside_caps
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.grounding import (
    CourseEvidence,
    build_course_evidence,
    evidence_block,
    grounding_provenance,
)
from apollo.overseer.misconception import (
    MisconceptionSignal,
    summarize_for_rubric,
)
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.overseer.remediation import add_remediation_reviews
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.topic_score import (
    _GRADED_NODE_TYPES,
    TopicScoreResult,
    compute_centrality,
    compute_topic_score,
    graded_topics_only,
)
from apollo.overseer.topic_score_serialize import serialize_topics
from apollo.overseer.transcript_coverage import compute_transcript_coverage_with_spans
from apollo.overseer.xp import (
    compute_misconception_bonus,
    compute_progress_envelope,
    compute_xp_earned,
)
from apollo.persistence.attempt_history import (
    has_prior_graded_attempt,
    prior_wrongness_findings,
)
from apollo.persistence.models import (
    GradingRun,
    ProblemAttempt,
    QuestionOpportunity,
    SessionPhase,
    StudentProgress,
    TutoringMessage,
    TutoringSession,
)
from apollo.persistence.neo4j_client import KG_DEGRADED_ERRORS, Neo4jClient
from apollo.persistence.progress_repo import apply_xp
from apollo.projections.mastery import update_mastery_from_artifact
from apollo.projections.scorecard import render_scorecard
from apollo.schemas.problem import Problem
from config.settings import (
    interaction2_enabled,
    interaction3_enabled,
    interaction5_enabled,
    interaction_allowed_for_concept,
)

_LOG = logging.getLogger(__name__)

# WU-5A2 — the Layer-3 belief-PERSIST flag (default OFF EVERYWHERE incl. prod +
# staging). A7 removed its former Done-time producer; the helper remains only
# for compatibility with the artifact-derived mastery interlock.
_GRAPH_SIM_LAYER3_FLAG: str = "APOLLO_GRAPH_SIM_LAYER3_ENABLED"

# T13 — the raw student-turn role for `_student_utterances` (R6, RESOLVED): live
# transcript roles are exactly {"apollo", "student"}; the Apollo learner turns
# (read by `_attempt_misconception_scores`) are "apollo", the student's raw
# teaching utterances (which feed the bank_pattern tier) are "student".
_STUDENT_ROLE: str = "student"

# INTERACTION5 — the Apollo learner role that authors a Hoot lookup-aside message
# (the same "apollo" role `_attempt_misconception_scores` reads); paired with the
# `ASIDE_MESSAGE_INTENT_TAG` intent it isolates the aside rows `_full_transcript`
# EXCLUDES from grading.
_APOLLO_ROLE: str = "apollo"

# INTERACTION5 — the flat Hoot-assist credit cap. Single source of truth: passed
# to `apply_aside_caps` AND reported in `grading_provenance["aside_penalty"]` so
# the two can never drift.
_ASIDE_CREDIT_CAP: float = 0.5

# M1 (P3.4) — the durable grading claim. `handle_done` spans 6-7 independent
# Postgres commits, so NO transaction-scoped primitive can serialize it: a
# `FOR UPDATE` or `pg_advisory_xact_lock` taken at the first commit is released
# ~4 minutes before the last write. The claim is a compare-and-swap on the
# session phase instead — durable, pool-mode agnostic, migration-free (`phase`
# is Text and `SOLVING` is already the marker `KGStore._ensure_unfrozen` and
# `restart_problem._FROZEN_PHASES` respect).
_CLAIM_PHASE: str = SessionPhase.SOLVING.value

# Stale-claim reclaim window (spec OQ-b). A Done that crashes between the claim
# and the grade commit would otherwise leave `phase='SOLVING'` forever and the
# attempt could never be re-Done. TUNABLE: it must exceed the worst observed
# Done wall time (grading spans ~4 minutes today) with headroom; 15 minutes is
# the approved default. `handle_retry` resetting the phase to TEACHING remains
# the student-visible escape, and the compensating release below makes the
# reclaim the rare path rather than the normal one.
_STALE_CLAIM_AFTER: timedelta = timedelta(minutes=15)


def _graph_sim_layer3_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_LAYER3_FLAG, "").lower() in ("1", "true", "yes")


async def _project_mastery(db: AsyncSession, *, attempt_id: int) -> None:
    """Campaign-plan Task B2 — the composite-EWMA mastery projection, run
    AFTER `write_artifacts` has durably committed the canonical row. Reads
    that row back (its id/created_at only exist post-commit) and hands it to
    `update_mastery_from_artifact`, then owns its OWN commit — mirroring
    `write_artifacts`' own-failure-domain posture: this is telemetry-derived
    bookkeeping, not the grade itself, so ANY exception here is logged and
    swallowed rather than raised into the Done response. Guarded at the call
    site so this NEVER runs alongside the dormant WU-5A2
    `run_learner_update` (both write `apollo_mastery_events` /
    `apollo_learner_state`; running both would double-apply evidence)."""
    try:
        row = (
            await db.execute(
                select(GradingRun).where(
                    GradingRun.attempt_id == attempt_id,
                    GradingRun.role == "canonical",
                )
            )
        ).scalar_one_or_none()
        if row is None:  # defensive — write_artifacts already returned non-None
            return
        await update_mastery_from_artifact(db, artifact_row=row)
        await db.commit()
    except Exception:
        _LOG.exception("mastery_projection_failed attempt_id=%s", attempt_id)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive, rollback itself failing
            _LOG.exception("mastery_projection_rollback_failed attempt_id=%s", attempt_id)


async def _attempt_misconception_scores(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> dict[str, float]:
    """Read every Apollo turn for this attempt and reduce misconception
    signals to a per-bank-code score map for the rubric's axis.

    Reads from tutoring-message metadata. Skips messages
    whose metadata is null or has no misconception payload. Returns an
    empty dict when nothing fired — the rubric treats that as
    axis-absent and falls back to the pre-P2.8 60/25/15 weights.
    """
    rows = (
        (
            await db.execute(
                select(TutoringMessage.message_metadata)
                .where(TutoringMessage.attempt_id == attempt_id)
                .where(TutoringMessage.role == "apollo")
                .order_by(TutoringMessage.turn_index)
            )
        )
        .scalars()
        .all()
    )

    signals: list[MisconceptionSignal] = []
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        raw = payload.get("misconception")
        if not isinstance(raw, dict):
            continue
        state = raw.get("state", "default")
        if state not in {"default", "probe", "socratic"}:
            continue
        signals.append(
            MisconceptionSignal(
                fired=bool(raw.get("fired", False)),
                state=state,  # type: ignore[arg-type]
                bank_code=raw.get("bank_code"),
                confidence=float(raw.get("confidence", 0.0) or 0.0),
            )
        )

    return summarize_for_rubric(signals)


async def _student_utterances(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> tuple[str, ...]:
    """T13 — the raw student teaching utterances for this attempt, in turn
    order, that feed the misconception detector's ``bank_pattern`` tier.

    Reads ``TutoringMessage.content`` where ``TutoringMessage.role == "student"`` (R6 —
    the CONFIRMED student-turn role, distinct from the "apollo" learner
    turns ``_attempt_misconception_scores`` reads) ordered by
    ``turn_index``. Returns a tuple (immutable) so the detector's frozen
    value objects stay list-free end to end. Empty tuple when the student
    never spoke (a valid, common case — the detector's tiers all abstain on
    empty utterances)."""
    rows = (
        (
            await db.execute(
                select(TutoringMessage.content)
                .where(TutoringMessage.attempt_id == attempt_id)
                .where(TutoringMessage.role == _STUDENT_ROLE)
                .order_by(TutoringMessage.turn_index)
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


async def _student_message_count(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> int:
    """How many student messages this attempt has persisted.

    Feeds the empty-attempt guard in ``handle_done`` (2026-08-07 bimodal-fix
    defect I1). Counts ``role == "student"`` rows only — student rows are never
    aside-tagged, so no intent filter is needed."""
    return (
        await db.execute(
            select(func.count())
            .select_from(TutoringMessage)
            .where(TutoringMessage.attempt_id == attempt_id)
            .where(TutoringMessage.role == _STUDENT_ROLE)
        )
    ).scalar_one()


async def _full_transcript(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> tuple[tuple[str, str], ...]:
    """Return both-role attempt messages in canonical turn order.

    Excludes INTERACTION4 hint-lane aside text (`intent ==
    ASIDE_MESSAGE_INTENT_TAG`) from grading — the adjudicator must not credit
    the student with Hoot's explanation (brief: "Transcript & grading
    interaction"). The student's triggering question is untagged and stays
    in — asking a question is real signal about gaps. `or_(... is_(None),
    ...)` because SQL `!=` never matches NULL, and most rows have no intent.
    """
    rows = (
        await db.execute(
            select(TutoringMessage.role, TutoringMessage.content)
            .where(TutoringMessage.attempt_id == attempt_id)
            .where(
                or_(
                    TutoringMessage.intent.is_(None),
                    TutoringMessage.intent != ASIDE_MESSAGE_INTENT_TAG,
                )
            )
            .order_by(TutoringMessage.turn_index)
        )
    ).all()
    return tuple((role, content) for role, content in rows)


async def _question_ledger(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> tuple[Any, ...] | None:
    """This attempt's ``QuestionOpportunity`` rows in insertion order, or
    ``None`` when the read failed.

    ONE read serving BOTH 2026-08-07 bimodal-fix consumers: the adjudicator's
    ``tally_context`` (P1.3) and the scorer's ``asked_node_ids`` (P1.2b). Its own
    failure domain — it runs AHEAD of the sole grading lane and must never touch
    the ``CoverageGradingError -> 503`` contract, so ANY exception logs and
    yields ``None``, which makes both consumers reproduce the pre-fix grade.
    Ordered by ``id`` (never node id) so the tally block handed to the LLM is
    reproducible across runs."""
    try:
        rows = (
            (
                await db.execute(
                    select(QuestionOpportunity)
                    .where(QuestionOpportunity.attempt_id == attempt_id)
                    .order_by(QuestionOpportunity.id)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        _LOG.exception("apollo_question_ledger_fetch_failed attempt_id=%s", attempt_id)
        return None
    return tuple(rows)


def _latest_student_quote(evidence: Any) -> str | None:
    """The most recent verbatim student quote in a ledger row's evidence list.

    Evidence entries are ``{"turn_id": int, "quote": str}`` appended in turn
    order by the questioning controller, so the LAST usable one is the student's
    most recent demonstration of that node. Anything malformed (the column is
    free-form JSON) yields ``None`` rather than raising — this feeds a prompt,
    not the grade arithmetic."""
    if not isinstance(evidence, list):
        return None
    for item in reversed(evidence):
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        if isinstance(quote, str) and quote.strip():
            return quote
    return None


def _probed_node_ids(rows: Any) -> frozenset[str]:
    """The graded-node ids P1.2b treats as engaged this attempt.

    A ``QuestionOpportunity`` row is NOT by itself proof that the questioning
    loop engaged with a node. Two paths mint a row without any engagement: a
    degenerate ``fallback_served`` turn (a verbatim public clause standing in
    for a question, which deliberately spends no probe — see
    ``smart_questions/controller``), and a tally update that merely restates
    ``missing`` with no quote. Counting those as "probed" put the node straight
    back in the denominator at credit 0 — the exact false F that P1.2b exists to
    remove. A row therefore counts only when it records real engagement:

    * ``times_asked > 0`` — Apollo actually put a question about it to the
      student; or
    * a tally state other than ``missing`` — the engine concluded something
      about the student's teaching of it; or
    * a verbatim evidence quote — the student demonstrably taught it.

    Pure and total: unusable/NULL columns coerce rather than raise, because this
    feeds the grade denominator and must never break a Done.
    """
    return frozenset(
        str(row.reference_node_id)
        for row in rows
        if int(row.times_asked or 0) > 0
        or str(row.state) != "missing"
        or _latest_student_quote(row.evidence) is not None
    )


def _tally_context(rows: Any) -> list[dict[str, Any]]:
    """The adjudicator's per-node prior context (P1.3), shape pinned across
    slices: ``[{node_id, state, times_asked, student_quote|null}, ...]``.

    Defect U1: the live tally (questioning engine) and the grader are two
    decoupled LLM systems, so identical tallies produced F(0) and A+(100) and a
    node Apollo marked ``understood`` — celebrated in the UI — was routinely
    zeroed by the grader. Handing the tally to the adjudicator as PRIOR context
    (not as a verdict) is the cheap half of that fix; the prompt rule that a
    quoted ``understood`` node needs a cited reason to score low lives in
    ``overseer/transcript_coverage``."""
    return [
        {
            "node_id": str(row.reference_node_id),
            "state": str(row.state),
            "times_asked": int(row.times_asked or 0),
            "student_quote": _latest_student_quote(row.evidence),
        }
        for row in rows
    ]


async def _aside_texts(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> tuple[str, ...]:
    """INTERACTION5 — the Hoot lookup-aside answers shown to the student this
    attempt, in turn order, that feed the Hoot-assist grading cap.

    Reads ``TutoringMessage.content`` where ``role == "apollo"`` and
    ``intent == ASIDE_MESSAGE_INTENT_TAG`` — the exact complement of
    ``_full_transcript``'s exclusion filter: that DROPS these rows from the graded
    dialogue (so the student is never credited with Hoot's explanation), and this
    collects the same rows so the adjudicator can flag which rubric nodes Hoot
    pre-explained. Ordered by ``turn_index``; returns an immutable tuple. Empty
    when the student never used a lookup aside (the common case — the cap pass
    then no-ops)."""
    rows = (
        (
            await db.execute(
                select(TutoringMessage.content)
                .where(TutoringMessage.attempt_id == attempt_id)
                .where(TutoringMessage.role == _APOLLO_ROLE)
                .where(TutoringMessage.intent == ASIDE_MESSAGE_INTENT_TAG)
                .order_by(TutoringMessage.turn_index)
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def _compute_topic_score_safe(
    *,
    coverage: Mapping[str, Any],
    reference_graph: KGGraph,
    attempt_id: int,
    evidence_spans: dict[str, str] | None = None,
    asked_node_ids: frozenset[str] | None = None,
    misconceptions: Mapping[str, Any] | None = None,
    ceiling_active: bool = False,
) -> TopicScoreResult | None:
    """Soft-failing wrapper around ``compute_topic_score`` (2026-07-10 spec
    §3): computed ALWAYS (flag-independent — the artifact gets telemetry
    before any serving flip), but any exception here must never break a Done.
    Centrality is computed from the reference graph. Any exception here
    is logged and swallowed — the caller receives ``None`` and proceeds with
    ``topic_score`` absent from both the artifact and the served payload.

    ``misconceptions``/``ceiling_active`` (P3.2 seam S7) are the wrongness
    containers and the DARK level-4 ceiling. The defaults are what levels 0-2
    pass, and they take ``compute_topic_score``'s early return, so the payload
    is byte-identical to pre-P3.2 by construction."""
    try:
        return compute_topic_score(
            # The adjudicator returns the `CoverageVerdict` TypedDict, which
            # mypy treats as unrelated to the scorer's plain `dict` parameter;
            # spell the hop once here instead of at every call site.
            coverage=cast("dict[Any, Any]", coverage),
            reference_nodes=reference_graph.nodes,
            centrality=compute_centrality(reference_graph),
            evidence_spans=evidence_spans,
            asked_node_ids=asked_node_ids,
            misconceptions=misconceptions,
            ceiling_active=ceiling_active,
        )
    except Exception:
        _LOG.exception("topic_score_computation_failed attempt_id=%s", attempt_id)
        return None


def _evaluate_wrongness(
    tally_findings: Sequence[wrongness.LedgerFinding],
    *,
    coverage: Mapping[str, Any],
    topic_score: TopicScoreResult | None,
    graded_node_ids: frozenset[str],
    attempt_id: int,
    level: int,
) -> tuple[wrongness.WrongnessFinding, ...]:
    """Evaluate S2′ at Done and emit the level-≥1 shadow corpus.

    AT DONE, not per turn: pre-P3.1 the ledger carries no per-turn credit and
    S2′ needs one. The second reader is the adjudicator's corroboration map
    (``coverage["wrongness"]``, present only when candidates were supplied);
    **fail-safe = miss** — an absent row never corroborates, so the
    corroborator's silence can only ever REMOVE a consequence. Logging is this
    function's only effect: ``findings=`` counts evidence ENTRIES and
    ``nodes=`` distinct nodes, because ``select_findings`` returns one rung per
    entry and a node probed twice yields two.
    """
    if not tally_findings or topic_score is None:
        return ()
    second_reader: Mapping[str, Mapping[str, bool]] = coverage.get("wrongness") or {}
    findings = wrongness.select_findings(
        findings=tally_findings,
        credits={topic.canonical_key: topic.credit for topic in topic_score.topics},
        second_reader=second_reader,
        graded_node_ids=graded_node_ids,
        raw_score=topic_score.score,
    )
    for finding in findings:
        reader = second_reader.get(finding.node_id)
        _LOG.info(
            "apollo_wrongness_observed attempt_id=%s node_id=%s rung=%s span_verified=%s "
            "second_reader=%s would_ceiling=%s kind=%s",
            attempt_id,
            finding.node_id,
            # corroborated = both readers agree it stands uncorrected (the only
            # score-relevant rung, inert below level 4); resolved = the student
            # fixed it (what the XP bonus rewards); reported = tally-only.
            "corroborated"
            if finding.corroborated
            else ("resolved" if finding.resolved else "reported"),
            bool(finding.quote),
            "absent" if reader is None else dict(reader),
            finding.would_ceiling,
            finding.kind,
        )
    _LOG.info(
        "apollo_wrongness_summary attempt_id=%s findings=%d nodes=%d corroborated=%d "
        "would_ceiling=%d level=%d",
        attempt_id,
        len(findings),
        len({f.node_id for f in findings}),
        len({f.node_id for f in findings if f.corroborated}),
        len({f.node_id for f in findings if f.would_ceiling}),
        level,
    )
    return findings


async def _wrongness_bonus_xp(
    db: AsyncSession,
    *,
    findings: Sequence[wrongness.WrongnessFinding],
    attempt: ProblemAttempt,
    course_id: int,
) -> int:
    """Decision-7 bonus XP: the student FIXED a contradiction Apollo elicited.

    The population is ``resolved AND apollo_elicited``, never ``corroborated``:
    S2′ requires NOT corrected_later, so "corroborated and resolved" is the
    empty set by construction. ``apollo_elicited``
    (``last_asked_turn < correction_turn``) is the anti-farming guard — assert
    something wrong unprompted, fix it yourself, collect XP — and subtracting
    ``prior_wrongness_findings`` makes it once per user × problem × node, so a
    re-roll cannot re-earn it.

    **Additive only, own failure domain.** Any exception logs
    ``apollo_wrongness_xp_bonus_failed`` and awards 0 on top of the base XP;
    the return is never negative, which is what keeps ``apply_xp`` (it raises
    on a negative delta) safe."""
    try:
        earned = {f.node_id for f in findings if f.resolved and f.apollo_elicited}
        if not earned:
            return 0
        prior = await prior_wrongness_findings(
            db,
            attempt_id=int(attempt.id),
            problem_id=int(attempt.problem_id),
            course_id=course_id,
        )
        return compute_misconception_bonus(
            newly_resolved_keys=sorted(earned - {row["canonical_key"] for row in prior})
        )
    except Exception:
        _LOG.exception("apollo_wrongness_xp_bonus_failed attempt_id=%s", attempt.id)
        return 0


def _course_evidence_safe(
    sess: TutoringSession,
    *,
    concept_slug: str | None,
) -> CourseEvidence | None:
    """INTERACTION2 — the session's course-grounding block, or ``None``.

    Soft-failing by construction and deliberately so: this runs AHEAD of the
    sole grading lane, whose `CoverageGradingError` -> 503 contract is the only
    hard failure allowed on this path. A flag that is off, a concept outside
    the allowlist, a NULL/corrupt `grounding_bundle`, or a bundle with nothing
    student-safe left in it all yield `None`, which builds the adjudication and
    narrative prompts BYTE-IDENTICALLY to the pre-feature code. Any unexpected
    exception is logged and swallowed to the same `None`.
    """
    if not interaction2_enabled() or not interaction_allowed_for_concept(concept_slug):
        return None
    try:
        return build_course_evidence(sess.grounding_bundle)
    except Exception:
        _LOG.exception("apollo_grounding_build_failed session_id=%s", sess.id)
        return None


async def _find_problem(
    db: AsyncSession, concept_id: int, problem_id: int, *, course_id: int
) -> Problem:
    for p in await list_problems_for_concept(db, concept_id=concept_id, search_space_id=course_id):
        if p.database_id == problem_id:
            return p
    raise RuntimeError(f"problem {problem_id!r} not in bank for cluster {concept_id!r}")


async def _fetch_attempt_transcript(db: AsyncSession, attempt_id: int) -> list[dict[str, Any]]:
    """Return the graded attempt's ordered chat turns for report display."""
    try:
        messages = (
            (
                await db.execute(
                    select(TutoringMessage)
                    .where(TutoringMessage.attempt_id == attempt_id)
                    .where(TutoringMessage.role.in_(("student", "apollo")))
                    .order_by(TutoringMessage.turn_index)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "role": message.role,
                "content": message.content,
                "turn_index": message.turn_index,
            }
            for message in messages
        ]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "transcript fetch soft-fail for attempt %s: %s",
            attempt_id,
            exc,
            exc_info=True,
        )
        return []


async def _claim_grading_slot(db: AsyncSession, *, session_id: int) -> bool:
    """Compare-and-swap the session into the grading claim.

    Returns True when THIS Done owns the attempt, False when another Done
    already does. This single write replaces BOTH the blind
    `sess.phase = SOLVING` assignment and `store.freeze`'s transient
    `PROBLEM_REVEAL` write, closing the window in which the session sat in a
    phase `restart_problem._FROZEN_PHASES` does not cover (memo §5) and in which
    a restart could wipe the transcript mid-grade.

    `phase` is NULLABLE, so the predicate is `IS DISTINCT FROM`: SQL `<>` never
    matches NULL and would refuse to claim a NULL-phase session forever. The
    second disjunct reclaims a STALE claim; `updated_at` is stamped by this same
    statement, so the predicate reads "session row untouched for
    `_STALE_CLAIM_AFTER`". Both sides of that comparison use the application
    clock deliberately — one clock, and the same statement runs unchanged on the
    SQLite unit harness.

    PHASE-ONLY CAS, deliberately (P3.4 fix-round-2): a fencing-token variant
    (`_claim_grading_slot` returning its `updated_at` stamp, threaded into an
    `AND updated_at = claim_stamp` guard on the release/fence below) was built
    and REVERTED. `LearningActivity.updated_at` carries a model-level
    `onupdate`, so ANY unrelated write to the session row during the grading
    window — `handlers/chat`'s pending-intent commit, the aside metadata
    counter, session_init status writes, all reachable mid-Done per that
    module's own comment — silently bumps it and invalidates the RIGHTFUL
    owner's OWN stamp. That turned a rare failure mode (a Done stale for
    >15 minutes) into a common-case one (the owner's own fence/release racing
    an unrelated chat commit). Integrity never depended on the stamp — see
    `_fence_grade_commit` for why a plain `phase = 'SOLVING'` CAS is already
    sufficient to guarantee at most one grade-visible writer — so phase-only
    wins outright. `_release_grading_claim` and `_fence_grade_commit` each
    document the one accepted availability (never integrity) residual this
    leaves.

    Callers must snapshot the pre-claim `phase` from the SAME `sess` read that
    precedes this call — that value is what gets passed as `_release_grading_claim`'s
    `prior_phase` on any failure path. The small window between that read and
    this CAS is an accepted design tradeoff (not itself locked); the release's
    own `WHERE phase = SOLVING` guard bounds its blast radius so a claim that
    raced ahead in that window is never clobbered by a release racing behind
    it — except the one residual case a reclaim itself creates (see
    `_release_grading_claim`).
    """
    now = datetime.now(UTC)
    claimed = (
        await db.execute(
            update(TutoringSession)
            .where(TutoringSession.id == session_id)
            .where(
                or_(
                    TutoringSession.phase.is_distinct_from(_CLAIM_PHASE),
                    TutoringSession.updated_at < now - _STALE_CLAIM_AFTER,
                )
            )
            .values(phase=_CLAIM_PHASE, updated_at=now)
            .returning(TutoringSession.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    await db.commit()
    return claimed is not None


async def _release_grading_claim(
    db: AsyncSession, *, session_id: int, prior_phase: str | None
) -> None:
    """Compensating CAS for every pre-grade failure path.

    Without it, a Done that raises before the grade commit — the sole-lane
    `CoverageGradingError` 503, a degraded-KG raise, any unexpected error —
    leaves the claim set, and the student's retry hits "another Done owns this
    attempt" forever: the attempt is bricked (spec §5).

    Guarded on `phase = SOLVING` (phase-only CAS — P3.4 fix-round-2 reverted a
    fencing-token guard here; see `_claim_grading_slot`'s docstring for why).

    ACCEPTED AVAILABILITY RESIDUAL (never a grade-integrity issue — see
    `_fence_grade_commit`): if THIS Done's own claim went stale (>15 minutes,
    no further session-row writes) and was legitimately reclaimed by another
    Done, and THIS Done later fails and reaches this release, the guard
    matches the RECLAIMER's LIVE claim too — both sit at `phase = 'SOLVING'`,
    indistinguishable without a stamp. The release clobbers it, resetting
    phase to `release_to` while the reclaimer is still mid-grade. The
    reclaimer's own subsequent `_fence_grade_commit` then finds
    `phase != 'SOLVING'` and is fenced out: its completed grading work is
    discarded (no partial write — the fence runs before any grade-visible
    write) and it raises `GradingInProgressError`. No corruption results; the
    student's retry re-grades from scratch. This requires BOTH claims to be
    genuinely stale (>15 min) AND the original claimant to fail only AFTER
    the reclaim — judged rare enough to accept versus the fencing token's
    common-case regression (see `_claim_grading_slot`).

    When the claim was itself a stale RECLAIM, `prior_phase` is already
    `SOLVING` — restoring it verbatim would re-brick the attempt, so that
    case (and a NULL prior phase) falls back to `TEACHING`, the phase
    `handle_retry` resets to. Never raises: a failure here must not mask the
    error that triggered it.

    `prior_phase` must be the value snapshotted from the SAME `sess` read that
    preceded the `_claim_grading_slot` call, not a fresh read taken here — the
    read-to-CAS window that opens up is an accepted design tradeoff, and this
    call's own `phase = SOLVING` guard is what bounds it (see
    `_claim_grading_slot`'s docstring).
    """
    release_to = (
        prior_phase if prior_phase not in (None, _CLAIM_PHASE) else SessionPhase.TEACHING.value
    )
    try:
        await db.rollback()
        await db.execute(
            update(TutoringSession)
            .where(TutoringSession.id == session_id)
            .where(TutoringSession.phase == _CLAIM_PHASE)
            .values(phase=release_to, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    except Exception:
        _LOG.exception("apollo_done_claim_release_failed session_id=%s", session_id)


async def _fence_grade_commit(db: AsyncSession, *, session_id: int) -> bool:
    """Terminal fencing CAS (P3.4 controller delta on M1) — Done's LAST
    Postgres write, executed in the SAME transaction as the grade commit that
    follows it in `_grade_claimed_attempt`.

    A blind `sess.phase = REPORT` assignment here would let a STALE Done —
    one whose claim was legitimately reclaimed by another Done via
    `_claim_grading_slot`'s `_STALE_CLAIM_AFTER` disjunct while this one was
    still grading — stomp the grade of the Done that now owns the attempt, if
    the stale Done ever finishes anyway. Guarding the write on
    `phase = _CLAIM_PHASE` closes that: once the reclaiming Done has landed on
    REPORT, this statement matches zero rows.

    PHASE-ONLY CAS, deliberately (P3.4 fix-round-2; see `_claim_grading_slot`
    for why a fencing token was tried and reverted). THIS is the statement
    that makes phase-only safe for grade INTEGRITY: it is a single
    `UPDATE ... WHERE phase = 'SOLVING'`, and Postgres serializes concurrent
    UPDATEs to the same row — whichever caller's statement acquires the row
    lock first commits the `SOLVING → REPORT` transition, and every OTHER
    concurrent caller then re-evaluates the SAME predicate against the
    post-commit row, sees `phase = 'REPORT'`, and gets zero rows. So AT MOST
    ONE `_grade_claimed_attempt` call ever passes this line, and every write
    that matters — `attempt.result`, `diagnostic_report`, XP, artifacts — comes
    strictly AFTER it in that same call. No stamp is needed for that
    guarantee; whether the row lock happens to go to a stale reclaimant or the
    "rightful" one is immaterial to integrity — either way exactly one
    complete, consistent grade lands and every other Done is cleanly fenced
    out (zero grade-visible writes, `GradingInProgressError`).

    ACCEPTED AVAILABILITY RESIDUAL: two Dones racing their fence at the exact
    same instant (one a live claim, one a stale claimant that finished
    anyway) can have EITHER one win the row-lock race — not necessarily the
    "rightful" one by wall-clock claim order. The loser's fully-computed
    grade is discarded even though its work was real; the student sees a
    retryable 409 and re-grades. This is a fairness/latency cost, never a
    correctness one.

    Returns True when this Done still owns the claim — the caller may go on to
    write `attempt.result` / `diagnostic_report` and commit, in the SAME
    transaction as this statement. Returns False when it has been fenced out —
    the caller must roll back and raise `GradingInProgressError` WITHOUT
    writing anything grade-visible (no `attempt.result`, no
    `diagnostic_report`, no XP — see `_grade_claimed_attempt`). Same style as
    `_claim_grading_slot`: `synchronize_session=False`, one-statement CAS, the
    caller commits.
    """
    result = await db.execute(
        update(TutoringSession)
        .where(TutoringSession.id == session_id)
        .where(TutoringSession.phase == _CLAIM_PHASE)
        .values(phase=SessionPhase.REPORT.value, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount > 0


def _progress_block(envelope: Any) -> dict[str, Any]:
    """The structured progress envelope served on every Done response.

    Single source of truth for level / threshold display; the flat `xp_*` /
    `level_*` keys beside it stay for older clients during the FE migration
    window. Shared by the graded path and the already-graded replay so the two
    payloads can never drift.
    """
    return {
        "xp_earned": envelope.xp_earned,
        "xp_before": envelope.xp_before,
        "xp_after": envelope.xp_after,
        "level_before": envelope.level_before,
        "level_after": envelope.level_after,
        "level_up": envelope.level_up,
        "title_after": envelope.title_after,
        "level_progress_pct": envelope.level_progress_pct,
        "xp_to_next_level": envelope.xp_to_next_level,
    }


async def _stored_grade_payload(
    db: AsyncSession, *, sess: TutoringSession, attempt: ProblemAttempt
) -> dict[str, Any] | None:
    """Replay the already-committed grade for a Done on a graded attempt.

    Reads the SAME `diagnostic_report` snapshot the re-serving surfaces read
    (`handlers/browse`'s `served_overall_from_report` / `feedback_from_report`),
    so a double-clicked Done is shown exactly the grade that is persisted rather
    than a freshly re-adjudicated one that last-writer-wins would then overwrite.
    Awards NO XP: `xp_earned` is 0 and the envelope is a zero-delta read of the
    current progress row. A Done that dies between the grade commit and
    `apply_xp` therefore forfeits that attempt's XP permanently — the replay
    serves `xp_earned: 0`; the student recovers only via `/retry` (new attempt).

    Returns `None` when the attempt is not gradable-from-storage (`result` is
    not `"graded"`, or the report is missing/malformed), which is the caller's
    signal to grade normally. `topics` / `feedback` / `scorecard` /
    `grading_provenance` are deliberately ABSENT — they are not persisted in
    `diagnostic_report`, and the student payload already treats them as optional
    keys, so omitting them is honest where fabricating them would not be.

    SIDE-EFFECT FREE by construction: this is a replay path, reachable on every
    double-clicked Done, and must never write anything. `progress_repo.load_progress`
    is deliberately NOT used here — it does an `INSERT ... ON CONFLICT DO
    NOTHING` upsert AND commits the caller's session, neither of which belongs
    on a read-only replay. A missing `StudentProgress` row (the attempt is
    graded but the student never got a progress row some other way) reads as
    `xp_total=0` rather than materializing one.
    """
    report = attempt.diagnostic_report
    if attempt.result != "graded" or not isinstance(report, dict):
        return None
    overall = served_overall_from_report(report)
    if overall is None:
        return None
    raw_rubric = report.get("rubric")
    served_rubric = (
        {**raw_rubric, "overall": overall} if isinstance(raw_rubric, dict) else {"overall": overall}
    )
    progress_row = (
        await db.execute(
            select(StudentProgress).where(
                StudentProgress.user_id == sess.user_id,
                StudentProgress.course_id == sess.course_id,
            )
        )
    ).scalar_one_or_none()
    xp_total = int(progress_row.xp_total) if progress_row is not None else 0
    envelope = compute_progress_envelope(xp_earned=0, xp_before=xp_total, xp_after=xp_total)
    return {
        "rubric": served_rubric,
        "diagnostic_narrative": feedback_from_report(report) or "",
        "coverage": report.get("coverage") or {},
        "already_graded": True,
        "progress": _progress_block(envelope),
        "xp_earned": envelope.xp_earned,
        "xp_before": envelope.xp_before,
        "xp_after": envelope.xp_after,
        "level_before": envelope.level_before,
        "level_after": envelope.level_after,
        "level_up": envelope.level_up,
        "transcript": await _fetch_attempt_transcript(db, int(attempt.id)),
    }


async def handle_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
    auto_done: bool = False,
) -> dict[str, Any]:
    store = KGStore(db, neo)

    sess = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == session_id))
    ).scalar_one()
    problem = await _find_problem(
        db, sess.concept_id, sess.current_problem_id, course_id=sess.course_id
    )

    attempt = (
        (
            await db.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == session_id)
                .where(ProblemAttempt.problem_id == problem.database_id)
                .order_by(ProblemAttempt.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if attempt is None:
        raise RuntimeError(f"no ProblemAttempt for session {session_id} / problem {problem.id}")

    # Empty-attempt guard (2026-08-07 bimodal-fix defect I1): an attempt with
    # zero student messages has nothing to adjudicate — grading it produced
    # F(0) rows whose narrative was invented from the reference solution
    # (phantom rows from browse/abandon flows; 9 of 87 graded pilot attempts).
    # Refuse BEFORE any mutation (no freeze, no phase change, no XP, no
    # narrative) and leave the attempt row untouched so a later real Done is
    # not treated as a reattempt.
    if await _student_message_count(db, attempt_id=int(attempt.id)) == 0:
        raise EmptyAttemptError(session_id=session_id, attempt_id=int(attempt.id))

    # Already-graded short-circuit (M1). A re-clicked Done used to re-run the
    # whole pipeline: a second large LLM call, a possibly DIFFERENT letter, and
    # a last-writer-wins overwrite of the report the student was already shown.
    # Replay the persisted grade instead — and note this is also what makes the
    # claim-loss branch below purely "still grading": the grade commit sets
    # `result='graded'` and `phase='REPORT'` in the SAME transaction, so a loser
    # can never observe a held claim on an already-graded attempt.
    stored = await _stored_grade_payload(db, sess=sess, attempt=attempt)
    if stored is not None:
        _LOG.info(
            "apollo_done_served_stored_grade session_id=%s attempt_id=%s auto_done=%s",
            session_id,
            int(attempt.id),
            auto_done,
        )
        return stored

    # Read the student graph before claiming so the frozen subgraph is still
    # persisted; a degraded KG is tolerated (log-and-continue) rather than
    # 500-ing. The result is not used for grading — the transcript grader
    # (below) grades from the transcript, so an unavailable KG never yields a
    # false F. This is a Neo4j READ, so it does not break "the claim is Done's
    # first POSTGRES write".
    try:
        await store.read_graph(attempt_id=attempt.id)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=pre_freeze_graph attempt_id=%s error=%s",
            attempt.id,
            exc,
        )

    # M1 — the claim, and Done's FIRST Postgres write. It replaces BOTH the
    # blind `sess.phase = SOLVING` assignment and `store.freeze`'s transient
    # `PROBLEM_REVEAL` write, so there is no longer a window in which the
    # session sits in a phase `restart_problem._FROZEN_PHASES` does not cover.
    prior_phase = sess.phase
    if not await _claim_grading_slot(db, session_id=session_id):
        _LOG.info(
            "apollo_done_claim_lost session_id=%s attempt_id=%s auto_done=%s",
            session_id,
            int(attempt.id),
            auto_done,
        )
        raise GradingInProgressError(session_id=session_id, attempt_id=int(attempt.id))

    # Every pre-grade failure path must release the claim (spec §5): a 503 out
    # of the sole grading lane would otherwise leave the claim held and the
    # student's retry would find the attempt permanently owned by a dead Done.
    # `BaseException` (not `Exception`): a client disconnect mid-grade raises
    # `asyncio.CancelledError`, which is NOT an `Exception` subclass — missing
    # it would leak the claim for up to `_STALE_CLAIM_AFTER`. The release is
    # wrapped in `asyncio.shield` so a cancellation delivered WHILE we are
    # compensating cannot interrupt it (a SECOND cancellation detaches the
    # release rather than awaiting it — accepted residual); the outer
    # `raise` below still propagates the original (or a second) cancellation
    # to the caller once the shielded release has run. The CRITICAL post-claim
    # re-check just below lives INSIDE this try too (P3.4 fix-round-2 review
    # fix): if `db.refresh` / `_stored_grade_payload` itself raised, the claim
    # would otherwise leak with no release.
    try:
        # CRITICAL (P3.4 fix-round-1): the already-graded hoist above read
        # `attempt` BEFORE this claim, and the claim CAS inspects only the
        # SESSION row's phase. A Done that stalled here (e.g. in the
        # `read_graph` call just above) while ANOTHER Done graded this SAME
        # attempt to completion wakes to find `phase='REPORT'` — which the
        # claim predicate treats as freely claimable (`IS DISTINCT FROM
        # 'SOLVING'`) — and legitimately WINS the claim on an attempt its own
        # stale, pre-claim snapshot still says is ungraded. Without this
        # re-check, that stale Done would fall through to
        # `_grade_claimed_attempt` and re-grade: overwrite `diagnostic_report`
        # and double-award XP (`apply_xp` runs before the artifact-writer's
        # unique-constraint backstop, so nothing there catches it). Refresh
        # `attempt` from the row this claim just observed and re-run the SAME
        # short-circuit; a hit means this Done's claim was accidental —
        # release it (the true prior phase is REPORT, not `prior_phase`
        # above, which is whatever phase preceded THIS Done's own —
        # irrelevant — claim attempt) and serve the stored grade instead of
        # re-grading.
        await db.refresh(attempt)
        stored = await _stored_grade_payload(db, sess=sess, attempt=attempt)
        if stored is not None:
            _LOG.info(
                "apollo_done_claim_won_on_stale_already_graded_hoist "
                "session_id=%s attempt_id=%s auto_done=%s",
                session_id,
                int(attempt.id),
                auto_done,
            )
            await _release_grading_claim(
                db, session_id=session_id, prior_phase=SessionPhase.REPORT.value
            )
            return stored

        return await _grade_claimed_attempt(
            db=db,
            store=store,
            sess=sess,
            problem=problem,
            attempt=attempt,
            auto_done=auto_done,
        )
    except BaseException:
        await asyncio.shield(
            _release_grading_claim(db, session_id=session_id, prior_phase=prior_phase)
        )
        raise


async def _grade_claimed_attempt(
    *,
    db: AsyncSession,
    store: KGStore,
    sess: TutoringSession,
    problem: Problem,
    attempt: ProblemAttempt,
    auto_done: bool,
) -> dict[str, Any]:
    """The grading pipeline proper — run ONLY by the Done that owns the claim.

    Split out of `handle_done` so the claim wrapper can release on any failure
    without re-indenting (and therefore re-writing) the whole pipeline. Every
    write from here to the grade commit (`attempt.result` / `diagnostic_report`
    / `phase=REPORT`, one transaction) is protected by the M1 claim, and the
    `phase=REPORT` write is itself a fenced CAS (`_fence_grade_commit`, P3.4
    controller delta) — see its call below.
    """
    reference_graph = problem.to_kg_graph(attempt_id=attempt.id)

    # Task A3 — grading-latency clock. Captured before grading runs so the
    # persisted artifact's `grading_latency_ms` covers the WHOLE grading
    # pipeline for this Done-click, not just one half of it.
    _artifact_t0 = time.monotonic()

    # The transcript grader is the sole grading lane. A CoverageGradingError
    # here is NOT caught: it propagates to the CoverageGradingError -> 503
    # retryable handler (apollo/api.py) so the student is told "try again"
    # rather than served a fabricated grade. This also covers Neo4j-degraded
    # Done clicks — grading reads the transcript, never the (possibly empty)
    # frozen graph, so a degraded KG never yields a false F.
    #
    # `narrative_spans` are the per-attempt student quotes for the diagnostic
    # narrative, verbatim-gated so the narrative can only ever attribute to the
    # student words they typed THIS attempt.
    #
    # INTERACTION2 (default OFF) additionally hands the adjudicator a capped,
    # student-safe block of THIS course's own material so coverage judgments use
    # the professor's definitions and notation. Strictly additive: `None`
    # reproduces today's prompt and today's grade, and the evidence is
    # pre-truncated by `build_course_evidence`, so the transcript below is never
    # the thing that gets cut.
    course_evidence = _course_evidence_safe(sess, concept_slug=getattr(problem, "concept_id", None))
    transcript = await _full_transcript(db, attempt_id=int(attempt.id))

    # The question ledger, read ONCE for both bimodal-fix consumers below
    # (P1.3 `tally_context` for the adjudicator, P1.2b `asked_node_ids` for the
    # scorer). A failed read is `None` for both, which reproduces the pre-fix
    # grade exactly. An EMPTY ledger is deliberately NOT `None`: an attempt whose
    # questioning loop engaged no node at all is real signal (the auto-done /
    # restart-orphan pathologies), and the scorer's own degenerate-case guard
    # keeps it gradeable. `_probed_node_ids` — NOT the raw row set — is what the
    # scorer gets: a row minted by a degenerate fallback turn or a bare `missing`
    # tally update is not engagement, and counting it as probed would put the
    # node back in the denominator at credit 0.
    ledger_rows = await _question_ledger(db, attempt_id=int(attempt.id))
    tally_context = None if ledger_rows is None else _tally_context(ledger_rows)
    asked_node_ids = None if ledger_rows is None else _probed_node_ids(ledger_rows)

    # INTERACTION5 (default OFF) — the Hoot-assist grading cap. Gated on the flag
    # AND the problem concept passing the shared allowlist. Its own failure
    # domain, mirroring the INTERACTION3 pattern: the aside fetch is wrapped so
    # ANY exception is logged and swallowed, leaving `hoot_asides` empty so the
    # cap path degrades to today's grade. It runs AHEAD of the sole grading lane
    # and must NEVER touch the CoverageGradingError -> 503 contract. Flag off / no
    # aside used → `hoot_asides == ()` reproduces today's prompts, schema, and
    # coverage byte-for-byte.
    aside_cap_active = interaction5_enabled() and interaction_allowed_for_concept(
        problem.concept_id
    )
    hoot_asides: tuple[str, ...] = ()
    if aside_cap_active:
        try:
            hoot_asides = await _aside_texts(db, attempt_id=int(attempt.id))
        except Exception:
            _LOG.exception("apollo_aside_fetch_failed attempt_id=%s", attempt.id)
            hoot_asides = ()

    # P3.2 wrongness ladder (`APOLLO_WRONGNESS_LEVEL`, default 0 = OFF), read
    # ONCE and paired with the concept allowlist inside
    # `effective_wrongness_level` exactly like INTERACTION5 above. Level 0 skips
    # the block entirely, which is what makes it byte-identical to today.
    # `getattr` (as `_course_evidence_safe`'s call already does) because this is
    # the FIRST unconditional read of `concept_id` on this path — INTERACTION5's
    # is short-circuited by its flag — and a problem shim without the attribute
    # must degrade to "outside the pilot", never raise inside the grade path.
    level = wrongness.effective_wrongness_level(getattr(problem, "concept_id", None))
    graded_node_ids: frozenset[str] = frozenset()
    tally_findings: tuple[wrongness.LedgerFinding, ...] = ()
    wrongness_candidates: dict[str, str] | None = None
    if level >= wrongness.LEVEL_PRODUCE and ledger_rows is not None:
        # The tally's own contradiction labels, off the ledger ALREADY read
        # above (never a second query). Candidates name graded nodes only, and
        # `None` (not `{}`) leaves the adjudication schema, prompts and coverage
        # dict byte-identical when nothing was flagged.
        graded_node_ids = frozenset(
            node.node_id for node in reference_graph.nodes if node.node_type in _GRADED_NODE_TYPES
        )
        tally_findings = wrongness.ledger_findings(ledger_rows)
        wrongness_candidates = (
            wrongness.candidate_quotes(tally_findings, graded_node_ids=graded_node_ids) or None
        )

    coverage, narrative_spans = await compute_transcript_coverage_with_spans(
        transcript=transcript,
        reference_graph=reference_graph,
        problem=problem,
        course_evidence=evidence_block(course_evidence),
        hoot_asides=hoot_asides,
        tally_context=tally_context,
        wrongness_candidates=wrongness_candidates,
    )

    # Apply the flat cap to the coverage BEFORE rubric / topic-score / diagnostic
    # / artifacts, so every downstream consumer sees the SAME capped values. Any
    # exception here leaves `coverage` the original UNCAPPED verdict (the RHS is
    # evaluated in full before the assignment binds, so a raise never half-caps)
    # and grading proceeds — the cap can only ever lower a grade, never break one.
    aside_assisted_ids: tuple[str, ...] = ()
    if aside_cap_active and hoot_asides:
        try:
            coverage, aside_assisted_ids = apply_aside_caps(coverage, cap=_ASIDE_CREDIT_CAP)
        except Exception:
            _LOG.exception("apollo_aside_penalty_failed attempt_id=%s", attempt.id)

    # Class 2 Phase 2 (P2.8): pull per-attempt misconception signals from
    # tutoring-message metadata and reduce them to the per-bank-code score
    # map the rubric expects. The axis enters at 5% taken from the
    # existing 60/25/15. When no misconceptions fired, the dict is empty
    # and the rubric is byte-identical to its pre-P2.8 output.
    misconception_scores = await _attempt_misconception_scores(
        db,
        attempt_id=attempt.id,
    )
    rubric = compute_rubric(
        coverage,
        reference_graph.nodes,
        misconception_scores=misconception_scores,
    )

    # Topic-score (2026-07-10 spec §2/§3) — COMPUTED ALWAYS. Soft-fail
    # contract: `_compute_topic_score_safe` never raises — `topic_score` is
    # `None` on any failure, and every downstream use below is guarded on that.
    topic_score: TopicScoreResult | None = _compute_topic_score_safe(
        coverage=coverage,
        reference_graph=reference_graph,
        attempt_id=int(attempt.id),
        evidence_spans=narrative_spans,
        asked_node_ids=asked_node_ids,
    )

    # P3.2: S2′ over the RAW result (it needs the raw score for `would_ceiling`),
    # then level >=3 SUPERSEDES that result with the container-bearing one, so
    # exactly ONE `TopicScoreResult` reaches `served_rubric` / `topics[]` /
    # narrative / artifact. A soft-failed rescore keeps the raw result.
    wrongness_findings = _evaluate_wrongness(
        tally_findings,
        coverage=coverage,
        topic_score=topic_score,
        graded_node_ids=graded_node_ids,
        attempt_id=int(attempt.id),
        level=level,
    )
    # Keyed by node id (S7). `select_findings` returns one rung per EVIDENCE
    # ENTRY, but only a node's LATEST entry can corroborate, so this dedup is
    # exact rather than last-write-wins.
    corroborated = {f.node_id: f for f in wrongness_findings if f.corroborated}
    if level >= wrongness.LEVEL_SURFACE and corroborated:
        surfaced = _compute_topic_score_safe(
            coverage=coverage,
            reference_graph=reference_graph,
            attempt_id=int(attempt.id),
            evidence_spans=narrative_spans,
            asked_node_ids=asked_node_ids,
            misconceptions=corroborated,
            ceiling_active=level >= wrongness.LEVEL_CEILING,
        )
        if surfaced is not None:
            topic_score = surfaced

    # Serving (spec §3): `served_rubric` REPLACES `overall` with the topic
    # score/letter while every legacy axis block is carried over UNCHANGED
    # (mid-deploy safety for older UI clients). This builds a NEW dict —
    # `rubric` itself (the object `attempt.diagnostic_report` and
    # `write_artifacts` below both still receive) is never mutated. A
    # soft-failed `topic_score` (None) leaves `served_rubric is rubric`
    # (byte-identical downstream).
    serve_topic_score = topic_score is not None
    if serve_topic_score:
        served_rubric = {
            **rubric,
            "overall": {"score": topic_score.score, "letter": topic_score.letter},
        }
    else:
        served_rubric = rubric

    # Narrative grounding (2026-07-14): feed the narrator the verbatim student
    # transcript so credit statements quote what the student actually said
    # instead of expanding topic names into claims they never made. Best-effort:
    # a fetch failure logs and degrades to the ungrounded prompt — it must
    # never block grading.
    try:
        narrative_utterances: tuple[str, ...] = await _student_utterances(db, attempt_id=attempt.id)
    except Exception:  # noqa: BLE001
        _LOG.warning("apollo_narrative_utterances_fetch_failed attempt_id=%s", attempt.id)
        narrative_utterances = ()

    # P2.1 consistency (2026-08-07): the narrator and the structured topic
    # feedback see the GRADED topics only. An `unprobed` topic (P1.2b) carries
    # credit 0 but is excluded from the grade and labelled "not part of this
    # grade" in the served `topics[]` — narrating it as a gap would make one
    # payload say both things at once. The served/artifact `topic_score` below
    # is still the FULL result, so nothing is hidden from the UI or the record.
    narrative_topic_score = graded_topics_only(topic_score)
    diagnostic_result = await asyncio.to_thread(
        generate_diagnostic,
        coverage=coverage,
        reference_steps=[s.model_dump() for s in problem.reference_solution],
        problem_text=problem.problem_text,
        rubric=rubric,
        topic_score=narrative_topic_score,
        student_utterances=narrative_utterances,
        course_evidence=evidence_block(course_evidence),
    )
    # ``generate_diagnostic`` now returns the flattened back-compat narrative
    # plus optional structured topic feedback. Accept string-only test doubles
    # and rolling-deploy shims as legacy narrative results.
    if isinstance(diagnostic_result, tuple):
        diagnostic_narrative, feedback = diagnostic_result
    else:
        diagnostic_narrative = diagnostic_result
        feedback = None

    # Interaction 3 — citation-only review pointers for weak topic feedback.
    # This entire optional pass is one failure domain: build a decorated copy
    # and publish it only after every selected topic succeeds. Any exception
    # leaves the diagnostic feedback and every grade-bearing value untouched.
    if (
        interaction3_enabled()
        and interaction_allowed_for_concept(problem.concept_id)
        and narrative_topic_score is not None
        and feedback is not None
    ):
        try:
            remediated_feedback = await add_remediation_reviews(
                db=db,
                search_space_id=sess.search_space_id,
                # Same graded-only view the feedback keys were generated from —
                # a review pointer for an `unprobed` topic would point at
                # something the grade explicitly excluded.
                topic_score=narrative_topic_score,
                feedback=feedback,
                grounding_bundle=getattr(sess, "grounding_bundle", None),
            )
            if remediated_feedback is not None:
                feedback = remediated_feedback
        except Exception:
            _LOG.exception("remediation_review_failed attempt_id=%s", attempt.id)

    # Re-attempt detection (unchanged from V2).
    is_reattempt_in_session = attempt.result is not None
    is_reattempt_cross_session = await has_prior_graded_attempt(
        db=db,
        user_id=sess.user_id,
        course_id=sess.course_id,
        problem_id=problem.database_id,
        exclude_attempt_id=attempt.id,
    )
    is_reattempt = is_reattempt_in_session or is_reattempt_cross_session

    # XP ordering (spec §3: "XP continues to derive from rubric.overall (now
    # the topic score)"): `served_rubric` is already the REPLACED overall by
    # this point, so XP is earned against the topic score, not the axis blend
    # — this line MUST stay after the `served_rubric` assignment above and
    # before any use of `xp_earned`.
    xp_earned = compute_xp_earned(
        overall_score=served_rubric["overall"]["score"],
        difficulty=attempt.difficulty,
        is_reattempt=is_reattempt,
    )
    # Decision-7 bonus (level >=3): additive-only, so XP still only ever goes up.
    if level >= wrongness.LEVEL_SURFACE and wrongness_findings:
        xp_earned += await _wrongness_bonus_xp(
            db,
            findings=wrongness_findings,
            attempt=attempt,
            course_id=int(sess.search_space_id),
        )

    # M1b fence (P3.4 controller delta) — the terminal phase transition is
    # itself a CAS, checked BEFORE any grade-visible write (spec: "Order the
    # fence check BEFORE apply_xp and the report write") so a fenced-out Done
    # writes NOTHING. A Done that claimed the slot >15 min ago
    # (`_STALE_CLAIM_AFTER`) may have been legitimately reclaimed by another
    # Done while this one was still grading; if that Done has ALREADY landed
    # the grade (`phase='REPORT'`), this stale Done must lose here rather than
    # overwrite the grade of record — `attempt.result` / `diagnostic_report` /
    # XP / artifacts belong to the winner alone.
    if not await _fence_grade_commit(db, session_id=int(sess.id)):
        _LOG.info(
            "apollo_done_fenced_out session_id=%s attempt_id=%s",
            int(sess.id),
            int(attempt.id),
        )
        await db.rollback()
        raise GradingInProgressError(session_id=int(sess.id), attempt_id=int(attempt.id))

    attempt.result = "graded"
    attempt.solver_trace = None
    diagnostic_report = {
        "narrative": diagnostic_narrative,
        "rubric": rubric,
        "coverage": coverage,
        # The overall the student was actually SHOWN (topic score when it
        # computed, legacy axis blend otherwise). `rubric` above deliberately
        # stays the RAW rubric — rerun/janitor consumers depend on it — so any
        # surface re-serving the grade later (browse cards, progress recents)
        # must read this snapshot first and fall back to `rubric.overall`.
        "served_overall": dict(served_rubric["overall"]),
    }
    # Teacher-surface consistency (2026-08-07 review fix). The per-problem node
    # drill-down (`projections/performance_problems`) re-derives each node's
    # status from `coverage` alone, and `coverage` can only ever say
    # covered/partial/missing — it has no way to know P1.2b dropped a node from
    # THIS student's grade, so it would report a class-wide "missed" on a topic
    # nobody was asked about and that no grade counted. Snapshot the excluded
    # keys beside `served_overall`, on the same principle: the teacher reads what
    # the student was served, never a re-derivation. Omitted when nothing was
    # excluded, so those rows keep the pre-fix shape exactly.
    unprobed_node_ids = (
        [topic.canonical_key for topic in topic_score.topics if topic.status == "unprobed"]
        if topic_score is not None
        else []
    )
    if unprobed_node_ids:
        diagnostic_report = {**diagnostic_report, "unprobed_node_ids": unprobed_node_ids}
    # Audit stamp (2026-08-07 bimodal-fix P0.4): a Done triggered by budget
    # exhaustion — not by the student — is marked so grade forensics can
    # separate consented grades from auto-grades. Key absent on a student
    # Done, keeping those rows byte-identical to the pre-stamp shape.
    if auto_done:
        diagnostic_report = {**diagnostic_report, "auto_done": True}
    attempt.diagnostic_report = diagnostic_report
    # `_fence_grade_commit` already wrote `phase='REPORT'` durably (guarded on
    # `phase = _CLAIM_PHASE`, above).
    # `set_committed_value` (not a plain `sess.phase = ...` assignment) syncs
    # the in-memory attribute for the rest of this function and for any caller
    # that reads `sess.phase` after a successful Done, WITHOUT marking it
    # dirty — a plain assignment would make the ORM re-emit a second,
    # unguarded `UPDATE ... SET phase='REPORT'` on THIS commit, duplicating
    # exactly the write the fence CAS above already protected.
    set_committed_value(sess, "phase", SessionPhase.REPORT.value)
    await db.commit()

    progress = await apply_xp(
        db=db,
        user_id=sess.user_id,
        course_id=sess.course_id,
        xp_delta=xp_earned,
    )

    envelope = compute_progress_envelope(
        xp_earned=xp_earned,
        xp_before=progress["xp_before"],
        xp_after=progress["xp_after"],
    )

    # Retention (§7 / §6.4, WU-3C1): stamp `graded_at` on the now-frozen
    # subgraph. This is the FINAL, idempotent, post-commit retention write —
    # the student-facing grade + XP are already durable (committed above), so a
    # RetentionError here surfaces (NO FALLBACK) WITHOUT voiding the grade; the
    # next Done / retry / janitor re-stamps idempotently. Δt-anchoring in
    # Layer-3 (§3) reads this stored value, never now().
    #
    # WU-5A2: capture ONE `done_ts` and thread it into BOTH `stamp_graded_at`
    # (Neo4j `graded_at`) AND `run_learner_update` (Postgres `last_evidence_at`)
    # so the two stores stamp the IDENTICAL freeze instant (no second clock).
    #
    # Degraded-mode relaxation (NEO4J-DEGRADED, deliberate NO-FALLBACK carve-
    # out — documented in the owner doc): catch (KGUnavailableError,
    # RetentionError) UNCONDITIONALLY, not just on a degraded pre-freeze read —
    # the real failure mode is a connection dying DURING the ~4-minute grading
    # pipeline, so a HEALTHY read at the top of this function does not
    # guarantee a healthy stamp at the end. RetentionError has no registered
    # HTTP handler today, so letting it propagate 500s a fully successful,
    # already-committed grade; log-and-continue instead.
    done_ts = datetime.now(UTC)
    try:
        await store.stamp_graded_at(attempt_id=attempt.id, ts=done_ts)
    except (KGUnavailableError, RetentionError) as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=stamp_graded_at attempt_id=%s error=%s",
            attempt.id,
            exc,
        )

    # The student-facing payload is constructed from OLD-path values ONLY,
    # EXCEPT for `rubric`, which is `served_rubric` — byte-identical to
    # `rubric` (same object) unless `topic_score` computed successfully, in
    # which case `overall` is the topic score/letter (spec §3). The shadow
    # result is NEVER merged into it (WU-4C1).
    student_response = {
        "rubric": served_rubric,
        "diagnostic_narrative": diagnostic_narrative,
        "coverage": coverage,
        # Item #9: structured progress envelope is the single source of
        # truth for level / threshold display. Flat fields stay during
        # the FE migration window so older clients still render.
        "progress": _progress_block(envelope),
        "xp_earned": envelope.xp_earned,
        "xp_before": envelope.xp_before,
        "xp_after": envelope.xp_after,
        "level_before": envelope.level_before,
        "level_after": envelope.level_after,
        "level_up": envelope.level_up,
    }
    # Spec §3: `student_response["topics"]` is served whenever the topic score
    # computed successfully — same shape as the artifact's
    # `scores.topic_score.topics` (serialize_topics is the single shared
    # serializer, `topic_score_serialize.py`). Absent (not null) otherwise.
    if serve_topic_score:
        student_response["topics"] = serialize_topics(topic_score)
        if feedback is not None:
            student_response["feedback"] = feedback

    student_response["transcript"] = await _fetch_attempt_transcript(db, int(attempt.id))

    # Canonical transcript/topic artifact capture — written on every Done.
    # Task B1 — student scorecard projection (spec §2). Additive
    # `student_response["scorecard"]` key: `write_artifacts` returns the
    # CANONICAL payload it just persisted — the exact grade the student was
    # served — and `render_scorecard` is a pure template over it (no
    # recomputation; spec §3 step 3). A failed artifact write returns `None`,
    # so no scorecard is attached rather than templating over a payload that
    # was never durable.
    artifact_latency_ms = int((time.monotonic() - _artifact_t0) * 1000)
    canonical_payload = await write_artifacts(
        db,
        attempt=attempt,
        sess=sess,
        coverage=coverage,
        rubric=rubric,
        latency_ms=artifact_latency_ms,
        topic_score=topic_score,
    )
    if canonical_payload is not None:
        student_response["scorecard"] = render_scorecard(canonical_payload)
        # Task B2 — mastery ledger projection (spec section 2/3). Guarded off
        # whenever the dormant WU-5A2 Bayesian path is live (see
        # `_project_mastery`'s docstring): the two write paths must never
        # both fire for the same attempt.
        if _graph_sim_layer3_enabled():
            _LOG.info(
                "mastery_projection_skipped_layer3_active attempt_id=%s",
                int(attempt.id),
            )
        else:
            await _project_mastery(db, attempt_id=int(attempt.id))

    # The topic/dock payload (which quotes the student) ships whenever the
    # topic score computed successfully.
    serialized_topics = serialize_topics(topic_score) if serve_topic_score else []
    grading_provenance: dict[str, Any] = {
        "grader_used": "llm_transcript",
        "evidence_source": "transcript",
        "score_before_dock": (topic_score.coverage_component if topic_score is not None else None),
        "topics": serialized_topics,
        "docks": [
            {
                "key": misconception["canonical_key"],
                "points": misconception["dock_points"],
                "evidence_span": misconception["evidence_span"],
                "resolved": misconception["resolved"],
            }
            for topic in serialized_topics
            for misconception in topic["misconceptions"]
        ],
        # INTERACTION2 eval hook — additive key, invisible to the UI until it
        # opts in. Always present (`used: false` when the flag is off, the
        # bundle is NULL, or nothing survived the student-safe filter) so a
        # replay can diff grounded vs ungrounded grades from the payload alone.
        "grounding": grounding_provenance(course_evidence),
        "graph_lane": None,
        # INTERACTION4 hint-lane provenance (additive; brief: "Hint usage
        # count lands in grading_provenance"). 0 whenever the flag is off or
        # unused this session — never affects the score itself.
        "reference_question_asides_used": int(
            (getattr(sess, "metadata_", None) or {}).get(ASIDE_COUNT_SESSION_METADATA_KEY, 0)
        ),
    }

    # INTERACTION5 provenance (additive; absent otherwise) — present ONLY when the
    # gate was on AND asides were actually fetched this Done, mirroring how
    # "grounding" is emitted. `assisted_node_ids` is the set of rubric nodes the
    # cap lowered (empty if nothing matched, or if the cap pass soft-failed and
    # grading proceeded uncapped). Off / no asides → key absent, provenance
    # byte-identical to pre-feature.
    if aside_cap_active and hoot_asides:
        grading_provenance["aside_penalty"] = {
            "enabled": True,
            "cap": _ASIDE_CREDIT_CAP,
            "assisted_node_ids": list(aside_assisted_ids),
        }
    student_response["grading_provenance"] = grading_provenance

    return student_response
