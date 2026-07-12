"""Verification and advisory review signals for generated problems."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from apollo.provisioning.metered_chat import CostBudgetExceeded
from apollo.provisioning.promotion_lint import _answer_equation_step, _gate_9
from apollo.schemas.problem import Problem

_LOG = logging.getLogger(__name__)

QUALITATIVE_RUBRIC_PROMPT = """\
Decompose the supplied reference solution into atomic claims. For each claim,
decide whether it is supported by the problem statement alone. Do not use
outside knowledge to validate correctness, and do not treat the reference
solution itself as evidence. A claim is supported only when the statement
explicitly supplies it or it follows directly from the statement's wording.
Return the requested JSON object only.
"""


@dataclass(frozen=True)
class RoundTripVerdict:
    verdict: Literal["verified", "refuted", "unresolved", "inapplicable"]
    diagnostic: str


@dataclass(frozen=True)
class RubricClaim:
    claim: str
    supported: bool
    note: str


@dataclass(frozen=True)
class RubricReport:
    claims: tuple[RubricClaim, ...]
    unsupported_count: int
    ceiling: Literal["faithfulness_only"] = "faithfulness_only"


class _RubricClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    supported: bool
    note: str


class _RubricPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: tuple[_RubricClaimPayload, ...] = Field(min_length=1)


def _rubric_schema() -> dict:
    return {
        "name": "generated_problem_qualitative_rubric",
        "strict": True,
        "schema": _RubricPayload.model_json_schema(),
    }


def round_trip_check(problem: Problem) -> RoundTripVerdict:
    """Solve a generated problem's governing system and check its stated answer."""
    graph = problem.model_dump()
    if _answer_equation_step(problem, graph) is None:
        return RoundTripVerdict(
            verdict="inapplicable",
            diagnostic="no distinct governing system and stated target answer",
        )

    decision = _gate_9(problem, graph, {})
    diagnostic = (
        f"gate 9: {decision.verdict}: {decision.reason}; target={decision.target!r}; "
        f"solved={decision.solved!r}; stated={decision.stated!r}"
    )
    return RoundTripVerdict(verdict=decision.verdict, diagnostic=diagnostic)


def qualitative_rubric(problem: Problem, *, chat_fn: Callable[..., str]) -> RubricReport | None:
    """Assess solution claims against the statement as an advisory signal."""
    try:
        raw = chat_fn(
            purpose="problem_generation_qualitative_rubric",
            messages=[
                {"role": "system", "content": QUALITATIVE_RUBRIC_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "problem_statement": problem.problem_text,
                            "reference_solution": [
                                step.model_dump() for step in problem.reference_solution
                            ],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            response_format={"type": "json_schema", "json_schema": _rubric_schema()},
            temperature=0.0,
        )
        payload = _RubricPayload.model_validate(json.loads(raw))
    except CostBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - advisory judge is fail-open
        _LOG.warning(
            "apollo_problem_generation_qualitative_rubric_failed",
            extra={
                "event": "apollo_problem_generation_qualitative_rubric_failed",
                "error_type": type(exc).__name__,
            },
        )
        return None

    claims = tuple(
        RubricClaim(claim=item.claim, supported=item.supported, note=item.note)
        for item in payload.claims
    )
    return RubricReport(
        claims=claims,
        unsupported_count=sum(not claim.supported for claim in claims),
    )


__all__ = [
    "QUALITATIVE_RUBRIC_PROMPT",
    "RoundTripVerdict",
    "RubricClaim",
    "RubricReport",
    "qualitative_rubric",
    "round_trip_check",
]
