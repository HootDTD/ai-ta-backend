"""Rubric-facing misconception signal value objects.

The retired misconception inference and authored-bank paths are intentionally
absent. ``MisconceptionSignal`` and ``summarize_for_rubric`` remain because the
P2.8 rubric axis and output-filter contract still consume them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MisconceptionState = Literal["default", "probe", "socratic"]


@dataclass(frozen=True)
class MisconceptionSignal:
    """Per-turn signal shape retained for rubric and output-filter consumers."""

    fired: bool
    state: MisconceptionState
    description: str | None = None
    confusion_pair: tuple[str, str] | None = None
    bank_id: str | None = None
    bank_code: str | None = None
    probe: str | None = None
    rt_steps: tuple[str, ...] | None = None
    confidence: float = 0.0
    evidence: str = ""

    @classmethod
    def default(cls, *, evidence: str = "") -> "MisconceptionSignal":
        return cls(fired=False, state="default", evidence=evidence)


def summarize_for_rubric(
    signals: list[MisconceptionSignal],
    *,
    resolved_window: int = 2,
) -> dict[str, float]:
    """Reduce turn-ordered signals to the retained P2.8 rubric-axis scores."""
    fired_codes = {s.bank_code for s in signals if s.fired and s.bank_code}
    if not fired_codes:
        return {}

    tail = signals[-resolved_window:] if signals else []
    tail_codes = {s.bank_code for s in tail if s.fired and s.bank_code}
    return {code: 0.5 if code in tail_codes else 1.0 for code in fired_codes}


__all__ = ["MisconceptionState", "MisconceptionSignal", "summarize_for_rubric"]
