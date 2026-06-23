"""WU-3C2 — misconception competition + polarity/direction screen (§5).

Two anti-over-normalization guardrails:

- **Misconception competition.** Misconception entities compete in EVERY
  resolution. A polar near-miss ("pressure *increases* with speed") scores
  higher against ``misc.pressure_velocity_same_direction`` than against the
  lexically-close ``def.pressure_velocity_tradeoff`` — wrong claims are
  out-competed, not merely thresholded. This is also how contradiction
  detection stays algorithmic (§6).
- **Polarity/direction screen.** On fuzzy matches above threshold, a candidate
  whose direction word is inverted relative to the student text is rejected
  (symbolic matching is already sign-exact, so this guards only the lexical
  tiers).

Pure functions; no mutation.
"""

from __future__ import annotations

from apollo.resolution.structural import ScoredMatch

# Direction-word antonym pairs. A student phrase and a candidate phrase that
# disagree on one of these pairs (one uses the left word, the other the right)
# are polar opposites — the polarity screen rejects the lexical match.
_DIRECTION_ANTONYMS: tuple[tuple[str, str], ...] = (
    # Physics / general direction pairs.
    ("up", "down"),
    ("higher", "lower"),
    ("increase", "decrease"),
    ("increases", "decreases"),
    ("rise", "drop"),
    ("rises", "drops"),
    ("rising", "dropping"),
    ("more", "less"),
    ("faster", "slower"),
    # Macroeconomics polarity pairs (DESIGN §"Polarity antonyms"). These let the
    # lexical tiers reject a direction-inverted macro claim (e.g. a "trade
    # surplus" gloss matched against a "trade deficit" reference).
    ("surplus", "deficit"),
    ("rises", "falls"),
    ("rise", "fall"),
    ("appreciate", "depreciate"),
    ("appreciates", "depreciates"),
    ("expansionary", "contractionary"),
    ("inflation", "deflation"),
    ("gross", "net"),
    ("nominal", "real"),
    ("multiply", "divide"),
)

# A misconception within this score margin of the best non-misconception still
# wins the competition — a wrong claim that is "about as good a lexical match"
# is out-competed deliberately, not thresholded away.
_MISCONCEPTION_MARGIN = 0.05


def _words(text: str) -> set[str]:
    return {w.strip(".,;:!?").lower() for w in text.split()}


def polarity_screen(student_text: str, candidate_text: str) -> bool:
    """Return False iff the two texts are direction-inverted (one uses the left
    word of an antonym pair where the other uses the right). True otherwise
    (including when no direction word is present — neutral text always passes).
    """
    sw = _words(student_text)
    cw = _words(candidate_text)
    for left, right in _DIRECTION_ANTONYMS:
        # Inversion = one text asserts exactly the LEFT direction while the
        # other asserts exactly the RIGHT. If a word from the pair appears in
        # BOTH texts (e.g. "rises" describing the shared speed clause), the
        # pair is not discriminating between the two phrases — skip it.
        if left in sw and left in cw:
            continue
        if right in sw and right in cw:
            continue
        if (left in sw and right in cw) or (right in sw and left in cw):
            return False
    return True


def apply_misconception_competition(
    student_text: str,
    matches: list[ScoredMatch],
) -> ScoredMatch | None:
    """Choose the winning match from the per-node candidate matches.

    A misconception match wins if its score is within ``_MISCONCEPTION_MARGIN``
    of (or above) the best non-misconception score — wrong claims are
    out-competed. With no misconception in the running the highest score wins
    normally. Deterministic tie-break on ``canonical_key``. Empty -> None."""
    if not matches:
        return None

    def _rank_key(m: ScoredMatch) -> tuple[float, str]:
        return (m.score, m.candidate.canonical_key)

    best_overall = max(matches, key=_rank_key)
    misconceptions = [m for m in matches if m.candidate.is_misconception]
    if not misconceptions:
        return best_overall

    best_misc = max(misconceptions, key=_rank_key)
    non_misc = [m for m in matches if not m.candidate.is_misconception]
    best_non_misc_score = max((m.score for m in non_misc), default=0.0)

    if best_misc.score + _MISCONCEPTION_MARGIN >= best_non_misc_score:
        return best_misc
    return best_overall
