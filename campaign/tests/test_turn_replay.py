"""Gate for the turn-level replay harness (P3.2 W2-C).

Everything here runs in PLAYBACK mode: canned producer JSON, a patched
adjudicator seam, no database, and — asserted, not assumed — no network.

The four committed W0 fixtures are replayed end to end, each pinning its own
named role: 086 must be refused by the empty-attempt guard, 083 must reach
Done on its single turn, 124 and 167 must carry their ledger shapes through.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apollo.smart_questions import controller
from apollo.smart_questions.leakage import WORD_RE
from campaign import transcript_replay, turn_replay, turn_replay_clients
from campaign.turn_replay import (
    EMPTY_ATTEMPT_REFUSAL,
    LIVE_SAMPLE_MINIMUM,
    LiveClient,
    NetworkBlockedError,
    RecordedClient,
    TurnReplayError,
    TurnResponse,
    compare_arms,
    load_fixture,
    load_fixtures,
    loopback_only_sockets,
    main,
    reconstruct_producer_responses,
    replay_recorded,
    run,
    summary_row,
    to_jsonl,
    turn_row,
)

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(turn_replay.DEFAULT_FIXTURE_DIR)
FIXTURE_NAMES = (
    "attempt_083_paragraph_dump",
    "attempt_086_zero_transcript",
    "attempt_124_conflicting_graded",
    "attempt_167_self_correction",
)


def _fixture(name: str) -> turn_replay.TurnReplayFixture:
    return load_fixture(FIXTURE_DIR / f"{name}.json")


def _raw_payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _tokens(text: str) -> list[str]:
    """The word tokens ``unified._verbatim_span`` itself compares on."""
    return [token.casefold() for token in WORD_RE.findall(text)]


# --------------------------------------------------------------------------- #
# End-to-end playback over the four committed fixtures                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_every_fixture_replays_end_to_end_in_playback_mode(name: str) -> None:
    replay = await replay_recorded(_fixture(name))
    fixture = _fixture(name)
    if fixture.student_turn_ids:
        assert replay.refusal is None
        assert len(replay.turns) == len(fixture.student_turn_ids)
        assert replay.grade is not None
        assert replay.grade.letter
    else:
        assert replay.refusal == EMPTY_ATTEMPT_REFUSAL


async def test_attempt_086_is_refused_by_the_empty_attempt_guard() -> None:
    """Mirrors ``handlers/done``'s P0.1 guard: zero student messages, nothing to
    replay and nothing to adjudicate. The harness must REFUSE, not grade an
    empty attempt into the phantom F(0) rows the guard exists to prevent."""
    replay = await replay_recorded(_fixture("attempt_086_zero_transcript"))
    assert replay.refusal == EMPTY_ATTEMPT_REFUSAL
    assert replay.turns == ()
    assert replay.grade is None
    assert summary_row(replay)["score"] is None


async def test_attempt_083_reaches_done_on_its_single_turn() -> None:
    """The G9 cohort shape: one polished paragraph, zero questions asked, done.

    This measures TURN SHAPE, never grade movement (spec §4.1) — replay cannot
    generate the student's answer to a question that was never asked.
    """
    replay = await replay_recorded(_fixture("attempt_083_paragraph_dump"))
    assert len(replay.turns) == 1
    turn = replay.turns[0]
    assert turn.model_action == "done"
    assert turn.action == "done"
    assert turn.target_node_id is None
    # Pre-W2-A the gate does not exist, so it cannot have fired.
    assert turn.done_gate_fired is False
    assert {row["reference_node_id"] for row in replay.ledger} == {
        row["reference_node_id"]
        for row in _raw_payload("attempt_083_paragraph_dump")["question_opportunities"]
    }


@pytest.mark.parametrize("name", ("attempt_124_conflicting_graded", "attempt_167_self_correction"))
async def test_conflicting_fixture_ledger_shape_carries_through(name: str) -> None:
    """Every recorded node, state and evidence quote survives the replay.

    The replay starts with NO ledger rows (as the attempt did) and rebuilds them
    through the real controller writers, so this asserts the rebuild lands on the
    recorded shape rather than that the fixture was copied forward.

    Quotes are compared as WORD TOKENS, not bytes, and that is a finding rather
    than a convenience: a replayed quote is re-derived by today's
    ``unified._verbatim_span``, whose slice ends at the last word token, so it
    drops the trailing sentence punctuation that several historical prod rows
    carry ( ``"...they have of you."`` -> ``"...they have of you"`` ). Same
    words, same order, same source message — the persisted 2026-07/08 rows are
    simply not fixed points of today's gate. Byte equality here would pin a
    historical artifact, not a contract.
    """
    replay = await replay_recorded(_fixture(name))
    recorded_rows = _raw_payload(name)["question_opportunities"]
    replayed = {row["reference_node_id"]: row for row in replay.ledger}

    assert set(replayed) == {row["reference_node_id"] for row in recorded_rows}
    for row in recorded_rows:
        node_id = row["reference_node_id"]
        assert replayed[node_id]["state"] == row["state"]
        recorded_quotes = [entry["quote"] for entry in row["evidence"]]
        replayed_quotes = [entry["quote"] for entry in replayed[node_id]["evidence"]]
        assert [_tokens(quote) for quote in replayed_quotes] == [
            _tokens(quote) for quote in recorded_quotes
        ]
        # Nothing invented: every replayed span is a slice of its recorded quote.
        assert all(
            replayed_quote in recorded_quote
            for replayed_quote, recorded_quote in zip(replayed_quotes, recorded_quotes, strict=True)
        )


async def test_attempt_167_self_correction_quote_survives_into_the_ledger() -> None:
    """The G-FIX regression content has to REACH the ledger to be testable.

    S2' keys on evidence recency, so if the replay lost this quote the
    self-correction protection would silently have nothing to protect.
    """
    replay = await replay_recorded(_fixture("attempt_167_self_correction"))
    quotes = [entry["quote"] for row in replay.ledger for entry in row["evidence"]]
    assert any("I was wrong about governance" in quote for quote in quotes)


# --------------------------------------------------------------------------- #
# Determinism, network, samples                                                #
# --------------------------------------------------------------------------- #


async def test_replays_a_recorded_transcript_deterministically() -> None:
    fixture = _fixture("attempt_167_self_correction")
    first = await replay_recorded(fixture, sample=0)
    second = await replay_recorded(fixture, sample=0)
    assert [turn_row(turn) for turn in first.turns] == [turn_row(turn) for turn in second.turns]
    assert summary_row(first) == summary_row(second)


async def test_no_network_in_recorded_mode() -> None:
    """Playback must complete with every TCP connect fatal."""

    def refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("recorded mode attempted a socket connect")

    with patch.object(socket.socket, "connect", refuse):
        replay = await replay_recorded(_fixture("attempt_124_conflicting_graded"))
    assert replay.grade is not None
    assert len(replay.turns) == 8


async def test_recorded_mode_never_touches_bounded_client() -> None:
    with patch("apollo.smart_questions.unified.bounded_client") as bounded:
        await replay_recorded(_fixture("attempt_083_paragraph_dump"))
    bounded.assert_not_called()


async def test_samples_flag_runs_n_draws() -> None:
    fixture = _fixture("attempt_083_paragraph_dump")
    replays = await run((fixture,), live=False, samples=3)
    assert [replay.sample for replay in replays] == [0, 1, 2]
    assert {len(replay.turns) for replay in replays} == {1}


async def test_live_lane_uses_one_client_for_the_whole_run() -> None:
    """The live arm keeps ONE recording client across every turn.

    Exercised with a canned client rather than the network: the point under test
    is the lane (one client, one growing recording), not the model.
    """
    fixture = _fixture("attempt_124_conflicting_graded")
    drafts = [response.draft for response in reconstruct_producer_responses(fixture)]
    client = RecordedClient(drafts, repeat_last=True)
    replay = await turn_replay.replay_live(fixture, client=client)
    assert len(replay.turns) == 8
    assert client.calls >= 8
    assert client.recorded[:8] == tuple(drafts)


def test_cli_writes_one_summary_row_per_fixture_and_sample(tmp_path: Path) -> None:
    out = tmp_path / "arm.jsonl"
    assert main(["--fixtures", str(FIXTURE_DIR), "--samples", "2", "--out", str(out)]) == 0
    rows = turn_replay.load_jsonl(out)
    summaries = [row for row in rows if row["kind"] == "summary"]
    assert len(summaries) == 2 * len(FIXTURE_NAMES)
    assert {row["kind"] for row in rows} == {"turn", "summary"}


def test_cli_prints_to_stdout_when_no_out_path_is_given(capsys: Any) -> None:
    single = FIXTURE_DIR / "attempt_086_zero_transcript.json"
    assert main(["--fixtures", str(single.parent), "--samples", "1"]) == 0
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert {row["kind"] for row in printed} == {"turn", "summary"}


def test_cli_live_lane_is_not_wrapped_in_the_loopback_guard() -> None:
    """A live arm must reach the model; only playback claims "no network"."""
    with patch("campaign.turn_replay.replay_live") as live:
        live.return_value = turn_replay.FixtureReplay(
            fixture="f",
            attempt_ref="prod-2026-08-11/attempt-083",
            sample=0,
            refusal=EMPTY_ATTEMPT_REFUSAL,
            turns=(),
            grade=None,
            recorded={},
        )
        args = turn_replay.build_parser().parse_args(
            ["--fixtures", str(FIXTURE_DIR), "--mode", "live", "--samples", "4"]
        )
        lines = turn_replay._run_cli(args)
    assert live.call_count == 4 * len(FIXTURE_NAMES)
    assert len(lines) == 4 * len(FIXTURE_NAMES)


def test_live_mode_refuses_fewer_than_four_samples() -> None:
    """House rule (spec §4): no live arm is concluded from fewer draws."""
    parser = turn_replay.build_parser()
    args = parser.parse_args(["--mode", "live", "--samples", "1"])
    with pytest.raises(SystemExit) as excinfo:
        turn_replay._run_cli(args)
    assert str(LIVE_SAMPLE_MINIMUM) in str(excinfo.value)


def test_empty_fixture_directory_fails_loudly(tmp_path: Path) -> None:
    """An empty directory is a harness defect, never a failing gate."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        load_fixtures(empty)
    assert str(empty) in str(excinfo.value)


