"""Unit tests for campaign.cast.student (Task D3(a)).

Every real I/O seam (HTTP, DB read, LLM chat) is a fake — this tests only the
driver's flow logic: turn loop, problem-mismatch retry, clarification
follow-ups, artifact capture, JSONL emission, and single-attempt-failure
isolation. The real seam implementations (HttpxApolloClient,
SqlArtifactReader, default_chat_fn, mint_student_token) are pragma-excluded
and exercised only in Phase F against a live stack.
"""

from __future__ import annotations

import json

import pytest

from campaign.cast import student
from campaign.cast.personas.schema import ExpectedLedger, PersonaAttempt

pytestmark = pytest.mark.unit


def _persona(**overrides) -> PersonaAttempt:
    kwargs = dict(
        persona="strong",
        subject="fluid_mechanics",
        concept="bernoulli_principle",
        problem_id="bernoulli_full_find_p2",
        system_prompt="teach bernoulli",
        scripted_beats=["beat one", "beat two"],
        clarification_policy="answer_correctly",
        expected=ExpectedLedger(credited=["eq.a", "eq.b"]),
    )
    kwargs.update(overrides)
    return PersonaAttempt.model_validate(kwargs)


class FakeApolloClient:
    """Scripted fake: records every call, returns canned responses.

    Models the REAL ``/next`` contract (``handle_next`` in
    ``apollo/handlers/next.py``): each re-roll mints a NEW ``attempt_id`` and
    returns a fresh ``problem``. This is deliberately NOT ``/retry``
    (``handle_retry`` only returns ``{"ok": True}`` and never re-selects) —
    see the finding this fixture was corrected for.
    """

    def __init__(self, *, problem_id="bernoulli_full_find_p2", replies=None, retry_problem_id=None):
        self.calls: list[tuple[str, dict]] = []
        self._problem_id = problem_id
        self._replies = list(replies or [])
        self._retry_problem_id = retry_problem_id or problem_id
        self._session_id = 101
        self._attempt_id = 5001
        self._next_attempt_id = self._attempt_id

    async def create_session(self, *, search_space_id, hoot_transcript, difficulty, token):
        self.calls.append(
            (
                "create_session",
                {
                    "search_space_id": search_space_id,
                    "hoot_transcript": hoot_transcript,
                    "difficulty": difficulty,
                    "token": token,
                },
            )
        )
        return {
            "session_id": self._session_id,
            "attempt_id": self._attempt_id,
            "problem": {"id": self._problem_id},
        }

    async def next(self, *, session_id, difficulty, token):
        self.calls.append(
            ("next", {"session_id": session_id, "difficulty": difficulty, "token": token})
        )
        self._problem_id = self._retry_problem_id
        self._next_attempt_id += 1
        return {
            "session_id": session_id,
            "attempt_id": self._next_attempt_id,
            "problem": {"id": self._problem_id},
        }

    async def chat(self, *, session_id, message, token):
        self.calls.append(("chat", {"session_id": session_id, "message": message, "token": token}))
        reply = self._replies.pop(0) if self._replies else "ok, thanks!"
        return {"apollo_reply": reply}

    async def done(self, *, session_id, token):
        self.calls.append(("done", {"session_id": session_id, "token": token}))
        return {
            "rubric": {"overall": {"score": 90}},
            "scorecard": {"band": "Strong", "score_0_100": 90},
        }


class FailingDoneClient(FakeApolloClient):
    async def done(self, *, session_id, token):
        raise RuntimeError("boom")


class FakeArtifactReader:
    def __init__(self, canonical=None, pair=None):
        self.canonical = canonical
        self.pair = pair
        self.reads: list = []

    async def read(self, attempt_id):
        self.reads.append(attempt_id)
        return self.canonical, self.pair


async def _scripted_chat_fn(persona, transcript, beat):
    if beat is not None:
        return f"teaching: {beat}"
    return f"clarify[{persona.clarification_policy}]"


def _read_jsonl(path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# --- build_hoot_transcript ----------------------------------------------------


def test_build_hoot_transcript_names_the_concept():
    persona = _persona()
    text = student.build_hoot_transcript(persona)
    assert "bernoulli principle" in text
    assert "fluid mechanics" in text


# --- run_attempt: happy path --------------------------------------------------


@pytest.mark.asyncio
async def test_run_attempt_happy_path_plays_every_beat_and_captures_artifacts():
    persona = _persona()
    client = FakeApolloClient()
    reader = FakeArtifactReader(
        canonical={"grader_used": "llm_fallback", "scores": {"composite": 0.9}},
        pair={"grader_used": "graph", "scores": {"composite": 0.85}},
    )

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok-1",
        search_space_id=7,
    )

    assert record.status == "ok"
    assert record.problem_matched is True
    assert record.session_id == 101
    assert record.attempt_id == 5001
    assert reader.reads == [5001]
    assert record.artifact_canonical["grader_used"] == "llm_fallback"
    assert record.artifact_pair["grader_used"] == "graph"
    assert record.scorecard == {"band": "Strong", "score_0_100": 90}
    assert record.wall_time_ms >= 0

    # Exactly 2 beats -> 2 student + 2 apollo turns, no clarification (strong
    # persona doesn't expect one).
    assert len(record.transcript) == 4
    assert record.transcript[0] == {"role": "student", "content": "teaching: beat one"}
    kinds = [c[0] for c in client.calls]
    assert kinds == ["create_session", "chat", "chat", "done"]


