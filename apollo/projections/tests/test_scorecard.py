"""Campaign-plan Task B1 — ``render_scorecard`` tests (spec §2). Pure unit
tests: the function is a template over a plain dict, no DB/Neo4j/LLM."""

from __future__ import annotations

import pytest

from apollo.grading.artifact_build import build_llm_artifact
from apollo.grading.composite import load_weights
from apollo.projections.scorecard import (
    BANDS,
    COLD_START_WATCH_OUT_NOTE,
    WATCH_OUT_CHECKED,
    WATCH_OUT_NOT_CHECKED_EMPTY_BANK,
    load_bands,
    render_scorecard,
)

pytestmark = pytest.mark.unit


def _artifact(**overrides) -> dict:
    base = {
        "scores": {"composite": 0.6},
        "node_ledger": [
            {
                "canonical_key": "bernoulli_equation",
                "status": "credited",
                "method": "exact",
                "confidence": 1.0,
                "evidence_span": "the pressure plus half rho v squared is constant",
            },
            {
                "canonical_key": "continuity_equation",
                "status": "unresolved",
                "method": None,
                "confidence": 0.0,
                "evidence_span": "",
            },
            {
                "canonical_key": "misc.reverses_causality",
                "status": "misconception",
                "method": "fuzzy",
                "confidence": 0.8,
                "evidence_span": "faster flow causes lower pressure because of magic",
            },
        ],
        "misconceptions": [
            {
                "canonical_key": "misc.reverses_causality",
                "evidence_span": "faster flow causes lower pressure because of magic",
                "confidence": 0.8,
                "opposes": "bernoulli_equation",
            }
        ],
        "clarification_trace": [
            {
                "node_id": "n1",
                "candidate_key": "continuity_equation",
                "probe_question": "What happens to velocity when the pipe narrows?",
                "original_statement": "it stays the same",
                "clarification_text": "it speeds up",
                "state": "confirmed",
                "credit": "granted",
            }
        ],
    }
    base.update(overrides)
    return base


# --- band thresholds -------------------------------------------------------


def test_bands_default_table():
    assert BANDS == (
        ("Strong", 0.85),
        ("Proficient", 0.70),
        ("Developing", 0.50),
        ("Beginning", 0.0),
    )


@pytest.mark.parametrize(
    "composite,expected_band",
    [
        (1.0, "Strong"),
        (0.85, "Strong"),
        (0.8499, "Proficient"),
        (0.70, "Proficient"),
        (0.6999, "Developing"),
        (0.50, "Developing"),
        (0.4999, "Beginning"),
        (0.0, "Beginning"),
    ],
)
def test_band_edges(composite, expected_band):
    art = _artifact(scores={"composite": composite})
    assert render_scorecard(art)["band"] == expected_band


def test_band_thresholds_env_overridable(monkeypatch):
    monkeypatch.setenv("APOLLO_BAND_STRONG", "0.5")
    bands = load_bands()
    assert dict(bands)["Strong"] == 0.5
    art = _artifact(scores={"composite": 0.5})
    assert render_scorecard(art)["band"] == "Strong"


def test_band_env_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APOLLO_BAND_STRONG", "not-a-float")
    assert dict(load_bands())["Strong"] == 0.85


# --- score -------------------------------------------------------------


def test_score_0_100_is_composite_times_100_rounded():
    art = _artifact(scores={"composite": 0.6})
    assert render_scorecard(art)["score_0_100"] == 60

    art2 = _artifact(scores={"composite": 0.8499})
    assert render_scorecard(art2)["score_0_100"] == 85


# --- rubric blocks -------------------------------------------------------


def test_taught_well_carries_credited_nodes_with_evidence_verbatim():
    art = _artifact()
    taught_well = render_scorecard(art)["taught_well"]
    assert taught_well == [
        {
            "key": "bernoulli_equation",
            "evidence_span": "the pressure plus half rho v squared is constant",
        }
    ]


def test_taught_well_omits_evidence_span_when_none():
    """Q2 fix (lane B4): a credited node with no matched student utterance
    (``evidence_span is None`` — the honest LLM-fallback shape, since the LLM
    grader produces per-node coverage but no per-node span) renders the
    ``taught_well`` item WITHOUT an ``evidence_span`` key at all — never an
    empty-string quote pretending to be the student's words (spec §2: "in the
    student's own words")."""
    art = _artifact(
        node_ledger=[
            {
                "canonical_key": "bernoulli_equation",
                "status": "credited",
                "method": None,
                "confidence": 0.9,
                "evidence_span": None,
            },
        ]
    )
    taught_well = render_scorecard(art)["taught_well"]
    assert taught_well == [{"key": "bernoulli_equation"}]
    assert "evidence_span" not in taught_well[0]


