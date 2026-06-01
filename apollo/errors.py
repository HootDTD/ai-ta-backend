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

    def __init__(self, rejected_term: str, draft: str) -> None:
        self.rejected_term = rejected_term
        self.draft = draft
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
