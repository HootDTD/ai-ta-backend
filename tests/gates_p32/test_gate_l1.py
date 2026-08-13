"""§4 gates for LEVEL 1 — the dark bake. One test per gate ID.

| Gate | Spec §4 row | Threshold | Asserted here by |
|---|---|---|---|
| **G-L1** | Inertness: whole ``score_details`` blob byte-identical to level 0, flag ON | exact | `test_gate_G_L1_whole_score_details_inertness` + 3 companions |
| **G-L1b** | Rows with ``wrongness != "none"`` and no surviving verbatim span | **0** | `test_gate_G_L1b_*` (rides `unified._decode_updates`, never re-implements it) |
| **G-L1c** | Over-fire monitor during the dark bake | ``< 10%`` of ledger rows | `test_gate_G_L1c_*` (the reporting helper, plus the log line it reads) |

**WHAT THIS SUITE GUARANTEES, AND WHAT IT DOES NOT — read both.**

**(i) Deterministic, CI-enforced — "the code path is inert."** Both LLM seams
are patched with fixed recorded outputs and the ONLY variable is
``APOLLO_WRONGNESS_LEVEL``: the per-turn producer is not on the Done path at all
(the ledger is seeded directly), the at-Done adjudicator returns a recorded
verdict, the narrator's client is an input-hashed fake, and a loopback socket
guard refuses anything else. ``handle_done`` then runs on IDENTICAL seeded state
once per level and the persisted blobs are compared. Level 0 vs 1 is asserted as
LITERAL whole-blob equality: nothing is populated at level 1, so there is no
permitted delta.

**(ii) NOT deterministic-testable — deferred to the final campaign, stated
honestly.** Whether the LIVE model's tally or verdicts shift *because* the schema
and prompt gained a field is a DISTRIBUTION question, not an identity question.
It is measured against arm A's own noise floor (level 0 x2) and **may never be
asserted as byte-identity**. Two design choices bound the exposure and must be
named in the campaign report: the producer's wrongness block is absent below
level 1 (level 0 is structurally byte-identical), and the adjudicator's
``contradicted`` field and ``FLAGGED CLAIMS`` block appear only when the tally
already raised >=1 candidate — so an attempt with no finding has a
byte-identical adjudication prompt at EVERY level, and G-L1c bounds the
remainder to <10% of ledger rows.

One further honesty note, from wave-2 F-28: when ``topic_score`` soft-fails,
``_evaluate_wrongness`` returns early and emits NO shadow rows for that attempt.
The campaign must count those attempts separately rather than read their absence
as "no findings".
"""

from __future__ import annotations

import json
import logging
import socket

import pytest

from apollo.smart_questions import unified
from tests.gates_p32 import k_criteria
from tests.gates_p32._harness import blob, run_gate_done, score_fingerprint

pytestmark = pytest.mark.unit

_DONE_LOGGER = "apollo.handlers.done"


# --------------------------------------------------------------------------- #
# Methodology controls — the guards themselves must be able to fire            #
# --------------------------------------------------------------------------- #


def test_the_hermeticity_guard_refuses_a_non_loopback_connection():
    """Every claim in this suite is "deterministic because nothing reached a
    model". A guard that never fires would make that claim unfalsifiable, so it
    is exercised directly."""
    with pytest.raises(AssertionError, match="non-loopback"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.01)
            sock.connect(("93.184.216.34", 80))


def test_the_hermeticity_guard_still_delegates_for_loopback():
    """...and it is not a blanket refusal: a Testcontainers/local harness must
    still be able to connect, so loopback reaches the real ``connect`` (and
    fails, if at all, with a network error rather than the guard's assertion)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.05)
        try:
            sock.connect(("127.0.0.1", 1))
        except AssertionError:  # pragma: no cover - the failure this test guards
            pytest.fail("the guard refused loopback")
        except OSError:
            pass  # refused/timed out by the OS — the guard delegated, which is the point


# --------------------------------------------------------------------------- #
# G-L1 — whole-blob inertness at level 1                                       #
# --------------------------------------------------------------------------- #


async def test_gate_G_L1_whole_score_details_inertness(monkeypatch):
    """LITERAL equality of the persisted ``GradingRun.score_details`` blob.

    Not ``served_overall``, not the topic score alone — the whole blob, because
    §2.5's absent-axis hazard moves ``llm_rubric.overall`` one rung BELOW the
    level labelled "score" and a narrower assertion would sail past it.
    """
    level0 = await run_gate_done(monkeypatch, level=0)
    level1 = await run_gate_done(monkeypatch, level=1)

    assert blob(level1.score_details) == blob(level0.score_details)
    # ...and the projection agrees, so a later G-L3 failure can never be blamed
    # on the projection hiding a level-1 difference.
    assert score_fingerprint(level1.score_details) == score_fingerprint(level0.score_details)


async def test_gate_G_L1_diagnostic_report_inertness(monkeypatch):
    """The other persisted blob: ``attempt.diagnostic_report`` in full, and its
    three grade-bearing sub-blobs named individually so a failure says which."""
    level0 = await run_gate_done(monkeypatch, level=0)
    level1 = await run_gate_done(monkeypatch, level=1)

    for key in ("rubric", "coverage", "served_overall", "narrative"):
        assert blob(level1.diagnostic_report[key]) == blob(level0.diagnostic_report[key]), key
    assert blob(level1.diagnostic_report) == blob(level0.diagnostic_report)


async def test_gate_G_L1_served_student_payload_inertness(monkeypatch):
    """R9's surface: the student sees nothing. Whole ``student_response``,
    including the scorecard's *Watch out* list, which level 3 is what lights up."""
    level0 = await run_gate_done(monkeypatch, level=0)
    level1 = await run_gate_done(monkeypatch, level=1)

    assert blob(level1.student_response) == blob(level0.student_response)
    assert level1.served_misconceptions == level0.served_misconceptions == []
    assert level1.topic_misconceptions == []