@pytest.mark.asyncio
async def test_run_attempt_to_jsonl_dict_round_trips_through_json():
    persona = _persona()
    client = FakeApolloClient()
    reader = FakeArtifactReader(
        canonical={"grader_used": "llm_fallback", "scores": {"composite": 0.7}}
    )
    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
    )
    payload = record.to_jsonl_dict()
    json.dumps(payload)  # must be JSON-serialisable
    assert payload["persona"] == "strong"
    assert payload["expected"]["credited"] == ["eq.a", "eq.b"]
    assert payload["expected"]["expects_clarification"] is False
    assert payload["wall_times"]["total_ms"] == record.wall_time_ms
    assert payload["error"] is None


# --- problem mismatch / retry --------------------------------------------------


@pytest.mark.asyncio
async def test_run_attempt_retries_on_problem_mismatch_until_matched():
    persona = _persona(problem_id="target_problem")
    client = FakeApolloClient(problem_id="wrong_problem", retry_problem_id="target_problem")
    reader = FakeArtifactReader()

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
        max_problem_retries=3,
    )

    assert record.problem_matched is True
    kinds = [c[0] for c in client.calls]
    assert kinds.count("next") == 1
    # /next mints a new attempt_id -- the re-rolled one must be captured,
    # not the original session-creation attempt_id.
    assert record.attempt_id == 5002


@pytest.mark.asyncio
async def test_run_attempt_gives_up_after_max_retries_but_still_completes():
    persona = _persona(problem_id="target_problem")
    client = FakeApolloClient(problem_id="wrong_problem", retry_problem_id="still_wrong")
    reader = FakeArtifactReader()

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
        max_problem_retries=2,
    )

    assert record.status == "ok"
    assert record.problem_matched is False
    kinds = [c[0] for c in client.calls]
    assert kinds.count("next") == 2
    # still-mismatched, but the LAST re-roll's attempt_id must be captured.
    assert record.attempt_id == 5003


# --- clarification loop --------------------------------------------------------


@pytest.mark.asyncio
async def test_run_attempt_plays_clarification_followups_when_expected():
    persona = _persona(
        persona="vague_then_clarifies",
        expected=ExpectedLedger(credited=["eq.a"], expects_clarification=True),
    )
    client = FakeApolloClient(
        replies=["sure, teaching turn one", "hmm, what do you mean exactly?", "got it, thanks!"]
    )
    reader = FakeArtifactReader()

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
    )

    # 2 scripted beats + 1 clarification follow-up turn (stops once the reply
    # no longer contains "?").
    student_turns = [t for t in record.transcript if t["role"] == "student"]
    assert len(student_turns) == 3
    assert student_turns[-1]["content"] == "clarify[answer_correctly]"


@pytest.mark.asyncio
async def test_run_attempt_clarification_loop_bounded_by_max_turns():
    persona = _persona(
        persona="vague_then_clarifies",
        expected=ExpectedLedger(credited=["eq.a"], expects_clarification=True),
    )
    # Apollo keeps asking forever -- must stop at clarification_max_turns.
    client = FakeApolloClient(replies=["still unclear?"] * 10)
    reader = FakeArtifactReader()

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
        clarification_max_turns=2,
    )

    apollo_turns = [t for t in record.transcript if t["role"] == "apollo"]
    # 2 beats + 2 clarification turns = 4 apollo replies.
    assert len(apollo_turns) == 4


@pytest.mark.asyncio
async def test_run_attempt_skips_clarification_when_not_expected():
    persona = _persona()  # strong, expects_clarification=False
    client = FakeApolloClient(replies=["ok?", "sure?"])
    reader = FakeArtifactReader()

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
    )

    # Only the 2 scripted beats, even though Apollo's replies look like
    # questions -- the persona doesn't expect a clarification.
    assert len(record.transcript) == 4


# --- failure isolation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_attempt_records_error_status_without_raising():
    persona = _persona()
    client = FailingDoneClient()
    reader = FakeArtifactReader()

    record = await student.run_attempt(
        persona,
        client=client,
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
    )

    assert record.status == "error"
    assert "boom" in record.error
    assert record.attempt_id is None
    assert record.artifact_canonical is None
    assert reader.reads == []  # never reached the artifact read


