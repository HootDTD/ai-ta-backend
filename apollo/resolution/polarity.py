"""NLI polarity guard — cheap deterministic pre-screen for the NLI tier.

``polarity_allows_match`` returns ``allowed=False`` only on a high-confidence
polarity conflict (single-negation XOR; inverse-proportionality; antonym poles
for a shared quantity). It is conservative — double negation and unknown
polarity PASS to NLI.

This module is the **single source of truth** for the NLI negation/antonym
lexicon. The separate ``competition.polarity_screen`` guards only the lexical
tiers and must not be imported here.
"""

from __future__ import annotations

from dataclasses import dataclass

_NEGATION = {
    "not",
    "no",
    "n't",
    "never",
    "cannot",
    "can't",
    "doesn't",
    "isn't",
    "won't",
    "without",
    "neither",
    "nor",
}

_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("increase", "decrease"),
    ("increases", "decreases"),
    ("rises", "falls"),
    ("rise", "fall"),
    ("higher", "lower"),
    ("more", "less"),
    ("faster", "slower"),
    ("up", "down"),
    ("compressible", "incompressible"),
    ("constant", "varying"),
    ("laminar", "turbulent"),
    ("isothermal", "adiabatic"),
    ("subsonic", "supersonic"),
    ("elastic", "inelastic"),
    ("conserved", "unconserved"),
    ("appreciate", "depreciate"),
    ("surplus", "deficit"),
    ("expansionary", "contractionary"),
    ("inflation", "deflation"),
)

# Words that, when immediately following "no" or "not", form a litotes
# ("no change" ≈ "constant") — the pattern is ambiguous polarity, so it must
# PASS to NLI rather than fire as a negation mismatch.
# NOTE: "effect"/"effects" are intentionally excluded — "no effect" vs
# "has an effect" is a genuine absence-vs-presence conflict, not a
# zero-magnitude litotes.
_NULL_CHANGE: frozenset[str] = frozenset(
    {
        "change",
        "changes",
        "difference",
        "differences",
        "variation",
        "variations",
        "fluctuation",
        "fluctuations",
    }
)


@dataclass(frozen=True)
class PolarityDecision:
    allowed: bool
    reason: str


def _tokens(text: str) -> set[str]:
    return {w.strip(".,;:!?").lower() for w in text.split()}


def _negation_count(raw: str) -> int:
    """Count unambiguous negations in *raw*.

    "no/not + null-change-word" (litotes patterns like "no change") are
    treated as unknown/ambiguous polarity and excluded from the count so they
    pass to NLI rather than fire as a negation mismatch.
    """
    words = [w.strip(".,;:!?").lower() for w in raw.split()]
    n = 0
    for i, w in enumerate(words):
        # Use the already-stripped/lowercased token w for the contraction
        # check — avoids O(n²) re-split and correctly handles tokens like
        # "wasn't," whose trailing punctuation was stripped into "wasn't".
        is_neg = w in _NEGATION or w.endswith("n't")
        if not is_neg:
            continue
        # Litotes guard: "no change / no difference / …" is ambiguous.
        next_w = words[i + 1] if i + 1 < len(words) else ""
        if w in {"no", "not"} and next_w in _NULL_CHANGE:
            continue
        n += 1
    return n


def polarity_allows_match(student_text: str, reference_text: str) -> PolarityDecision:
    """Return a PolarityDecision for the student/reference text pair.

    ``allowed=False`` only on a high-confidence polarity conflict:

    - single-negation XOR (one side negated, the other not)
    - inverse-proportionality (one side says "inversely proportional", the
      other just "proportional")
    - antonym poles for a shared quantity

    Conservative: double negation and unknown polarity return
    ``allowed=True, reason="same_or_unknown"``.
    """
    s, r = _tokens(student_text), _tokens(reference_text)
    # 1. Negation XOR (single-negation only; double negation is ambiguous -> allow).
    sn, rn = _negation_count(student_text), _negation_count(reference_text)
    if (sn % 2) != (rn % 2):
        return PolarityDecision(False, "negation_mismatch")
    # 2. Inverse-proportionality: one side qualifies "proportional" with "inverse(ly)".
    s_inv = ("proportional" in s) and bool(s & {"inversely", "inverse"})
    r_inv = ("proportional" in r) and bool(r & {"inversely", "inverse"})
    if ("proportional" in s and "proportional" in r) and (s_inv != r_inv):
        return PolarityDecision(False, "direction_mismatch")
    # 3. Antonym poles for a shared quantity.
    for left, right in _ANTONYM_PAIRS:
        if left in s and left in r:  # same pole -> not discriminating
            continue
        if right in s and right in r:
            continue
        if (left in s and right in r) or (right in s and left in r):
            return PolarityDecision(False, "direction_mismatch")
    return PolarityDecision(True, "same_or_unknown")