def test_taught_well_omits_evidence_span_when_empty_string():
    """Q2 fix (lane B4): a legacy/edge ``evidence_span == ""`` credited row is
    treated identically to ``None`` — the empty string is dropped, never
    rendered as an empty quote."""
    art = _artifact(
        node_ledger=[
            {
                "canonical_key": "bernoulli_equation",
                "status": "credited",
                "method": None,
                "confidence": 0.9,
                "evidence_span": "",
            },
        ]
    )
    taught_well = render_scorecard(art)["taught_well"]
    assert taught_well == [{"key": "bernoulli_equation"}]
    assert "evidence_span" not in taught_well[0]


def test_no_empty_string_evidence_span_anywhere_in_rendered_scorecard():
    """Q2 defect gate (lane B4): under ANY input, no ``taught_well`` item in a
    rendered scorecard carries ``evidence_span == ""``. Exercised against an
    LLM-fallback-shaped ledger (all spans absent) — the exact shape that
    produced ``evidence_span: ""`` in all 12 F1c adjudication packets."""
    art = _artifact(
        node_ledger=[
            {"canonical_key": "k1", "status": "credited", "evidence_span": None},
            {"canonical_key": "k2", "status": "credited", "evidence_span": ""},
            {"canonical_key": "k3", "status": "unresolved", "evidence_span": None},
        ],
        misconceptions=[],
        clarification_trace=[],
    )
    scorecard = render_scorecard(art)
    assert all(item.get("evidence_span") != "" for item in scorecard["taught_well"])
    assert all("evidence_span" not in item for item in scorecard["taught_well"])


def test_missing_or_unclear_carries_unresolved_nodes_as_guidance():
    art = _artifact()
    missing = render_scorecard(art)["missing_or_unclear"]
    assert missing == [
        {"key": "continuity_equation", "guidance": "Next time, explain continuity_equation"}
    ]


def test_missing_or_unclear_handles_no_key():
    art = _artifact(
        node_ledger=[
            {
                "canonical_key": None,
                "status": "unresolved",
                "method": None,
                "confidence": 0.0,
                "evidence_span": "some stray utterance",
            },
        ]
    )
    missing = render_scorecard(art)["missing_or_unclear"]
    assert missing == [{"key": None, "guidance": "Next time, explain this step"}]


def test_missing_or_unclear_renders_missing_node_shape_with_null_evidence():
    """Task 3 scorecard fix: a MISSING_NODE ledger row (``artifact_build.
    _missing_ledger_entry`` — a reference node the student never mentioned)
    has ``evidence_span``/``confidence`` explicitly ``None``, not ``""``/
    ``0.0``. The scorecard template must still render readable guidance off
    its (real, display-safe) ``canonical_key`` without choking on the
    ``None`` fields it never reads for this block."""
    art = _artifact(
        node_ledger=[
            {
                "canonical_key": "continuity_equation",
                "status": "unresolved",
                "method": None,
                "confidence": None,
                "evidence_span": None,
            },
        ]
    )
    missing = render_scorecard(art)["missing_or_unclear"]
    assert missing == [
        {"key": "continuity_equation", "guidance": "Next time, explain continuity_equation"}
    ]


def test_watch_out_quotes_the_triggering_utterance():
    art = _artifact()
    watch_out = render_scorecard(art)["watch_out"]
    assert watch_out == [
        {
            "key": "misc.reverses_causality",
            "quote": "faster flow causes lower pressure because of magic",
        }
    ]


# --- Lane B3a/D1: empty-bank vs found-none watch_out disambiguation ---------


def _empty_bank_abstention() -> dict:
    """The persisted ``abstention`` block a lane-B3a/D1 empty-bank artifact
    carries (``build_*_artifact`` nests the marker here on the empty-bank path)."""
    return {
        "abstained": False,
        "reasons": [],
        "misconceptions_status": {
            "assertable": False,
            "reason": "empty_bank",
            "detail": "no misconceptions asserted (empty bank)",
        },
    }


