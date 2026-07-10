from unittest.mock import AsyncMock

from apollo.clarification import resolve_turn
from apollo.resolution.candidates import Candidate

_SPACE = 1
_CONCEPT = 7


class _NestedCM:
    """Minimal stand-in for the async context manager AsyncSession.begin_nested()
    returns — real SQLAlchemy exposes it as a SYNC method returning an async
    context manager, not a coroutine, so a bare AsyncMock() default (which
    makes begin_nested() itself awaitable/coroutine-returning) does not match
    production shape. Tests that don't care about nesting use this via
    ``_db_mock()`` so ``async with db.begin_nested():`` always works."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _db_mock() -> AsyncMock:
    db = AsyncMock()
    db.begin_nested = lambda: _NestedCM()
    return db


_USER = "a0000000-0000-4000-8000-000000000001"


class _Row:
    def __init__(
        self,
        node_id,
        candidate_key,
        original,
        *,
        id=1,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
    ):
        self.id = id
        self.node_id = node_id
        self.candidate_key = candidate_key
        self.original_statement = original
        self.search_space_id = search_space_id
        self.concept_id = concept_id
        self.user_id = user_id


def _cand(
    key,
    display,
    *,
    is_misconception=False,
    opposes_key=None,
    node_type="condition",
):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type=node_type,
        is_misconception=is_misconception,
        symbolic=None,
        aliases=(),
        display_name=display,
        opposes_key=opposes_key,
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

    await resolve_turn.resolve_pending_clarifications(
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


async def test_records_refuted_outcome_state_is_bare_outcome(monkeypatch):
    """record_outcome still takes state=outcome.outcome (a bare string), not
    the RescoreResult itself — the rescorer schema change (T3/Q2) is caller-
    transparent to the state machine."""
    recorded = {}

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "p~v")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        recorded.update(state=state)

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_CAPTURE", raising=False)

    await resolve_turn.resolve_pending_clarifications(
        db=object(),
        attempt_id=1,
        student_message="opposite claim",
        candidates=(_cand("cond.bernoulli", "inverse p-v"),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )
    assert recorded["state"] == "refuted"
    assert isinstance(recorded["state"], str)


# --------------------------------------------------------------------------- #
# R2 — the clarification-refuted emergent capture seam (T3, spec §5.3.2).
#
# The refuted candidate can be EITHER of two real shapes drawn from the SAME
# closed candidate set (apollo.resolution.candidates.build_candidate_set):
#
#   Case A — a REFERENCE-NODE candidate (is_misconception=False): the student
#     was probed about a reference idea and refuted it outright. There is no
#     "misconception" object at all yet -- the node's OWN canonical_key IS its
#     entity_key (build_node's entity_key === the step's canonical_key,
#     confirmed by candidates_from_reference_solution). The emergent signature
#     is emergent.<candidate.canonical_key>.
#
#   Case B — a MISCONCEPTION candidate (is_misconception=True): the student
#     was probed about a misconception idea (hand-authored OR a promoted
#     emergent one, both reachable via candidates_from_misconceptions) and
#     REFUTED it — i.e. confirmed the OPPOSITE, so the misconception's own
#     candidate.opposes_key (the node it opposes) names the entity_key.
#     The emergent signature is emergent.<candidate.opposes_key>.
#
# In BOTH cases the emergent signature is ALWAYS `emergent.<entity_key of the
# opposed reference node>` (never the misconception's OWN key) — this test
# pins that identity for each reachable case. A candidate with no resolvable
# entity_key (Case B misconception candidate with opposes_key=None) is a
# scope boundary: NOT captured.
# --------------------------------------------------------------------------- #


async def test_refuted_reference_node_candidate_captures_own_key_as_signature(monkeypatch):
    """Case A: refuting a reference-node candidate anchors the emergent
    signature on the candidate's OWN canonical_key (== its entity_key)."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "pressure and speed are related")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    captured = {}

    async def fake_capture(db, **kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=1,
        student_message="higher where faster",  # the opposite claim
        candidates=(_cand("cond.bernoulli", "inverse p-v", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    assert captured["signature"] == "emergent.cond.bernoulli"
    assert captured["opposes"] == "cond.bernoulli"
    assert captured["search_space_id"] == _SPACE
    assert captured["concept_id"] == _CONCEPT
    assert captured["user_id"] == _USER
    assert captured["attempt_id"] == 1
    assert captured["confidence"] == 1.0  # literal-stub judge shim
    assert captured["evidence_span"] == "higher where faster"


async def test_refuted_misconception_candidate_captures_opposes_key_as_signature(monkeypatch):
    """Case B: refuting a MISCONCEPTION candidate anchors the emergent
    signature on the misconception's opposes_key (the node it opposes), NOT
    the misconception's own canonical_key."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "misc.nominal_for_real", "students often confuse nominal and real")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    captured = {}

    async def fake_capture(db, **kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=2,
        student_message="no, real GDP is inflation-adjusted",
        candidates=(
            _cand(
                "misc.nominal_for_real",
                "confuses nominal for real",
                is_misconception=True,
                opposes_key="def.real_basis",
            ),
        ),
        judge=lambda req: "refuted",
        answered_turn=5,
    )

    assert captured["signature"] == "emergent.def.real_basis"
    assert captured["opposes"] == "def.real_basis"
    assert captured["attempt_id"] == 2


async def test_refuted_misconception_candidate_without_opposes_key_not_captured(monkeypatch):
    """Scope boundary: a misconception candidate with no opposes_key has no
    resolvable entity_key -> NOT captured (no signature to accrete)."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "misc.unlinked", "o")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    capture_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", capture_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=3,
        student_message="m",
        candidates=(
            _cand("misc.unlinked", "d", is_misconception=True, opposes_key=None),
        ),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    capture_mock.assert_not_awaited()


async def test_refuted_reference_node_candidate_without_canonical_key_not_captured(monkeypatch):
    """Defensive scope boundary mirror: an (unrealistic but defensively
    handled) candidate with a falsy canonical_key is never captured either."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "", "o")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    capture_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", capture_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=4,
        student_message="m",
        candidates=(_cand("", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    capture_mock.assert_not_awaited()


async def test_confirmed_and_vague_never_capture(monkeypatch):
    """Only a refuted outcome triggers capture — confirmed/vague write
    nothing to the emergent map."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [
            _Row("s1", "cond.bernoulli", "o1", id=1),
            _Row("s2", "cond.other", "o2", id=2),
        ]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    capture_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", capture_mock)
    db = _db_mock()

    outcomes = iter(["confirmed", "vague"])

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=5,
        student_message="m",
        candidates=(
            _cand("cond.bernoulli", "d1", is_misconception=False),
            _cand("cond.other", "d2", is_misconception=False),
        ),
        judge=lambda req: next(outcomes),
        answered_turn=4,
    )

    capture_mock.assert_not_awaited()


async def test_refuted_row_with_no_matching_candidate_not_captured(monkeypatch):
    """Defensive scope boundary: a refuted row whose candidate_key no longer
    matches any candidate in THIS turn's closed set (candidate_by_key misses)
    resolves candidate=None -> _refuted_signature returns None -> not
    captured, no crash."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.vanished", "o")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    capture_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", capture_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=9,
        student_message="m",
        # The probed candidate is no longer in this turn's candidate set.
        candidates=(_cand("cond.bernoulli", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    capture_mock.assert_not_awaited()


async def test_refuted_capture_flag_off_not_called(monkeypatch):
    """Flag-off byte-identity: APOLLO_EMERGENT_MAP_CAPTURE unset -> the
    capture seam is never invoked, even on a refuted outcome, and
    resolve_pending_clarifications behavior is otherwise unchanged."""
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_CAPTURE", raising=False)

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "o")]

    recorded = {}

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        recorded.update(state=state)

    capture_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", capture_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=6,
        student_message="m",
        candidates=(_cand("cond.bernoulli", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    capture_mock.assert_not_awaited()
    assert recorded["state"] == "refuted"  # resolution itself unaffected


async def test_refuted_capture_own_failure_domain_resolution_still_recorded(monkeypatch):
    """Own-failure-domain (T3): a capture-write failure is swallowed+logged
    and does NOT prevent record_outcome from having already been called — the
    resolution txn is intact regardless of capture success."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "o")]

    recorded = {}

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        recorded.update(state=state)

    async def fake_capture(db, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)
    db = _db_mock()

    # Must not raise.
    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=7,
        student_message="m",
        candidates=(_cand("cond.bernoulli", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    assert recorded["state"] == "refuted"


async def test_refuted_capture_uses_nested_savepoint(monkeypatch):
    """The capture write runs inside its own db.begin_nested() savepoint (the
    clarification loop's precedent — see apollo/handlers/chat.py) so a
    failure there cannot poison the outer resolution transaction. Verified
    via a fake AsyncSession whose begin_nested() is an awaited context
    manager."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "o")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    capture_calls = []

    async def fake_capture(db, **kwargs):
        capture_calls.append(kwargs)
        return 1

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)

    nested_calls = {"n": 0}

    class _NestedCM:
        async def __aenter__(self_inner):
            nested_calls["n"] += 1
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

    db = _db_mock()
    db.begin_nested = lambda: _NestedCM()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=8,
        student_message="m",
        candidates=(_cand("cond.bernoulli", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    assert len(capture_calls) == 1
    assert nested_calls["n"] == 1


# --------------------------------------------------------------------------- #
# T7 (plan Wave 3, spec §5.5 Q3): materialize_if_promotable is invoked from
# THIS capture seam's own success path, inside the same nested savepoint +
# try/except as the observation write. `neo` is threaded from the caller
# (handle_chat -> resolve_pending_clarifications -> _capture_refuted) with a
# backward-compatible default of None.
# --------------------------------------------------------------------------- #


async def test_refuted_capture_invokes_materialize_with_resolved_signature(monkeypatch):
    """After record_clarification_refuted succeeds, materialize_if_promotable
    is called with the SAME (signature, opposes) pair and the threaded neo
    client."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "pressure and speed are related")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    async def fake_capture(db, **kwargs):
        return 1

    materialize_mock = AsyncMock()
    neo_sentinel = object()

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)
    monkeypatch.setattr(resolve_turn, "materialize_if_promotable", materialize_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=1,
        student_message="higher where faster",
        candidates=(_cand("cond.bernoulli", "inverse p-v", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
        neo=neo_sentinel,
    )

    materialize_mock.assert_awaited_once()
    args, kwargs = materialize_mock.await_args
    assert args[0] is db
    assert args[1] is neo_sentinel
    assert kwargs["signature"] == "emergent.cond.bernoulli"
    assert kwargs["opposes_entity_key"] == "cond.bernoulli"
    assert kwargs["search_space_id"] == _SPACE
    assert kwargs["concept_id"] == _CONCEPT


async def test_refuted_capture_neo_defaults_to_none_backward_compatible(monkeypatch):
    """resolve_pending_clarifications callers that omit `neo` (every
    pre-T7 caller/test) still work -- materialize_if_promotable receives
    neo=None, which its own contract treats as a projection-skip, not an
    error (eventual-consistency: the entity/link still land in Postgres and
    :Canon picks it up at the next projection for that concept)."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "o")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    async def fake_capture(db, **kwargs):
        return 1

    materialize_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)
    monkeypatch.setattr(resolve_turn, "materialize_if_promotable", materialize_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=1,
        student_message="m",
        candidates=(_cand("cond.bernoulli", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
        # neo omitted -- defaults to None.
    )

    materialize_mock.assert_awaited_once()
    _args, kwargs = materialize_mock.await_args
    assert _args[1] is None


async def test_refuted_capture_materialize_failure_own_failure_domain(monkeypatch):
    """A materialize-step failure is swallowed by the SAME except as a
    capture-write failure -- resolution stays recorded, no exception
    escapes."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "o")]

    recorded = {}

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        recorded.update(state=state)

    async def fake_capture(db, **kwargs):
        return 1

    async def fake_materialize(db, neo, **kwargs):
        raise RuntimeError("materialize exploded")

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", fake_capture)
    monkeypatch.setattr(resolve_turn, "materialize_if_promotable", fake_materialize)
    db = _db_mock()

    # Must not raise.
    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=7,
        student_message="m",
        candidates=(_cand("cond.bernoulli", "d", is_misconception=False),),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    assert recorded["state"] == "refuted"


async def test_refuted_capture_materialize_not_called_when_not_captured(monkeypatch):
    """Scope boundary: when _refuted_signature resolves nothing (no
    resolvable entity_key), materialize_if_promotable is never invoked --
    mirrors record_clarification_refuted's own not-called assertion."""
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "misc.unlinked", "o")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        pass

    capture_mock = AsyncMock()
    materialize_mock = AsyncMock()
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    monkeypatch.setattr(resolve_turn, "record_clarification_refuted", capture_mock)
    monkeypatch.setattr(resolve_turn, "materialize_if_promotable", materialize_mock)
    db = _db_mock()

    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=1,
        student_message="m",
        candidates=(
            _cand("misc.unlinked", "unlinked misconception", is_misconception=True, opposes_key=None),
        ),
        judge=lambda req: "refuted",
        answered_turn=4,
    )

    capture_mock.assert_not_awaited()
    materialize_mock.assert_not_awaited()