def test_missing_fixture_directory_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_fixtures(tmp_path / "does-not-exist")


def test_fixture_version_mismatch_fails_loudly(tmp_path: Path) -> None:
    payload = _raw_payload("attempt_083_paragraph_dump")
    payload["fixture_version"] = 2
    path = tmp_path / "future.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TurnReplayError, match="fixture_version"):
        load_fixture(path)


# --------------------------------------------------------------------------- #
# Seams: the harness must USE production code, not copy it                     #
# --------------------------------------------------------------------------- #


def test_tally_state_rebuild_matches_controller_build_tally_state() -> None:
    """The harness imports the production builder — it does not reimplement it.

    A private rebuild would drift the moment the tally-state enum or the
    evidence shape moved, and the drift would be invisible: both sides would
    still "work", on different data.
    """
    assert turn_replay._build_tally_state is controller._build_tally_state
    assert turn_replay._apply_tally_updates is controller._apply_tally_updates
    assert turn_replay._write_opportunity_audit is controller._write_opportunity_audit

    fixture = _fixture("attempt_167_self_correction")
    graph = fixture.problem.to_kg_graph(attempt_id=-1)
    rows = fixture.recorded_ledger_rows()
    state = {item.node_id: item for item in controller._build_tally_state(graph, list(rows))}
    for row in _raw_payload("attempt_167_self_correction")["question_opportunities"]:
        node_id = row["reference_node_id"]
        assert state[node_id].status == row["state"]
        assert state[node_id].times_asked == row["times_asked"]
        assert [quote.quote for quote in state[node_id].evidence] == [
            entry["quote"] for entry in row["evidence"]
        ]


