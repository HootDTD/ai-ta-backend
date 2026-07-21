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
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apollo.persistence.learner_model_seed import _ENTRY_TYPE_TO_KIND_PREFIX
from apollo.provisioning.generation_contract import ontology_block
from apollo.provisioning.provisioning_schema import build_solution_schema
from apollo.provisioning.tag_mint import ApprovedPair
from apollo.schemas.problem import Problem

_LOG = logging.getLogger(__name__)

__all__ = [
    "GroundingSpan",
    "ReferenceSolutionDraft",
    "SolutionDraftError",
    "find_or_generate",
    "construct_authored_reference",
    "build_authored_approved_pair",
    "solution_hash",
    "build_approved_pair",
]


# --------------------------------------------------------------------------- #
# Schema-explicit Stage-2 system prompts (the prompt↔parser contract).
#
# Each prompt keeps its own FRAMING + top-level-key envelope; the shared
# per-step contract is sourced ONCE from ``generation_contract.ontology_block``
# (DAG-3) so a rule added there propagates to EVERY generation prompt and the
# prose can never drift from ``NODE_CONTENT_TYPES``.
# --------------------------------------------------------------------------- #
_SOLUTION_OUTPUT_CONTRACT = (
    'Output a single JSON object whose key "reference_solution" is a NON-EMPTY '
    'array of step objects (plus, optionally, the "symbol_table" key described '
    "below). No other keys.\n" + ontology_block()
)

_SOLUTION_EXTRACT_SYSTEM_PROMPT = (
    "Extract the worked reference solution from the provided course passages.\n"
    + _SOLUTION_OUTPUT_CONTRACT
)

_SOLUTION_GENERATE_SYSTEM_PROMPT = (
    "Using ONLY the provided course passages, produce the reference solution.\n"
    + _SOLUTION_OUTPUT_CONTRACT
)

_SOLUTION_GENERATE_AUGMENT_INSTRUCTION = (
    "RECALL QUESTIONS -- explain-why extension:\n"
    "If the problem only asks to recall, define, state, list, or identify something "
    "(or is a declarative statement rather than a question), EXTEND it into an "
    "explain-why problem instead of producing a definition-only solution:\n"
    '  * Rewrite the problem so it ALSO asks WHY (for example, "Define X and explain '
    'why it occurs"); return the rewritten text as "augmented_problem_text" and the '
    'new target as "augmented_target_unknown".\n'
    "  * The reference_solution MUST then include procedure_step entries carrying "
    "the explanation's reasoning.\n"
    "  * Ground the WHY in the provided passages and general reasoning of the "
    "discipline. NEVER fabricate course-specific claims: never write phrases like "
    '"according to the course" or "as discussed in class", and never attribute a '
    "position to the course or instructor. If the passages state no reason, argue "
    "from first principles of the discipline.\n"
    "If the problem already requires reasoning, return null for both augmented "
    "fields and do not rewrite it.\n"
)

_SOLUTION_GENERATE_AUGMENT_OUTPUT_CONTRACT = (
    'Output a single JSON object with the keys "reference_solution" (a NON-EMPTY '
    'array of step objects), "augmented_problem_text", and '
    '"augmented_target_unknown" (the two augmented fields each a string or '
    'null), plus, optionally, the "symbol_table" key described below. No other '
    "keys.\n" + ontology_block()
)

_SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT = (
    "Using ONLY the provided course passages, produce the reference solution.\n"
    + _SOLUTION_GENERATE_AUGMENT_INSTRUCTION
    + _SOLUTION_GENERATE_AUGMENT_OUTPUT_CONTRACT
)

_SOLUTION_AUGMENT_RETRY_DIRECTIVE = (
    "Your draft contained no procedure_step entries, so it cannot be used as a "
    "reference. Apply the explain-why extension now: rewrite the problem to also "
    "ask WHY (fill augmented_problem_text / augmented_target_unknown) and produce "
    "procedure_step entries carrying the reasoning, following every rule above."
)


