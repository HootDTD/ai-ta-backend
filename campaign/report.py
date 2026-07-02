"""Gate evaluation report generator (Plan Task E3).

Pure logic over already-computed campaign artifacts: the S1-S5
:class:`~campaign.judges.base.JudgeResult`\\ s (E1), the paired
graph/LLM grading attempts (one dict per Done-click, as the DB's
``apollo_grading_artifacts`` rows would be shaped once Phase A lands),
the Fable adjudication verdicts (E2), and the frozen
:class:`~campaign.config.CampaignConfig` for the run. It touches no DB, no
Neo4j, no LLM, no filesystem in its core (:func:`build_report`) — everything
is handed in as plain data so the whole module is unit-testable against
fixtures, matching the E1 judges' precedent.

**Deviation from the plan sketch:** this worktree branched directly off
``staging`` (not off ``feat/apollo-canonical-artifact``), so
``apollo.persistence.models.GradingArtifact`` / ``apollo.grading.composite``
/ ``apollo.projections.scorecard`` do not exist here yet (Phase A/B ran in a
different worktree). :func:`build_report` therefore accepts attempt records
as plain dicts shaped like the eventual artifact/scorecard payload (see
``AttemptRecord`` docstring below) rather than importing those modules. Once
this branch is rebased onto the artifact branch, a thin adapter can convert
real ``GradingArtifact`` rows into that same dict shape without touching this
module's gate logic.

Attempt record shape consumed by :func:`build_report` (one per Done-click,
already paired graph+LLM per spec Section 5)::

    {
        "attempt_id": str,
        "subject": str,
        "band": str | None,               # student-facing scorecard band
        "grading_latency_ms": int | None, # Done-click grading latency
        "shadow_succeeded": bool,         # graph path ran without exception
        "shadow_abstained": bool,         # graph path abstained (needed fallback)
        "graph_composite": float | None,  # graph artifact's composite (0-1)
        "llm_composite": float | None,    # LLM/pair artifact's overall score (0-1)
    }

Fable adjudication verdict shape (E2)::

    {"attempt_id": str, "verdict": "sane" | "not_sane" | "not_sane_harmful", "reason": str}

(The task brief also allows the plain two-value ``sane``/``not_sane`` enum;
:func:`adjudication_gate` treats any verdict string containing ``"harmful"``
as an actively-harmful output for the zero-harmful gate, and everything else
is bucketed by exact equality to ``"sane"``.)
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from campaign.config import CampaignConfig
from campaign.judges.base import JudgeResult

__all__ = [
    "S1_BAR",
    "S2_BAR",
    "S3_BAR",
    "S4_BAR",
    "S5_PRECISION_BAR",
    "ADJUDICATION_SANE_BAR",
    "GRAPH_GRADED_BAR",
    "LATENCY_P95_MS_BAR",
    "MIN_SUBJECTS",
    "BANDS",
    "GateOutcome",
    "GateReport",
    "band_for_score",
    "stage_gate",
    "adjudication_gate",
    "graph_graded_fraction",
    "graph_graded_gate",
    "latency_p95_ms",
    "ops_gate",
    "breadth_gate",
    "paired_comparison",
    "classify_subject",
    "build_report",
    "render_markdown",
    "write_report",
]

# --- Gate bars (spec §4 table + §4 quantitative gates) ----------------------

S1_BAR = 0.95
S2_BAR = 0.95
S3_BAR = 0.95
S4_BAR = 0.90
S5_PRECISION_BAR = 0.90
ADJUDICATION_SANE_BAR = 0.95
GRAPH_GRADED_BAR = 0.70
LATENCY_P95_MS_BAR = 15_000
MIN_SUBJECTS = 4

#: Mirrors the eventual student-scorecard bands (spec §2 / plan Task B1),
#: reproduced locally since ``apollo.projections`` doesn't exist on this
#: branch yet (see module docstring deviation note). ``(label, floor)``,
#: checked highest-floor-first.
BANDS: tuple[tuple[str, float], ...] = (
    ("Strong", 0.85),
    ("Proficient", 0.70),
    ("Developing", 0.50),
    ("Beginning", 0.0),
)

_STAGE_BARS: dict[str, float] = {
    "s1_reference_graph": S1_BAR,
    "s2_ingestion": S2_BAR,
    "s3_student_fidelity": S3_BAR,
    "s4_apollo_coherence": S4_BAR,
    "s5_misconceptions": S5_PRECISION_BAR,
}


def band_for_score(score: float, bands: tuple[tuple[str, float], ...] = BANDS) -> str:
    """Same "highest floor the score clears" rule the eventual B1 renderer
    uses. ``bands`` must be sorted highest-floor-first (the module default
    is); an empty/misordered custom sequence is the caller's problem."""
    for label, floor in bands:
        if score >= floor:
            return label
    return bands[-1][0] if bands else "Unknown"


