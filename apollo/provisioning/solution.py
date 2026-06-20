"""WU-3B2e stage 2 — find-or-generate a reference solution for a scraped question.

For each ``CandidateQuestion`` (3B2d), ``find_or_generate`` retrieve-first-then-
RAG-generate a reference solution and returns a ``ReferenceSolutionDraft``:

  * **extracted** branch — if a retrieved span carries a printed/worked solution
    (the retrieve adapter flags it ``carries_solution=True``), an extraction
    ``chat_fn`` pass parses that span into a ``reference_solution`` →
    ``solution_source='extracted'``, ``grounding`` = the retrieved spans.
  * **generated** branch — otherwise ``chat_fn`` RAG-generates the solution from
    the question + the retrieved spans-as-context → ``solution_source='generated'``,
    ``grounding`` = the SAME retrieved spans (so the stage-3 Phase-B faithfulness
    judge has real context to entail against — the §8B.2 "ground both in
    retrieved passages" requirement).

FAIL-CLOSED (the inversion of the §6 leakage judge): a malformed/empty generate
with NO usable extracted solution raises ``SolutionDraftError`` — NEVER an
empty-step draft (``Problem`` requires ``reference_solution`` min_length=1). The
generated ``reference_solution`` is validated against the ``Problem`` schema
BEFORE a draft is returned, so a half-valid solution never reaches the gate.

``build_approved_pair`` assembles the REAL ``tag_mint.ApprovedPair`` (IMPORTED,
never redefined) the 3B2g caller feeds into ``tag_and_mint``; its ``problem`` dict
is ``Problem.model_validate``-able (round-trip pinned in the tests).

INPUTS ARE COURSE MATERIAL ONLY — the scraped question + retrieved course
passages + the generated solution are academic content; this unit reads NO
learner state and never sees student PII.

§1.8 / OPS-6 CAVEAT (recorded here + in apollo.md): a coherent-but-WRONG solution
that PASSES the stage-3 gate + all 8 promotion gates IS still shown to students
as teachable (shadow diagnostic, NO Layer-3 belief movement — the
``APOLLO_GRAPH_SIM_LAYER3_ENABLED`` flag is OFF). The pairing gate REDUCES but
does NOT eliminate this case; the real pre-exposure safety is the
``APOLLO_AUTOPROVISION_ENABLED`` flag-OFF default + the §6.7 calibration gate, and
3B2h quarantine is the retroactive catch. This unit must not claim to prevent the
fabricated-coherent-wrong case.

NO network: ``retrieve_fn``/``chat_fn`` are injected callables (mocked in
Tier-1). NO DB write (pure compute over the injected fns; ``db`` is accepted for
signature parity / future reads, unused in v1).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apollo.provisioning.tag_mint import ApprovedPair
from apollo.schemas.problem import Problem

__all__ = [
    "GroundingSpan",
    "ReferenceSolutionDraft",
    "SolutionDraftError",
    "find_or_generate",
    "solution_hash",
    "build_approved_pair",
]


class SolutionDraftError(RuntimeError):
    """Raised when a draft cannot be built without guessing (retrieve empty AND
    the generate response is unparseable / yields an empty or non-``Problem``-valid
    ``reference_solution``). FAIL-CLOSED — the caller (3B2g) marks the run failed
    and writes ``apollo_ingest_errors(stage='find_or_generate')``; NEVER an
    empty-step draft."""


class GroundingSpan(BaseModel):
    """One retrieved course passage the generator/judge grounds against. Frozen
    (immutable) — provenance mirrors the scrape's content key. ``text`` is
    course-material text only (NO PII). ``carries_solution`` is the deterministic
    marker the retrieve adapter (3B2g) sets when the span holds a printed/worked
    solution (the extracted-branch signal)."""

    model_config = ConfigDict(frozen=True)

    text: str
    document_id: int | None = None
    page: int | None = None
    chunk_content_hash: str | None = None
    carries_solution: bool = False


class ReferenceSolutionDraft(BaseModel):
    """The stage-2 output type. ``reference_solution`` is a list of
    ``ReferenceStep``-shaped dicts (``Problem``-validatable); ``grounding`` is the
    spans used (the extracted source OR the generate context); ``provenance``
    threads the idempotency key (``chunk_content_hash``) + the retrieval hit
    count."""

    solution_source: Literal["extracted", "generated"]
    reference_solution: list[dict]
    grounding: tuple[GroundingSpan, ...] = ()
    provenance: dict = Field(default_factory=dict)

    @field_validator("grounding", mode="before")
    @classmethod
    def _coerce_grounding(cls, v: Any) -> Any:
        """Accept a list/tuple of spans interchangeably (immutable tuple stored)."""
        if isinstance(v, list):
            return tuple(v)
        return v


def _canonical_json(value: Any) -> str:
    """Deterministic canonical JSON (sorted keys, no whitespace drift) for hashing
    — the same value hashes identically across runs/processes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def solution_hash(draft: ReferenceSolutionDraft) -> str:
    """``sha256`` over the canonical-JSON of ``draft.reference_solution`` — the
    downstream idempotency key component (3B2g keys the write on
    ``(chunk_content_hash, solution_hash)``). Deterministic + path-independent:
    an equal ``reference_solution`` hashes identically; a different one differs."""
    blob = _canonical_json(draft.reference_solution)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_reference_solution(raw: str) -> list[dict] | None:
    """Parse a ``chat_fn`` response into a ``reference_solution`` list. Returns
    ``None`` on a non-JSON / non-object response or a missing/non-list
    ``reference_solution`` (the caller maps ``None`` to the fail-closed path)."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    steps = parsed.get("reference_solution")
    if not isinstance(steps, list):
        return None
    return steps


def _validate_problem_shape(
    question: Any, reference_solution: list[dict]
) -> None:
    """Validate the (question + ``reference_solution``) against the ``Problem``
    schema BEFORE returning a draft. A malformed/empty solution raises
    ``SolutionDraftError`` (fail-closed) — never a half-built draft reaches the
    gate. ``Problem`` enforces ``reference_solution`` min_length=1, depends_on
    resolution, and the procedure-step order contract."""
    try:
        Problem.model_validate(_problem_dict(question, reference_solution))
    except Exception as exc:  # noqa: BLE001 - any validation failure → fail-closed
        raise SolutionDraftError(
            f"generated reference_solution is not Problem-valid: {exc}"
        ) from exc


def _problem_dict(question: Any, reference_solution: list[dict]) -> dict:
    """Assemble a ``Problem``-shaped dict from a ``CandidateQuestion`` (duck-typed)
    + a ``reference_solution``. ``id``/``concept_id`` are derived from the
    question's provenance (content-hash keyed, deterministic)."""
    chunk_hash = getattr(question, "chunk_content_hash", "") or ""
    concept_slug = getattr(question, "concept_slug", "") or "provisional.inventory"
    return {
        "id": f"scrape.{chunk_hash}",
        "concept_id": concept_slug,
        "difficulty": getattr(question, "difficulty", "intro"),
        "problem_text": getattr(question, "problem_text", ""),
        "given_values": dict(getattr(question, "given_values", {}) or {}),
        "target_unknown": getattr(question, "target_unknown", ""),
        "reference_solution": reference_solution,
    }


