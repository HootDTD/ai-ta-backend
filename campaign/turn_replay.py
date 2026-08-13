"""Turn-level replay of a recorded Apollo attempt through the LIVE questioning engine.

``transcript_replay`` replays the at-Done grader; this replays the *per-turn*
producer. It feeds a recorded attempt's student turns, one at a time, through
the real :func:`apollo.smart_questions.unified.evaluate_and_ask` with a client
injected through seam **S3**, rebuilds the question-opportunity ledger with the
real controller writers, and finishes by grading the attempt through
``transcript_replay.grade_replay`` — the same core the grader gate uses.

Two client modes, chosen by constructor argument and never by env sniff, and
re-exported from ``campaign/turn_replay_clients.py`` (split out purely because
this module crossed the 800-line file cap):

* :class:`RecordedClient` — PLAYBACK. Replays canned producer JSON. Fully
  deterministic, zero network; this is what the regression tests run.
* :class:`LiveClient` — RECORDING. Delegates to ``bounded_client()`` and keeps
  every raw response, so a live campaign arm produces the exact material a
  later playback run replays.

**Reusability contract (P3.1 Phase 0 reuses this UNCHANGED).** Three things are
the contract: the injected client (S3 — ``client.chat.completions.create``
returning ``choices[0].message.content``), the frozen fixture schema under
``campaign/fixtures/turn_replay/`` (``fixture_version: 1``), and the JSONL row
schema emitted here (``kind: "turn"`` / ``kind: "summary"``). P3.1 ADDS
per-node ``credit``/``basis`` fields to the turn rows. It does not change the
seam, the fixture schema, or the row kinds.

**What this cannot do (spec §4.1).** Replay cannot generate a student's answer
to a question that was never asked in the recording. Nothing here synthesizes
student text: the transcript is the recording, verbatim, and the reconstructed
producer output is rebuilt from what the controller PERSISTED. A gated turn
therefore measures *gating*, never grade movement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.ontology import KGGraph
from apollo.overseer.topic_score import _GRADED_NODE_TYPES
from apollo.overseer.wrongness import (
    LEVEL_PRODUCE,
    candidate_quotes,
    effective_wrongness_level,
    ledger_findings,
)
from apollo.schemas.problem import Problem
from apollo.smart_questions.controller import (
    _apply_tally_updates,
    _build_tally_state,
    _ladder_inputs,
    _write_opportunity_audit,
)
from apollo.smart_questions.selection import SelectionPolicy, build_selection_policy
from apollo.smart_questions.unified import (
    QuestionBudget,
    evaluate_and_ask,
    question_cap,
)
from campaign.transcript_replay import (
    LedgerRow,
    ReplayOutcome,
    fixture_paths,
    grade_replay,
    ledger_rows,
)
from campaign.turn_replay_clients import (
    LiveClient,
    NetworkBlockedError,
    RecordedClient,
    ReplayClient,
    TurnReplayError,
    loopback_only_sockets,
)

FIXTURE_VERSION = 1
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "turn_replay"

#: Mirrors ``handlers/done``'s P0.1 empty-attempt guard (``EmptyAttemptError``):
#: an attempt with zero student messages has nothing to replay and nothing to
#: adjudicate, and grading it produced the phantom F(0) rows that guard exists
#: to refuse. The harness refuses it too, and says so in the output.
EMPTY_ATTEMPT_REFUSAL = "empty_attempt"

#: House rule (spec §4): a live-LLM arm is never concluded from fewer draws.
LIVE_SAMPLE_MINIMUM = 4

# Placeholder identities. The fixtures are PII-scrubbed and carry no course,
# session, or attempt id; the controller writers need *some* value for the rows
# they mint, and nothing offline ever reads them back.
_REPLAY_COURSE_ID = -1
_REPLAY_SESSION_ID = -1
_REPLAY_ATTEMPT_ID = -1

_WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TurnReplayFixture:
    """One committed, PII-scrubbed prod attempt (``fixture_version: 1``)."""

    name: str
    attempt_ref: str
    concept_id: int
    concept_slug: str
    problem: Problem
    messages: tuple[tuple[str, str], ...]
    ledger_payload: tuple[dict[str, Any], ...]
    adjudicator_output: dict[str, Any]
    recorded: dict[str, Any]

    @property
    def student_turn_ids(self) -> tuple[int, ...]:
        """Message indexes of the student turns, in order.

        Indexes into the WHOLE message list, not into a student-only list:
        ``evaluate_and_ask`` enumerates the transcript uniformly and the ledger's
        ``turn_id`` values are those same indexes.
        """
        return tuple(index for index, (role, _) in enumerate(self.messages) if role == "student")

    def recorded_ledger_rows(self) -> list[LedgerRow]:
        """The ledger as prod left it at Done — the END state, not the start.

        A turn replay does NOT start here (it starts with no rows at all, as the
        attempt did); this is the recorded shape a replay's own ledger is
        compared against.
        """
        return cast(list[LedgerRow], ledger_rows(list(self.ledger_payload)))


def load_fixture(path: Path) -> TurnReplayFixture:
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("fixture_version")
    if version != FIXTURE_VERSION:
        raise TurnReplayError(
            f"{path.name}: fixture_version {version!r} is not the frozen {FIXTURE_VERSION}"
        )
    return TurnReplayFixture(
        name=path.stem,
        attempt_ref=str(payload["attempt_ref"]),
        concept_id=int(payload["concept"]["id"]),
        concept_slug=str(payload["concept"]["slug"]),
        problem=Problem.model_validate(payload["problem"]),
        messages=tuple((str(m["role"]), str(m["content"])) for m in payload["messages"]),
        ledger_payload=tuple(payload["question_opportunities"]),
        adjudicator_output=payload["adjudicator_output"],
        recorded=payload["recorded"],
    )


def load_fixtures(directory: Path = DEFAULT_FIXTURE_DIR) -> tuple[TurnReplayFixture, ...]:
    """Every fixture in ``directory``; an empty directory fails loudly."""
    return tuple(load_fixture(path) for path in fixture_paths(directory))


# --------------------------------------------------------------------------- #
# Producer-output reconstruction                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TurnResponse:
    """The producer output(s) for ONE student turn: draft, then any regenerate."""

    draft: str
    regenerate: str | None = None

    def as_responses(self) -> tuple[str, ...]:
        return (self.draft,) if self.regenerate is None else (self.draft, self.regenerate)


def _normalized(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def reconstruct_producer_responses(fixture: TurnReplayFixture) -> tuple[TurnResponse, ...]:
    """Producer JSON per student turn, RECONSTRUCTED from what prod persisted.

    Production never stored the raw producer response, so this is not a
    recording — it is rebuilt from the question-opportunity ledger and the
    served Apollo turns, both of which are production data:

    * ``tally_updates`` — one per evidence entry whose ``turn_id`` is this turn,
      carrying that row's state and the verbatim-gated student quote the
      producer emitted at the time.
    * ``action`` / ``target_node_id`` — ``ask`` when a row records
      ``asked_turn == turn + 1`` (the controller stamps the served Apollo turn
      index there), or when the next Apollo turn is itself a question; ``done``
      otherwise.
    * ``question`` — the row's persisted ``question``, i.e. exactly what was
      served.

    Two approximations, both documented, both lossy in the same direction the
    ledger itself is lossy — and neither invents student content:

    1. ``status`` is the row's FINAL state, because the ledger keeps one state
       per node and no per-turn history.
    2. Only the LAST ask per node survives in ``asked_turn``, so an earlier ask
       on a re-asked node replays with no resolved target; the turn still
       replays as ``ask`` and ``evaluate_and_ask`` picks the target from its own
       policy.

    **Never synthesizes a student answer** (spec §4.1).
    """
    rows = list(fixture.ledger_payload)
    served_by_turn = {
        int(row["asked_turn"]): row for row in rows if row.get("asked_turn") is not None
    }
    responses: list[TurnResponse] = []
    for turn_id in fixture.student_turn_ids:
        updates = [
            {
                "node_id": str(row["reference_node_id"]),
                "status": str(row["state"]),
                "evidence": {"turn_id": turn_id, "quote": entry["quote"]},
            }
            for row in rows
            for entry in (row.get("evidence") or [])
            if entry.get("turn_id") == turn_id
        ]
        served = served_by_turn.get(turn_id + 1)
        reply = fixture.messages[turn_id + 1] if turn_id + 1 < len(fixture.messages) else None
        apollo_question = (
            _normalized(reply[1]) if reply is not None and reply[0] == "apollo" else ""
        )
        asked = served is not None or apollo_question.endswith("?")
        question = str(served["question"]) if served and served.get("question") else apollo_question
        responses.append(
            TurnResponse(
                draft=json.dumps(
                    {
                        "tally_updates": updates,
                        "action": "ask" if asked else "done",
                        "target_node_id": (
                            str(served["reference_node_id"]) if served is not None else None
                        ),
                        "acknowledgement": "",
                        "question": question if asked else "",
                    },
                    ensure_ascii=False,
                )
            )
        )
    return tuple(responses)


# --------------------------------------------------------------------------- #
# Replay records                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TurnRecord:
    """Everything one replayed turn produced. One JSONL row per instance."""

    fixture: str
    sample: int
    turn_index: int
    #: `APOLLO_WRONGNESS_LEVEL` as `effective_wrongness_level` resolved it for
    #: THIS fixture's concept — recorded per row so a JSONL file can never be
    #: mistaken for an arm it was not run at.
    level: int
    producer_calls: int
    raw_response: str
    #: What the MODEL asked for, straight out of the raw response.
    model_action: str | None
    #: What the engine SERVED after policy, budget and (level >= 2) the done-gate.
    action: str
    target_node_id: str | None
    #: The model said "done" and the engine served "ask" — the only observable
    #: signature of W2-A's level-2 done-gate, read off the seam rather than by
    #: importing gate internals, so this stays true before and after it lands.
    done_gate_fired: bool
    fallback_served: bool
    askable_ids: tuple[str, ...]
    reserved_for_graded: int
    graded_only: bool
    tally_updates: tuple[dict[str, Any], ...]

    @property
    def wrongness_findings(self) -> int:
        return sum(1 for u in self.tally_updates if u.get("wrongness", "none") != "none")


@dataclass(frozen=True)
class FixtureReplay:
    """One fixture, one sample: every turn plus the at-Done grade."""

    fixture: str
    attempt_ref: str
    sample: int
    refusal: str | None
    turns: tuple[TurnRecord, ...]
    grade: ReplayOutcome | None
    recorded: dict[str, Any]
    level: int = 0
    ledger: tuple[dict[str, Any], ...] = ()

    @property
    def wrongness_findings(self) -> int:
        return sum(turn.wrongness_findings for turn in self.turns)


class _NullSession:
    """The one thing the controller writers need from a session: ``add``."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


