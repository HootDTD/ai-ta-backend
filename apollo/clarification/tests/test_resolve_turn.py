from apollo.clarification import resolve_turn
from apollo.resolution.candidates import Candidate


class _Row:
    def __init__(self, node_id, candidate_key, original):
        self.id = 1
        self.node_id = node_id
        self.candidate_key = candidate_key
        self.original_statement = original


def _cand(key, display):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=display,
        opposes_key=None,
        exact_aliases=(),
    )


async def test_records_confirmed_outcome(monkeypatch):
    recorded = {}

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "p~v")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        recorded.update(state=state, text=clarification_text, turn=answered_turn)

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)

    outcomes = await resolve_turn.resolve_pending_clarifications(
        db=object(),
        attempt_id=1,
        student_message="lower where faster",
        candidates=(_cand("cond.bernoulli", "inverse p-v"),),
        judge=lambda req: "confirmed",
        answered_turn=4,
    )
    assert recorded["state"] == "confirmed"
    assert recorded["text"] == "lower where faster"
    assert recorded["turn"] == 4
    # T12: the recorded (candidate_key, outcome) pair is returned so chat.py
    # can seed the V2 view on `confirmed` only.
    assert outcomes == (("cond.bernoulli", "confirmed"),)


async def test_returns_refuted_and_vague_outcomes_too(monkeypatch):
    """T12: refuted/vague outcomes are still returned (chat.py filters them
    out before seeding -- they must never seed)."""

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "p~v"), _Row("s2", "cond.other", "x")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    verdicts = iter(["refuted", "vague"])

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)

    outcomes = await resolve_turn.resolve_pending_clarifications(
        db=object(),
        attempt_id=1,
        student_message="m",
        candidates=(_cand("cond.bernoulli", "d1"), _cand("cond.other", "d2")),
        judge=lambda req: next(verdicts),
        answered_turn=4,
    )
    assert outcomes == (
        ("cond.bernoulli", "refuted"),
        ("cond.other", "vague"),
    )


async def test_judge_failure_leaves_waiting(monkeypatch):
    from apollo.errors import ResolutionUnavailableError

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "k", "o")]

    calls = {"record": 0}

    async def fake_record(db, **kw):
        calls["record"] += 1

    def boom(req):
        raise ResolutionUnavailableError(stage="clarification_rescore", last_error="x")

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    await resolve_turn.resolve_pending_clarifications(
        db=object(),
        attempt_id=1,
        student_message="m",
        candidates=(_cand("k", "d"),),
        judge=boom,
        answered_turn=4,
    )
    assert calls["record"] == 0  # left asked_waiting; no terminal write
