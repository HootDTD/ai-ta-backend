"""Overseer.diagnostic — student-facing narrative aligned with the rubric verdict.

The diagnostic LLM does NOT decide the grade. The rubric (computed in
apollo/overseer/rubric.py) is the verdict. This module produces a short
natural-language report that explains the verdict — leading with the
lowest-scoring axis, calling out what broke, and ending with a concrete
next step.

When a ``TopicScoreResult`` is passed in, ``generate_diagnostic`` replaces the
axis-based prompt with the ledger-grounded prompt built by
``apollo.overseer.topic_narrative.build_topic_narrative_prompt`` and requests
structured per-topic JSON. It returns both the back-compatible flattened
narrative and the structured feedback block. No ``topic_score`` argument (or a
soft-failed ``None``) leaves the axis prompt behavior unchanged and returns no
structured feedback.

``course_evidence`` (INTERACTION2, default OFF) threads a capped, student-safe
block of the course's own material into the ledger-grounded prompt so feedback
can cite where to read next. ``None`` — flag off, NULL session bundle, or
nothing student-safe — leaves both prompts byte-identical to the pre-feature
build."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from openai import OpenAI

from apollo.overseer.topic_narrative import build_topic_narrative_prompt, sanitize_narrative
from apollo.overseer.topic_score import TopicScoreResult
from config.models import MAIN_MODEL

_LOG = logging.getLogger(__name__)

_UNAVAILABLE_NARRATIVE = "[Diagnostic narrative unavailable — the grade above is still accurate.]"

_TOPIC_FEEDBACK_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "apollo_topic_feedback",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "topic_feedback": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "canonical_key": {"type": "string"},
                            "note": {"type": "string"},
                            "quote": {"type": ["string", "null"]},
                        },
                        "required": ["canonical_key", "note", "quote"],
                        "additionalProperties": False,
                    },
                },
                "next_step": {"type": "string"},
            },
            "required": ["headline", "topic_feedback", "next_step"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = """Write feedback directly to a student who just taught Apollo how to solve a
specific problem. The deterministic assessment is already complete. Explain the result in a way
that helps the student improve; do not re-grade it.

HARD RULES — never violate:
- Address the student only as "you" and "your." Never say "the student" or refer to the student
  as "they/their." Sound like a coach who heard the explanation, not a report for a teacher.
- Do not contradict the rubric's per-axis scores. If an axis is A+, do not
  describe it as deficient; if an axis is F, do not describe it as strong.
- Frame every gap pedagogically: "you didn't explicitly walk Apollo through X" /
  "the teaching skipped X" / "Apollo had to infer X without being shown" — never
  as Apollo failing.
- Missing reference entries are formative feedback (what to expand on next), not an accusation.
- Reference entries describe the IDEAL solution, not what the student said. Never present a
  reference detail (a name, date, title, equation, or term) as something the student personally
  stated; credit covered entries in general terms only.
- Synthesize instead of listing every missing entry. Prioritize at most two related, high-value
  gaps and explain what adding them would do for Apollo's understanding.
- Never mention a misconception category when no misconception was detected. In particular,
  never say that no misconceptions or errors were recorded.
- Avoid audit language such as "partially covered," "entirely missing," "ledger," and "rubric."

Output format: two short paragraphs followed by exactly one final line beginning "Next step:".
- Lead with a specific strength from the covered entries and why it helped Apollo understand.
- Then explain the one or two most valuable improvements and why they matter.
- Make the next step a concrete revision the student can say, show, connect, compare, or
  illustrate. Never use the vague instruction "focus on understanding."
- Do not repeat the score or letter grade and do not add a generic conclusion.