def test_ledger_row_evidence_is_a_list_for_the_production_readers() -> None:
    """A tuple here would make the ledger look EMPTY instead of failing.

    ``done._latest_student_quote`` and ``controller._evidence_rows`` both gate on
    ``isinstance(value, list)`` and silently yield nothing for any other
    sequence, so the row type is load-bearing.
    """
    rows = _fixture("attempt_124_conflicting_graded").recorded_ledger_rows()
    assert all(isinstance(row.evidence, list) for row in rows)
    assert controller._evidence_rows(rows[0].evidence)


def test_ask_turn_charges_times_asked_with_the_controller_free_pass() -> None:
    """The one controller rule the offline harness mirrors (no DB, no UPDATE).

    A degenerate fallback serve on a node never probed before spends no probe;
    every later serve on the same node charges. ``last_asked_turn`` stamps either
    way.
    """
    row = transcript_replay.LedgerRow(reference_node_id="n1")
    turn_replay._charge_ask(row, turn_index=3, fallback_served=True)
    assert (row.times_asked, row.last_asked_turn) == (0, 4)

    row.asked_turn = 4
    turn_replay._charge_ask(row, turn_index=5, fallback_served=True)
    assert (row.times_asked, row.last_asked_turn) == (1, 6)

    turn_replay._charge_ask(row, turn_index=7, fallback_served=False)
    assert (row.times_asked, row.last_asked_turn) == (2, 8)