def test_watch_out_checked_when_no_empty_bank_marker():
    """A seeded-bank grade that fired no misconceptions: `watch_out` is empty
    because the grader CHECKED and found none — status `checked`, no note."""
    art = _artifact(misconceptions=[])
    out = render_scorecard(art)
    assert out["watch_out"] == []
    assert out["watch_out_status"] == WATCH_OUT_CHECKED
    assert out["watch_out_note"] is None


def test_watch_out_not_checked_when_empty_bank_marker_present():
    """Cold-start empty bank: `watch_out` is empty because soundness was NEVER
    assessed — status `not_checked_empty_bank` + an explicit teacher-facing
    note, sourced from the persisted `abstention.misconceptions_status` marker."""
    art = _artifact(misconceptions=[], abstention=_empty_bank_abstention())
    out = render_scorecard(art)
    assert out["watch_out"] == []
    assert out["watch_out_status"] == WATCH_OUT_NOT_CHECKED_EMPTY_BANK
    assert out["watch_out_note"] == COLD_START_WATCH_OUT_NOTE
    assert "cold start" in out["watch_out_note"].lower()


def test_scorecard_distinguishes_empty_bank_from_found_none():
    """A's explicit ask: the two identical-looking empty-`watch_out` scorecards
    (checked-found-none vs never-checked-empty-bank) must NOT render the same.
    The distinction is what stops a teacher reading "(none)" on a cold-start
    class as "checked, all clear"."""
    found_none = render_scorecard(_artifact(misconceptions=[]))
    empty_bank = render_scorecard(
        _artifact(misconceptions=[], abstention=_empty_bank_abstention())
    )
    # Same empty watch_out list...
    assert found_none["watch_out"] == empty_bank["watch_out"] == []
    # ...but the scorecards are NOT identical: status + note diverge.
    assert found_none != empty_bank
    assert found_none["watch_out_status"] != empty_bank["watch_out_status"]
    assert found_none["watch_out_note"] is None
    assert empty_bank["watch_out_note"] is not None


def test_misconception_node_ledger_row_excluded_from_taught_well():
    art = _artifact()
    keys = {entry["key"] for entry in render_scorecard(art)["taught_well"]}
    assert "misc.reverses_causality" not in keys


def test_clarifications_shown_inline():
    art = _artifact()
    clar = render_scorecard(art)["clarifications"]
    assert clar == [
        {
            "question": "What happens to velocity when the pipe narrows?",
            "answer": "it speeds up",
            "credit": "granted",
        }
    ]


def test_clarifications_shown_inline_on_llm_fallback_artifact():
    """A2/G2 fix: the served-served-canonical LLM artifact (``grader_used=
    "llm_fallback"``) must render its real clarification trace too — the
    scorecard's clarifications block is grader-agnostic, but this closes the
    loop end-to-end through the actual ``build_llm_artifact`` builder rather
    than a hand-built dict."""
    trace = [
        {
            "node_id": "n1",
            "candidate_key": "continuity_equation",
            "probe_question": "What happens to velocity when the pipe narrows?",
            "original_statement": "it stays the same",
            "clarification_text": "it speeds up",
            "state": "confirmed",
            "credit": "granted",
        }
    ]
    art = build_llm_artifact(
        coverage={"per_step": {"k1": "covered"}, "confidences": {"k1": 0.9}},
        rubric={"overall": {"score": 71}},
        weights=load_weights(),
        graph_failure=None,
        latency_ms=None,
        clarification_trace=trace,
    )
    clar = render_scorecard(art)["clarifications"]
    assert clar == [
        {
            "question": "What happens to velocity when the pipe narrows?",
            "answer": "it speeds up",
            "credit": "granted",
        }
    ]


def test_empty_ledgers_render_empty_blocks():
    art = _artifact(node_ledger=[], misconceptions=[], clarification_trace=[])
    out = render_scorecard(art)
    assert out["taught_well"] == []
    assert out["missing_or_unclear"] == []
    assert out["watch_out"] == []
    assert out["clarifications"] == []


def test_missing_optional_keys_default_gracefully():
    """An artifact missing optional list keys entirely (e.g. a minimal
    hand-built dict) still renders — defensive `.get(..., [])`."""
    out = render_scorecard({"scores": {"composite": 0.9}})
    assert out["band"] == "Strong"
    assert out["taught_well"] == []
    assert out["missing_or_unclear"] == []
    assert out["watch_out"] == []
    assert out["clarifications"] == []


# --- determinism -------------------------------------------------------


def test_determinism_same_artifact_identical_output():
    art = _artifact()
    assert render_scorecard(art) == render_scorecard(art)