@dataclass(frozen=True)
class GateOutcome:
    """One gate's verdict: whether it passed, the measured value, the bar it
    was measured against, and a human-readable detail line for the report."""

    name: str
    passed: bool
    value: float | None
    bar: float | None
    detail: str


@dataclass(frozen=True)
class GateReport:
    """The whole campaign run's gate evaluation. ``gates`` is every
    :class:`GateOutcome` computed (stage audits, adjudication, per-subject
    graph-graded fraction, ops, breadth); ``passed`` is true only if every
    one of them passed. ``evidence`` carries the raw counts/values a human
    reviewing a failure needs, keyed by gate name."""

    run_id: str
    config_sha: str
    gates: tuple[GateOutcome, ...]
    evidence: Mapping[str, Any]
    paired: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def failures(self) -> tuple[GateOutcome, ...]:
        return tuple(g for g in self.gates if not g.passed)


def stage_gate(result: JudgeResult, bar: float | None = None) -> GateOutcome:
    """Turn one S1-S5 :class:`JudgeResult` into a pass/fail :class:`GateOutcome`
    against its spec bar. ``bar`` defaults to the stage's known bar
    (looked up by ``result.stage``); pass it explicitly for a stage name this
    module doesn't recognise."""
    effective_bar = bar if bar is not None else _STAGE_BARS.get(result.stage)
    if effective_bar is None:
        raise ValueError(f"no known gate bar for stage {result.stage!r}; pass bar= explicitly")
    passed = result.total > 0 and result.pass_rate >= effective_bar
    detail = (
        f"{result.stage}: {result.passed}/{result.total} = {result.pass_rate:.1%} "
        f"(bar {effective_bar:.0%})"
    )
    if result.total == 0:
        detail += " — zero items audited, treated as failing"
    return GateOutcome(
        name=result.stage, passed=passed, value=result.pass_rate, bar=effective_bar, detail=detail
    )


def adjudication_gate(
    verdicts: Sequence[Mapping[str, Any]], *, bar: float = ADJUDICATION_SANE_BAR
) -> GateOutcome:
    """Fable adjudication gate (spec §4): >=``bar`` sane AND zero harmful
    outputs. An empty sample never silently reads as "100% sane"."""
    total = len(verdicts)
    if total == 0:
        return GateOutcome(
            name="adjudication",
            passed=False,
            value=0.0,
            bar=bar,
            detail="adjudication: 0 packets sampled — treated as failing",
        )
    sane = sum(1 for v in verdicts if str(v.get("verdict", "")) == "sane")
    harmful = sum(1 for v in verdicts if "harmful" in str(v.get("verdict", "")))
    sane_rate = sane / total
    passed = sane_rate >= bar and harmful == 0
    detail = f"adjudication: {sane}/{total} = {sane_rate:.1%} sane, {harmful} harmful (bar {bar:.0%}, 0 harmful)"
    return GateOutcome(name="adjudication", passed=passed, value=sane_rate, bar=bar, detail=detail)


def _is_graph_graded(attempt: Mapping[str, Any]) -> bool:
    """Per spec §4: "graph-graded" = the graph path succeeded and did not
    abstain, i.e. it would have graded the student without falling back to
    the LLM — computed counterfactually so shadow-mode runs (where the LLM
    is always served regardless) still measure the metric the promotion
    decision cares about."""
    return bool(attempt.get("shadow_succeeded")) and not bool(attempt.get("shadow_abstained"))


def graph_graded_fraction(attempts: Sequence[Mapping[str, Any]]) -> float:
    """Fraction of ``attempts`` that are graph-graded (see :func:`_is_graph_graded`).
    Empty input -> ``0.0`` (never a vacuous pass)."""
    if not attempts:
        return 0.0
    return sum(1 for a in attempts if _is_graph_graded(a)) / len(attempts)


