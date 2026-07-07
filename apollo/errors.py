"""Named exception types for Apollo.

Every failure mode gets its own exception class. No fallbacks — every
raised exception surfaces as a visible error in the UI via the FastAPI
exception handlers registered in apollo/api.py.
"""
from __future__ import annotations


class ApolloError(Exception):
    """Base class for all Apollo-specific exceptions."""


class ParserCouldNotExtractError(ApolloError):
    """Parser returned zero entries from a non-trivial teaching utterance."""

    def __init__(self, utterance: str) -> None:
        self.utterance = utterance
        super().__init__(f"Parser could not extract any entries from: {utterance!r}")


class FilterRejectedError(ApolloError):
    """Output filter rejected Apollo's draft because it contained a term
    the student has not introduced. NO FALLBACK — surfaces as UI error."""

    def __init__(self, rejected_term: str, draft: str, kg: dict | None = None) -> None:
        self.rejected_term = rejected_term
        self.draft = draft
        # The live per-attempt KG at rejection time. Attached by the chat
        # handler so the 422 carries it and the FE can refresh "Apollo's
        # Understanding" instead of showing a stale/empty panel.
        self.kg = kg
        super().__init__(
            f"Apollo's draft was rejected by the output filter: contained "
            f"out-of-allowlist term {rejected_term!r}"
        )


class MalformedEquationError(ApolloError):
    """A KG equation entry could not be parsed by SymPy. Solver halts
    immediately; does not silently skip."""

    def __init__(self, entry_id: str, symbolic: str, parse_error: str) -> None:
        self.entry_id = entry_id
        self.symbolic = symbolic
        self.parse_error = parse_error
        super().__init__(
            f"KG entry {entry_id!r} has malformed equation {symbolic!r}: {parse_error}"
        )


class NoMatchingConceptError(ApolloError):
    """Overseer.concept_inference could not match the Hoot transcript
    to any concept cluster Apollo has problems for. Returns 409 to frontend."""

    def __init__(self, transcript_summary: str) -> None:
        self.transcript_summary = transcript_summary
        super().__init__(f"No matching concept for transcript: {transcript_summary!r}")


class PoolExhaustedError(ApolloError):
    """Problem pool at the requested difficulty has no unattempted problems."""

    def __init__(self, concept_cluster_id: str, difficulty: str) -> None:
        self.concept_cluster_id = concept_cluster_id
        self.difficulty = difficulty
        super().__init__(
            f"Problem pool exhausted for cluster {concept_cluster_id!r} "
            f"at difficulty {difficulty!r}"
        )


class ProblemNotFoundError(ApolloError):
    """Standalone session entry named a problem_id that is not in the
    concept's teachable pool (bad id, tier-1, quarantined, or another
    concept's problem). Surfaces as 404 to the FE."""

    def __init__(self, *, problem_id: str, concept_id: int) -> None:
        self.problem_id = problem_id
        self.concept_id = concept_id
        super().__init__(
            f"Problem {problem_id!r} not found in teachable pool of concept {concept_id}"
        )