def test_done_gate_detection_reads_the_seam_not_gate_internals() -> None:
    """``done_gate_fired`` is "the model said done and the engine served ask".

    Derived from the seam so it stays true before and after W2-A's level-2
    done-gate lands, and so the harness never imports gate internals.
    """
    record = turn_replay.TurnRecord(
        fixture="f",
        sample=0,
        turn_index=0,
        level=2,
        producer_calls=1,
        raw_response='{"action": "done"}',
        model_action="done",
        action="ask",
        target_node_id="n1",
        done_gate_fired=True,
        fallback_served=False,
        askable_ids=("n1",),
        reserved_for_graded=1,
        graded_only=True,
        tally_updates=(),
    )
    assert turn_row(record)["done_gate_fired"] is True
    assert turn_replay._model_action('{"action": "done"}') == "done"
    assert turn_replay._model_action("not json") is None


# --------------------------------------------------------------------------- #
# Client seam (S3)                                                             #
# --------------------------------------------------------------------------- #


def test_recorded_client_replays_in_order_then_raises() -> None:
    client = RecordedClient(("a", "b"))
    assert client.chat.completions.create(model="m").choices[0].message.content == "a"
    assert client.chat.completions.create(model="m").choices[0].message.content == "b"
    with pytest.raises(TurnReplayError, match="exhausted"):
        client.chat.completions.create(model="m")
    assert client.recorded == ("a", "b")
    assert client.calls == 2


def test_recorded_client_repeat_last_covers_the_regenerate() -> None:
    client = RecordedClient(("a",), repeat_last=True)
    client.chat.completions.create(model="m")
    assert client.chat.completions.create(model="m").choices[0].message.content == "a"
    assert client.calls == 2


def test_live_client_records_every_raw_response() -> None:
    """A live arm's recording is exactly what a later playback arm replays."""

    class _Fake:
        def __init__(self) -> None:
            self.chat = self

        @property
        def completions(self) -> Any:
            return self

        def create(self, **kwargs: Any) -> Any:
            return turn_replay_clients._Completion(
                choices=(
                    turn_replay_clients._Choice(
                        message=turn_replay_clients._Message(content='{"a": 1}')
                    ),
                )
            )

    client = LiveClient(factory=_Fake)
    client.chat.completions.create(model="m")
    assert client.recorded == ('{"a": 1}',)
    assert client.requests[0]["model"] == "m"


def test_live_client_defaults_to_bounded_client_and_is_never_built_eagerly() -> None:
    with patch("campaign.turn_replay_clients.bounded_client") as bounded:
        client = LiveClient()
        bounded.assert_not_called()
        client.chat.completions.create(model="m")
        bounded.assert_called_once()


# --------------------------------------------------------------------------- #
# Network guard                                                                #
# --------------------------------------------------------------------------- #


def test_loopback_guard_blocks_non_loopback_and_allows_loopback() -> None:
    calls: list[Any] = []
    with patch.object(socket.socket, "connect", lambda self, address: calls.append(address)):
        with loopback_only_sockets():
            sock = socket.socket()
            sock.connect(("127.0.0.1", 5432))
            with pytest.raises(NetworkBlockedError):
                sock.connect(("api.openai.com", 443))
    assert calls == [("127.0.0.1", 5432)]


def test_loopback_guard_covers_connect_ex_too() -> None:
    """``connect_ex`` is a second door into the same socket — guard both."""
    calls: list[Any] = []
    with patch.object(socket.socket, "connect_ex", lambda self, address: calls.append(address)):
        with loopback_only_sockets():
            sock = socket.socket()
            sock.connect_ex(("127.0.0.1", 5432))
            with pytest.raises(NetworkBlockedError):
                sock.connect_ex(("api.openai.com", 443))
    assert calls == [("127.0.0.1", 5432)]