def _charge_ask(row: Any, *, turn_index: int, fallback_served: bool) -> None:
    """Mirror ``controller.plan_next_question``'s ask bookkeeping, offline.

    The ONE piece of controller logic this harness reimplements, because the
    real one (``_bump_times_asked``) is an atomic ``UPDATE ... RETURNING`` and
    there is no database here. The rule is copied verbatim from its call site: a
    degenerate ``fallback_served`` turn on a node never probed before spends no
    probe; every later serve on the same node charges. ``last_asked_turn`` is
    stamped either way.
    """
    if not fallback_served or row.asked_turn is not None:
        row.times_asked = int(row.times_asked or 0) + 1
    row.last_asked_turn = turn_index + 1


def _serialize_update(update: Any) -> dict[str, Any]:
    return {
        "node_id": update.node_id,
        "status": update.status,
        "wrongness": update.wrongness,
        "contradicts": (
            update.contradiction.reference_clause if update.contradiction is not None else None
        ),
        "kind": update.contradiction.kind if update.contradiction is not None else None,
        "turn_id": update.evidence.turn_id if update.evidence is not None else None,
        "quote": update.evidence.quote if update.evidence is not None else None,
    }


def _serialize_row(row: Any) -> dict[str, Any]:
    return {
        "reference_node_id": str(row.reference_node_id),
        "state": str(row.state),
        "times_asked": int(row.times_asked or 0),
        "last_asked_turn": row.last_asked_turn,
        "asked_turn": row.asked_turn,
        "answered_turn": row.answered_turn,
        "evidence": list(row.evidence or []),
    }