def _provenance(
    question: Any, *, retrieval_hits: int
) -> dict:
    """Build the draft provenance from the question's content key + the retrieval
    hit count (the idempotency key threads through unchanged)."""
    return {
        "document_id": getattr(question, "document_id", None),
        "page": getattr(question, "page", None),
        "chunk_content_hash": getattr(question, "chunk_content_hash", None),
        "retrieval_hits": retrieval_hits,
    }


def _spans_as_context(spans: Sequence[GroundingSpan]) -> str:
    """Render the retrieved spans as a single context blob for the generate /
    extraction prompt (course material only — no PII)."""
    return "\n\n".join(s.text for s in spans)


async def find_or_generate(
    db,
    question,
    *,
    retrieve_fn: Callable[..., Awaitable[Sequence[GroundingSpan]]],
    chat_fn: Callable[..., str],
) -> ReferenceSolutionDraft:
    """Retrieve-first-then-RAG-generate a reference solution. See the module
    docstring for the full contract. ``db`` is accepted for signature parity /
    future reads (unused in v1 compute).

    Calls ``retrieve_fn(question)`` once. If any retrieved span carries a printed
    solution (``carries_solution``), an extraction ``chat_fn`` pass over those
    spans yields the ``reference_solution`` → ``solution_source='extracted'``.
    Otherwise ``chat_fn`` RAG-generates from the question + the retrieved spans →
    ``solution_source='generated'``, ``grounding`` = the SAME retrieved spans.
    FAIL-CLOSED via ``SolutionDraftError`` (no empty-step draft)."""
    spans = tuple(await retrieve_fn(question))
    grounding = spans
    context = _spans_as_context(spans)
    has_printed = any(s.carries_solution for s in spans)

    if has_printed:
        source: Literal["extracted", "generated"] = "extracted"
        raw = chat_fn(
            purpose="solution_extract",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the worked reference solution from the provided "
                        "course passages into a JSON object with a "
                        "'reference_solution' list of typed steps."
                    ),
                },
                {
                    "role": "user",
                    "content": _canonical_json(
                        {
                            "problem_text": getattr(question, "problem_text", ""),
                            "context": context,
                        }
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    else:
        source = "generated"
        raw = chat_fn(
            purpose="solution_generate",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Using ONLY the provided course passages, produce a "
                        "reference solution as a JSON object with a "
                        "'reference_solution' list of typed steps."
                    ),
                },
                {
                    "role": "user",
                    "content": _canonical_json(
                        {
                            "problem_text": getattr(question, "problem_text", ""),
                            "given_values": dict(
                                getattr(question, "given_values", {}) or {}
                            ),
                            "target_unknown": getattr(
                                question, "target_unknown", ""
                            ),
                            "context": context,
                        }
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )

    reference_solution = _parse_reference_solution(raw)
    if not reference_solution:
        raise SolutionDraftError(
            "no usable reference_solution: the generate/extract response was "
            "unparseable or empty (fail-closed, never an empty-step draft)"
        )

    _validate_problem_shape(question, reference_solution)

    return ReferenceSolutionDraft(
        solution_source=source,
        reference_solution=reference_solution,
        grounding=grounding,
        provenance=_provenance(question, retrieval_hits=len(spans)),
    )


def build_approved_pair(
    question, draft: ReferenceSolutionDraft, *, search_space_id: int
) -> ApprovedPair:
    """Assemble the REAL ``tag_mint.ApprovedPair`` from an approved
    (question, draft). The ``problem`` dict is ``Problem``-validatable;
    ``solution_source`` is carried from the draft; ``misconceptions=[]`` (3B2d/3B2g
    may enrich later); ``search_space_id`` is supplied by the caller (3B2g) from
    the job scope. IMPORTS the real ``ApprovedPair`` — never mocked/redefined."""
    problem = _problem_dict(question, draft.reference_solution)
    return ApprovedPair(
        problem=problem,
        search_space_id=search_space_id,
        solution_source=draft.solution_source,
        misconceptions=[],
    )