def graph_graded_gate(
    attempts: Sequence[Mapping[str, Any]], *, bar: float = GRAPH_GRADED_BAR
) -> dict[str, GateOutcome]:
    """One :class:`GateOutcome` per subject present in ``attempts``."""
    by_subject: dict[str, list[Mapping[str, Any]]] = {}
    for attempt in attempts:
        subject = str(attempt.get("subject", ""))
        by_subject.setdefault(subject, []).append(attempt)
    outcomes: dict[str, GateOutcome] = {}
    for subject, subject_attempts in sorted(by_subject.items()):
        fraction = graph_graded_fraction(subject_attempts)
        name = f"graph_graded:{subject}"
        outcomes[subject] = GateOutcome(
            name=name,
            passed=fraction >= bar,
            value=fraction,
            bar=bar,
            detail=(
                f"{subject}: {fraction:.1%} graph-graded "
                f"({sum(1 for a in subject_attempts if _is_graph_graded(a))}/{len(subject_attempts)}, "
                f"bar {bar:.0%})"
            ),
        )
    return outcomes


def latency_p95_ms(attempts: Sequence[Mapping[str, Any]]) -> float | None:
    """95th-percentile ``grading_latency_ms`` across attempts that recorded
    one (nulls excluded — a missing latency is not a zero). ``None`` if no
    attempt recorded a latency."""
    values = sorted(
        float(a["grading_latency_ms"])
        for a in attempts
        if a.get("grading_latency_ms") is not None
    )
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    # Nearest-rank percentile (matches the informal "p95" bar in the spec;
    # no interpolation needed at campaign sample sizes).
    rank = max(0, min(len(values) - 1, int(round(0.95 * (len(values) - 1)))))
    return values[rank]


def ops_gate(
    attempts: Sequence[Mapping[str, Any]],
    *,
    event_loop_stall_warnings: Sequence[str] = (),
    bar_ms: float = LATENCY_P95_MS_BAR,
) -> GateOutcome:
    """Ops gate (spec §4): p95 grading latency <= ``bar_ms`` AND zero
    event-loop-stall warnings captured from the backend's stderr during the
    run."""
    p95 = latency_p95_ms(attempts)
    latency_ok = p95 is not None and p95 <= bar_ms
    stall_ok = len(event_loop_stall_warnings) == 0
    passed = latency_ok and stall_ok
    p95_display = "n/a (no latencies recorded)" if p95 is None else f"{p95:.0f}ms"
    detail = (
        f"ops: p95={p95_display} (bar <= {bar_ms:.0f}ms), "
        f"{len(event_loop_stall_warnings)} event-loop-stall warning(s)"
    )
    return GateOutcome(name="ops", passed=passed, value=p95, bar=bar_ms, detail=detail)


def classify_subject(
    subject_key: str, subject_kinds: Mapping[str, str] | None = None
) -> str:
    """``"seeded" | "wu_aas" | "held_out" | "unknown"`` for a subject key.
    ``subject_kinds`` overrides/extends the lookup (tests pass fixtures
    directly rather than depending on ``campaign.cast.subjects``' live
    registry, which is Task D1's, not this task's, contract to keep stable)."""
    if subject_kinds and subject_key in subject_kinds:
        return subject_kinds[subject_key]
    return "unknown"


def breadth_gate(
    attempts: Sequence[Mapping[str, Any]],
    *,
    subject_kinds: Mapping[str, str],
    min_subjects: int = MIN_SUBJECTS,
) -> GateOutcome:
    """Breadth gate (spec §4/§5): all other gates must hold on >= ``min_subjects``
    subjects, including >= 1 WU-AAS-authored and >= 1 held-out. This function
    only checks the CORPUS COMPOSITION side (subject count + kind coverage);
    "all other gates hold" is enforced by :func:`build_report` requiring every
    per-subject gate to pass before the overall report passes."""
    subjects = {str(a.get("subject", "")) for a in attempts}
    kinds = {classify_subject(s, subject_kinds) for s in subjects}
    has_wu_aas = "wu_aas" in kinds
    has_held_out = "held_out" in kinds
    passed = len(subjects) >= min_subjects and has_wu_aas and has_held_out
    detail = (
        f"breadth: {len(subjects)} subjects (bar >= {min_subjects}), "
        f"wu_aas={'yes' if has_wu_aas else 'no'}, held_out={'yes' if has_held_out else 'no'}"
    )
    return GateOutcome(name="breadth", passed=passed, value=float(len(subjects)), bar=float(min_subjects), detail=detail)


