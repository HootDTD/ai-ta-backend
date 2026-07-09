"""Apollo output filter — two-stage structural-leakage barrier (V3).

Stage 1 (deterministic, fast):
    Concept-scoped pre-filter against `concept.forbidden_named_laws`.
    Rejects on the first verbatim word in the draft that is forbidden
    for this concept AND not in the student's vocabulary (history +
    KG summary). Same semantics as V2 but the term list is per-concept,
    not a global fluid-mechanics frozenset.

Stage 2 (LLM-as-judge):
    Catches paraphrases the deterministic stage cannot (e.g. "speed times
    area is constant" leaking continuity). Runs on a cheap model. Acts on
    leaks only above `leakage_judge.CONFIDENCE_THRESHOLD`.

Policy: see `apollo/agent/LEAKAGE_POLICY.md` — this file is the
enforcement of that contract. The judge prompt cites the policy verbatim.

NO FALLBACK: rejection raises FilterRejectedError; the caller surfaces
the error in the UI.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Iterable

from apollo.agent.leakage_judge import (
    CONFIDENCE_THRESHOLD,
    LeakageJudge,
    llm_leakage_judge,
)
from apollo.errors import FilterRejectedError
from apollo.overseer.misconception import MisconceptionSignal
from apollo.solver.sufficiency import SufficiencyVerdict
from apollo.subjects import ConceptDefinition

_LOG = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


# Spelling-tolerance for named-law matching. A student who writes "bernulis"
# has clearly introduced Bernoulli; the canonical name should not be treated
# as un-introduced leakage just because the surface spelling differs. The
# threshold + min-length guard keep this scoped to genuine law-name typos and
# prevent short forbidden words (e.g. "pascal", "stokes") from being unlocked
# by an unrelated lookalike token.
_FUZZY_RATIO_THRESHOLD: float = 0.8
_FUZZY_MIN_LEN: int = 5


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower().strip("'") for m in _WORD_RE.finditer(text)]


def _depossess(token: str) -> str:
    """Strip a trailing possessive ``'s`` so "bernoulli's" -> "bernoulli".

    Plain trailing ``s`` is left alone — stripping it would mangle real law
    names like "stokes" -> "stoke"."""
    if token.endswith("'s"):
        return token[:-2]
    return token


def _fuzzy_match(canonical: str, token: str) -> bool:
    """True if ``token`` is the same word as ``canonical`` modulo a spelling
    slip. Exact match always wins; otherwise both words must clear the
    min-length guard and the similarity ratio threshold."""
    if canonical == token:
        return True
    if len(canonical) < _FUZZY_MIN_LEN or len(token) < _FUZZY_MIN_LEN:
        return False
    return SequenceMatcher(None, canonical, token).ratio() >= _FUZZY_RATIO_THRESHOLD


def _introduced_laws(allowed: set[str], named_laws: Iterable[str]) -> set[str]:
    """Canonical named-law layer (layer 1).

    Returns the set of canonical law names the student has effectively
    introduced — by exact mention, by a parser-canonicalized KG label
    (both already folded into `allowed`), or by a near-spelling (layer 2,
    `_fuzzy_match`). A law in this set is one Apollo is cleared to name.
    """
    introduced: set[str] = set()
    for law in named_laws:
        law_l = law.lower()
        for token in allowed:
            if _fuzzy_match(law_l, token):
                introduced.add(law_l)
                break
    return introduced


def _student_vocabulary(
    history: list[dict[str, str]],
    kg_summary: str,
) -> set[str]:
    """All words the student has actually introduced — history + summary.

    The summary is the deliberate vocabulary mirror (see
    `KGStore.summarize_for_apollo` and `LEAKAGE_POLICY.md`); it surfaces
    student-chosen labels, equation symbols, mapped terms.
    """
    vocab: set[str] = set()
    for msg in history:
        if msg.get("role") == "user":
            vocab.update(_tokenize(msg.get("content", "")))
    vocab.update(_tokenize(kg_summary))
    return vocab


def _pre_filter_offender(
    draft: str,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
) -> str | None:
    """Return the first forbidden word in the draft that the student has
    not introduced, or None if the draft is clean.

    A forbidden token is cleared when:
      - it (or its possessive stem) is a word the student literally used or
        that surfaced in the KG summary (`allowed`), OR
      - its stem is a canonical named-law the student introduced, spelling
        slips included (`introduced` — layers 1+2).
    Possessive stemming also closes the inverse leak: Apollo cannot smuggle
    an un-introduced law past the filter by writing "Bernoulli's" instead of
    "Bernoulli"."""
    forbidden: Iterable[str] = concept.forbidden_named_laws.all_terms()
    forbidden_set = set(forbidden)
    if not forbidden_set:
        return None
    allowed = _student_vocabulary(history, kg_summary)
    introduced = _introduced_laws(allowed, concept.forbidden_named_laws.named_laws)
    for token in _tokenize(draft):
        stem = _depossess(token)
        if token in allowed or stem in allowed:
            continue
        if stem in introduced or token in introduced:
            continue
        if token in forbidden_set:
            return token
        if stem != token and stem in forbidden_set:
            return stem
    return None


def _misconception_leak(
    draft: str,
    misconception: MisconceptionSignal | None,
) -> str | None:
    """Class 2 Phase 2: defense-in-depth against misconception payload leak.

    The persona shift (P2.5) only ever serializes the authored
    `probe_question` and `rt_steps` into Apollo's system prompt — it
    never includes `description` or `bank_id`. This function backs that
    contract: if those internal-only strings appear verbatim in the
    student-visible draft, block.

    Returns the offending substring (the `description` or `bank_id`),
    or None if the draft is clean.

    Notes:
    - Match is case-insensitive on `description` (LLMs sometimes
      lowercase or sentence-case verbatim phrases).
    - `bank_id` is matched as a verbatim substring with simple word
      boundaries — short numeric ids like "42" can collide with real
      content (e.g. "set v=42 m/s"), so we only match when the bank_id
      string is alphanumeric and >= 3 characters. The check is therefore
      meaningful for `code`-style bank ids ("no_density") and skipped for
      surrogate integers. The `code` channel is the durable safety net.
    - When misconception is None or unfired, returns None.
    """
    if misconception is None or not misconception.fired:
        return None

    if misconception.description:
        if misconception.description.lower() in draft.lower():
            return misconception.description

    bank_code = misconception.bank_code
    if bank_code and len(bank_code) >= 3 and bank_code.lower() in draft.lower():
        return bank_code

    bank_id = misconception.bank_id
    if (
        bank_id
        and len(bank_id) >= 3
        and not bank_id.isdigit()
        and bank_id.lower() in draft.lower()
    ):
        return bank_id

    return None


def _check_sufficiency_alignment(
    draft: str,
    sufficiency: SufficiencyVerdict | None,
    concept_id: str,
) -> None:
    """Class 2 Phase 1: warn (don't block) if the draft ignores the
    sufficiency hint when state is `insufficient`.

    The plan deliberately keeps this warn-only on first iteration so a
    miscalibrated hint never silently blocks a valid Apollo reply. The
    log line lets us tune `next_premise_hint` quality on real data
    before tightening to a hard block.
    """
    if sufficiency is None or sufficiency.state != "insufficient":
        return
    hint = sufficiency.next_premise_hint
    if not hint:
        return

    # Two checks. Variable names like "v2", "A1", "P2" contain digits and
    # don't survive `_tokenize` (which keeps letters only), so the
    # variable-name check uses substring match on the lowercased draft.
    # The hint check uses the alphabetic tokenizer because hints are
    # English-y prose ("equation: Continuity").
    draft_lower = draft.lower()
    draft_tokens = set(_tokenize(draft))

    references = False
    for var in sufficiency.missing_variables:
        if var and var.lower() in draft_lower:
            references = True
            break
    if not references:
        for token in _tokenize(hint):
            if len(token) > 2 and token in draft_tokens:
                references = True
                break

    if not references:
        _LOG.info(
            "sufficiency_signal_ignored",
            extra={
                "event": "sufficiency_signal_ignored",
                "state": sufficiency.state,
                "next_premise_hint": hint,
                "missing_variables": list(sufficiency.missing_variables),
                "concept_id": concept_id,
            },
        )


def validate_or_raise(
    draft: str,
    *,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
    judge: LeakageJudge | None = None,
    sufficiency: SufficiencyVerdict | None = None,
    misconception: MisconceptionSignal | None = None,
) -> str:
    """Two-stage check. Returns the draft unchanged if clean.

    Raises FilterRejectedError on:
    - first forbidden word the student hasn't introduced (pre-filter), OR
    - judge verdict `leaks=true` with confidence >= CONFIDENCE_THRESHOLD.

    `judge` is dependency-injected for tests. Production callers pass
    None and get the default LLM judge.

    `sufficiency` (Class 2 Phase 1, optional): when provided and state is
    `insufficient`, logs a warning if the draft ignores the
    `next_premise_hint`. Warn-only — does not raise. Block semantics
    can be added later once the hint quality is calibrated on
    production data.
    """
    offender = _pre_filter_offender(draft, concept, history, kg_summary)
    if offender is not None:
        _LOG.info(
            "filter_rejected_pre",
            extra={
                "event": "filter_rejected",
                "stage": "pre_filter",
                "rejected_term": offender,
                "concept_id": concept.concept_id,
            },
        )
        raise FilterRejectedError(rejected_term=offender, draft=draft)

    misconception_offender = _misconception_leak(draft, misconception)
    if misconception_offender is not None:
        _LOG.info(
            "filter_rejected_misconception",
            extra={
                "event": "filter_rejected",
                "stage": "misconception_leak",
                "rejected_term": misconception_offender,
                "concept_id": concept.concept_id,
            },
        )
        raise FilterRejectedError(
            rejected_term=misconception_offender, draft=draft
        )

    judge_fn: LeakageJudge = judge or llm_leakage_judge
    verdict = judge_fn(
        draft=draft,
        concept=concept,
        history=history,
        kg_summary=kg_summary,
    )
    if verdict.leaks and verdict.confidence >= CONFIDENCE_THRESHOLD:
        rejected = verdict.offending_phrase or "<paraphrase>"
        _LOG.info(
            "filter_rejected_judge",
            extra={
                "event": "filter_rejected",
                "stage": "llm_judge",
                "rejected_term": rejected,
                "reason": verdict.reason,
                "confidence": verdict.confidence,
                "concept_id": concept.concept_id,
            },
        )
        raise FilterRejectedError(rejected_term=rejected, draft=draft)
    if verdict.leaks:
        # Below-threshold leak — log for tuning, don't block.
        _LOG.info(
            "filter_low_confidence_leak",
            extra={
                "event": "filter_low_confidence_leak",
                "rejected_term": verdict.offending_phrase,
                "reason": verdict.reason,
                "confidence": verdict.confidence,
                "concept_id": concept.concept_id,
            },
        )

    # Sufficiency-signal alignment runs last and is warn-only — it never
    # blocks the draft, only logs for tuning. See `_check_sufficiency_alignment`.
    _check_sufficiency_alignment(draft, sufficiency, concept.concept_id)

    return draft


__all__ = ["validate_or_raise"]