def test_loopback_guard_restores_the_original_methods() -> None:
    before, before_ex = socket.socket.connect, socket.socket.connect_ex
    with loopback_only_sockets():
        assert socket.socket.connect is not before
        assert socket.socket.connect_ex is not before_ex
    assert socket.socket.connect is before
    assert socket.socket.connect_ex is before_ex
    assert "connect" not in socket.socket.__dict__
    assert "connect_ex" not in socket.socket.__dict__


@pytest.mark.parametrize(
    ("address", "allowed"),
    (
        (("127.0.0.1", 1), True),
        (("127.0.0.53", 1), True),
        (("::1", 1, 0, 0), True),
        (("localhost", 1), True),
        ("/tmp/unix.sock", True),
        (("10.0.0.1", 1), False),
        (("api.openai.com", 443), False),
        ((None, 443), False),
        ((), False),
    ),
)
def test_loopback_classification(address: Any, allowed: bool) -> None:
    assert turn_replay_clients._is_loopback(address) is allowed


# --------------------------------------------------------------------------- #
# Reconstruction honesty                                                       #
# --------------------------------------------------------------------------- #


def test_reconstruction_emits_one_response_per_student_turn() -> None:
    fixture = _fixture("attempt_124_conflicting_graded")
    responses = reconstruct_producer_responses(fixture)
    assert len(responses) == len(fixture.student_turn_ids) == 8
    assert all(json.loads(response.draft)["action"] in {"ask", "done"} for response in responses)


def test_reconstruction_never_invents_student_text() -> None:
    """Every reconstructed quote is a recorded ledger quote, verbatim (§4.1)."""
    fixture = _fixture("attempt_167_self_correction")
    recorded_quotes = {
        entry["quote"] for row in fixture.ledger_payload for entry in row["evidence"]
    }
    for response in reconstruct_producer_responses(fixture):
        for update in json.loads(response.draft)["tally_updates"]:
            assert update["evidence"]["quote"] in recorded_quotes


async def test_explicit_responses_shorter_than_the_transcript_is_rejected() -> None:
    fixture = _fixture("attempt_124_conflicting_graded")
    with pytest.raises(TurnReplayError, match="student turn"):
        await replay_recorded(fixture, responses=[TurnResponse(draft="{}")])


# --------------------------------------------------------------------------- #
# JSONL + arm comparison                                                       #
# --------------------------------------------------------------------------- #


async def test_jsonl_row_schema_is_the_p31_contract() -> None:
    """P3.1 Phase 0 ADDS credit/basis fields; it does not change these keys."""
    replay = await replay_recorded(_fixture("attempt_083_paragraph_dump"))
    lines = list(to_jsonl([replay]))
    rows = [json.loads(line) for line in lines]
    assert [row["kind"] for row in rows] == ["turn", "summary"]
    assert set(rows[0]) == {
        "kind",
        "fixture",
        "sample",
        "turn_index",
        "level",
        "producer_calls",
        "raw_response",
        "model_action",
        "action",
        "target_node_id",
        "done_gate_fired",
        "fallback_served",
        "askable_ids",
        "reserved_for_graded",
        "graded_only",
        "tally_updates",
        "wrongness_findings",
    }
    assert set(rows[1]) == {
        "kind",
        "fixture",
        "attempt_ref",
        "sample",
        "level",
        "refusal",
        "turns",
        "asks",
        "done_gate_fires",
        "wrongness_findings",
        "score",
        "letter",
        "credited_topics",
        "asked_node_ids",
        "topic_credits",
        "recorded_score",
        "recorded_letter",
        "ledger",
    }


async def test_turn_rows_carry_the_wrongness_label_even_at_level_zero() -> None:
    """The label rides on every update; below level 1 it is always ``none``."""
    replay = await replay_recorded(_fixture("attempt_167_self_correction"))
    updates = [update for turn in replay.turns for update in turn.tally_updates]
    assert updates
    assert {update["wrongness"] for update in updates} == {"none"}
    assert replay.wrongness_findings == 0