class SolutionDraftError(RuntimeError):
    """Raised when a draft cannot be built without guessing (retrieve empty AND
    the generate response is unparseable / yields an empty or non-``Problem``-valid
    ``reference_solution``). FAIL-CLOSED — the caller (3B2g) marks the run failed
    and writes a content-ingest error at ``find_or_generate``; NEVER an
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

    solution_source: Literal["extracted", "generated", "authored", "llm_paired"]
    reference_solution: list[dict]
    grounding: tuple[GroundingSpan, ...] = ()
    provenance: dict = Field(default_factory=dict)
    augmented_problem_text: str | None = None
    augmented_target_unknown: str | None = None

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


def _parse_generated(raw: str) -> tuple[list[dict] | None, str | None, str | None]:
    """Parse the augmentation envelope, normalizing malformed optional fields."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None, None
    if not isinstance(parsed, dict):
        return None, None, None
    steps = parsed.get("reference_solution")
    if not isinstance(steps, list):
        return None, None, None
    augmented_text = parsed.get("augmented_problem_text")
    augmented_target = parsed.get("augmented_target_unknown")
    return (
        steps,
        augmented_text if isinstance(augmented_text, str) and augmented_text.strip() else None,
        augmented_target
        if isinstance(augmented_target, str) and augmented_target.strip()
        else None,
    )


def _has_procedure_step(steps: list[dict]) -> bool:
    return any(
        isinstance(step, dict) and step.get("entry_type") == "procedure_step" for step in steps
    )


def _parse_symbol_table(raw: str) -> dict | None:
    """Extract the OPTIONAL top-level ``symbol_table`` object (DAG-3); ``None``
    when absent or malformed — an absent table is legacy content, never an error."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    table = parsed.get("symbol_table") if isinstance(parsed, dict) else None
    return table if isinstance(table, dict) else None


# DAG-3 defect-retry harness depth: the initial draft plus up to this many
# feedback retries. Mirrors the derivation path's detect→retry loop; on
# exhaustion the draft is FLAGGED (provenance) and returned, never crashed —
# generated solutions are auto-held for teacher review downstream (PR #127).
_GENERATION_DEFECT_RETRIES = 2


def _generation_defects(
    question: Any,
    reference_solution: list[dict],
    symbol_table: dict | None,
    *,
    problem_text: str | None,
    target_unknown: str | None,
) -> list[str]:
    """Run the vocabulary-independent defect classes over a draft.

    Imports lazily: ``graph_derivation`` imports this module at module level,
    so the reverse import must happen at call time. ``find_or_generate`` has no
    concept vocabulary, hence ``GENERATION_DEFECT_CLASSES`` (no foreign_symbol,
    no node_count) — every symbolic class self-deactivates on prose drafts."""
    from apollo.provisioning.authored_sets.graph_derivation import (
        GENERATION_DEFECT_CLASSES,
        find_derivation_defects,
    )

    graph = _problem_dict(
        question,
        reference_solution,
        problem_text=problem_text,
        target_unknown=target_unknown,
    )
    if symbol_table is not None:
        graph["symbol_table"] = symbol_table
    return find_derivation_defects(
        graph,
        canonical_symbols={},
        normalization_map={},
        classes=GENERATION_DEFECT_CLASSES,
    )


def _validate_problem_shape(
    question: Any,
    reference_solution: list[dict],
    *,
    problem_text: str | None = None,
    target_unknown: str | None = None,
) -> None:
    """Validate the (question + ``reference_solution``) against the ``Problem``
    schema BEFORE returning a draft. A malformed/empty solution raises
    ``SolutionDraftError`` (fail-closed) — never a half-built draft reaches the
    gate. ``Problem`` enforces ``reference_solution`` min_length=1, depends_on
    resolution, and the procedure-step order contract."""
    try:
        Problem.model_validate(
            _problem_dict(
                question,
                reference_solution,
                problem_text=problem_text,
                target_unknown=target_unknown,
            )
        )
    except Exception as exc:  # noqa: BLE001 - any validation failure → fail-closed
        raise SolutionDraftError(
            f"generated reference_solution is not Problem-valid: {exc}"
        ) from exc


def _problem_dict(
    question: Any,
    reference_solution: list[dict],
    *,
    problem_text: str | None = None,
    target_unknown: str | None = None,
) -> dict:
    """Assemble a ``Problem``-shaped dict from a ``CandidateQuestion`` (duck-typed)
    + a ``reference_solution``. ``id``/``concept_id`` are derived from the
    question's provenance (content-hash keyed, deterministic)."""
    chunk_hash = getattr(question, "chunk_content_hash", "") or ""
    concept_slug = getattr(question, "concept_slug", "") or "provisional.inventory"
    return {
        "id": f"scrape.{chunk_hash}",
        "concept_id": concept_slug,
        "difficulty": getattr(question, "difficulty", "intro"),
        "problem_text": (
            problem_text if problem_text is not None else getattr(question, "problem_text", "")
        ),
        "given_values": dict(getattr(question, "given_values", {}) or {}),
        "target_unknown": (
            target_unknown
            if target_unknown is not None
            else getattr(question, "target_unknown", "")
        ),
        "reference_solution": reference_solution,
    }