async def test_gate_G_L1_narrator_input_never_moves(monkeypatch):
    """Sharper than comparing narrative TEXT: the narrator's prompt pair is
    hashed, so this fails even if a moved input happened to render identically.

    Level 3 is the first rung permitted to move it (that is the misconception
    line), and asserting the difference here is what proves the digest is a live
    signal rather than a constant.
    """
    digests = {
        level: (await run_gate_done(monkeypatch, level=level)).narrator_digests
        for level in (0, 1, 2, 3)
    }

    assert digests[1] == digests[0], "level 1 moved the narrator's input"
    assert digests[2] == digests[0], "level 2 moved the narrator's input"
    assert digests[3] != digests[0], "the level-3 misconception line never reached the narrator"
    assert all(len(value) == 1 for value in digests.values())


async def test_gate_G_L1_the_only_level_1_effect_is_the_internal_record(monkeypatch):
    """Level 1 is not a no-op — it PRODUCES. Pinning what it does produce is
    what stops "inert" from being read as "did nothing", and stops a future
    change from making it inert for real (wave-2 §6.4: a test asserting
    "nothing is persisted at level 1" would assert the bug wave 2 fixed)."""
    level0 = await run_gate_done(monkeypatch, level=0)
    level1 = await run_gate_done(monkeypatch, level=1)

    assert level0.wrongness_candidates is None
    assert level1.wrongness_candidates == {"eq1": "Pressure rises wherever the flow speeds up."}
    assert level0.shadow_misconceptions is None
    # `shadow: true` is W3-B's write-side marker (wave-2 F-20): the array is
    # persisted from level 1 for the two INTERNAL readers, so every entry below
    # `LEVEL_SURFACE` is stamped and the teacher readers exclude it. Pinned as
    # part of the whole-entry shape, not separately, so a future author cannot
    # drop the marker and keep this gate green.
    assert level1.shadow_misconceptions == [
        {
            "canonical_key": "eq1",
            "resolved": False,
            "evidence_span": "Pressure rises wherever the flow speeds up.",
            "shadow": True,
        }
    ]


# --------------------------------------------------------------------------- #
# G-L1b — zero wrongness rows without a surviving verbatim span                #
# --------------------------------------------------------------------------- #

_TRANSCRIPT = [("student", "pressure rises wherever the flow speeds up")]
_CONTRADICTION = {"reference_clause": "P + 0.5*rho*v**2 = const", "kind": "reversal"}


def _decode(**overrides):
    item = {
        "node_id": "n1",
        "status": "conflicting",
        "evidence": {"turn_id": 0, "quote": "pressure rises wherever the flow speeds up"},
        "wrongness": "contradicts_material",
        "contradiction": _CONTRADICTION,
    }
    item.update(overrides)
    return unified._decode_updates(
        {"tally_updates": [item]}, valid_ids={"n1"}, transcript=_TRANSCRIPT
    )


def test_gate_G_L1b_no_wrongness_row_survives_without_a_verbatim_span(caplog):
    """The threshold is **0**, and the gate is the PRE-EXISTING verbatim drop
    (``status != "missing" and evidence is None``), asserted rather than
    re-implemented — a second wrongness-specific check would be a second thing
    to keep in sync, and §2.1 is explicit that the shipped one already covers it.
    """
    with caplog.at_level(logging.WARNING):
        survivors = _decode(evidence={"turn_id": 0, "quote": "words the student never typed"})

    assert survivors == ()
    assert "apollo_question_evidence_rejected" in caplog.text


@pytest.mark.parametrize("wrongness", ["contradicts_material", "contradicts_self"])
def test_gate_G_L1b_every_surviving_labelled_row_carries_a_verbatim_span(wrongness):
    """The positive half: whatever DOES survive is anchored to the student's own
    words, taken from the transcript rather than from the model's rendering."""
    (update,) = _decode(
        wrongness=wrongness,
        evidence={"turn_id": 0, "quote": "Pressure rises wherever the flow speeds up!"},
    )

    assert update.wrongness == wrongness
    assert update.evidence is not None
    assert update.evidence.quote in _TRANSCRIPT[0][1]


