"""Frozen coverage verdict contract emitted by ``coverage.py:577-586``."""

from __future__ import annotations

import math
from typing import NotRequired, TypedDict


class NegotiationCounts(TypedDict):
    dual: int
    disputed: int
    paraphrased: int
    skipped: int


class CoverageVerdict(TypedDict):
    per_step: dict[str, str]
    procedure_scores: dict[str, float]
    confidences: dict[str, float]
    negotiation_counts: NegotiationCounts
    # INTERACTION5 (Hoot-aside grading cap): an OPTIONAL, additive per-node
    # ``{node_id: bool}`` map present ONLY when a Hoot lookup aside was supplied
    # to the adjudicator. Absent, the verdict is byte-identical to the
    # pre-feature contract, so no existing consumer or grade is affected.
    hoot_assisted: NotRequired[dict[str, bool]]


_KEYS = frozenset({"per_step", "procedure_scores", "confidences", "negotiation_counts"})
_OPTIONAL_KEYS = frozenset({"hoot_assisted"})
_NEGOTIATION_KEYS = frozenset({"dual", "disputed", "paraphrased", "skipped"})


def _validate_score_map(value: object, *, key: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a dict")
    for node_id, score in value.items():
        if (
            not isinstance(node_id, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
        ):
            raise ValueError(f"{key} must map string node ids to numbers")
        numeric = float(score)
        if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
            raise ValueError(f"{key}[{node_id!r}] must be finite and in [0, 1]")


def _validate_assist_map(value: object) -> None:
    """Validate the optional ``hoot_assisted`` per-node flag map.

    Mirrors ``_validate_score_map``'s strictness: keys must be string node ids
    and values must be genuine booleans (``bool`` only — a stray ``0``/``1`` is
    rejected the same way the score map rejects a non-numeric)."""
    if not isinstance(value, dict):
        raise ValueError("hoot_assisted must be a dict")
    for node_id, flag in value.items():
        if not isinstance(node_id, str) or not isinstance(flag, bool):
            raise ValueError("hoot_assisted must map string node ids to booleans")


def validate_coverage_verdict(value: object) -> None:
    """Raise ``ValueError`` unless *value* matches the frozen schema.

    The four required keys must be present and exactly correct. ``hoot_assisted``
    (INTERACTION5) is the sole permitted OPTIONAL key; any other extra key is a
    contract violation."""
    if not isinstance(value, dict):
        raise ValueError(f"coverage keys must be exactly {sorted(_KEYS)}")
    keys = set(value)
    if not _KEYS <= keys or keys - _KEYS - _OPTIONAL_KEYS:
        raise ValueError(
            f"coverage keys must be exactly {sorted(_KEYS)} "
            f"(optionally plus {sorted(_OPTIONAL_KEYS)})"
        )
    per_step = value["per_step"]
    if not isinstance(per_step, dict):
        raise ValueError("per_step must be a dict")
    for node_id, verdict in per_step.items():
        if not isinstance(node_id, str) or verdict not in {"covered", "missing"}:
            raise ValueError("per_step must map string node ids to covered or missing")
    _validate_score_map(value["procedure_scores"], key="procedure_scores")
    _validate_score_map(value["confidences"], key="confidences")
    counts = value["negotiation_counts"]
    if not isinstance(counts, dict) or set(counts) != _NEGOTIATION_KEYS:
        raise ValueError(f"negotiation_counts keys must be exactly {sorted(_NEGOTIATION_KEYS)}")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts.values()):
        raise ValueError("negotiation_counts values must be non-negative integers")
    if "hoot_assisted" in value:
        _validate_assist_map(value["hoot_assisted"])


__all__ = ["CoverageVerdict", "NegotiationCounts", "validate_coverage_verdict"]
