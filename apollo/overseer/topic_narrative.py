"""Ledger-grounded diagnostic narrative prompt (2026-07-10 design spec
``docs/superpowers/specs/2026-07-10-apollo-topic-score-design.md`` section 4).

The axis-based narrative (``diagnostic.py``) narrates the fixed 60/25/15 rubric
and can hallucinate claims beyond the coverage map (staging session 43: the
narrative invented "expression involving ∫sin x dx", never taught). This
module builds the REPLACEMENT prompt when ``APOLLO_TOPIC_SCORE_SERVED`` is on:
it is built entirely from an already-computed ``TopicScoreResult`` — every
topic's status and whole-number percentage and every misconception's evidence
span + correction state are named explicitly in the prompt. Internal scoring
details never reach the narrator.

Pure module: no IO, no LLM call. ``build_topic_narrative_prompt`` returns the
``(system, user)`` message pair; the caller (``diagnostic.py``) is responsible
for the actual completion call, exactly like the existing axis-based path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from apollo.overseer.topic_score import TopicScoreResult

_TOPIC_SYSTEM_PROMPT = """You write feedback directly to a student who just taught Apollo how
to solve a problem. The assessment is already complete. Use the supplied assessment evidence
to help the student recognize what worked and improve the explanation; do not re-grade it.

AUDIENCE AND VOICE — never violate:
- Speak to the student as "you" and "your". Never call them "the student," refer to them as
  "they/their," or sound like a report written for a teacher.
- Write as a perceptive coach who heard the explanation, not as an auditor reciting a checklist.
- Be warm, specific, candid, and concise. Do not use bureaucratic phrases such as "partially
  covered the topic," "entirely missing," "no misconceptions were recorded," or "the ledger."

EVIDENCE AND ACCURACY:
- Use ONLY the topics and misconception evidence supplied below. Do not invent subject-matter
  details, claims, examples, or requirements.
- When a "What the student actually said" transcript is provided, it is the verbatim record of
  the student's teaching. Ground every credit statement in it: when crediting a strength or a
  partial topic, quote a short span of the student's own words or closely paraphrase what they
  actually said. NEVER expand a topic's name into a detailed explanation the transcript does not
  contain — if you cannot point to where the student taught a credited topic, credit it in one
  plain clause by its topic name, without attributing specific claims to the student.
- The supplied statuses and percentages stay authoritative: never use the transcript to argue a
  topic deserved more or less credit than the assessment shows.
- Treat a covered topic as a genuine strength. For a partial topic, distinguish what the
  explanation established from what still needs to be made explicit. Treat a missing topic as
  an opportunity to extend the explanation, never as proof that the student does not know it.
- Synthesize; do not inventory the rubric. Mention at most two of the most important gaps, chosen
  by lowest percentage. Combine closely related gaps into one idea when possible.
- Discuss a misconception only when one is supplied. Quote or closely paraphrase its evidence,
  state plainly why it needs attention, and acknowledge it if marked corrected. If none are
  supplied, say nothing at all about misconceptions, correctness, or the absence of errors.
- NEVER expose internal identifiers, scoring machinery, decimal credit/weight/dock values, or
  the words "ledger" and "rubric." Percentages are available for prioritization but should be
  omitted unless one is genuinely useful to the student.
- Use inline math delimited ONLY as `$...$` — never `\\( \\)`, never `\\[ \\]`, and never a
  bare LaTeX command outside a `$...$` span.

RESPONSE SHAPE:
- Write 90–160 words in two short paragraphs, then one final line. Do not use headings or bullets.
- Paragraph 1: lead with the strongest specific thing the student explained and why it helped
  Apollo understand. If nothing was covered, begin neutrally and encouragingly without inventing
  praise.
- Paragraph 2: explain the one or two highest-value improvements. Make the contrast actionable:
  what the student communicated, when partial evidence exists, and what to add or connect next.
- Finish with exactly one line beginning "Next step:" Give one concrete revision or re-teaching
  move tied to the most important gap. Phrase it as something the student can say, show, connect,
  compare, or illustrate — not "focus on understanding" or "study more."