Tone: diagnostic, supportive, not judgmental. Do not invent details. Do not add
physics beyond what the reference solution and coverage tell you."""


def generate_diagnostic(
    *,
    coverage: dict[str, Any],
    reference_steps: list[dict[str, Any]],
    problem_text: str,
    rubric: dict[str, Any],
    model: str | None = None,
    topic_score: TopicScoreResult | None = None,
    student_utterances: Sequence[str] = (),
    course_evidence: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Generate the narrative and optional feedback without raising into grading."""
    try:
        return _generate_diagnostic(
            coverage=coverage,
            reference_steps=reference_steps,
            problem_text=problem_text,
            rubric=rubric,
            model=model,
            topic_score=topic_score,
            student_utterances=student_utterances,
            course_evidence=course_evidence,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("diagnostic unexpected soft-fail: %s", exc)
        return _UNAVAILABLE_NARRATIVE, None


def _generate_diagnostic(
    *,
    coverage: dict[str, Any],
    reference_steps: list[dict[str, Any]],
    problem_text: str,
    rubric: dict[str, Any],
    model: str | None = None,
    topic_score: TopicScoreResult | None = None,
    student_utterances: Sequence[str] = (),
    course_evidence: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Build one completion and convert it to the diagnostic return pair.

    A topic score selects one strict-JSON ``MAIN_MODEL`` completion. Successful
    JSON is sanitized field-by-field, receives deterministic code-generated
    ``recap`` lines, and is flattened for the first tuple item. A completion or
    parse failure returns the legacy narrative (raw completion text, or the
    fixed unavailable string) and ``None`` feedback. No topic score keeps the
    existing axis prompt and always returns ``None`` feedback.

    ``course_evidence`` (INTERACTION2) reaches the LEDGER-GROUNDED path only.
    The axis prompt is the soft-fail fallback and stays frozen: grounding must
    not alter the shape of a degraded narrative.
    """
    model = model or MAIN_MODEL

    use_topic_prompt = topic_score is not None
    if use_topic_prompt:
        system_prompt, user_content = build_topic_narrative_prompt(
            topic_score,
            problem_text=problem_text,
            student_utterances=student_utterances,
            course_evidence=course_evidence,
        )
    else:
        system_prompt = _SYSTEM_PROMPT
        user_payload = {
            "problem": problem_text,
            "rubric": rubric,
            "coverage": coverage,
            "reference_required_entries": [
                {
                    "id": s["id"],
                    "type": s["entry_type"],
                    "label": s.get("content", {}).get("label"),
                    "action": s.get("content", {}).get("action"),
                }
                for s in reference_steps
            ],
        }
        user_content = json.dumps(user_payload)

    try:
        client = OpenAI()
        completion_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.4,
        }
        if use_topic_prompt:
            completion_kwargs["response_format"] = _TOPIC_FEEDBACK_RESPONSE_FORMAT
        resp = client.chat.completions.create(
            **completion_kwargs,
        )
        raw_narrative = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("diagnostic LLM soft-fail: %s", exc)
        return _legacy_narrative(
            _UNAVAILABLE_NARRATIVE,
            coverage=coverage,
            rubric=rubric,
            topic_score=topic_score,
        ), None

    if use_topic_prompt:
        try:
            feedback = _parse_topic_feedback(
                raw_narrative,
                topic_score=topic_score,
                coverage=coverage,
                rubric=rubric,
            )
            return _flatten_topic_feedback(feedback), feedback
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("diagnostic JSON soft-fail: %s", exc)

    return _legacy_narrative(
        raw_narrative,
        coverage=coverage,
        rubric=rubric,
        topic_score=topic_score,
    ), None


def _legacy_narrative(
    narrative: str,
    *,
    coverage: dict[str, Any],
    rubric: dict[str, Any],
    topic_score: TopicScoreResult | None,
) -> str:
    """Apply the pre-scorecard recap and sanitization behavior."""
    narrative = _append_misconception_line(narrative, rubric)
    narrative = _append_negotiation_line(narrative, coverage)
    ledger_keys = (
        tuple(topic.canonical_key for topic in topic_score.topics)
        if topic_score is not None
        else ()
    )
    return sanitize_narrative(narrative, canonical_keys=ledger_keys)


def _parse_topic_feedback(
    raw: str,
    *,
    topic_score: TopicScoreResult,
    coverage: dict[str, Any],
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Validate, gate, and sanitize the model's structured topic feedback."""
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {
        "headline",
        "topic_feedback",
        "next_step",
    }:
        raise ValueError("topic feedback must contain exactly the declared top-level fields")
    if not isinstance(payload["headline"], str) or not isinstance(payload["next_step"], str):
        raise ValueError("headline and next_step must be strings")
    if not isinstance(payload["topic_feedback"], list):
        raise ValueError("topic_feedback must be a list")

    topics_by_key = {topic.canonical_key: topic for topic in topic_score.topics}
    expected_keys = [topic.canonical_key for topic in topic_score.topics]
    ledger_keys = tuple(expected_keys)
    feedback_items: list[dict[str, Any]] = []
    received_keys: list[str] = []

    for item in payload["topic_feedback"]:
        if not isinstance(item, dict) or set(item) != {"canonical_key", "note", "quote"}:
            raise ValueError("each topic feedback item must contain exactly key, note, and quote")
        canonical_key = item["canonical_key"]
        note = item["note"]
        quote = item["quote"]
        if not isinstance(canonical_key, str) or canonical_key not in topics_by_key:
            raise ValueError("topic feedback contains an unknown canonical key")
        if not isinstance(note, str) or not (isinstance(quote, str) or quote is None):
            raise ValueError("topic note/quote has an invalid type")

        topic = topics_by_key[canonical_key]
        gated_quote = _gate_topic_quote(
            quote,
            evidence_span=topic.evidence_span,
            canonical_keys=ledger_keys,
        )
        feedback_items.append(
            {
                "canonical_key": canonical_key,
                "note": sanitize_narrative(note, canonical_keys=ledger_keys),
                "quote": gated_quote,
                # INTERACTION5: additive, code-injected from the ledger (never the
                # LLM) so the flat Hoot-assist cap can't be argued away by prose.
                # False when the topic was not Hoot-assisted; absent-safe for the
                # pre-feature payload shape (survives remediation's deepcopy).
                "hoot_assisted": bool(getattr(topic, "hoot_assisted", False)),
            }
        )
        received_keys.append(canonical_key)

    if received_keys != expected_keys:
        raise ValueError("topic feedback must contain every topic exactly once in topic order")

    recap = _deterministic_recap(
        coverage=coverage,
        rubric=rubric,
        canonical_keys=ledger_keys,
    )
    return {
        "headline": sanitize_narrative(payload["headline"], canonical_keys=ledger_keys),
        "topic_feedback": feedback_items,
        "recap": recap,
        "next_step": sanitize_narrative(payload["next_step"], canonical_keys=ledger_keys),
    }


def _gate_topic_quote(
    quote: str | None,
    *,
    evidence_span: str | None,
    canonical_keys: Sequence[str],
) -> str | None:
    """Accept only an exact gated span that is already sanitizer-clean."""
    if quote is None or evidence_span is None or quote != evidence_span:
        return None
    sanitized = sanitize_narrative(quote, canonical_keys=canonical_keys)
    return quote if sanitized == quote else None


def _deterministic_recap(
    *,
    coverage: dict[str, Any],
    rubric: dict[str, Any],
    canonical_keys: Sequence[str],
) -> list[str]:
    """Build recap entries from code-owned appenders, never model output."""
    lines = [
        _append_misconception_line("", rubric),
        _append_negotiation_line("", coverage),
    ]
    return [sanitize_narrative(line, canonical_keys=canonical_keys) for line in lines if line]


def _flatten_topic_feedback(feedback: dict[str, Any]) -> str:
    """Flatten structured feedback in one stable back-compat order."""
    parts = [
        feedback["headline"],
        *(item["note"] for item in feedback["topic_feedback"]),
        *feedback["recap"],
        f"Next step: {feedback['next_step']}",
    ]
    return "\n\n".join(parts)


def _append_negotiation_line(narrative: str, coverage: dict[str, Any]) -> str:
    """Append a one-line summary of how many KG entries the student
    negotiated.

    Class 2 Phase 3 (P3.11). The pedagogical signal here is metacognitive:
    the student looked at Apollo's understanding and acted on it. We
    surface the count so the diagnostic can credit that behavior without
    re-litigating which entries were touched.

    Source: `coverage["negotiation_counts"]` (P3.4). Tolerates legacy
    coverage dicts (no key) by treating counts as zero.

    Output is empty when nothing was negotiated. Phrased generically —
    never names individual entries."""
    counts = coverage.get("negotiation_counts") or {}
    paraphrased = int(counts.get("paraphrased", 0) or 0)
    skipped = int(counts.get("skipped", 0) or 0)
    disputed = int(counts.get("disputed", 0) or 0)
    total = paraphrased + skipped + disputed
    if total == 0:
        return narrative

    parts: list[str] = []
    if paraphrased:
        parts.append(f"{paraphrased} paraphrased")
    if skipped:
        parts.append(f"{skipped} skipped")
    if disputed:
        parts.append(f"{disputed} disputed")
    breakdown = ", ".join(parts)

    line = f"You negotiated {total} entr{'y' if total == 1 else 'ies'} with Apollo: {breakdown}."
    if narrative and not narrative.endswith("\n"):
        narrative += "\n\n"
    return narrative + line


def _append_misconception_line(narrative: str, rubric: dict[str, Any]) -> str:
    """Append a deterministic one-liner about misconceptions navigated.

    Class 2 Phase 2 (P2.9). Visible to the student only when at least
    one misconception was detected during the attempt. Phrased to avoid
    naming the misconception itself — the student already saw the
    Socratic moves; this is a recap, not a re-revelation.

    No-op when no misconceptions fired; narrative returned unchanged.
    """
    block = rubric.get("misconception_corrected") or {}
    detected = int(block.get("detected", 0) or 0)
    if detected <= 0:
        return narrative
    resolved = int(block.get("resolved", 0) or 0)
    line = (
        f"During the conversation, you and Apollo navigated through "
        f"{detected} suspected misconception"
        f"{'s' if detected != 1 else ''}; you resolved {resolved} of them."
    )
    if narrative and not narrative.endswith("\n"):
        narrative += "\n\n"
    return narrative + line
