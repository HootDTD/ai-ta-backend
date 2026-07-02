"""Campaign-plan Task B1 — student scorecard renderer (spec 2026-07-01 §2).

``render_scorecard`` is a PURE TEMPLATE over an already-built canonical
grading artifact payload (the dict shape ``apollo.grading.artifact_build``'s
``build_graph_artifact``/``build_llm_artifact`` produce, or the identical
JSONB-column shape of a persisted ``GradingArtifact`` row). It performs NO
computation beyond formatting: band assignment is a threshold lookup on the
artifact's already-computed ``scores.composite``; the rubric blocks are
straight reshapes of the node/misconception/clarification ledgers the
artifact already carries. Nothing here is graded, resolved, or generated —
the artifact's ledgers ARE the answer (spec §2: "nothing computed fresh").
Both graders' artifacts render through this exact same template, which is
how the live flow (spec §3 step 3) shows "the same scorecard shape either
way" regardless of ``grader_used``.

Pure module: no DB/Neo4j/LLM imports. ``os.environ`` is read fresh on every
call (mirrors ``apollo.grading.composite.load_weights``) so campaign tuning
runs can retune band thresholds between attempts without a process restart.
"""

from __future__ import annotations

import os

# Band threshold env var names (Task B1). Defaults match the spec's initial
# calibration; the campaign's tuning phase overrides them per run, never the
# code. "Beginning" has no configurable floor — it is whatever composite
# doesn't clear "Developing" (always 0.0).
_ENV_STRONG = "APOLLO_BAND_STRONG"
_ENV_PROFICIENT = "APOLLO_BAND_PROFICIENT"
_ENV_DEVELOPING = "APOLLO_BAND_DEVELOPING"

_DEFAULT_STRONG = 0.85
_DEFAULT_PROFICIENT = 0.70
_DEFAULT_DEVELOPING = 0.50

# The spec's literal default band table (name, threshold), high-to-low. Kept
# as a module constant for callers/tests that want the documented defaults
# without touching the environment; `load_bands()` is the live, env-aware
# source of truth used by `render_scorecard`.
BANDS: tuple[tuple[str, float], ...] = (
    ("Strong", _DEFAULT_STRONG),
    ("Proficient", _DEFAULT_PROFICIENT),
    ("Developing", _DEFAULT_DEVELOPING),
    ("Beginning", 0.0),
)


def _env_float(name: str, default: float) -> float:
    """Read ``name`` from the environment as a float; fall back to ``default``
    on missing or malformed (mirrors ``composite.py``'s env-float reader)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def load_bands() -> tuple[tuple[str, float], ...]:
    """Build the (name, threshold) band table from the environment, falling
    back to the spec's defaults on missing/malformed values. Read fresh on
    every call — no process-lived caching."""
    return (
        ("Strong", _env_float(_ENV_STRONG, _DEFAULT_STRONG)),
        ("Proficient", _env_float(_ENV_PROFICIENT, _DEFAULT_PROFICIENT)),
        ("Developing", _env_float(_ENV_DEVELOPING, _DEFAULT_DEVELOPING)),
        ("Beginning", 0.0),
    )


def _band_for(composite: float, bands: tuple[tuple[str, float], ...]) -> str:
    """First band (in high-to-low order) whose threshold ``composite`` meets
    or exceeds. The last band's threshold is always 0.0, so this always
    resolves — the fallback return is unreachable defensive code."""
    for name, threshold in bands:
        if composite >= threshold:
            return name
    return bands[-1][0]  # pragma: no cover - unreachable, last threshold is 0.0


def _taught_well(node_ledger: list[dict]) -> list[dict]:
    """*Taught well*: credited nodes with the student's own evidence span
    verbatim (spec §2)."""
    return [
        {"key": entry.get("canonical_key"), "evidence_span": entry.get("evidence_span") or ""}
        for entry in node_ledger
        if entry.get("status") == "credited"
    ]


def _missing_or_unclear(node_ledger: list[dict]) -> list[dict]:
    """*Missing or unclear*: unresolved nodes phrased as next-time guidance
    (spec §2) via a fixed, deterministic template string — no generation."""
    out: list[dict] = []
    for entry in node_ledger:
        if entry.get("status") != "unresolved":
            continue
        key = entry.get("canonical_key")
        name = key if key else "this step"
        out.append({"key": key, "guidance": f"Next time, explain {name}"})
    return out


def _watch_out(misconceptions: list[dict]) -> list[dict]:
    """*Watch out*: asserted misconceptions quoting the triggering student
    utterance (spec §2)."""
    return [
        {"key": m.get("canonical_key"), "quote": m.get("evidence_span") or ""}
        for m in misconceptions
    ]


def _clarifications(clarification_trace: list[dict]) -> list[dict]:
    """Clarification exchanges shown inline (spec §2) — question, the
    student's answer, and whether it earned credit, straight off the
    artifact's clarification-trace rows (``artifact_writer._load_clarification_trace``
    shape: ``probe_question``/``clarification_text``/``credit``)."""
    return [
        {
            "question": row.get("probe_question"),
            "answer": row.get("clarification_text"),
            "credit": row.get("credit"),
        }
        for row in clarification_trace
    ]


def render_scorecard(artifact: dict) -> dict:
    """Pure template over a canonical artifact payload (spec §2): nothing
    computed fresh. Deterministic — the same artifact always renders to the
    identical scorecard dict.

    ``artifact`` is the dict shape ``build_graph_artifact``/
    ``build_llm_artifact`` (``apollo.grading.artifact_build``) produce, keyed
    by ``scores``, ``node_ledger``, ``misconceptions``, ``clarification_trace``
    (and other identity/versions fields this renderer ignores).
    """
    scores = artifact.get("scores") or {}
    composite = float(scores.get("composite", 0.0))
    node_ledger = artifact.get("node_ledger") or []
    return {
        "score_0_100": round(composite * 100),
        "band": _band_for(composite, load_bands()),
        "taught_well": _taught_well(node_ledger),
        "missing_or_unclear": _missing_or_unclear(node_ledger),
        "watch_out": _watch_out(artifact.get("misconceptions") or []),
        "clarifications": _clarifications(artifact.get("clarification_trace") or []),
    }
