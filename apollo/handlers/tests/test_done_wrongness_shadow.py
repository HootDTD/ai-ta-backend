"""Level 0/1 of the P3.2 ladder at Done: candidates, S2′, and the shadow corpus.

Level 0 must be byte-identical to the pre-feature build — proved here by the
adjudicator receiving ``wrongness_candidates=None`` and by a digest of the whole
served payload, not by reading the diff. Level 1 adds exactly three things and
nothing else: the corroborator gets candidates, S2′ is evaluated, and every rung
is logged. No score, no container, no XP moves at level 1.
"""

from __future__ import annotations

import json
import logging

import pytest

from apollo.handlers.tests import _wrongness_fixtures as wf

pytestmark = pytest.mark.unit


def _served_digest(out: dict) -> str:
    """A stable digest of everything the student is served, minus the wall-clock
    grading latency (which is the only legitimately per-run value)."""
    payload = {k: v for k, v in out.items() if k != "grading_provenance"}
    provenance = dict(out.get("grading_provenance") or {})
    payload["grading_provenance"] = provenance
    return json.dumps(payload, sort_keys=True, default=str)


# --- level 0: nothing happens ---------------------------------------------


async def test_level_0_never_builds_candidates(monkeypatch):
    _out, started = await wf.run_done(monkeypatch, level=0)

    kwargs = started["compute_transcript_coverage_with_spans"].await_args.kwargs
    assert kwargs["wrongness_candidates"] is None


async def test_level_0_served_payload_is_identical_to_a_ledger_with_no_labels(monkeypatch):
    """The whole-payload inertness pin (§2.5's hazard is about `score_details`,
    so a `served_overall`-only assertion is not enough). A contradicted ledger
    at level 0 produces byte-for-byte what an untagged ledger produces — the
    adjudication is held identical (no candidates are built at level 0, so a
    real corroborator would return no map either)."""
    tagged, _ = await wf.run_done(monkeypatch, level=0, wrongness_map=None)
    clean, _ = await wf.run_done(
        monkeypatch,
        level=0,
        wrongness_map=None,
        ledger=(
            wf.LedgerRow("eq1", evidence=[{"turn_id": 4, "quote": wf.MATERIAL_QUOTE}]),
            wf.LedgerRow("c1", evidence=[{"turn_id": 5, "quote": "Steady flow only."}]),
        ),
    )

    assert _served_digest(tagged) == _served_digest(clean)


async def test_level_0_logs_no_wrongness_line(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=0)

    assert "apollo_wrongness_observed" not in caplog.text
    assert "apollo_wrongness_summary" not in caplog.text


# --- level 1: produce, corroborate, log ------------------------------------


async def test_level_1_sends_the_flagged_quote_to_the_corroborator(monkeypatch):
    _out, started = await wf.run_done(monkeypatch, level=1)

    kwargs = started["compute_transcript_coverage_with_spans"].await_args.kwargs
    # Only the contradicted GRADED node, carrying the ledger's verbatim quote —
    # the corroborator is never handed a span it could originate a finding from.
    assert kwargs["wrongness_candidates"] == {"eq1": wf.MATERIAL_QUOTE}


async def test_level_1_logs_wrongness_observed_with_would_ceiling(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=1)

    line = next(m for m in caplog.messages if m.startswith("apollo_wrongness_observed"))
    assert "node_id=eq1" in line
    assert "rung=corroborated" in line
    assert "span_verified=True" in line
    # Both graded nodes are credited 1.0, so the raw score is 100 > 84.
    assert "would_ceiling=True" in line
    assert "kind=reversal" in line


async def test_level_1_logs_summary_line(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=1)

    line = next(m for m in caplog.messages if m.startswith("apollo_wrongness_summary"))
    assert "findings=1" in line and "nodes=1" in line
    assert "corroborated=1" in line and "would_ceiling=1" in line
    assert "level=1" in line


async def test_summary_counts_entries_and_nodes_separately(monkeypatch, caplog):
    """`select_findings` returns one rung per EVIDENCE ENTRY (integration
    finding F-06), so a node probed twice yields two rungs. The summary must
    report both numbers or the shadow corpus over-counts affected students."""
    twice = (
        wf.LedgerRow(
            "eq1",
            evidence=[
                *wf.tagged_evidence(turn_id=3),
                *wf.tagged_evidence(turn_id=6),
            ],
        ),
    )
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=1, ledger=twice)

    line = next(m for m in caplog.messages if m.startswith("apollo_wrongness_summary"))
    assert "findings=2" in line
    assert "nodes=1" in line
    # Only the LATEST entry can corroborate, so the earlier rung is reported only.
    assert "corroborated=1" in line


async def test_absent_second_reader_row_is_not_corroborated(monkeypatch, caplog):
    """Fail-safe = miss (§8 D6): the corroborator's silence removes a
    consequence, it never creates one."""
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=1, wrongness_map={})

    observed = next(m for m in caplog.messages if m.startswith("apollo_wrongness_observed"))
    assert "rung=reported" in observed
    assert "second_reader=absent" in observed
    assert "would_ceiling=False" in observed
    summary = next(m for m in caplog.messages if m.startswith("apollo_wrongness_summary"))
    assert "corroborated=0" in summary


async def test_denied_by_the_second_reader_is_not_corroborated(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=1, wrongness_map=wf.second_reader(contradicted=False))

    assert "rung=reported" in caplog.text
    assert "corroborated=0" in caplog.text


async def test_level_1_moves_no_number(monkeypatch):
    """Level 1's ship condition: findings exist and are logged, and the served
    grade is what the same attempt scores with the ladder off."""
    off, _ = await wf.run_done(monkeypatch, level=0)
    on, _ = await wf.run_done(monkeypatch, level=1)

    assert _served_digest(off) == _served_digest(on)


async def test_ledger_read_is_still_a_single_query(monkeypatch):
    """P1.3/P1.2b/P3.2 share ONE `_question_ledger` read; a second one would
    double the Done's DB round trips for a telemetry-adjacent feature."""
    _out, started = await wf.run_done(monkeypatch, level=1)

    started["_question_ledger"].assert_awaited_once()


async def test_failed_ledger_read_produces_no_findings(monkeypatch, caplog):
    """`_question_ledger` owns its failure domain: `None` degrades every
    consumer, wrongness included, rather than raising into the grade path."""
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        out, started = await wf.run_done(monkeypatch, level=1, ledger=None)

    assert (
        started["compute_transcript_coverage_with_spans"].await_args.kwargs["wrongness_candidates"]
        is None
    )
    assert "apollo_wrongness_observed" not in caplog.text
    assert out["rubric"]["overall"]["score"] == 100


async def test_ungraded_node_is_reported_but_never_corroborated(monkeypatch, caplog):
    """A node outside `_GRADED_NODE_TYPES` may carry wrongness for the narrative
    and teacher surfaces and may never move the score — widening the graded
    denominator is P1.4's decision, not this one."""
    ledger = (wf.LedgerRow("not_a_rubric_node", evidence=wf.tagged_evidence()),)
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        _out, started = await wf.run_done(
            monkeypatch,
            level=1,
            ledger=ledger,
            wrongness_map=wf.second_reader(node_id="not_a_rubric_node"),
        )

    assert (
        started["compute_transcript_coverage_with_spans"].await_args.kwargs["wrongness_candidates"]
        is None
    )
    assert "rung=reported" in caplog.text
    assert "corroborated=0" in caplog.text
