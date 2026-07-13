"""Two-layer answer-leak guard for generated problem statements.

Layer 1 is deterministic and deliberately high precision: it extracts solved
values for the target (plus explicit final-step results), ignores every
``given_values`` value, and looks for equivalent numbers or a target equation
in ``problem_text``.  Problems whose reference solution has no extractable
answer abstain rather than guessing from prose.

Layer 2 is the abstention path.  Callers inject a ``MeteredChat.cheap``-shaped
``chat_fn``; the judge uses strict structured output at temperature 0.0.  A
positive judge result blocks only at ``CONFIDENCE_THRESHOLD``.  Parse errors
and low-confidence positives are advisory and pass open because generated
problems are held for teacher review downstream.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from apollo.schemas.problem import Problem, ReferenceStep

_LOG = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD: float = 0.6

# Half a percent covers ordinary display rounding (for example 44.29 -> 44.3) while
# remaining narrow enough not to conflate unrelated quantities in a statement.
_NUMERIC_REL_TOLERANCE: float = 0.005
_NUMERIC_ABS_TOLERANCE: float = 1e-9
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?(?!\w)")
_RESULT_WORD_RE = re.compile(
    r"\b(?:answer|result|value|yields?|equals?|is)\s*(?:is|of|=|:)?\s*"
    r"(?P<result>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProblemLeakVerdict:
    """Stable handoff consumed by the future GEN-2 generation gate."""

    leaked: bool
    confidence: float
    reasons: list[str]
    method: Literal["deterministic", "judge"]


@dataclass(frozen=True)
class _AnswerCandidate:
    rendered: str
    numeric: float | None
    source: str


ChatFn = Callable[..., str]


_JUDGE_SYSTEM_PROMPT = """You audit generated student problems for answer leakage.
Decide whether problem_text reveals an answer that reference_solution derives.
Given values and a request such as "find v2" are legitimate and are not leaks.
Only mark leaked=true when a student could read the answer itself from the
problem statement. Return only the requested JSON object."""


def _judge_schema() -> dict[str, Any]:
    return {
        "name": "problem_statement_leak_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "leaked": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "quoted_span": {"type": ["string", "null"]},
            },
            "required": ["leaked", "confidence", "quoted_span"],
            "additionalProperties": False,
        },
    }


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


def _as_number(text: str) -> float | None:
    stripped = text.strip().rstrip(".,;:)")
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", stripped):
        return None
    try:
        value = float(stripped)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _numeric_result(text: str) -> float | None:
    """Parse a bare number or a number followed only by a short unit string."""
    direct = _as_number(text)
    if direct is not None:
        return direct
    match = re.fullmatch(
        r"\s*(?P<number>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
        r"\s+[A-Za-zµμ°%][A-Za-z0-9µμ°%²³⁻⁰¹²³⁴⁵⁶⁷⁸⁹/*^().· -]{0,30}"
        r"[.,;:]?\s*",
        text,
    )
    return _as_number(match.group("number")) if match else None


def _clean_symbolic(text: str) -> str | None:
    candidate = text.strip().strip("$` ").rstrip(".,;:")
    # Keep matching prose-safe: an answer expression must be compact and must
    # contain mathematical structure, not merely an ordinary English word.
    if not candidate or len(candidate) > 120 or "\n" in candidate:
        return None
    if _numeric_result(candidate) is not None:
        return candidate
    if not re.search(r"[\d=+*/^()\\]|\b(?:pi|sqrt|inf|infinity)\b", candidate, re.I):
        return None
    return candidate


def _target_rhs(text: str, target: str) -> Iterator[str]:
    if not target:
        return
    pattern = re.compile(
        rf"(?<![\w]){re.escape(target)}(?![\w])\s*=(?!=)\s*"
        r"(?P<rhs>[^,;\n]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        rhs = match.group("rhs").strip()
        # Stop before explanatory prose following a solved expression.
        rhs = re.split(r"\s+(?:so|therefore|which|and then)\b", rhs, maxsplit=1)[0]
        if rhs:
            yield rhs


def _final_results(step: ReferenceStep) -> Iterator[str]:
    for text in _strings(step.content):
        if "=" in text:
            rhs = text.rsplit("=", 1)[1].strip()
            rhs = re.split(r"\s+(?:so|therefore|which|and then)\b", rhs, maxsplit=1)[0]
            if rhs:
                yield rhs
        for match in _RESULT_WORD_RE.finditer(text):
            yield match.group("result")


def _extract_answers(problem: Problem) -> list[_AnswerCandidate]:
    raw: list[tuple[str, str]] = []
    for step in problem.reference_solution:
        for text in _strings(step.content):
            raw.extend(
                (rhs, f"target equation in step {step.step}")
                for rhs in _target_rhs(text, problem.target_unknown)
            )

    final_step = max(problem.reference_solution, key=lambda step: step.step)
    raw.extend((result, f"final step {final_step.step}") for result in _final_results(final_step))

    candidates: list[_AnswerCandidate] = []
    seen: set[tuple[str, float | None]] = set()
    for rendered, source in raw:
        symbolic = _clean_symbolic(rendered)
        if symbolic is None:
            continue
        numeric = _numeric_result(symbolic)
        key = (re.sub(r"\s+", "", symbolic).casefold(), numeric)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(_AnswerCandidate(rendered=symbolic, numeric=numeric, source=source))
    return candidates


def _numbers_equivalent(left: float, right: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=_NUMERIC_REL_TOLERANCE,
        abs_tol=_NUMERIC_ABS_TOLERANCE,
    )


def _is_given_value(value: float, problem: Problem) -> bool:
    return any(_numbers_equivalent(value, float(given)) for given in problem.given_values.values())


def _normalized_symbolic(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _contains_symbolic(statement: str, answer: str) -> bool:
    normalized_answer = _normalized_symbolic(answer)
    if not normalized_answer:
        return False
    flexible = "".join(
        rf"\s*{re.escape(char)}\s*" if char in "+-*/^=()\\" else re.escape(char)
        for char in normalized_answer
    )
    return (
        re.search(
            rf"(?<!\w){flexible}(?!\w)",
            statement.casefold(),
        )
        is not None
    )


def _target_equation_matches(problem: Problem, answer: _AnswerCandidate) -> str | None:
    """Return the statement RHS when it explicitly assigns the answer target.

    This relationship is a leak even when the same scalar also happens to be a
    given: the bare given remains legitimate, but ``target = given`` reveals
    the solved relationship.
    """
    for rhs in _target_rhs(problem.problem_text, problem.target_unknown):
        cleaned = _clean_symbolic(rhs)
        if cleaned is None:
            continue
        rhs_number = _numeric_result(cleaned)
        if answer.numeric is not None and rhs_number is not None:
            if _numbers_equivalent(rhs_number, answer.numeric):
                return cleaned
        elif (
            answer.numeric is None
            and rhs_number is None
            and _normalized_symbolic(cleaned) == _normalized_symbolic(answer.rendered)
        ):
            return cleaned
    return None


def _deterministic_leak(problem: Problem, answers: list[_AnswerCandidate]) -> str | None:
    statement_numbers = [
        (match.group(0), float(match.group(0)))
        for match in _NUMBER_RE.finditer(problem.problem_text)
    ]
    for answer in answers:
        equation_rhs = _target_equation_matches(problem, answer)
        if equation_rhs is not None:
            return (
                f"problem_text states {problem.target_unknown} = {equation_rhs}, "
                f"matching the reference answer ({answer.source})"
            )
        if answer.numeric is not None:
            for rendered, value in statement_numbers:
                if _is_given_value(value, problem):
                    continue
                if _numbers_equivalent(value, answer.numeric):
                    return (
                        f"problem_text contains numeric answer {rendered!r} "
                        f"matching {answer.rendered!r} ({answer.source})"
                    )
            continue

        if _contains_symbolic(problem.problem_text, answer.rendered):
            return f"problem_text contains symbolic answer {answer.rendered!r} ({answer.source})"
    return None


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(confidence):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _judge(problem: Problem, chat_fn: ChatFn) -> ProblemLeakVerdict:
    payload = {
        "problem_text": problem.problem_text,
        "given_values": problem.given_values,
        "target_unknown": problem.target_unknown,
        "reference_solution": [step.model_dump(mode="json") for step in problem.reference_solution],
    }
    try:
        raw = chat_fn(
            purpose="problem_leak_judge",
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, sort_keys=True)},
            ],
            response_format={"type": "json_schema", "json_schema": _judge_schema()},
            temperature=0.0,
        )
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("judge response is not an object")
    except Exception as exc:  # noqa: BLE001 - advisory judge fails open
        _LOG.warning("problem_leak_judge soft-fail (open) on parse error: %s", exc)
        return ProblemLeakVerdict(
            leaked=False,
            confidence=0.0,
            reasons=["no extractable answer", "judge unavailable (advisory)"],
            method="judge",
        )

    leaked = bool(parsed.get("leaked", False))
    confidence = _clamp_confidence(parsed.get("confidence", 0.0))
    quoted = parsed.get("quoted_span")
    quoted_span = str(quoted) if quoted else None
    if leaked and confidence >= CONFIDENCE_THRESHOLD:
        _LOG.info(
            "problem_statement_leak_detected",
            extra={
                "event": "problem_statement_leak_detected",
                "method": "judge",
                "problem_id": problem.id,
                "confidence": confidence,
                "quoted_span": quoted_span,
            },
        )
        reason = (
            f"judge found answer leak at {quoted_span!r}"
            if quoted_span
            else "judge found answer leak"
        )
        return ProblemLeakVerdict(True, confidence, [reason], "judge")
    if leaked:
        _LOG.info(
            "problem_statement_low_confidence_leak",
            extra={
                "event": "problem_statement_low_confidence_leak",
                "problem_id": problem.id,
                "confidence": confidence,
                "quoted_span": quoted_span,
            },
        )
        detail = f", quoted_span={quoted_span!r}" if quoted_span else ""
        return ProblemLeakVerdict(
            False,
            confidence,
            [f"judge advisory: leaked=true, confidence={confidence:.2f}{detail}"],
            "judge",
        )
    return ProblemLeakVerdict(
        False,
        confidence,
        [f"judge found no answer leak (confidence={confidence:.2f})"],
        "judge",
    )


def check_problem_leak(
    problem: Problem,
    *,
    chat_fn: ChatFn | None = None,
) -> ProblemLeakVerdict:
    """Check whether ``problem.problem_text`` reveals its reference answer.

    ``chat_fn`` is ``MeteredChat.cheap``-shaped.  It is used only when the
    deterministic layer cannot extract an answer.  Omitting it requests a
    deterministic-only check; an abstention then returns a clean advisory
    verdict with reason ``"no extractable answer"``.
    """
    answers = _extract_answers(problem)
    if answers:
        reason = _deterministic_leak(problem, answers)
        if reason is not None:
            _LOG.info(
                "problem_statement_leak_detected",
                extra={
                    "event": "problem_statement_leak_detected",
                    "method": "deterministic",
                    "problem_id": problem.id,
                    "reason": reason,
                },
            )
            return ProblemLeakVerdict(True, 1.0, [reason], "deterministic")
        return ProblemLeakVerdict(
            False,
            1.0,
            ["extracted reference answer is absent from problem_text"],
            "deterministic",
        )

    if chat_fn is None:
        return ProblemLeakVerdict(False, 0.0, ["no extractable answer"], "deterministic")
    return _judge(problem, chat_fn)


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "ProblemLeakVerdict",
    "check_problem_leak",
]