def _model_action(raw: str) -> str | None:
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    action = decoded.get("action") if isinstance(decoded, dict) else None
    return action if isinstance(action, str) else None


# --------------------------------------------------------------------------- #
# The replay                                                                   #
# --------------------------------------------------------------------------- #


def _wrongness_candidates(
    rows: Sequence[Any], *, graph: KGGraph, level: int
) -> dict[str, str] | None:
    """The S5 corroborator candidates for the at-Done half, or ``None``.

    Mirrors `done._grade_claimed_attempt` exactly — the same two `wrongness`
    functions over the same graded-node set — so the replayed adjudication asks
    the same question production would. ``None`` (not ``{}``) below level 1, and
    ``None`` when nothing was flagged, because an empty map still changes the
    adjudicator's prompt.
    """
    if level < LEVEL_PRODUCE:
        return None
    graded = frozenset(node.node_id for node in graph.nodes if node.node_type in _GRADED_NODE_TYPES)
    return candidate_quotes(ledger_findings(rows), graded_node_ids=graded) or None


async def _replay(
    fixture: TurnReplayFixture,
    *,
    make_client: Callable[[int], ReplayClient],
    sample: int,
    grade: bool,
) -> FixtureReplay:
    # The ladder level is NOT a harness argument: it is read from
    # `APOLLO_WRONGNESS_LEVEL` through `effective_wrongness_level`, the single
    # reader, exactly as production reads it — concept allowlist included. An
    # arm is therefore selected the same way it is deployed (`--level`, or the
    # env var), and a harness-local level could never disagree with the one the
    # controller actually applied.
    level = effective_wrongness_level(fixture.problem.concept_id)
    if not fixture.student_turn_ids:
        return FixtureReplay(
            fixture=fixture.name,
            attempt_ref=fixture.attempt_ref,
            sample=sample,
            refusal=EMPTY_ATTEMPT_REFUSAL,
            turns=(),
            grade=None,
            recorded=fixture.recorded,
            level=level,
        )

    graph = fixture.problem.to_kg_graph(attempt_id=_REPLAY_ATTEMPT_ID)
    session = cast(AsyncSession, _NullSession())
    # The attempt started with NO ledger rows and minted them turn by turn.
    # Seeding the recorded END state instead would start the replay with the
    # final `times_asked` already spent and every node already settled.
    rows: list[Any] = []
    records: list[TurnRecord] = []

    for ordinal, turn_id in enumerate(fixture.student_turn_ids):
        transcript = list(fixture.messages[: turn_id + 1])
        tally_state = _build_tally_state(graph, list(rows))
        questions_asked = sum(int(row.times_asked or 0) for row in rows)
        cap = question_cap()
        # The four level-2 inputs come from `controller._ladder_inputs`, the
        # production resolver, IMPORTED rather than reimplemented — the same rule
        # the tally-state rebuild follows. Without this the harness is
        # level-blind: `evaluate_and_ask` would keep taking its pre-P3.2
        # defaults, the done-gate could never fire, and every arm would replay
        # identically no matter what the flag said. `_ladder_inputs` skips its
        # cross-attempt SELECT when `problem.database_id is None`, which every
        # PII-scrubbed fixture is, so `_NullSession` is never queried.
        ladder = await _ladder_inputs(
            session,
            problem=fixture.problem,
            attempt_id=_REPLAY_ATTEMPT_ID,
            course_id=_REPLAY_COURSE_ID,
            reference_graph=graph,
            rows=list(rows),
            tally_state=tally_state,
            questions_asked=questions_asked,
            cap=cap,
        )
        budget = QuestionBudget(
            questions_asked=questions_asked,
            cap=cap,
            carried_challenges=ladder.carried_challenges,
        )
        client = make_client(ordinal)
        before = client.calls
        result = await evaluate_and_ask(
            transcript=transcript,
            reference_graph=graph,
            problem=fixture.problem,
            tally_state=tally_state,
            budget=budget,
            client=client,
            wrongness=ladder.wrongness,
            contested_ids=ladder.contested_ids,
            contested_quotes=ladder.contested_quotes,
            challenge_gate=ladder.challenge_gate,
        )
        raw = client.recorded[before] if client.calls > before else ""
        by_id = _apply_tally_updates(
            session,
            course_id=_REPLAY_COURSE_ID,
            session_id=_REPLAY_SESSION_ID,
            attempt_id=_REPLAY_ATTEMPT_ID,
            rows=list(rows),
            updates=result.tally_updates,
        )
        if result.action == "ask" and result.target_node_id is not None:
            target = by_id.get(result.target_node_id)
            if target is not None:
                _charge_ask(target, turn_index=turn_id, fallback_served=result.fallback_served)
        _write_opportunity_audit(
            session,
            course_id=_REPLAY_COURSE_ID,
            attempt_id=_REPLAY_ATTEMPT_ID,
            session_id=_REPLAY_SESSION_ID,
            rows=by_id,
            result=result,
            turn_index=turn_id,
        )
        rows = list(by_id.values())
        policy: SelectionPolicy = build_selection_policy(
            reference_graph=graph,
            tally_state=_build_tally_state(graph, list(rows)),
            questions_asked=budget.questions_asked,
            cap=budget.cap,
            contested_ids=ladder.contested_ids,
        )
        model_action = _model_action(raw)
        records.append(
            TurnRecord(
                fixture=fixture.name,
                sample=sample,
                turn_index=turn_id,
                level=level,
                producer_calls=client.calls - before,
                raw_response=raw,
                model_action=model_action,
                action=result.action,
                target_node_id=result.target_node_id,
                done_gate_fired=model_action == "done" and result.action == "ask",
                fallback_served=result.fallback_served,
                askable_ids=tuple(policy.askable_ids),
                reserved_for_graded=policy.reserved_for_graded,
                graded_only=policy.graded_only,
                tally_updates=tuple(_serialize_update(u) for u in result.tally_updates),
            )
        )

    outcome = (
        await grade_replay(
            problem=fixture.problem,
            transcript=fixture.messages,
            adjudicator_output=fixture.adjudicator_output,
            rows=rows,
            # S5: at level >= 1 the at-Done corroborator is offered the tally's
            # candidates, built by the same two `wrongness` functions `done.py`
            # calls, off the ledger this replay just minted. `None` below level
            # 1 leaves the adjudication prompts and verdict byte-identical.
            wrongness_candidates=_wrongness_candidates(rows, graph=graph, level=level),
            name=fixture.name,
        )
        if grade
        else None
    )
    return FixtureReplay(
        fixture=fixture.name,
        attempt_ref=fixture.attempt_ref,
        sample=sample,
        refusal=None,
        turns=tuple(records),
        grade=outcome,
        recorded=fixture.recorded,
        level=level,
        ledger=tuple(_serialize_row(row) for row in rows),
    )


