"""Ledger-grounded diagnostic narrative prompt (2026-07-10 design spec
``docs/superpowers/specs/2026-07-10-apollo-topic-score-design.md`` section 4).

The axis-based narrative (``diagnostic.py``) narrates the fixed 60/25/15 rubric
and can hallucinate claims beyond the coverage map (staging session 43: the
narrative invented "expression involving ∫sin x dx", never taught). This
module builds the REPLACEMENT prompt when ``APOLLO_TOPIC_SCORE_SERVED`` is on:
it is built entirely from an already-computed ``TopicScoreResult`` — every
topic's status/credit and every misconception's evidence span + dock points
are named explicitly in the prompt, with a hard instruction not to claim
anything beyond that ledger.

Pure module: no IO, no LLM call. ``build_topic_narrative_prompt`` returns the
``(system, user)`` message pair; the caller (``diagnostic.py``) is responsible
for the actual completion call, exactly like the existing axis-based path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from apollo.overseer.topic_score import TopicScoreResult

_TOPIC_SYSTEM_PROMPT = """You are the Overseer's diagnostic narrator. The student just taught an
ignorant agent (Apollo) to solve a specific problem. A deterministic topic-based score has
already graded the student — you have the full ledger: every topic (equation / condition /
simplification / procedure step) with its coverage status and credit, and every misconception
finding with its evidence quote and point cost. Your job is to NARRATE this ledger — not to
re-grade it and not to introduce anything the ledger does not contain.

HARD RULES — never violate:
- Explain ONLY the components listed in the ledger below. Do not claim the student covered,
  missed, or was tested on anything not present in the topics list.
- For every misconception in the ledger, quote its evidence span verbatim (or close
  paraphrase of the quoted text) and state its point cost. If it is marked resolved, praise the
  correction explicitly — do not describe it as still wrong.
- Do not invent physics/math/economics beyond what the ledger's topic names and evidence spans
  say. No claims beyond this ledger.
- Use inline math delimited ONLY as `$...$` (a single dollar sign on each side) — never
  `\\( \\)`, never `\\[ \\]`, never bare LaTeX commands outside a `$...$` span.

Output format:
- At most 3 short paragraphs narrating the ledger (covered/partial/missing topics, then
  misconceptions with their evidence + cost), followed by exactly one final line starting with
  "Next step:" naming a concrete next action tied to the weakest/most costly ledger entry.
- Tone: diagnostic, supportive, not judgmental."""


def _status_label(status: str) -> str:
    return {"covered": "covered", "partial": "partially covered", "missing": "missing"}.get(
        status, status
    )


def _format_topic_line(topic) -> str:  # noqa: ANN001 - TopicCredit, avoid import cycle noise
    name = topic.display_name or topic.canonical_key
    line = f"- Topic `{topic.canonical_key}` ({name}): {_status_label(topic.status)}, credit={topic.credit:.2f}, weight={topic.weight:.2f}"
    if topic.misconceptions:
        for m in topic.misconceptions:
            resolved = "corrected" if m.resolved else "uncorrected"
            span = m.evidence_span if m.evidence_span else "(no evidence span)"
            line += (
                f"\n  * Misconception `{m.canonical_key}` ({resolved}, "
                f'-{m.dock_points:.2f} points): "{span}"'
            )
    return line


def build_topic_narrative_prompt(result: TopicScoreResult, *, problem_text: str) -> tuple[str, str]:
    """Build the ``(system, user)`` prompt pair for the ledger-grounded narrative.

    Pure: no IO. ``user`` enumerates every topic (in ``result.topics`` order,
    including the synthetic ``_general`` bucket last, matching
    ``compute_topic_score``'s own ordering) with its status/credit/weight and
    any attached misconceptions (evidence span + dock points + resolved flag).
    Nothing outside ``result`` and ``problem_text`` is referenced, so the
    generated prompt can never smuggle in claims the ledger does not support.
    """
    topic_lines = "\n".join(_format_topic_line(t) for t in result.topics) or "(no topics graded)"

    user = (
        f"Problem: {problem_text}\n\n"
        f"Score: {result.score} ({result.letter})\n"
        f"Coverage component: {result.coverage_component:.3f}\n"
        f"Misconception dock: {result.misconception_dock:.3f}\n\n"
        f"Ledger:\n{topic_lines}\n"
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
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


__all__ = ["build_topic_narrative_prompt", "sanitize_narrative"]