def test_gate_G_L1b_a_label_without_a_contradiction_object_is_dropped_whole(caplog):
    """A finding with nothing to point at is not a finding. The WHOLE update is
    dropped — the label is never silently stripped and the ``state`` write
    laundered through."""
    with caplog.at_level(logging.WARNING):
        assert _decode(contradiction=None) == ()

    assert "apollo_wrongness_missing_contradiction" in caplog.text


async def test_gate_G_L1b_the_persisted_ledger_never_holds_a_spanless_finding(monkeypatch):
    """Corpus-level restatement, at the Done seam: every entry the level-1
    corroborator is offered carries a non-empty quote, because
    ``candidate_quotes`` filters on exactly that."""
    run = await run_gate_done(monkeypatch, level=1)

    assert run.wrongness_candidates
    assert all(quote.strip() for quote in run.wrongness_candidates.values())
    assert all(entry["evidence_span"] for entry in run.shadow_misconceptions or ())


# --------------------------------------------------------------------------- #
# G-L1c — the over-fire monitor                                                #
# --------------------------------------------------------------------------- #


async def test_gate_G_L1c_shadow_summary_carries_both_numerator_and_denominator(
    monkeypatch, caplog
):
    """G-L1c is a RATIO, and producing the corpus that answers it is the only
    reason level 1 exists. ``findings=`` alone is the numerator; without
    ``ledger_entries=`` the rate cannot be computed from the shadow corpus at all
    (wave-2 F-28). The default ledger is one tagged entry plus one clean one."""
    with caplog.at_level(logging.INFO, logger=_DONE_LOGGER):
        await run_gate_done(monkeypatch, level=1)

    (summary,) = k_criteria.parse_shadow_summaries(caplog.messages)
    assert summary.findings == 1
    assert summary.ledger_entries == 2
    assert summary.level == 1


async def test_gate_G_L1c_reporting_helper_reads_the_real_log_line(monkeypatch, caplog):
    """The gate's deliverable is a helper the CAMPAIGN imports, so it is pinned
    against a real ``handle_done``'s output rather than against a hand-written
    string that could agree with a fiction."""
    with caplog.at_level(logging.INFO, logger=_DONE_LOGGER):
        await run_gate_done(monkeypatch, level=1)

    rate = k_criteria.over_fire(k_criteria.parse_shadow_summaries(caplog.messages))
    assert rate.tagged == 1
    assert rate.ledger_entries == 2
    assert rate.rate == pytest.approx(0.5)
    # This fixture is DELIBERATELY over the threshold (1 of 2 rows labelled): the
    # gate must be able to fail, and a synthetic ledger of two entries is not a
    # corpus. The live threshold is applied to the campaign's corpus, not here.
    assert rate.within_threshold is False
    assert k_criteria.OVER_FIRE_MAX == 0.10


async def test_gate_G_L1c_observed_lines_carry_would_ceiling_per_finding(monkeypatch, caplog):
    """``would_ceiling`` exists ONLY in this log line — it is the level-4
    counterfactual and moves nothing — so K1's input dies with it."""
    with caplog.at_level(logging.INFO, logger=_DONE_LOGGER):
        await run_gate_done(monkeypatch, level=1)

    (observation,) = k_criteria.parse_shadow_observations(caplog.messages)
    assert observation.node_id == "eq1"
    assert observation.rung == "corroborated"
    assert observation.span_verified is True
    assert observation.would_ceiling is True


async def test_gate_G_L1c_no_shadow_corpus_below_level_1(monkeypatch, caplog):
    """Level 0 emits neither line, so an empty corpus can never be misread as a
    level-1 bake that found nothing."""
    with caplog.at_level(logging.INFO, logger=_DONE_LOGGER):
        await run_gate_done(monkeypatch, level=0)

    assert k_criteria.parse_shadow_summaries(caplog.messages) == ()
    assert k_criteria.parse_shadow_observations(caplog.messages) == ()


async def test_gate_G_L1_structural_pin_level_0_never_builds_a_wrongness_prompt(monkeypatch):
    """The level-0 producer pins (sha256 of the schema and the system prompt) are
    owned by W1-A. What W3-C asserts is that the level-0 Done path REFERENCES
    them: no candidate map is built, so the adjudicator's schema, system prompt
    and user message are the ones those digests cover."""
    level0 = await run_gate_done(monkeypatch, level=0)

    assert level0.wrongness_candidates is None
    schema_off = json.dumps(unified._schema(), sort_keys=True)
    schema_on = json.dumps(unified._schema(wrongness=True), sort_keys=True)
    assert schema_off != schema_on, "the wrongness switch is a no-op — nothing is being gated"
    assert "wrongness" not in schema_off