def paired_comparison(
    attempts: Sequence[Mapping[str, Any]], *, bands: tuple[tuple[str, float], ...] = BANDS
) -> dict[str, Any]:
    """Graph-vs-LLM comparison (spec §4/§5 "paired artifacts"): per-attempt
    band agreement, mean signed delta (graph - llm), and the top-10 most
    divergent attempts for human review. Attempts missing either score are
    excluded (reported as ``skipped_missing_pair``).

    Post-F1c note: the graph composite is coverage-weighted and the LLM
    composite is rubric-derived (see 3cf239f), so they now live on different
    scales. ``band_agreement_rate`` — whether the two scores land in the same
    letter band — is the PRIMARY paired metric this function reports; it is
    scale-invariant. ``mean_delta`` (and the raw per-pair ``delta`` values in
    ``top_divergent``) are kept as an INFORMATIONAL, cross-scale review
    finding only — a large or shifting mean delta is expected and is not by
    itself evidence of miscalibration. Never gate on ``mean_delta``."""
    paired = [
        a
        for a in attempts
        if a.get("graph_composite") is not None and a.get("llm_composite") is not None
    ]
    skipped = len(attempts) - len(paired)
    if not paired:
        return {
            "n_pairs": 0,
            "skipped_missing_pair": skipped,
            "band_agreement_rate": 0.0,
            "mean_delta": 0.0,
            "top_divergent": [],
        }
    deltas: list[dict[str, Any]] = []
    agreements = 0
    for attempt in paired:
        graph_score = float(attempt["graph_composite"])
        llm_score = float(attempt["llm_composite"])
        graph_band = band_for_score(graph_score, bands)
        llm_band = band_for_score(llm_score, bands)
        agree = graph_band == llm_band
        agreements += int(agree)
        deltas.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "subject": attempt.get("subject"),
                "graph_composite": graph_score,
                "llm_composite": llm_score,
                "delta": graph_score - llm_score,
                "band_agreement": agree,
                "graph_band": graph_band,
                "llm_band": llm_band,
            }
        )
    mean_delta = statistics.fmean(d["delta"] for d in deltas)
    top_divergent = sorted(deltas, key=lambda d: abs(d["delta"]), reverse=True)[:10]
    return {
        "n_pairs": len(paired),
        "skipped_missing_pair": skipped,
        "band_agreement_rate": agreements / len(paired),
        "mean_delta": mean_delta,
        "top_divergent": top_divergent,
    }


def build_report(
    *,
    run_id: str,
    config: CampaignConfig,
    config_sha: str,
    judge_results: Mapping[str, JudgeResult],
    attempts: Sequence[Mapping[str, Any]],
    adjudication_verdicts: Sequence[Mapping[str, Any]],
    subject_kinds: Mapping[str, str] | None = None,
    event_loop_stall_warnings: Sequence[str] = (),
) -> GateReport:
    """Compute every spec Section 4 gate for one campaign run.

    ``config`` is accepted (and its snapshot recorded in ``evidence``) even
    though no gate math reads it directly today — the frozen config is part
    of the report's provenance (which weights/thresholds this run's numbers
    were measured under), per the plan's "frozen CampaignConfig" input.
    """
    gates: list[GateOutcome] = []

    for stage_name in sorted(_STAGE_BARS):
        result = judge_results.get(stage_name)
        if result is None:
            gates.append(
                GateOutcome(
                    name=stage_name,
                    passed=False,
                    value=None,
                    bar=_STAGE_BARS[stage_name],
                    detail=f"{stage_name}: no judge result supplied — treated as failing",
                )
            )
        else:
            gates.append(stage_gate(result))

    gates.append(adjudication_gate(adjudication_verdicts))

    graph_graded_outcomes = graph_graded_gate(attempts)
    gates.extend(graph_graded_outcomes[subject] for subject in sorted(graph_graded_outcomes))

    gates.append(ops_gate(attempts, event_loop_stall_warnings=event_loop_stall_warnings))
    gates.append(breadth_gate(attempts, subject_kinds=subject_kinds or {}))

    paired = paired_comparison(attempts)

    evidence: dict[str, Any] = {
        "config_snapshot": config.snapshot(),
        "n_attempts": len(attempts),
        "n_adjudication_samples": len(adjudication_verdicts),
        "graph_graded_by_subject": {
            subject: outcome.value for subject, outcome in graph_graded_outcomes.items()
        },
        "latency_p95_ms": latency_p95_ms(attempts),
        "stage_totals": {
            name: {"passed": r.passed, "total": r.total, "pass_rate": r.pass_rate}
            for name, r in judge_results.items()
        },
    }

    return GateReport(
        run_id=run_id,
        config_sha=config_sha,
        gates=tuple(gates),
        evidence=evidence,
        paired=paired,
    )