def test_compare_arms_reports_letters_scores_and_credit_agreement() -> None:
    baseline = [
        {
            "kind": "summary",
            "fixture": "f",
            "sample": 0,
            "score": 90,
            "letter": "A",
            "topic_credits": {"n1": 1.0, "n2": 0.6},
            "wrongness_findings": 0,
        },
    ]
    candidate = [
        {
            "kind": "summary",
            "fixture": "f",
            "sample": 0,
            "score": 84,
            "letter": "B+",
            "topic_credits": {"n1": 1.0, "n2": 0.0},
            "wrongness_findings": 2,
        },
    ]
    report = compare_arms(baseline, candidate)
    assert report["compared"] == 1
    assert report["letters"] == {"baseline": {"A": 1}, "candidate": {"B+": 1}}
    assert report["score_delta"]["signed_mean"] == -6.0
    assert report["score_delta"]["moved"] == 1
    assert report["node_credit_agreement"] == {"agree": 1, "disagree": 1, "rate": 0.5}
    assert report["wrongness_findings"] == {"baseline": 0, "candidate": 2}


def test_compare_arms_reports_unmatched_samples() -> None:
    baseline = [{"kind": "summary", "fixture": "f", "sample": 0, "score": 1, "letter": "F"}]
    candidate = [{"kind": "summary", "fixture": "g", "sample": 0, "score": 1, "letter": "F"}]
    report = compare_arms(baseline, candidate)
    assert report["compared"] == 0
    assert report["baseline_only"] == ["f#0"]
    assert report["candidate_only"] == ["g#0"]


def test_compare_cli_emits_a_report(tmp_path: Path) -> None:
    left, right = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    row = {"kind": "summary", "fixture": "f", "sample": 0, "score": 90, "letter": "A"}
    left.write_text(json.dumps(row) + "\n", encoding="utf-8")
    right.write_text(json.dumps({**row, "score": 80, "letter": "B-"}) + "\n", encoding="utf-8")
    args = turn_replay.build_parser().parse_args(["--compare", str(left), str(right)])
    (payload,) = turn_replay._run_cli(args)
    assert json.loads(payload)["score_delta"]["signed_mean"] == -10.0


# --------------------------------------------------------------------------- #
# Arm selection (the ladder, read through the production single reader)        #
# --------------------------------------------------------------------------- #


def test_wrongness_candidates_are_built_only_from_level_1(monkeypatch) -> None:
    """The at-Done half of an arm. `None` (never `{}`) below level 1 and when
    nothing was flagged, because an empty map still changes the adjudicator's
    prompt — the same rule `done.py` follows."""
    fixture = _fixture("attempt_124_conflicting_graded")
    graph = fixture.problem.to_kg_graph(attempt_id=-1)
    graded = next(
        node.node_id for node in graph.nodes if node.node_type in turn_replay._GRADED_NODE_TYPES
    )
    rows = [
        transcript_replay.LedgerRow(
            reference_node_id=graded,
            state="understood",
            evidence=[
                {
                    "turn_id": 0,
                    "quote": "a verbatim claim",
                    "wrongness": "contradicts_material",
                    "contradicts": "the reference says otherwise",
                    "kind": "reversal",
                }
            ],
        )
    ]

    assert turn_replay._wrongness_candidates(rows, graph=graph, level=0) is None
    assert turn_replay._wrongness_candidates(rows, graph=graph, level=1) == {
        graded: "a verbatim claim"
    }
    # An untagged ledger flags nothing, so the map collapses back to `None`.
    clean = [transcript_replay.LedgerRow(reference_node_id=graded, state="understood")]
    assert turn_replay._wrongness_candidates(clean, graph=graph, level=1) is None


def test_the_level_flag_selects_the_arm_through_the_env(monkeypatch) -> None:
    """`--level N` WRITES `APOLLO_WRONGNESS_LEVEL`; everything downstream still
    READS it through `wrongness.effective_wrongness_level`, so an arm is chosen
    exactly the way a deployment chooses a rung. Omitting the flag leaves the
    ambient environment alone."""
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "1")
    with patch.object(turn_replay, "run", return_value=()) as ran:
        turn_replay._run_cli(turn_replay.build_parser().parse_args(["--level", "2"]))
        assert turn_replay.os.environ["APOLLO_WRONGNESS_LEVEL"] == "2"

        turn_replay._run_cli(turn_replay.build_parser().parse_args([]))
        assert turn_replay.os.environ["APOLLO_WRONGNESS_LEVEL"] == "2"
    assert ran.call_count == 2