async def replay_recorded(
    fixture: TurnReplayFixture,
    *,
    responses: Sequence[TurnResponse] | None = None,
    sample: int = 0,
    grade: bool = True,
) -> FixtureReplay:
    """PLAYBACK a fixture. Deterministic and hermetic — no network at all."""
    turns = tuple(responses) if responses is not None else reconstruct_producer_responses(fixture)
    if turns and len(turns) != len(fixture.student_turn_ids):
        raise TurnReplayError(
            f"{fixture.name}: {len(turns)} response(s) for "
            f"{len(fixture.student_turn_ids)} student turn(s)"
        )

    def make_client(ordinal: int) -> ReplayClient:
        return RecordedClient(turns[ordinal].as_responses(), repeat_last=True)

    return await _replay(fixture, make_client=make_client, sample=sample, grade=grade)


async def replay_live(
    fixture: TurnReplayFixture,
    *,
    sample: int = 0,
    grade: bool = True,
    client: ReplayClient | None = None,
) -> FixtureReplay:
    """RECORD a fixture against the real model. One client for the whole run."""
    live = client if client is not None else LiveClient()
    return await _replay(fixture, make_client=lambda _: live, sample=sample, grade=grade)


async def run(
    fixtures: Sequence[TurnReplayFixture],
    *,
    live: bool,
    samples: int,
) -> tuple[FixtureReplay, ...]:
    """Every fixture × every sample. Samples are independent draws."""
    replays: list[FixtureReplay] = []
    for sample in range(samples):
        for fixture in fixtures:
            replays.append(
                await (
                    replay_live(fixture, sample=sample)
                    if live
                    else replay_recorded(fixture, sample=sample)
                )
            )
    return tuple(replays)