class SessionFrozenError(ApolloError):
    """Attempted KG write on a frozen session."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id!r} is frozen; writes rejected")


class InvalidPhaseError(ApolloError):
    """Endpoint called while the session is in a phase that forbids it."""

    def __init__(self, session_id: int, phase: str) -> None:
        self.session_id = session_id
        self.phase = phase
        super().__init__(
            f"cannot perform this action while session {session_id} is in phase {phase!r}"
        )


class ReviewRequiredError(ApolloError):
    """P3 OLM Done-gate (P3.6). The student tried to submit Done while the
    KG had flagged entries (low parser_confidence or DISPUTED) that have
    not been touched with a negotiation move. The frontend renders a
    review modal; the student must clear each flag before re-submitting.

    Surfaces as 422 with `error_code: "review_required"` and a list of
    `entries` shaped as:
        {entry_id, type, reason: "low_confidence" | "disputed", summary}
    """

    def __init__(self, *, entries: list[dict]) -> None:
        self.entries = entries
        super().__init__(
            f"{len(entries)} KG entries need review before grading"
        )


class KGEntryNotFoundError(ApolloError):
    """Negotiation move targeted a KG entry that does not exist in the
    per-attempt subgraph. P3 — Negotiable OLM. Surfaces as 404 to the FE."""

    def __init__(self, *, attempt_id: int, node_id: str) -> None:
        self.attempt_id = attempt_id
        self.node_id = node_id
        super().__init__(
            f"KG entry {node_id!r} not found in attempt {attempt_id}"
        )


class CoverageGradingError(ApolloError):
    """Coverage matcher exhausted retries on a transient failure.

    Item #10: replaces the V2 soft-fail to "missing" with a named error so
    the UI can surface "grading unavailable, try again" instead of silently
    downgrading the grade. NO FALLBACK.
    """

    def __init__(self, *, stage: str, last_error: str) -> None:
        self.stage = stage
        self.last_error = last_error
        super().__init__(
            f"Coverage grading failed at stage {stage!r}: {last_error}"
        )


class CanonProjectionError(ApolloError):
    """The :Canon projection seeder hit an infrastructure failure (Neo4j
    unreachable / write failed, or the Postgres entity read failed
    mid-projection). NO FALLBACK: a failed projection must surface, never
    silently leave a partial :Canon graph that grading would later read as
    authoritative. `stage` is "load_entities" | "merge_canon" (WU-3C1)."""

    def __init__(self, *, stage: str, last_error: str) -> None:
        self.stage = stage
        self.last_error = last_error
        super().__init__(
            f"Canon projection failed at stage {stage!r}: {last_error}"
        )


class RetentionError(ApolloError):
    """A retention-critical Neo4j write failed in a way that must NOT be
    swallowed — specifically `stamp_graded_at` failing at Done. The frozen
    graph must carry `graded_at` or the janitor / Layer-3 Δt timeline is
    wrong. The grade itself is already committed when this raises, so it
    surfaces (NO FALLBACK) without voiding the student's grade; the next
    Done / retry / janitor re-stamps idempotently (WU-3C1)."""

    def __init__(self, *, attempt_id: int, last_error: str) -> None:
        self.attempt_id = attempt_id
        self.last_error = last_error
        super().__init__(
            f"Retention operation failed for attempt {attempt_id}: {last_error}"
        )


# Closed reason set for LearnerUpdateUnreconstructableError. The janitor's
# pre-flight (WU-5B3a build_rerun_inputs) raises with exactly one of these.
LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS = (
    "diagnostic_report_missing",  # attempt.diagnostic_report is None
    "rubric_missing",             # "rubric" absent, or rubric lacks "overall"
    "graded_at_missing",          # Neo4j read_node_graded_at returned {} (empty subgraph)
)


class LearnerUpdateUnreconstructableError(ApolloError):
    """The Done-time re-run inputs cannot be reconstructed from durable state,
    so the learner-model retry can NEVER succeed — a TERMINAL dead-letter, not a
    transient failure. Raised by `build_rerun_inputs`'s pre-flight BEFORE any LLM
    call when `attempt.diagnostic_report` is None / lacks `rubric` / the rubric
    lacks `overall` (calibration.py:88,93 dereference it unconditionally) OR the
    frozen Neo4j subgraph has no `graded_at` (empty/never-stamped → no done_ts to
    anchor the belief Δt). The janitor (WU-5B3a-1) catches this and marks the row
    `learner_update_failed_permanently` (no backoff, no LLM, never crash-loops).
    Mirrors RetentionError's shape. `reason` is one of
    LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS."""

    def __init__(self, *, attempt_id: int, reason: str) -> None:
        self.attempt_id = attempt_id
        self.reason = reason
        super().__init__(
            f"learner update unreconstructable for attempt {attempt_id}: {reason}"
        )


class ResolutionUnavailableError(ApolloError):
    """Resolver INFRASTRUCTURE failure (a resolver LLM call failed / timed out,
    or a Neo4j ``RESOLVES_TO`` / resolution-field write failed).

    NO FALLBACK and — critically — must NOT void the earned grade: at Done the
    grade/XP are already committed when resolution runs, so this surfaces loud
    while the caller (WU-4A's Done orchestrator) sets
    ``learner_update_pending = true`` on the attempt and the next Done / janitor
    retry re-runs resolution idempotently (§5 NO-FALLBACK, §6.4 transaction
    story). ``stage`` is one of
    ``{"llm_adjudication", "write_resolves_to", "persist_fields",
    "clarification_rescore"}`` (WU-3C2; ``clarification_rescore`` is the
    re-scorer stage added by the clarification loop)."""

    def __init__(self, *, stage: str, last_error: str) -> None:
        self.stage = stage
        self.last_error = last_error
        super().__init__(
            f"Resolution unavailable at stage {stage!r}: {last_error}"
        )


class TranscriptAuditUnavailableError(ApolloError):
    """Transcript-audit INFRASTRUCTURE failure (the one batched Done-time audit
    ``main_chat`` call failed / timed out or returned malformed JSON). A VALID
    but empty ``{"spans": {}}`` reply is "the student taught none of them" (all
    not-found) — it does NOT raise; only a transient/parse failure does.

    NO FALLBACK and — critically — the auditor NEVER degrades to "skip the audit
    and emit the missing finding". The orchestrator (WU-4B1
    ``build_audited_grade``) catches this at the audit boundary and converts it
    into the suppress-ALL-``missing`` abstention reason, so a ``missing`` event
    can never survive a failed audit. Mirrors :class:`ResolutionUnavailableError`
    (named-but-not-HTTP-registered here; WU-4C registers the handler). ``stage``
    is ``"transcript_audit"`` for symmetry with the other infra errors."""

    def __init__(self, *, last_error: str) -> None:
        self.stage = "transcript_audit"
        self.last_error = last_error
        super().__init__(
            f"Transcript audit unavailable at stage {self.stage!r}: {last_error}"
        )


class ResolutionInvalidOutputError(ApolloError):
    """The one LLM adjudication call returned a key that is NOT in the closed
    candidate set (a hallucination).

    Hard error (§5) — the resolver must never fabricate a resolution target.
    Carries the offending ``returned_key`` plus the ``allowed_keys`` (the closed
    candidate set) for the audit log (WU-3C2)."""

    def __init__(self, *, returned_key: str, allowed_keys: tuple[str, ...]) -> None:
        self.returned_key = returned_key
        self.allowed_keys = allowed_keys
        super().__init__(
            f"Resolution adjudication returned hallucinated key {returned_key!r} "
            f"not in the candidate set ({len(allowed_keys)} allowed keys)"
        )
