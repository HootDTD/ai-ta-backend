"""Teacher-initiated generation of held Tier-1 problem variants."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from apollo.persistence.models import ConceptProblem
from apollo.provisioning.authored_sets.verification import _empty_retrieve
from apollo.provisioning.metered_chat import CostBudgetExceeded, _resolve_model
from apollo.provisioning.problem_generation.operators import (
    VARIATION_OPERATORS,
    VariationOperator,
)
from apollo.provisioning.problem_leak_guard import check_problem_leak
from apollo.provisioning.solution import SolutionDraftError, find_or_generate
from apollo.schemas.problem import Difficulty, Problem

_LOG = logging.getLogger(__name__)

_ENABLED_FLAG = "APOLLO_PROBLEM_GENERATION"
_TOKEN_CEILING_ENV = "APOLLO_PROBLEM_GENERATION_TOKEN_CEILING"
_MAX_VARIANTS_ENV = "APOLLO_PROBLEM_GENERATION_MAX_VARIANTS"
_DEFAULT_TOKEN_CEILING = 200_000
_DEFAULT_MAX_VARIANTS = 10
_MAIN_MODEL_DEFAULT = "gpt-4o"
_DROP_REASONS = (
    "leaked",
    "duplicate",
    "solution_failed",
    "invalid_seed",
    "invalid_variant",
    "budget_exceeded",
)


class ProblemGenerationDisabled(RuntimeError):
    """Raised when the default-OFF authoring stage has not been enabled."""


class GeneratedCandidate(BaseModel):
    problem_text: str = Field(min_length=1)
    given_values: dict[str, float]
    target_unknown: str = Field(min_length=1)
    difficulty: Difficulty


@dataclass(frozen=True)
class GenerationRecord:
    seed_id: int | None
    operator: str | None
    outcome: str
    reasons: tuple[str, ...] = ()
    concept_problem_id: int | None = None


@dataclass(frozen=True)
class GenerationRunResult:
    requested: int
    written: list[int]
    dropped: dict[str, int]
    records: list[GenerationRecord]


def problem_generation_enabled() -> bool:
    """Read per call so a flag flip does not require a process restart."""
    return os.environ.get(_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


def generation_token_ceiling() -> int:
    return int(os.getenv(_TOKEN_CEILING_ENV, str(_DEFAULT_TOKEN_CEILING)))


def generation_max_variants() -> int:
    return int(os.getenv(_MAX_VARIANTS_ENV, str(_DEFAULT_MAX_VARIANTS)))


def _normalized_hash(statement: str) -> str:
    """Cheap exact dedup; promote still owns the deeper concept dedup gate."""
    lowered = statement.lower().translate(str.maketrans("", "", string.punctuation))
    normalized = re.sub(r"\s+", " ", lowered).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _candidate_schema() -> dict:
    schema = GeneratedCandidate.model_json_schema()
    schema["additionalProperties"] = False
    # given_values is an open symbol->number dict, so this schema cannot be
    # strict-closed (same constraint as provisioning_schema Decision #2);
    # ``_parse_candidate``'s pydantic validation is the real enforcement.
    return {"name": "generated_problem_candidate", "strict": False, "schema": schema}


def _parse_candidate(raw: str) -> GeneratedCandidate | None:
    try:
        return GeneratedCandidate.model_validate(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValidationError):
        return None


def _assignments(
    seeds: Sequence[tuple[ConceptProblem, Problem]], count: int
) -> list[tuple[ConceptProblem, Problem, VariationOperator]]:
    applicable = [
        (row, payload, operator)
        for row, payload in seeds
        for operator in VARIATION_OPERATORS
        if operator.applicable(payload)
    ]
    if not applicable:
        return []
    return [applicable[index % len(applicable)] for index in range(count)]


def _drop(
    dropped: dict[str, int],
    records: list[GenerationRecord],
    reason: str,
    *,
    seed_id: int | None,
    operator: str | None,
    details: Sequence[str] = (),
) -> None:
    dropped[reason] += 1
    records.append(
        GenerationRecord(
            seed_id=seed_id,
            operator=operator,
            outcome=reason,
            reasons=tuple(details),
        )
    )


async def generate_problem_variants(
    db,
    *,
    concept_id: int,
    seed_problem_ids: Sequence[int],
    count: int,
    metered_chat,
    search_space_id: int,
    retrieve_fn=None,
) -> GenerationRunResult:
    """Generate, solution-check, leak-check, and persist held variants."""
    if not problem_generation_enabled():
        raise ProblemGenerationDisabled(
            "problem generation is disabled; set APOLLO_PROBLEM_GENERATION=1"
        )

    requested = min(max(int(count), 0), max(generation_max_variants(), 0))
    dropped = {reason: 0 for reason in _DROP_REASONS}
    records: list[GenerationRecord] = []
    if requested == 0:
        return GenerationRunResult(requested, [], dropped, records)

    requested_ids = {int(seed_id) for seed_id in seed_problem_ids}
    rows = (
        (
            await db.execute(
                select(ConceptProblem).where(
                    ConceptProblem.id.in_(requested_ids),
                    ConceptProblem.concept_id == concept_id,
                    ConceptProblem.tier == 2,
                    ConceptProblem.quarantined_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    rows_by_id = {int(row.id): row for row in rows}
    valid_seeds: list[tuple[ConceptProblem, Problem]] = []
    for seed_id in seed_problem_ids:
        row = rows_by_id.get(int(seed_id))
        if row is None:
            _drop(dropped, records, "invalid_seed", seed_id=int(seed_id), operator=None)
            continue
        try:
            valid_seeds.append((row, Problem.model_validate(row.payload)))
        except ValidationError as exc:
            _LOG.warning(
                "apollo_problem_generation_invalid_seed_skipped",
                extra={
                    "event": "apollo_problem_generation_invalid_seed_skipped",
                    "concept_id": concept_id,
                    "concept_problem_id": row.id,
                    "validation_error_count": exc.error_count(),
                },
            )
            _drop(dropped, records, "invalid_seed", seed_id=int(seed_id), operator=None)

    if not valid_seeds:
        return GenerationRunResult(requested, [], dropped, records)

    existing_payloads = (
        (
            await db.execute(
                select(ConceptProblem.payload).where(
                    ConceptProblem.concept_id == concept_id,
                    ConceptProblem.quarantined_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    seen_hashes = {
        _normalized_hash(str(payload.get("problem_text", "")))
        for payload in existing_payloads
        if isinstance(payload, dict) and payload.get("problem_text")
    }
    seen_hashes.update(_normalized_hash(seed.problem_text) for _, seed in valid_seeds)

    pending: list[tuple[ConceptProblem, GenerationRecord]] = []
    model = _resolve_model("MAIN_MODEL", _MAIN_MODEL_DEFAULT)
    for seed_row, seed, operator in _assignments(valid_seeds, requested):
        try:
            raw = metered_chat.main(
                purpose="problem_generation_variant",
                messages=operator.build_messages(seed),
                response_format={"type": "json_schema", "json_schema": _candidate_schema()},
                temperature=0.0,
            )
            candidate = _parse_candidate(raw)
            if candidate is None or candidate.difficulty != seed.difficulty:
                _drop(
                    dropped,
                    records,
                    "invalid_variant",
                    seed_id=int(seed_row.id),
                    operator=operator.name,
                )
                continue

            statement_hash = _normalized_hash(candidate.problem_text)
            if statement_hash in seen_hashes:
                _drop(
                    dropped,
                    records,
                    "duplicate",
                    seed_id=int(seed_row.id),
                    operator=operator.name,
                )
                continue
            seen_hashes.add(statement_hash)

            try:
                draft = await find_or_generate(
                    db,
                    candidate,
                    retrieve_fn=retrieve_fn or _empty_retrieve,
                    chat_fn=metered_chat.main,
                )
            except SolutionDraftError as exc:
                _drop(
                    dropped,
                    records,
                    "solution_failed",
                    seed_id=int(seed_row.id),
                    operator=operator.name,
                    details=(str(exc),),
                )
                continue

            problem_code = f"gen-{seed_row.id}-{operator.name}-{statement_hash[:12]}"
            problem = Problem(
                id=problem_code,
                concept_id=seed.concept_id,
                difficulty=candidate.difficulty,
                problem_text=candidate.problem_text,
                given_values=candidate.given_values,
                target_unknown=candidate.target_unknown,
                reference_solution=draft.reference_solution,
            )
            # The leak judge is intentionally advisory/fail-open and catches broad
            # chat exceptions. Preserve that policy while ensuring the metering
            # circuit break remains visible to this run-level partial-result seam.
            leak_budget_error: CostBudgetExceeded | None = None

            def _metered_leak_chat(**kwargs) -> str:
                nonlocal leak_budget_error
                try:
                    return metered_chat.cheap(**kwargs)
                except CostBudgetExceeded as exc:
                    leak_budget_error = exc
                    return json.dumps({"leaked": False, "confidence": 0.0, "quoted_span": None})

            verdict = check_problem_leak(problem, chat_fn=_metered_leak_chat)
            if leak_budget_error is not None:
                raise leak_budget_error
            if verdict.leaked:
                _drop(
                    dropped,
                    records,
                    "leaked",
                    seed_id=int(seed_row.id),
                    operator=operator.name,
                    details=verdict.reasons,
                )
                continue

            provenance: dict[str, Any] = {
                "source": "generated",
                "aig_seed_id": int(seed_row.id),
                "variation_operator": operator.name,
                "model": model,
                "authored_review": {
                    "required": True,
                    "reason": "generated_variant",
                    "ocr_draft": draft.model_dump(),
                },
            }
            for key in ("generation_defects", "symbol_table"):
                if key in draft.provenance:
                    provenance[key] = draft.provenance[key]

            row = ConceptProblem(
                concept_id=concept_id,
                problem_code=problem_code,
                difficulty=candidate.difficulty,
                payload={
                    "id": problem_code,
                    "concept_id": seed.concept_id,
                    "difficulty": candidate.difficulty,
                    "problem_text": candidate.problem_text,
                    "given_values": candidate.given_values,
                    "target_unknown": candidate.target_unknown,
                },
                tier=1,
                solution_source="generated",
                provenance=provenance,
                search_space_id=search_space_id,
            )
            db.add(row)
            record = GenerationRecord(
                seed_id=int(seed_row.id), operator=operator.name, outcome="written"
            )
            records.append(record)
            pending.append((row, record))
        except CostBudgetExceeded as exc:
            _drop(
                dropped,
                records,
                "budget_exceeded",
                seed_id=int(seed_row.id),
                operator=operator.name,
                details=(str(exc),),
            )
            break

    if pending:
        await db.flush()
        written: list[int] = []
        replacements: dict[int, GenerationRecord] = {}
        for row, record in pending:
            row_id = int(row.id)
            written.append(row_id)
            replacements[id(record)] = GenerationRecord(
                seed_id=record.seed_id,
                operator=record.operator,
                outcome=record.outcome,
                concept_problem_id=row_id,
            )
        records = [replacements.get(id(record), record) for record in records]
    else:
        written = []
    return GenerationRunResult(requested, written, dropped, records)


__all__ = [
    "GeneratedCandidate",
    "GenerationRecord",
    "GenerationRunResult",
    "ProblemGenerationDisabled",
    "generate_problem_variants",
    "generation_max_variants",
    "generation_token_ceiling",
    "problem_generation_enabled",
]
