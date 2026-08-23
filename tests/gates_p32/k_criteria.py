"""K1-K6 kill criteria + the G-L1c over-fire monitor, as PURE functions.

Spec ``.planning/specs/2026-08-12-apollo-p32-wrongness-signal-design.md`` §2.6
lists six kill criteria ("any one -> decrement and stop") and §4's G-L1c row
gates the level-1 dark bake on an over-fire rate. Neither is a unit test: both
are corpus statistics the validation campaign computes. This module is the
computation, unit-tested on synthetic rows here (``test_gate_k_criteria.py``) so
the criteria are exercised BEFORE they are ever needed live — the plan's
"K-criteria dry run" (§WAVE 4, agent C1).

**Two input shapes, both already produced by the build:**

1. the level->=1 shadow LOG lines
   (``apollo_wrongness_observed`` / ``apollo_wrongness_summary``, emitted by
   ``apollo/handlers/done.py::_evaluate_wrongness``) — the only place
   ``would_ceiling`` and G-L1c's ledger-entry denominator exist;
2. the campaign JSONL ``kind: "summary"`` rows from
   ``campaign/turn_replay.py::summary_row`` — turn shape, letters, scores.

Everything else a criterion needs (human labels, verified complaints, the
control set's membership) is passed in explicitly rather than inferred, because
inferring it is exactly how a gate stops being able to fail.

Nothing here reads a file, a clock, an env var or a database.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median

# --------------------------------------------------------------------------- #
# Thresholds — pre-registered (spec §2.6 / §4). A miss is a stop, not a re-tune. #
# --------------------------------------------------------------------------- #

#: G-L1c — ``wrongness != "none"`` on this fraction of ledger rows or more means
#: recalibrate the producer before level 2.
OVER_FIRE_MAX = 0.10

#: K1 — the ceiling firing on more than this share of graded attempts is a blast
#: radius no bounded lever justifies...
K1_MAX_FIRE_RATE = 0.25
#: ...and firing on less than this share is not worth the risk surface at all.
K1_MIN_FIRE_RATE = 0.03

#: K2 — P-1 says a ``min()`` can never manufacture these. One occurrence is a
#: design defect, not a tuning problem.
K2_FORBIDDEN_LETTERS = frozenset({"D+", "D", "D-", "F"})

#: K3 — the done-gate may cost at most one extra turn (median, firing attempts)...
K3_MAX_ADDED_TURNS = 1.0
#: ...and at most a 20% p50 auto-done latency increase.
K3_MAX_LATENCY_INCREASE = 0.20

#: K5 — decision 5: gate on the CI LOWER BOUND, never the point estimate.
K5_MIN_PRECISION_CI_LOWER = 0.70
#: K5 — a node-slot whose corroborated membership flips across identical-code
#: samples is permanently ineligible; more than this share kills the arm.
K5_MAX_MEMBERSHIP_FLIP = 0.10

#: House rule (spec §4): nothing live is concluded from fewer draws.
LIVE_SAMPLE_MINIMUM = 4

_Z_95 = 1.959963984540054


# --------------------------------------------------------------------------- #
# Shadow-log parsing                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShadowObservation:
    """One ``apollo_wrongness_observed`` line (one finding, one rung)."""

    attempt_id: str
    node_id: str
    rung: str
    span_verified: bool
    would_ceiling: bool
    kind: str


@dataclass(frozen=True)
class ShadowSummary:
    """One ``apollo_wrongness_summary`` line (one attempt)."""

    attempt_id: str
    findings: int
    nodes: int
    ledger_entries: int
    corroborated: int
    would_ceiling: int
    level: int


# `second_reader=` renders a dict and therefore CONTAINS SPACES, so a generic
# whitespace split on `key=value` pairs would mis-parse the rest of the line.
# Each scalar field is pulled by name instead.
_OBSERVED_PREFIX = "apollo_wrongness_observed"
_SUMMARY_PREFIX = "apollo_wrongness_summary"
_FIELD = r"(?:^|\s){name}=(?P<v>\S+)"


def _field(line: str, name: str) -> str | None:
    match = re.search(_FIELD.format(name=re.escape(name)), line)
    return match.group("v") if match else None


def _int_field(line: str, name: str, *, default: int = 0) -> int:
    raw = _field(line, name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _bool_field(line: str, name: str) -> bool:
    return (_field(line, name) or "").lower() == "true"


def parse_shadow_observations(lines: Iterable[str]) -> tuple[ShadowObservation, ...]:
    """Every ``apollo_wrongness_observed`` line, in order. Unparseable -> skipped."""
    return tuple(
        ShadowObservation(
            attempt_id=_field(line, "attempt_id") or "",
            node_id=_field(line, "node_id") or "",
            rung=_field(line, "rung") or "",
            span_verified=_bool_field(line, "span_verified"),
            would_ceiling=_bool_field(line, "would_ceiling"),
            kind=_field(line, "kind") or "",
        )
        for line in lines
        if _OBSERVED_PREFIX in line
    )


def parse_shadow_summaries(lines: Iterable[str]) -> tuple[ShadowSummary, ...]:
    """Every ``apollo_wrongness_summary`` line, in order."""
    return tuple(
        ShadowSummary(
            attempt_id=_field(line, "attempt_id") or "",
            findings=_int_field(line, "findings"),
            nodes=_int_field(line, "nodes"),
            ledger_entries=_int_field(line, "ledger_entries"),
            corroborated=_int_field(line, "corroborated"),
            would_ceiling=_int_field(line, "would_ceiling"),
            level=_int_field(line, "level"),
        )
        for line in lines
        if _SUMMARY_PREFIX in line
    )


# --------------------------------------------------------------------------- #
# G-L1c — the over-fire monitor                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OverFire:
    """G-L1c: labelled ledger entries over ALL ledger entries."""

    tagged: int
    ledger_entries: int

    @property
    def rate(self) -> float:
        return (self.tagged / self.ledger_entries) if self.ledger_entries else 0.0

    @property
    def computable(self) -> bool:
        return self.ledger_entries > 0

    @property
    def within_threshold(self) -> bool:
        """``False`` means: recalibrate the producer before level 2."""
        return self.computable and self.rate < OVER_FIRE_MAX


def over_fire(summaries: Iterable[ShadowSummary]) -> OverFire:
    """Corpus-wide over-fire rate from the level->=1 shadow summaries."""
    rows = tuple(summaries)
    return OverFire(
        tagged=sum(row.findings for row in rows),
        ledger_entries=sum(row.ledger_entries for row in rows),
    )


# --------------------------------------------------------------------------- #
# Statistics                                                                   #
# --------------------------------------------------------------------------- #


def wilson_lower_bound(successes: int, trials: int, *, z: float = _Z_95) -> float:
    """95% Wilson lower bound on a proportion.

    Decision 5 gates on the CI lower bound precisely because a point estimate is
    unfalsifiable at this sample size: the exact binomial 95% CI on 4/4 is
    [40%, 100%], so "precision >= 0.85" cannot fail at n=4 (crit-A A5).
    """
    if trials <= 0:
        return 0.0
    p = successes / trials
    denominator = 1 + z**2 / trials
    centre = p + z**2 / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denominator)


# --------------------------------------------------------------------------- #
# The criteria                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KVerdict:
    """One kill criterion, evaluated. ``triggered`` means DECREMENT AND STOP."""

    criterion: str
    triggered: bool
    measured: str
    threshold: str
    #: ``False`` when the corpus cannot answer this criterion (too few
    #: observations, a human input not supplied). NEVER folded into
    #: ``triggered``: "we could not measure it" is not "it passed".
    computable: bool = True


def k1_ceiling_fire_rate(summaries: Iterable[ShadowSummary]) -> KVerdict:
    """Ceiling fires on >25% of graded attempts, or <3%."""
    rows = tuple(summaries)
    graded = len(rows)
    fired = sum(1 for row in rows if row.would_ceiling > 0)
    rate = (fired / graded) if graded else 0.0
    return KVerdict(
        criterion="K1",
        triggered=bool(graded) and (rate > K1_MAX_FIRE_RATE or rate < K1_MIN_FIRE_RATE),
        measured=f"{fired}/{graded} graded attempts would ceiling ({rate:.1%})",
        threshold=f"{K1_MIN_FIRE_RATE:.0%} <= rate <= {K1_MAX_FIRE_RATE:.0%}",
        computable=bool(graded),
    )


def k2_ceilinged_letters(letters: Iterable[str]) -> KVerdict:
    """Any ceilinged attempt landing in D or F. Impossible by P-1; one is a defect."""
    served = tuple(letters)
    offenders = sorted({letter for letter in served if letter in K2_FORBIDDEN_LETTERS})
    return KVerdict(
        criterion="K2",
        triggered=bool(offenders),
        measured=(
            f"{len(offenders)} forbidden letter(s) among {len(served)} ceilinged attempts: "
            f"{offenders or 'none'}"
        ),
        threshold="0 ceilinged attempts in D or F",
        computable=bool(served),
    )


def k3_gate_turn_cost(
    *,
    baseline_turns: Sequence[float],
    gated_turns: Sequence[float],
    baseline_latency_p50: float | None = None,
    gated_latency_p50: float | None = None,
) -> KVerdict:
    """Median added turns on FIRING attempts >1, or auto-done p50 latency +>20%."""
    if not baseline_turns or not gated_turns:
        return KVerdict(
            criterion="K3",
            triggered=False,
            measured="no firing attempts observed",
            threshold=f"median added turns <= {K3_MAX_ADDED_TURNS}",
            computable=False,
        )
    added = median(gated_turns) - median(baseline_turns)
    latency_delta: float | None = None
    if baseline_latency_p50:
        latency_delta = (gated_latency_p50 or 0.0) / baseline_latency_p50 - 1.0
    triggered = added > K3_MAX_ADDED_TURNS or (
        latency_delta is not None and latency_delta > K3_MAX_LATENCY_INCREASE
    )
    latency_text = "not measured" if latency_delta is None else f"{latency_delta:+.1%}"
    return KVerdict(
        criterion="K3",
        triggered=triggered,
        measured=f"median added turns {added:+.2f}; auto-done p50 latency {latency_text}",
        threshold=(
            f"added turns <= {K3_MAX_ADDED_TURNS} AND latency <= +{K3_MAX_LATENCY_INCREASE:.0%}"
        ),
    )


def k4_false_accusation_reports(verified_complaints: int) -> KVerdict:
    """A hair trigger, on purpose: ONE verified complaint stops the ladder.

    Not derivable from any corpus — a human confirms a pilot complaint that a
    narrative accused a student falsely — so the count is an explicit input.
    """
    return KVerdict(
        criterion="K4",
        triggered=verified_complaints >= 1,
        measured=f"{verified_complaints} verified false-accusation complaint(s)",
        threshold="0 verified complaints",
        computable=True,
    )


def k5_membership_flip_rate(samples: Sequence[Collection[str]]) -> float:
    """Share of node-slots whose corroborated membership is not unanimous."""
    slots = {slot for sample in samples for slot in sample}
    if not slots or len(samples) < 2:
        return 0.0
    flipped = sum(
        1 for slot in slots if 0 < sum(1 for sample in samples if slot in sample) < len(samples)
    )
    return flipped / len(slots)


def k5_precision_and_stability(
    *,
    true_positives: int,
    labelled_findings: int,
    samples: Sequence[Collection[str]],
) -> KVerdict:
    """Corroborated-rung precision (CI lower bound) OR membership instability."""
    enough_samples = len(samples) >= LIVE_SAMPLE_MINIMUM
    lower = wilson_lower_bound(true_positives, labelled_findings)
    flip = k5_membership_flip_rate(samples)
    computable = labelled_findings > 0 and enough_samples
    triggered = computable and (lower < K5_MIN_PRECISION_CI_LOWER or flip > K5_MAX_MEMBERSHIP_FLIP)
    return KVerdict(
        criterion="K5",
        triggered=triggered,
        measured=(
            f"precision {true_positives}/{labelled_findings}, 95% CI lower {lower:.2f}; "
            f"membership flip {flip:.1%} across {len(samples)} sample(s)"
        ),
        threshold=(
            f"CI lower >= {K5_MIN_PRECISION_CI_LOWER:.2f} AND flip <= "
            f"{K5_MAX_MEMBERSHIP_FLIP:.0%} over >= {LIVE_SAMPLE_MINIMUM} samples"
        ),
        computable=computable,
    )


def k6_control_false_positives(control_findings: Mapping[str, int]) -> KVerdict:
    """ANY control-set false positive. Non-zero is disqualifying, not tunable."""
    offenders = sorted(name for name, count in control_findings.items() if count)
    return KVerdict(
        criterion="K6",
        triggered=bool(offenders),
        measured=(
            f"{len(offenders)} of {len(control_findings)} control attempts flagged: "
            f"{offenders or 'none'}"
        ),
        threshold="0 false positives, all samples",
        computable=bool(control_findings),
    )


def evaluate_k_criteria(
    *,
    summaries: Iterable[ShadowSummary],
    ceilinged_letters: Iterable[str] = (),
    baseline_turns: Sequence[float] = (),
    gated_turns: Sequence[float] = (),
    baseline_latency_p50: float | None = None,
    gated_latency_p50: float | None = None,
    verified_complaints: int = 0,
    true_positives: int = 0,
    labelled_findings: int = 0,
    corroborated_samples: Sequence[Collection[str]] = (),
    control_findings: Mapping[str, int] | None = None,
) -> tuple[KVerdict, ...]:
    """All six, in order, ready to print beside their thresholds."""
    rows = tuple(summaries)
    return (
        k1_ceiling_fire_rate(rows),
        k2_ceilinged_letters(ceilinged_letters),
        k3_gate_turn_cost(
            baseline_turns=baseline_turns,
            gated_turns=gated_turns,
            baseline_latency_p50=baseline_latency_p50,
            gated_latency_p50=gated_latency_p50,
        ),
        k4_false_accusation_reports(verified_complaints),
        k5_precision_and_stability(
            true_positives=true_positives,
            labelled_findings=labelled_findings,
            samples=corroborated_samples,
        ),
        k6_control_false_positives(dict(control_findings or {})),
    )


def any_triggered(verdicts: Iterable[KVerdict]) -> bool:
    """Spec §2.6: "any one -> decrement and stop"."""
    return any(verdict.triggered for verdict in verdicts)


__all__ = [
    "K1_MAX_FIRE_RATE",
    "K1_MIN_FIRE_RATE",
    "K2_FORBIDDEN_LETTERS",
    "K3_MAX_ADDED_TURNS",
    "K3_MAX_LATENCY_INCREASE",
    "K5_MAX_MEMBERSHIP_FLIP",
    "K5_MIN_PRECISION_CI_LOWER",
    "LIVE_SAMPLE_MINIMUM",
    "OVER_FIRE_MAX",
    "KVerdict",
    "OverFire",
    "ShadowObservation",
    "ShadowSummary",
    "any_triggered",
    "evaluate_k_criteria",
    "k1_ceiling_fire_rate",
    "k2_ceilinged_letters",
    "k3_gate_turn_cost",
    "k4_false_accusation_reports",
    "k5_membership_flip_rate",
    "k5_precision_and_stability",
    "k6_control_false_positives",
    "over_fire",
    "parse_shadow_observations",
    "parse_shadow_summaries",
    "wilson_lower_bound",
]
