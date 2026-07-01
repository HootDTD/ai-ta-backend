"""Calibration apparatus for the NLI resolver tier.

Threshold-sweep logic, dev-set loading, and report formatting.  Pure module —
no ML models loaded here.  The real model sweep runs in Task 12 once
``transformers``/``torch`` are available.

Credit-decision convention (mirrors ``match_nli_semantic``)
------------------------------------------------------------
A pair is *credited* iff::

    result.label == "entailment"
    and result.entailment >= min_entailment
    and result.contradiction <= max_contradiction

Precision/recall/F1 measure the quality of CREDIT vs gold=="entailment":

- TP: credited AND gold=="entailment"
- FP: credited AND gold in {"neutral","contradiction"}
- FN: not credited AND gold=="entailment"

Precision convention: 1.0 when TP+FP==0 (no credit issued → zero false
positives → vacuously perfect precision).  This is intentional: a maximally
conservative threshold that never credits anything is assigned perfect
precision and zero recall, which is dominated in ``best_operating_point``
by any point that correctly credits at least one true positive.

Recall convention: 0.0 when there are no gold-entailment pairs in the
labeled set (undefined recall; we default to 0 so all grid points tie and
the tie-break rules apply).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from apollo.resolution.nli_adjudicator import NLIResult
from apollo.resolution.nli_config import NLIParams

# ---------------------------------------------------------------------------
# Grid constants — the exhaustive search space swept by sweep_thresholds.
# ---------------------------------------------------------------------------

MIN_ENTAILMENT_GRID: tuple[float, ...] = (
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
)
MAX_CONTRADICTION_GRID: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20)

VALID_GOLD_LABELS: frozenset[str] = frozenset({"entailment", "neutral", "contradiction"})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class LabeledPair(NamedTuple):
    """One labeled example for the calibration sweep.

    Can be constructed from a plain 3-tuple via ``LabeledPair(*tup)`` so that
    callers may pass bare ``(premise, hypothesis, gold)`` tuples — the sweep
    function unpacks each element before use.
    """

    premise: str
    hypothesis: str
    gold: str  # "entailment" | "neutral" | "contradiction"


@dataclass(frozen=True)
class GridPoint:
    """Precision/recall/F1 metrics at one ``(min_entailment, max_contradiction)`` cell."""

    min_entailment: float
    max_contradiction: float
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int


#: A ``SweepReport`` is the ordered list of ``GridPoint`` objects returned by
#: ``sweep_thresholds`` — one per grid cell in the order they were produced
#: (outer loop over ``MIN_ENTAILMENT_GRID``, inner over ``MAX_CONTRADICTION_GRID``).
SweepReport = list[GridPoint]


# ---------------------------------------------------------------------------
# Core sweep
# ---------------------------------------------------------------------------


def sweep_thresholds(
    labeled: Sequence[LabeledPair | tuple[str, str, str]],
    classify_fn: Callable[[str, str], NLIResult],
) -> SweepReport:
    """Sweep the 2-D threshold grid and return per-cell precision/recall/F1.

    Each element of *labeled* may be a ``LabeledPair`` NamedTuple **or** a
    plain ``(premise, hypothesis, gold)`` 3-tuple — both are unpacked the same
    way.

    All ``classify_fn`` calls are batched up-front so the classifier is called
    exactly ``len(labeled)`` times regardless of grid size.
    """
    # Classify all pairs once, up-front — avoids O(grid × N) classifier calls.
    results: list[tuple[LabeledPair, NLIResult]] = []
    for raw in labeled:
        pair = LabeledPair(*raw)  # accepts both bare tuples and LabeledPair instances
        r = classify_fn(pair.premise, pair.hypothesis)
        results.append((pair, r))

    report: SweepReport = []
    for min_ent in MIN_ENTAILMENT_GRID:
        for max_con in MAX_CONTRADICTION_GRID:
            tp = fp = fn = 0
            for pair, r in results:
                credited = (
                    r.label == "entailment"
                    and r.entailment >= min_ent
                    and r.contradiction <= max_con
                )
                if credited:
                    if pair.gold == "entailment":
                        tp += 1
                    else:
                        fp += 1
                else:
                    if pair.gold == "entailment":
                        fn += 1

            # Precision: 1.0 when no credit issued (TP+FP==0) — see module docstring.
            precision = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
            # Recall: 0.0 when no gold-entailment pairs.
            recall = 0.0 if (tp + fn) == 0 else tp / (tp + fn)
            f1 = (
                0.0
                if (precision + recall) == 0.0
                else 2.0 * precision * recall / (precision + recall)
            )
            report.append(
                GridPoint(
                    min_entailment=min_ent,
                    max_contradiction=max_con,
                    precision=precision,
                    recall=recall,
                    f1=f1,
                    tp=tp,
                    fp=fp,
                    fn=fn,
                )
            )
    return report


# ---------------------------------------------------------------------------
# Operating-point selection
# ---------------------------------------------------------------------------


def best_operating_point(
    report: SweepReport,
    min_precision: float = 0.95,
) -> NLIParams | None:
    """Return the ``NLIParams`` for the highest-recall grid point that meets
    the precision floor.

    Tie-break (deterministic):
    1. Higher recall (primary objective).
    2. Higher ``min_entailment`` (more conservative → fewer false positives).
    3. Lower ``max_contradiction`` (more conservative).

    Returns ``None`` if no grid point meets *min_precision*.

    The returned ``NLIParams`` is constructed with ``min_entailment`` and
    ``max_contradiction`` set to the selected values; all other fields
    (``top_k``, ``ambiguity_margin``, ``misconception_veto_entailment``) keep
    their ``NLIParams`` defaults.
    """
    candidates = [pt for pt in report if pt.precision >= min_precision]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda pt: (pt.recall, pt.min_entailment, -pt.max_contradiction),
    )
    return NLIParams(
        min_entailment=best.min_entailment,
        max_contradiction=best.max_contradiction,
    )


# ---------------------------------------------------------------------------
# Dev-set I/O
# ---------------------------------------------------------------------------


def load_dev_set(path: str | Path) -> list[LabeledPair]:
    """Load a JSONL dev set from *path*.

    Each non-empty line must be a JSON object with at minimum the keys
    ``premise``, ``hypothesis``, and ``gold``.  Extra keys (e.g. ``source``,
    ``category``) are silently ignored.

    Raises:
        ValueError: if any ``gold`` value is not in
            ``{"entailment", "neutral", "contradiction"}``.
        json.JSONDecodeError: if a line is not valid JSON.
    """
    pairs: list[LabeledPair] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        obj = json.loads(line)
        gold = obj["gold"]
        if gold not in VALID_GOLD_LABELS:
            raise ValueError(
                f"Line {lineno}: invalid gold label {gold!r}; "
                f"expected one of {sorted(VALID_GOLD_LABELS)}"
            )
        pairs.append(
            LabeledPair(
                premise=obj["premise"],
                hypothesis=obj["hypothesis"],
                gold=gold,
            )
        )
    return pairs


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_report(report: SweepReport, selected: NLIParams | None) -> str:
    """Render the precision-recall grid as a plain-text table.

    Always returns a non-empty string.  When *selected* is ``None`` the footer
    states that the precision floor is unreachable on the current dev set.
    """
    header = (
        f"{'min_ent':>7}  {'max_con':>7}  "
        f"{'prec':>6}  {'rec':>6}  {'f1':>6}  "
        f"{'tp':>4}  {'fp':>4}  {'fn':>4}"
    )
    separator = "-" * len(header)
    lines: list[str] = [header, separator]
    for pt in report:
        lines.append(
            f"{pt.min_entailment:7.2f}  {pt.max_contradiction:7.2f}  "
            f"{pt.precision:6.3f}  {pt.recall:6.3f}  {pt.f1:6.3f}  "
            f"{pt.tp:4d}  {pt.fp:4d}  {pt.fn:4d}"
        )
    lines.append("")
    if selected is None:
        lines.append("Selected: None — precision floor not reachable on this dev set.")
    else:
        lines.append(
            f"Selected: min_entailment={selected.min_entailment:.2f}  "
            f"max_contradiction={selected.max_contradiction:.2f}"
        )
    return "\n".join(lines)