def _provenance(question: Any, *, retrieval_hits: int) -> dict:
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
    augment_recall: bool = False,
) -> ReferenceSolutionDraft:
    """Retrieve-first-then-RAG-generate a reference solution. See the module
    docstring for the full contract. ``db`` is accepted for signature parity /
    future reads (unused in v1 compute).

    Calls ``retrieve_fn(question)`` once. If any retrieved span carries a printed
    solution (``carries_solution``), an extraction ``chat_fn`` pass over those
    spans yields the ``reference_solution`` → ``solution_source='extracted'``
    for deterministic retrieval or ``'llm_paired'`` for a structure-pass pair.
    Otherwise ``chat_fn`` RAG-generates from the question + the retrieved spans →
    ``solution_source='generated'``, ``grounding`` = the SAME retrieved spans.
    FAIL-CLOSED via ``SolutionDraftError`` (no empty-step draft)."""
    spans = tuple(await retrieve_fn(question))
    grounding = spans
    context = _spans_as_context(spans)
    has_printed = any(s.carries_solution for s in spans)

    if has_printed:
        source: Literal["extracted", "generated", "llm_paired"] = (
            "llm_paired"
            if getattr(retrieve_fn, "last_match_method", None) == "structure"
            else "extracted"
        )
        harness_purpose = "solution_extract"
        harness_schema = build_solution_schema()
        harness_messages = [
            {
                "role": "system",
                "content": _SOLUTION_EXTRACT_SYSTEM_PROMPT,
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
        ]
        raw = chat_fn(
            purpose=harness_purpose,
            messages=harness_messages,
            response_format={"type": "json_schema", "json_schema": harness_schema},
            temperature=0.0,
        )
        reference_solution = _parse_reference_solution(raw)
        augmented_text = None
        augmented_target = None
        augmented = False
    else:
        source = "generated"
        gen_messages = [
            {
                "role": "system",
                "content": (
                    _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT
                    if augment_recall
                    else _SOLUTION_GENERATE_SYSTEM_PROMPT
                ),
            },
            {
                "role": "user",
                "content": _canonical_json(
                    {
                        "problem_text": getattr(question, "problem_text", ""),
                        "given_values": dict(getattr(question, "given_values", {}) or {}),
                        "target_unknown": getattr(question, "target_unknown", ""),
                        "context": context,
                    }
                ),
            },
        ]
        gen_schema = build_solution_schema(augmentation=augment_recall)
        harness_purpose = "solution_generate"
        harness_schema = gen_schema
        harness_messages = gen_messages
        raw = chat_fn(
            purpose="solution_generate",
            messages=gen_messages,
            response_format={"type": "json_schema", "json_schema": gen_schema},
            temperature=0.0,
        )
        if augment_recall:
            reference_solution, augmented_text, augmented_target = _parse_generated(raw)
            if reference_solution and not _has_procedure_step(reference_solution):
                retry_raw = chat_fn(
                    purpose="solution_generate",
                    messages=[
                        *gen_messages,
                        {"role": "user", "content": _SOLUTION_AUGMENT_RETRY_DIRECTIVE},
                    ],
                    response_format={"type": "json_schema", "json_schema": gen_schema},
                    temperature=0.0,
                )
                retry_steps, retry_text, retry_target = _parse_generated(retry_raw)
                if retry_steps and _has_procedure_step(retry_steps):
                    reference_solution = retry_steps
                    augmented_text = retry_text
                    augmented_target = retry_target
                    raw = retry_raw
            augmented = (
                bool(augmented_text)
                and bool(reference_solution)
                and _has_procedure_step(reference_solution or [])
            )
        else:
            reference_solution = _parse_reference_solution(raw)
            augmented_text = None
            augmented_target = None
            augmented = False

    if not reference_solution:
        raise SolutionDraftError(
            "no usable reference_solution: the generate/extract response was "
            "unparseable or empty (fail-closed, never an empty-step draft)"
        )

    # ------------------------------------------------------------------ #
    # DAG-3 defect-retry harness (ported from the derivation path): run the
    # vocabulary-independent defect classes over the draft and feed the
    # localized defect list back for up to _GENERATION_DEFECT_RETRIES more
    # attempts. On exhaustion the draft is FLAGGED via provenance and
    # returned (never crashed) — the schema-invalid case alone still
    # fail-closes below in _validate_problem_shape, exactly as before.
    # Prose drafts produce zero symbolic defects (self-deactivation).
    # ------------------------------------------------------------------ #
    symbol_table = _parse_symbol_table(raw)

    def _current_defects() -> list[str]:
        return _generation_defects(
            question,
            reference_solution,
            symbol_table,
            problem_text=augmented_text if augmented else None,
            target_unknown=(
                (augmented_target or getattr(question, "target_unknown", None))
                if augmented
                else None
            ),
        )

    generation_defects = _current_defects()
    defect_retries = 0
    while generation_defects and defect_retries < _GENERATION_DEFECT_RETRIES:
        defect_retries += 1
        feedback = (
            "Your previous draft failed mechanical validation. Fix ONLY these "
            "defects and re-emit the COMPLETE JSON object:\n- "
            + "\n- ".join(generation_defects[:8])
        )
        retry_raw = chat_fn(
            purpose=harness_purpose,
            messages=[
                *harness_messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": feedback},
            ],
            response_format={"type": "json_schema", "json_schema": harness_schema},
            temperature=0.0,
        )
        if augment_recall and not has_printed:
            retry_steps, retry_text, retry_target = _parse_generated(retry_raw)
            if retry_steps:
                reference_solution = retry_steps
                augmented_text = retry_text
                augmented_target = retry_target
                augmented = bool(retry_text) and _has_procedure_step(retry_steps)
                raw = retry_raw
                symbol_table = _parse_symbol_table(raw)
        else:
            retry_steps = _parse_reference_solution(retry_raw)
            if retry_steps:
                reference_solution = retry_steps
                raw = retry_raw
                symbol_table = _parse_symbol_table(raw)
        generation_defects = _current_defects()
    if generation_defects:
        _LOG.warning(
            "generation_defects_flagged",
            extra={
                "event": "generation_defects_flagged",
                "retries": defect_retries,
                "defects": generation_defects[:8],
            },
        )

    if not augmented:
        augmented_text = None
        augmented_target = None

    original_target = getattr(question, "target_unknown", None)
    effective_augmented_target = (augmented_target or original_target) if augmented else None
    _validate_problem_shape(
        question,
        reference_solution,
        problem_text=augmented_text if augmented else None,
        target_unknown=effective_augmented_target,
    )

    provenance = _provenance(question, retrieval_hits=len(spans))
    if augmented:
        provenance["augmented"] = "explain_why"
    if generation_defects:
        provenance["generation_defects"] = generation_defects[:8]
    if symbol_table:
        provenance["symbol_table"] = symbol_table

    return ReferenceSolutionDraft(
        solution_source=source,
        reference_solution=reference_solution,
        grounding=grounding,
        provenance=provenance,
        augmented_problem_text=augmented_text if augmented else None,
        augmented_target_unknown=effective_augmented_target,
    )


def build_approved_pair(
    question, draft: ReferenceSolutionDraft, *, search_space_id: int
) -> ApprovedPair:
    """Assemble the REAL ``tag_mint.ApprovedPair`` from an approved
    (question, draft). The ``problem`` dict is ``Problem``-validatable;
    ``solution_source`` is carried from the draft; ``misconceptions=[]`` (3B2d/3B2g
    may enrich later); ``search_space_id`` is supplied by the caller (3B2g) from
    the job scope. IMPORTS the real ``ApprovedPair`` — never mocked/redefined."""
    problem = _problem_dict(
        question,
        draft.reference_solution,
        problem_text=draft.augmented_problem_text,
        target_unknown=draft.augmented_target_unknown,
    )
    return ApprovedPair(
        problem=problem,
        search_space_id=search_space_id,
        solution_source=draft.solution_source,
        misconceptions=[],
    )


# --------------------------------------------------------------------------- #
# Subject-AGNOSTIC Apollo — AUTHORED construction (the three-completeness path).
#
# Where ``find_or_generate`` RAG-generates from retrieved textbook spans, the
# authored path STRUCTURES the PROFESSOR'S solution (the authoritative ground
# truth) into a teachable reference graph over the UNIVERSAL mint-map node vocab
# (the same 6 entry types gate 1 enforces — no per-subject restriction, no forced
# symbolic target), then grounds the Stage-3 faithfulness judge on that authored
# solution (NOT RAG'd chunks). One ``chat_fn`` pass per problem, branched by
# completeness. The construction LLM uses equations / symbols ONLY where the
# method genuinely requires them; a prose argument carries none.
# --------------------------------------------------------------------------- #

_AUTHORED_COMPLETENESS_INSTRUCTION = {
    "worked": (
        "The professor PROVIDED a worked solution AND its procedure below. STRUCTURE "
        "that procedure faithfully into typed reference steps — do NOT invent steps "
        "the professor did not use and do NOT change their method."
    ),
    "answer_only": (
        "The professor PROVIDED the final answer but NO procedure. RECONSTRUCT the "
        "reference procedure that reaches EXACTLY this answer, staying faithful to it."
    ),
    "none": (
        "The professor PROVIDED only the problem statement. PRODUCE a correct, "
        "standard reference solution. (This case is FLAGGED lower-confidence for "
        "human review.)"
    ),
}


def _authored_output_contract() -> str:
    """The output contract over the UNIVERSAL node vocab (the 6 mint-map entry
    types). Subject-agnostic: no per-subject vocab restriction and NO forced target
    shape — the construction LLM uses equations / symbols ONLY where the method
    genuinely requires them (a prose argument is free of them). The per-step
    contract is the shared ``ontology_block`` (DAG-3) — a prose argument simply
    has no equations, so the symbolic rules are vacuous for it."""
    return (
        'Output a single JSON object whose key "reference_solution" is a NON-EMPTY '
        'array of step objects (plus, optionally, the "symbol_table" key described '
        "below). No other keys. Use equations / symbols ONLY where the method "
        "genuinely requires them; a prose argument needs none.\n" + ontology_block()
    )


def _authored_system_prompt(completeness: str) -> str:
    return (
        "You convert a PROFESSOR-AUTHORED problem into a teachable reference graph for "
        "a student to teach back.\n"
        + _AUTHORED_COMPLETENESS_INSTRUCTION.get(
            completeness, _AUTHORED_COMPLETENESS_INSTRUCTION["none"]
        )
        + "\n"
        + _authored_output_contract()
    )


def _authored_grounding_text(authored) -> str:
    """The authoritative grounding the faithfulness judge entails against — the
    professor's statement + solution + procedure (NOT RAG'd chunks)."""
    parts = [f"PROBLEM: {authored.statement}"]
    if getattr(authored, "solution", None):
        parts.append(f"AUTHORED SOLUTION: {authored.solution}")
    if getattr(authored, "worked_procedure", None):
        parts.append("AUTHORED PROCEDURE: " + _canonical_json(authored.worked_procedure))
    return "\n\n".join(parts)


def _validate_authored_node_vocab(reference_solution: list[dict]) -> None:
    """FAIL-CLOSED if the construction emitted an entry_type outside the UNIVERSAL
    mint map (the 6 entry types gate 1 also enforces). Subject-agnostic: any of the
    6 types is allowed for any subject — only a genuinely-foreign type is rejected."""
    for step in reference_solution:
        entry_type = step.get("entry_type")
        if entry_type not in _ENTRY_TYPE_TO_KIND_PREFIX:
            raise SolutionDraftError(
                f"authored construction used entry_type {entry_type!r} outside the "
                f"universal mint map {sorted(_ENTRY_TYPE_TO_KIND_PREFIX)}"
            )


async def construct_authored_reference(
    authored,
    *,
    chat_fn: Callable[..., str],
) -> ReferenceSolutionDraft:
    """Build a reference-solution draft from one ``AuthoredProblem``, branching on
    completeness (worked = structure / answer_only = reconstruct-to-answer / none =
    generate+flag), over the UNIVERSAL mint-map node vocab with NO forced target
    shape (subject-agnostic — the construction never pushes a prose author toward a
    symbolic target).

    The draft's ``grounding`` is the AUTHORED solution (so the Stage-3 faithfulness
    judge entails against the professor's ground truth, not RAG'd chunks). FAIL-CLOSED
    via ``SolutionDraftError`` — an unparseable/empty construction, a node type
    outside the universal mint map, or a non-``Problem``-valid graph raises rather
    than yielding a half-built draft. ``chat_fn`` is injected (mocked in tests)."""
    system_prompt = _authored_system_prompt(authored.completeness)
    raw = chat_fn(
        purpose="authored_construct",
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _canonical_json(
                    {
                        "problem_text": authored.statement,
                        "authored_solution": authored.solution or "",
                        "worked_procedure": authored.worked_procedure or [],
                        "given_values": dict(authored.given_values),
                        "target_unknown": authored.target_unknown,
                    }
                ),
            },
        ],
        response_format={"type": "json_schema", "json_schema": build_solution_schema()},
        temperature=0.0,
    )

    reference_solution = _parse_reference_solution(raw)
    if not reference_solution:
        raise SolutionDraftError(
            "authored construction produced no usable reference_solution "
            "(unparseable/empty; fail-closed, never an empty-step draft)"
        )

    _validate_authored_node_vocab(reference_solution)
    # Problem-shape validation against the authored problem dict (depends_on
    # resolution + procedure order); fail-closed on any violation.
    try:
        Problem.model_validate(authored.to_problem_dict(reference_solution))
    except Exception as exc:  # noqa: BLE001 — any validation failure → fail-closed
        raise SolutionDraftError(
            f"authored reference_solution is not Problem-valid: {exc}"
        ) from exc

    grounding = (GroundingSpan(text=_authored_grounding_text(authored), carries_solution=True),)
    # worked/answer_only are grounded on the professor's solution -> 'authored';
    # 'none' has no authored solution to extract from -> 'generated' (and flagged).
    source: Literal["extracted", "generated", "authored"] = (
        "authored" if authored.completeness in ("worked", "answer_only") else "generated"
    )
    return ReferenceSolutionDraft(
        solution_source=source,
        reference_solution=reference_solution,
        grounding=grounding,
        provenance={
            "completeness": authored.completeness,
            "flagged": authored.completeness == "none",
        },
    )


def build_authored_approved_pair(
    authored, draft: ReferenceSolutionDraft, *, search_space_id: int
) -> ApprovedPair:
    """Assemble the REAL ``ApprovedPair`` for an authored (problem, draft) — like
    ``build_approved_pair`` but the ``problem`` dict uses the authored problem_code
    (``authored.<hash>``) as its id, so the promoted payload id matches the Tier-1
    inventory row. IMPORTS the real ``ApprovedPair`` — never redefined."""
    return ApprovedPair(
        problem=authored.to_problem_dict(draft.reference_solution),
        search_space_id=search_space_id,
        solution_source=draft.solution_source,
        misconceptions=[],
    )