def render_markdown(report: GateReport) -> str:
    """Render ``GATE-REPORT.md`` content: overall verdict, every gate with
    its measured value vs bar, and the paired graph-vs-LLM comparison table
    (spec §5 exit criteria: "gate failures become the next work queue
    verbatim")."""
    lines: list[str] = []
    lines.append(f"# Gate Report — run `{report.run_id}`")
    lines.append("")
    lines.append(f"**Config SHA:** `{report.config_sha}`")
    lines.append(f"**Overall:** {'PASS' if report.passed else 'FAIL'}")
    lines.append("")
    lines.append("## Gates")
    lines.append("")
    lines.append("| Gate | Result | Value | Bar |")
    lines.append("|---|---|---|---|")
    for gate in report.gates:
        result = "PASS" if gate.passed else "FAIL"
        value = "n/a" if gate.value is None else f"{gate.value:.3f}"
        bar = "n/a" if gate.bar is None else f"{gate.bar:.3f}"
        lines.append(f"| {gate.name} | {result} | {value} | {bar} |")
    lines.append("")
    if report.failures:
        lines.append("## Failures (next work queue)")
        lines.append("")
        for gate in report.failures:
            lines.append(f"- **{gate.name}**: {gate.detail}")
        lines.append("")
    lines.append("## Paired graph-vs-LLM comparison")
    lines.append("")
    paired = report.paired
    lines.append(f"- Pairs compared: {paired['n_pairs']} (skipped, missing pair: {paired['skipped_missing_pair']})")
    lines.append(f"- **Band agreement rate (primary paired metric): {paired['band_agreement_rate']:.1%}**")
    lines.append(
        f"- Mean raw composite delta (graph - llm): {paired['mean_delta']:.4f} "
        "— **informational / cross-scale only**: the graph composite is "
        "coverage-weighted and the LLM composite is rubric-derived, so they "
        "sit on different scales and this delta is a review finding, not a "
        "gate signal (see paired_comparison() docstring)."
    )
    if paired["top_divergent"]:
        lines.append("")
        lines.append("| Attempt | Subject | Graph | LLM | Delta | Bands |")
        lines.append("|---|---|---|---|---|---|")
        for row in paired["top_divergent"]:
            lines.append(
                f"| {row['attempt_id']} | {row['subject']} | {row['graph_composite']:.3f} | "
                f"{row['llm_composite']:.3f} | {row['delta']:+.3f} | "
                f"{row['graph_band']} vs {row['llm_band']} |"
            )
    lines.append("")
    return "\n".join(lines)


def _scoreboard(report: GateReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "config_sha": report.config_sha,
        "passed": report.passed,
        "gates": [
            {
                "name": g.name,
                "passed": g.passed,
                "value": g.value,
                "bar": g.bar,
                "detail": g.detail,
            }
            for g in report.gates
        ],
        "evidence": report.evidence,
        "paired": report.paired,
    }


def write_report(report: GateReport, out_dir: Path | str) -> tuple[Path, Path]:
    """Write ``GATE-REPORT.md`` + ``scoreboard.json`` under ``out_dir``.
    Returns ``(markdown_path, json_path)``. Creates ``out_dir`` if needed."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    md_path = target / "GATE-REPORT.md"
    json_path = target / "scoreboard.json"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(_scoreboard(report), indent=2, sort_keys=True), encoding="utf-8")
    return md_path, json_path