# --------------------------------------------------------------------------- #
# JSONL                                                                        #
# --------------------------------------------------------------------------- #


def turn_row(record: TurnRecord) -> dict[str, Any]:
    """One ``kind: "turn"`` row. P3.1 adds ``credit``/``basis`` here."""
    return {
        "kind": "turn",
        "fixture": record.fixture,
        "sample": record.sample,
        "turn_index": record.turn_index,
        "level": record.level,
        "producer_calls": record.producer_calls,
        "raw_response": record.raw_response,
        "model_action": record.model_action,
        "action": record.action,
        "target_node_id": record.target_node_id,
        "done_gate_fired": record.done_gate_fired,
        "fallback_served": record.fallback_served,
        "askable_ids": list(record.askable_ids),
        "reserved_for_graded": record.reserved_for_graded,
        "graded_only": record.graded_only,
        "tally_updates": list(record.tally_updates),
        "wrongness_findings": record.wrongness_findings,
    }


def summary_row(replay: FixtureReplay) -> dict[str, Any]:
    """One ``kind: "summary"`` row per (fixture, sample)."""
    grade = replay.grade
    return {
        "kind": "summary",
        "fixture": replay.fixture,
        "attempt_ref": replay.attempt_ref,
        "sample": replay.sample,
        "level": replay.level,
        "refusal": replay.refusal,
        "turns": len(replay.turns),
        "asks": sum(1 for turn in replay.turns if turn.action == "ask"),
        "done_gate_fires": sum(1 for turn in replay.turns if turn.done_gate_fired),
        "wrongness_findings": replay.wrongness_findings,
        "score": None if grade is None else grade.score,
        "letter": None if grade is None else grade.letter,
        "credited_topics": None if grade is None else grade.credited_topics,
        "asked_node_ids": None if grade is None else list(grade.asked_node_ids or ()),
        "topic_credits": {} if grade is None else grade.topic_credits,
        "recorded_score": replay.recorded.get("served_score"),
        "recorded_letter": replay.recorded.get("served_letter"),
        "ledger": list(replay.ledger),
    }


