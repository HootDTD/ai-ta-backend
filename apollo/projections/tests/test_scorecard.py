"""Campaign-plan Task B1 — ``render_scorecard`` tests (spec §2). Pure unit
tests: the function is a template over a plain dict, no DB/Neo4j/LLM."""

from __future__ import annotations

import pytest

from apollo.projections.scorecard import BANDS, load_bands, render_scorecard

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