- Do not repeat the score or letter grade. Do not add a generic conclusion."""


def _status_label(status: str) -> str:
    return {"covered": "covered", "partial": "partially covered", "missing": "missing"}.get(
        status, status
    )


def _humanize_key(key: str) -> str:
    """Presentation fallback when a topic has no display_name.

    The narrator quotes whatever it sees, so the raw snake_case key must
    never reach the prompt — degrade to a readable phrase instead.
    """
    tail = key.rsplit(".", 1)[-1]
    for prefix in ("def_", "proc_", "eq_", "cond_"):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
            break
    return tail.replace("_", " ").strip() or "this topic"


def _format_topic_line(topic) -> str:  # noqa: ANN001 - TopicCredit, avoid import cycle noise
    name = topic.display_name or _humanize_key(topic.canonical_key)
    pct = round(topic.credit * 100)
    line = f'- Topic "{name}": {_status_label(topic.status)} — {pct}%'
    if topic.misconceptions:
        for m in topic.misconceptions:
            resolved = "corrected" if m.resolved else "uncorrected"
            span = m.evidence_span if m.evidence_span else "(no evidence span)"
            line += f'\n  * Misconception ({resolved}): "{span}"'
    return line


def build_topic_narrative_prompt(
    result: TopicScoreResult,
    *,
    problem_text: str,
    student_utterances: Sequence[str] = (),
) -> tuple[str, str]:
    """Build the ``(system, user)`` prompt pair for the ledger-grounded narrative.

    Pure: no IO. ``user`` enumerates every topic (in ``result.topics`` order,
    including the synthetic ``_general`` bucket last, matching
    ``compute_topic_score``'s own ordering) with its status and whole-number
    percentage (display names only — internals never reach the prompt; see
    ``sanitize_narrative`` for the output-side gate) and any attached
    misconceptions (evidence span + resolved flag).

    ``student_utterances`` (2026-07-14 narrative-grounding fix) is the verbatim
    student transcript in turn order. When non-empty it is appended so the
    narrator can ground credit statements in what the student ACTUALLY said
    instead of expanding topic display names into claims the student never
    made (the prod-session-10 overstatement class). Empty (the default) keeps
    the prompt byte-identical to the pre-fix build. Nothing outside ``result``,
    ``problem_text`` and the transcript is referenced, so the generated prompt
    can never smuggle in claims the ledger does not support.
    """
    topic_lines = "\n".join(_format_topic_line(t) for t in result.topics) or "(no topics graded)"

    user = (
        f"Problem: {problem_text}\n\n"
        f"Assessment evidence:\n{topic_lines}\n"
    )
    spoken = [u.strip() for u in student_utterances if u and u.strip()]
    if spoken:
        transcript_lines = "\n".join(f'{i}. "{u}"' for i, u in enumerate(spoken, start=1))
        user += (
            "\nWhat the student actually said (verbatim, in turn order):\n"
            f"{transcript_lines}\n"
        )
    return _TOPIC_SYSTEM_PROMPT, user


# Scoring internals are 0-1 decimals (credit 0.80, weight 0.77, dock 0.000,
# credit 1.00). Requiring that shape keeps legitimate prose like "weight = mg",
# "weight 1.5" or "$0.5 \rho v^2$" intact while still catching every
# ledger-shaped leak.
_SCORING_NUM = r"-?(?:0?\.\d+|1\.0+)"
_SCORING_TERM = (
    rf"\b(?:credit|weight|dock(?:ed)?|misconception[ _]dock)\b\s*[:=]?\s*{_SCORING_NUM}"
)
_SCORING_PAREN_RE = re.compile(rf"\(\s*[^()]*?{_SCORING_TERM}[^()]*?\)", re.IGNORECASE)
_SCORING_INLINE_RE = re.compile(_SCORING_TERM, re.IGNORECASE)
_EMPTY_PAREN_RE = re.compile(r"\(\s*[,;\s]*\)")
_DANGLING_COMMA_RE = re.compile(r",\s*(?=[,.;:)])")
_EMPTY_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+[.!?](?=\s|$)")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?])")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def sanitize_narrative(text: str, canonical_keys: Sequence[str] = ()) -> str:
    """Deterministic gate: strip ledger internals from a narrative.

    Belt-and-suspenders under the prompt fix (2026-07-11 feedback spec §2) —
    the prompt no longer contains canonical keys/weights, but the narrative is
    LLM output, so the served text is scrubbed regardless. Pure + idempotent;
    returns a new string. Whole-number percentages (the topic list's own
    numbers) are deliberately preserved.
    """
    cleaned = text
    for key in canonical_keys:
        if not key or key == "_general":
            continue
        cleaned = re.sub(rf"`?\b{re.escape(key)}\b`?", "", cleaned)
    cleaned = _SCORING_PAREN_RE.sub("", cleaned)
    cleaned = _SCORING_INLINE_RE.sub("", cleaned)
    cleaned = _EMPTY_PAREN_RE.sub("", cleaned)
    cleaned = _DANGLING_COMMA_RE.sub("", cleaned)
    cleaned = _EMPTY_SENTENCE_RE.sub("", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


__all__ = ["build_topic_narrative_prompt", "sanitize_narrative"]