@pytest.mark.asyncio
async def test_run_attempt_records_error_when_create_session_fails():
    persona = _persona()

    class BrokenClient(FakeApolloClient):
        async def create_session(self, **kwargs):
            raise ConnectionError("no backend")

    record = await student.run_attempt(
        persona,
        client=BrokenClient(),
        chat_fn=_scripted_chat_fn,
        artifact_reader=FakeArtifactReader(),
        token="tok",
        search_space_id=1,
    )
    assert record.status == "error"
    assert record.transcript == ()


# --- run_corpus -------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_corpus_runs_every_persona_and_writes_jsonl(tmp_path):
    personas = [
        _persona(problem_id="p1"),
        _persona(
            problem_id="p2",
            persona="partial",
            expected=ExpectedLedger(credited=["eq.a"], unresolved=["eq.b"]),
        ),
    ]
    out_path = tmp_path / "attempts.jsonl"

    async def client_factory_chat(persona, transcript, beat):
        return f"turn:{beat}"

    client = FakeApolloClient()
    reader = FakeArtifactReader(
        canonical={"grader_used": "llm_fallback", "scores": {"composite": 0.5}}
    )

    records = await student.run_corpus(
        personas,
        client=client,
        chat_fn=client_factory_chat,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
        attempts_path=out_path,
    )

    assert len(records) == 2
    assert all(r.status == "ok" for r in records)
    lines = _read_jsonl(out_path)
    assert len(lines) == 2
    assert {line["problem_id"] for line in lines} == {"p1", "p2"}


@pytest.mark.asyncio
async def test_run_corpus_one_bad_attempt_does_not_kill_the_run(tmp_path):
    good = _persona(problem_id="p_good")
    bad = _persona(problem_id="p_bad")
    out_path = tmp_path / "attempts.jsonl"

    call_count = {"n": 0}

    class FlakyClient(FakeApolloClient):
        async def create_session(self, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("transient failure")
            return await super().create_session(**kwargs)

    records = await student.run_corpus(
        [good, bad],
        client=FlakyClient(),
        chat_fn=_scripted_chat_fn,
        artifact_reader=FakeArtifactReader(),
        token="tok",
        search_space_id=1,
        attempts_path=out_path,
    )

    assert [r.status for r in records] == ["ok", "error"]
    lines = _read_jsonl(out_path)
    assert len(lines) == 2
    assert lines[1]["status"] == "error"


@pytest.mark.asyncio
async def test_run_corpus_without_attempts_path_skips_jsonl_write():
    personas = [_persona(problem_id="p1")]
    reader = FakeArtifactReader()
    records = await student.run_corpus(
        personas,
        client=FakeApolloClient(),
        chat_fn=_scripted_chat_fn,
        artifact_reader=reader,
        token="tok",
        search_space_id=1,
        attempts_path=None,
    )
    assert len(records) == 1


class _FakeRow:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_sql_artifact_reader_row_to_payload_maps_every_jsonb_column():
    row = _FakeRow(
        grader_used="graph",
        versions={"grader": "v1"},
        node_ledger=[{"canonical_key": "eq.a"}],
        edge_ledger=[],
        misconceptions=[],
        clarification_trace=[],
        scores={"composite": 0.9},
        abstention=None,
        grading_latency_ms=1200,
    )
    payload = student.SqlArtifactReader._row_to_payload(row)
    assert payload == {
        "grader_used": "graph",
        "versions": {"grader": "v1"},
        "node_ledger": [{"canonical_key": "eq.a"}],
        "edge_ledger": [],
        "misconceptions": [],
        "clarification_trace": [],
        "scores": {"composite": 0.9},
        "abstention": None,
        "grading_latency_ms": 1200,
    }


def test_sql_artifact_reader_stores_session_factory():
    def factory():
        return None

    reader = student.SqlArtifactReader(factory)
    assert reader._session_factory is factory


def test_append_attempt_record_creates_parent_dir(tmp_path):
    nested = tmp_path / "a" / "b" / "attempts.jsonl"
    record = student.AttemptRecord(
        status="ok",
        persona_id="x/y/z/strong",
        subject="x",
        concept="y",
        problem_id="z",
        persona_archetype="strong",
        expected={},
        session_id=1,
        attempt_id=1,
        problem_matched=True,
        transcript=(),
        done_response=None,
        artifact_canonical=None,
        artifact_pair=None,
        scorecard=None,
        wall_time_ms=5,
    )
    student.append_attempt_record(record, nested)
    assert nested.exists()
    assert len(_read_jsonl(nested)) == 1
