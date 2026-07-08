"""Tier-2 comparative LLM judge for the Apollo misconception detector (T5).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.4, amended by A1 (binding):

  * The forced JSON output carries a ``"confidence"`` float (0..1) per concept
    row IN ADDITION to the verdict — this is the verbalized-confidence
    fallback ``gate.py`` uses when no token-level logprob is available.
  * ``make_openai_judge`` is a NEW call path. It does NOT go through
    ``apollo.agent._llm.main_chat`` (that helper returns a plain ``str`` with
    no logprob access and is unsuitable here). It calls
    ``client.chat.completions.create(..., logprobs=True, top_logprobs=5)``
    DIRECTLY and walks ``resp.choices[0].logprobs.content`` to derive
    ``JudgeRaw.verdict_token_prob`` (``None`` when unavailable/unwalkable).
  * ``config.py`` defines both ``TAU_FIRE`` (token-prob path) and
    ``TAU_FIRE_VERBALIZED`` (verbalized path) — ``gate.py`` (a later task)
    selects between them based on whether ``verdict_token_prob`` is ``None``.

This module is split so ``judge_concepts`` (prompt-build + parse + soft-fail)
is fully pure/unit-testable via a stub ``JudgeFn``, and the ONE live
``client.chat.completions.create`` call in ``make_openai_judge`` is the sole
documented coverage exemption (its logprob-walk branch is still unit-tested
against a fabricated ``resp`` object — no network call in tests). Mirrors the
soft-fail structure of ``apollo/overseer/diagnostic.py``.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

from openai import OpenAI

from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    JudgeConceptInput,
    JudgeFn,
    JudgeRaw,
)

_LOG = logging.getLogger(__name__)

_VALID_VERDICTS = frozenset(
    {"clear", "needs_clarification", "misconception", "wrong"}
)

_SYSTEM_PROMPT = """You are the Apollo Overseer's comparative misconception judge.
You are given a problem, and for each of several concepts: the correct belief
a student should hold, and (optionally) a small bank of known misconceptions
for that concept. Compare what the student demonstrated during the teaching
session against the correct belief.