def to_jsonl(replays: Iterable[FixtureReplay]) -> Iterator[str]:
    for replay in replays:
        for record in replay.turns:
            yield json.dumps(turn_row(record), ensure_ascii=False, sort_keys=True)
        yield json.dumps(summary_row(replay), ensure_ascii=False, sort_keys=True)


def load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


# --------------------------------------------------------------------------- #
# Arm comparison                                                               #
# --------------------------------------------------------------------------- #


def _summaries(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (row["fixture"], int(row["sample"])): row for row in rows if row.get("kind") == "summary"
    }


def compare_arms(
    baseline: Iterable[dict[str, Any]], candidate: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Diff two arms' JSONL. Descriptive only — it concludes nothing.

    A single draw is inside the live noise floor (7-18% of letters and 16.7% of
    node credits move between identical runs, spec §4), so this reports the
    numbers and leaves the judgement to the campaign analyst.
    """
    left, right = _summaries(baseline), _summaries(candidate)
    shared = sorted(set(left) & set(right))
    deltas = [
        (right[key]["score"] or 0) - (left[key]["score"] or 0)
        for key in shared
        if left[key]["score"] is not None and right[key]["score"] is not None
    ]
    agree = disagree = 0
    for key in shared:
        left_credits = left[key].get("topic_credits") or {}
        right_credits = right[key].get("topic_credits") or {}
        for node_id in sorted(set(left_credits) | set(right_credits)):
            if left_credits.get(node_id) == right_credits.get(node_id):
                agree += 1
            else:
                disagree += 1
    return {
        "compared": len(shared),
        "baseline_only": sorted(f"{f}#{s}" for f, s in set(left) - set(right)),
        "candidate_only": sorted(f"{f}#{s}" for f, s in set(right) - set(left)),
        "letters": {
            "baseline": _letters(left, shared),
            "candidate": _letters(right, shared),
        },
        "score_delta": {
            "signed_mean": (sum(deltas) / len(deltas)) if deltas else 0.0,
            "min": min(deltas) if deltas else 0,
            "max": max(deltas) if deltas else 0,
            "moved": sum(1 for delta in deltas if delta != 0),
        },
        "node_credit_agreement": {
            "agree": agree,
            "disagree": disagree,
            "rate": (agree / (agree + disagree)) if (agree + disagree) else 1.0,
        },
        "wrongness_findings": {
            "baseline": sum(int(left[key].get("wrongness_findings") or 0) for key in shared),
            "candidate": sum(int(right[key].get("wrongness_findings") or 0) for key in shared),
        },
    }


def _letters(rows: dict[tuple[str, int], dict[str, Any]], keys: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in keys:
        letter = rows[key].get("letter")
        if isinstance(letter, str):
            counts[letter] = counts.get(letter, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m campaign.turn_replay",
        description="Replay recorded Apollo attempts turn by turn (see module docstring).",
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument(
        "--level",
        type=int,
        default=None,
        help=(
            "run this arm at APOLLO_WRONGNESS_LEVEL=N (spec 4 arms: 0 = the noise floor, "
            "2 = probe priority + done-gate, 4 = the ceiling). Sets the env var for this "
            "process only; omit it to use the ambient environment"
        ),
    )
    parser.add_argument(
        "--allow-single-draw",
        action="store_true",
        help=f"permit a live run with fewer than {LIVE_SAMPLE_MINIMUM} samples (dry runs only)",
    )
    parser.add_argument("--out", type=Path, default=None, help="JSONL output (default: stdout)")
    parser.add_argument(
        "--compare",
        nargs=2,
        type=Path,
        metavar=("BASELINE", "CANDIDATE"),
        help="diff two arms' JSONL instead of replaying",
    )
    return parser


def _run_cli(args: argparse.Namespace) -> tuple[str, ...]:
    if args.compare:
        baseline, candidate = args.compare
        return (json.dumps(compare_arms(load_jsonl(baseline), load_jsonl(candidate)), indent=2),)
    if args.level is not None:
        # The one place the flag is WRITTEN. Everything downstream still READS it
        # through `wrongness.effective_wrongness_level`, so `--level` selects an
        # arm exactly the way a deployment selects a rung — clamp, concept
        # allowlist and all — rather than through a harness-only switch that
        # could disagree with what production would have done.
        os.environ["APOLLO_WRONGNESS_LEVEL"] = str(args.level)
    live = args.mode == "live"
    samples = max(1, int(args.samples))
    if live and samples < LIVE_SAMPLE_MINIMUM and not args.allow_single_draw:
        raise SystemExit(
            f"--mode live needs --samples >= {LIVE_SAMPLE_MINIMUM} "
            "(no live arm is concluded from fewer draws); pass --allow-single-draw for a dry run"
        )
    fixtures = load_fixtures(args.fixtures)
    if live:
        replays = asyncio.run(run(fixtures, live=True, samples=samples))
    else:
        with loopback_only_sockets():
            replays = asyncio.run(run(fixtures, live=False, samples=samples))
    return tuple(to_jsonl(replays))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lines = _run_cli(args)
    if args.out is not None:
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        for line in lines:
            print(line)
    return 0


__all__ = [
    "DEFAULT_FIXTURE_DIR",
    "EMPTY_ATTEMPT_REFUSAL",
    "FIXTURE_VERSION",
    "LIVE_SAMPLE_MINIMUM",
    "FixtureReplay",
    "LiveClient",
    "NetworkBlockedError",
    "RecordedClient",
    "ReplayClient",
    "TurnRecord",
    "TurnReplayError",
    "TurnReplayFixture",
    "TurnResponse",
    "compare_arms",
    "load_fixture",
    "load_fixtures",
    "load_jsonl",
    "loopback_only_sockets",
    "main",
    "reconstruct_producer_responses",
    "replay_live",
    "replay_recorded",
    "run",
    "summary_row",
    "to_jsonl",
    "turn_row",
]


if __name__ == "__main__":
    raise SystemExit(main())