For EACH concept, output exactly one row with:
  - "concept_key": the concept's key, copied verbatim from the input.
  - "verdict": one of "clear" (student's belief matches the correct belief),
    "needs_clarification" (ambiguous — could be a slip or a real gap),
    "misconception" (student's belief matches a known confusion pattern),
    "wrong" (incorrect but does not match a known misconception pattern).
  - "evidence_span": the shortest verbatim student utterance that grounds
    your verdict (empty string if none).
  - "confidence": your confidence in this verdict, a float in [0, 1]. This is
    a REQUIRED field on every row, independent of the verdict you choose.

Never invent a concept_key that was not given to you. Never omit a row for a
concept you were given. Output ONLY the forced JSON object — no prose."""


def _user_prompt(problem_text: str, concepts: tuple[JudgeConceptInput, ...]) -> str:
    payload = {
        "problem": problem_text,
        "concepts": [
            {
                "concept_key": c.concept_key,
                "correct_belief": c.correct_belief,
                "known_misconceptions": [
                    {"code": e.code, "description": e.description}
                    for e in c.bank_entries
                ],
            }
            for c in concepts
        ],
    }
    return json.dumps(payload)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _all_clear(concepts: tuple[JudgeConceptInput, ...]) -> tuple[ConceptFinding, ...]:
    """Soft-fail fallback: every requested concept comes back `clear` with
    zero confidence rather than raising or dropping the concept."""
    return tuple(
        ConceptFinding(
            concept_key=c.concept_key,
            verdict="clear",
            confidence=0.0,
            severity=0.0,
            evidence_span="",
            signature=f"unkeyed:{c.concept_key}",
            source="judge",
            corroborated=False,
        )
        for c in concepts
    )


def _finding_from_row(
    concept_key: str,
    row: dict[str, Any] | None,
    *,
    verdict_token_prob: float | None,
) -> ConceptFinding:
    # A1 origin bit: True when confidence is a real verdict-token probability,
    # False when it falls back to the verbalized-confidence field. gate.py routes
    # to the stricter tau on the verbalized path.
    verdict_token_prob_present = verdict_token_prob is not None

    if row is None:
        return ConceptFinding(
            concept_key=concept_key,
            verdict="clear",
            confidence=0.0,
            severity=0.0,
            evidence_span="",
            signature=f"unkeyed:{concept_key}",
            source="judge",
            corroborated=False,
            verdict_token_prob_present=verdict_token_prob_present,
        )

    verdict = row.get("verdict")
    if verdict not in _VALID_VERDICTS:
        verdict = "clear"

    if verdict_token_prob is not None:
        confidence = _clamp01(float(verdict_token_prob))
    else:
        try:
            confidence = _clamp01(float(row.get("confidence", 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

    evidence_span = row.get("evidence_span") or ""
    if not isinstance(evidence_span, str):
        evidence_span = str(evidence_span)

    return ConceptFinding(
        concept_key=concept_key,
        verdict=verdict,
        confidence=confidence,
        severity=0.0,
        evidence_span=evidence_span,
        signature=f"unkeyed:{concept_key}",
        source="judge",
        corroborated=False,
        verdict_token_prob_present=verdict_token_prob_present,
    )


def judge_concepts(
    *,
    problem_text: str,
    concepts: tuple[JudgeConceptInput, ...],
    judge_fn: JudgeFn,
) -> tuple[ConceptFinding, ...]:
    """One batched comparative call across every concept.

    Returns one ``ConceptFinding`` per input concept (source='judge').
    Confidence is ``verdict_token_prob`` when the raw call supplied one,
    else the parsed per-concept ``confidence`` field (A1). Any failure —
    a raising ``judge_fn``, malformed JSON, a missing ``concepts`` key, a
    missing row for a requested concept — soft-fails to an all-`clear`
    (or per-row `clear`) result. A judge crash must never break grading.
    """
    if not concepts:
        return ()

    try:
        raw = judge_fn(
            system=_SYSTEM_PROMPT,
            user=_user_prompt(problem_text, concepts),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("misconception judge call failed, soft-failing to clear: %s", exc)
        return _all_clear(concepts)

    try:
        parsed = json.loads(raw.content)
        rows = parsed.get("concepts")
        if not isinstance(rows, list):
            raise ValueError("missing/invalid 'concepts' array")
        rows_by_key = {
            row.get("concept_key"): row for row in rows if isinstance(row, dict)
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("misconception judge returned malformed JSON, soft-failing: %s", exc)
        return _all_clear(concepts)

    return tuple(
        _finding_from_row(
            c.concept_key,
            rows_by_key.get(c.concept_key),
            verdict_token_prob=raw.verdict_token_prob,
        )
        for c in concepts
    )


def _extract_verdict_token_prob(resp: Any) -> float | None:
    """Walk ``resp.choices[0].logprobs.content`` looking for the token(s)
    that carry the verdict value, returning ``exp(logprob)`` for the
    highest-confidence verdict token found. Returns ``None`` when logprobs
    are absent or the structure is unwalkable — callers fall back to the
    verbalized ``confidence`` field (A1)."""
    try:
        choice = resp.choices[0]
        logprobs = getattr(choice, "logprobs", None)
        if logprobs is None:
            return None
        content = getattr(logprobs, "content", None)
        if not content:
            return None

        best: float | None = None
        for token_logprob in content:
            token = getattr(token_logprob, "token", None)
            logprob = getattr(token_logprob, "logprob", None)
            if token is None or logprob is None:
                continue
            if token.strip('"').lower() in _VALID_VERDICTS:
                prob = math.exp(logprob)
                if best is None or prob > best:
                    best = prob
        return best
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("verdict logprob walk failed, falling back to verbalized confidence: %s", exc)
        return None


def make_openai_judge(model: str | None = None) -> JudgeFn:
    """Production ``JudgeFn``.

    NEW call path (A1) — does NOT go through ``apollo.agent._llm.main_chat``
    (string-only, no logprob access). Calls
    ``client.chat.completions.create(..., logprobs=True, top_logprobs=5,
    temperature=0.0, response_format={"type": "json_object"})`` directly,
    then walks the response for a verdict-token probability. Falls back to
    ``None`` when logprobs are unavailable/unwalkable (gate.py then reads the
    per-concept ``confidence`` field instead, per A1). This function's single
    live ``client.chat.completions.create`` call is the ONLY documented
    coverage exemption in this module.
    """
    resolved_model = model or os.getenv("MAIN_MODEL", "gpt-4o")

    def _call(*, system: str, user: str) -> JudgeRaw:
        client = OpenAI()
        try:
            resp = client.chat.completions.create(  # pragma: no cover - live call
                model=resolved_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                logprobs=True,
                top_logprobs=5,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("misconception judge OpenAI call failed: %s", exc)
            return JudgeRaw(content="{}", verdict_token_prob=None)

        content = resp.choices[0].message.content or "{}"
        verdict_token_prob = _extract_verdict_token_prob(resp)
        return JudgeRaw(content=content, verdict_token_prob=verdict_token_prob)

    return _call
