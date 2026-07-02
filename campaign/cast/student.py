"""Task D3(a) — agent-student session driver (spec §5 "agent *students* run
full Apollo sessions playing scripted personas").

``run_attempt`` drives ONE ``PersonaAttempt`` (Task D2) through a real Apollo
session end-to-end over the REAL student-facing HTTP routes
(``apollo/api.py``: ``POST /apollo/sessions/from_hoot``, ``POST
/apollo/sessions/{id}/chat``, ``POST /apollo/sessions/{id}/done`` — read there
first, this module's shapes mirror them exactly), then reads back the two
``GradingArtifact`` rows (canonical + pair) the Done-click persisted and
returns one :class:`AttemptRecord`. ``run_corpus`` drives many attempts and
appends one JSONL line per attempt to a :class:`~campaign.runctx.RunContext`'s
``attempts.jsonl``.

Every real I/O boundary is an injected seam so the whole flow unit-tests with
fakes (no Docker, no DB, no network, no LLM) — only the seams' REAL
implementations (an actual HTTP call, an actual DB read, an actual OpenAI
call, an actual Supabase admin-API mint) are ``# pragma: no cover``, matching
the D1/D2 precedent ("the live path pragma-excluded"; Phase F runs it):

- :class:`ApolloClient` — ``create_session`` / ``next`` / ``chat`` / ``done``.
  :class:`HttpxApolloClient` is the real implementation.
- ``ChatFn`` — the agent-student "LLM": ``(persona, transcript, beat) ->
  message``. :func:`default_chat_fn` is the real (OpenAI) implementation.
- :class:`ArtifactReader` — ``read(attempt_id) -> (canonical, pair)`` artifact
  payload dicts. :class:`SqlArtifactReader` is the real (DB) implementation.
- :func:`mint_student_token` — the real Supabase-local-auth token mint.

Deviation from the plan sketch (recorded honestly, see D2's README precedent):
Apollo's session-creation route (``init_session_from_hoot``) does NOT accept a
``problem_id`` — it infers a concept from the free-text ``hoot_transcript``
via an LLM call, then the Overseer's personalized selector picks a problem for
that concept. There is no student-facing lever to force the exact persona
``problem_id``. :func:`build_hoot_transcript` names the persona's concept as
plainly as possible to make selection likely, and :func:`run_attempt` re-rolls
via ``POST .../next`` (bounded by ``max_problem_retries``) when the selected
problem doesn't match. NOTE (fixed after review): the re-roll route is
``/next``, not ``/retry`` — ``handle_retry`` (``apollo/handlers/lifecycle.py``)
only unfreezes the KG and returns to ``TEACHING`` on the SAME problem; it never
re-selects and its response carries no ``problem`` key. ``handle_next``
(``apollo/handlers/next.py``) is the route that actually calls
``select_problem_personalized`` again — it also mints a NEW ``ProblemAttempt``
row, so each re-roll's ``attempt_id`` must be re-captured (see
:func:`_resolve_problem`). A persistent mismatch is recorded on the
:class:`AttemptRecord` (``problem_matched=False``) rather than silently
treated as a hard failure — a real live run may need a richer selection lever
(e.g. a campaign-only test-selection endpoint) if this proves too lossy in
Phase F, noted here for that handoff.

Similarly, Apollo's clarification loop (spec §3 step 2) has no dedicated
"answer this clarification" endpoint — it is woven into ordinary ``/chat``
replies (see ``apollo/handlers/chat.py``). This driver has no reliable
structural signal for "Apollo just asked a clarification probe"; it uses the
same heuristic :func:`campaign.adapters.extract_apollo_questions` uses for S4
(a reply containing ``"?"``) to decide whether to spend one of the
``clarification_max_turns`` follow-up turns, and always delegates the actual
wording (including how ``clarification_policy`` should shape the answer) to
``chat_fn`` — the agent-student, not the driver, is the right owner of that
judgment.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from campaign.cast.personas.schema import PersonaAttempt

_LOG = logging.getLogger(__name__)

__all__ = [
    "CLARIFICATION_MAX_TURNS",
    "DEFAULT_MAX_PROBLEM_RETRIES",
    "AttemptRecord",
    "ApolloClient",
    "ChatFn",
    "ArtifactReader",
    "build_hoot_transcript",
    "run_attempt",
    "run_corpus",
    "append_attempt_record",
    "HttpxApolloClient",
    "SqlArtifactReader",
    "default_chat_fn",
    "mint_student_token",
]

#: Bounded follow-up turns spent on Apollo's clarification loop after the
#: scripted beats are exhausted (spec §3 "bounded clarification loop").
CLARIFICATION_MAX_TURNS = 3

#: Bounded re-rolls via ``POST .../next`` when the Overseer's personalized
#: selector didn't land on the persona's authored ``problem_id`` (see module
#: docstring deviation note).
DEFAULT_MAX_PROBLEM_RETRIES = 3


# ---------------------------------------------------------------------------
# Injected seams
# ---------------------------------------------------------------------------


class ApolloClient(Protocol):
    """The student-facing Apollo HTTP surface this driver needs, exactly as
    ``apollo/api.py`` defines it. A fake in tests; :class:`HttpxApolloClient`
    is the real implementation."""

    async def create_session(
        self, *, search_space_id: int, hoot_transcript: str, difficulty: str, token: str
    ) -> dict[str, Any]: ...

    async def next(
        self, *, session_id: int, difficulty: str, token: str
    ) -> dict[str, Any]: ...

    async def chat(self, *, session_id: int, message: str, token: str) -> dict[str, Any]: ...

    async def done(self, *, session_id: int, token: str) -> dict[str, Any]: ...


#: ``(persona, transcript_so_far, beat) -> next student message``. ``beat`` is
#: the current scripted beat string during the teaching phase, or ``None``
#: during the bounded clarification follow-up phase (the agent must decide
#: what to say purely from ``persona.clarification_policy`` + the transcript).
ChatFn = Callable[[PersonaAttempt, Sequence[dict[str, Any]], str | None], Awaitable[str]]


class ArtifactReader(Protocol):
    """Reads back the two ``GradingArtifact`` rows (canonical + pair) a
    Done-click persisted for one attempt. Either element is ``None`` when
    that role's row doesn't exist (e.g. the shadow flag was off, so no
    ``pair`` row was ever written)."""

    async def read(self, attempt_id: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]: ...


# ---------------------------------------------------------------------------
# Attempt record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRecord:
    """One campaign attempt's full result: persona identity + expected
    ledger, both captured artifact payloads, the rendered scorecard, the
    transcript, and timings — the exact fields Task D3 asks this driver to
    emit into ``attempts.jsonl``."""

    status: str  # "ok" | "error"
    persona_id: str
    subject: str
    concept: str
    problem_id: str
    persona_archetype: str
    expected: dict[str, Any]
    session_id: int | None
    attempt_id: Any | None
    problem_matched: bool | None
    transcript: tuple[dict[str, Any], ...]
    done_response: dict[str, Any] | None
    artifact_canonical: dict[str, Any] | None
    artifact_pair: dict[str, Any] | None
    scorecard: dict[str, Any] | None
    wall_time_ms: int
    error: str | None = None

    def to_jsonl_dict(self) -> dict[str, Any]:
        """The exact JSON-serialisable record written to ``attempts.jsonl``."""
        return {
            "status": self.status,
            "persona_id": self.persona_id,
            "subject": self.subject,
            "concept": self.concept,
            "problem_id": self.problem_id,
            "persona": self.persona_archetype,
            "expected": self.expected,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "problem_matched": self.problem_matched,
            "transcript": list(self.transcript),
            "done_response": self.done_response,
            "artifact_canonical": self.artifact_canonical,
            "artifact_pair": self.artifact_pair,
            "scorecard": self.scorecard,
            "wall_times": {"total_ms": self.wall_time_ms},
            "error": self.error,
        }


def append_attempt_record(record: AttemptRecord, path: Path | str) -> None:
    """Append one JSONL line for ``record`` to ``path`` (created if missing)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_jsonl_dict(), sort_keys=True))
        fh.write("\n")


def _persona_id(persona: PersonaAttempt) -> str:
    return f"{persona.subject}/{persona.concept}/{persona.problem_id}/{persona.persona}"


def build_hoot_transcript(persona: PersonaAttempt) -> str:
    """A free-text Hoot handoff transcript naming the persona's concept as
    plainly as possible (see module docstring deviation note — session
    creation infers the concept from this text via an LLM call; there is no
    structural lever to name it directly)."""
    subject_name = persona.subject.replace("_", " ")
    concept_name = persona.concept.replace("_", " ")
    return (
        f"Student: I've been working on {subject_name}, specifically {concept_name}. "
        f"I think I understand it now — can I explain it back to you and you check my work?"
    )


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


async def _play_scripted_beats(
    persona: PersonaAttempt,
    *,
    client: ApolloClient,
    session_id: int,
    token: str,
    chat_fn: ChatFn,
    transcript: list[dict[str, Any]],
) -> None:
    for beat in persona.scripted_beats:
        message = await chat_fn(persona, tuple(transcript), beat)
        transcript.append({"role": "student", "content": message})
        response = await client.chat(session_id=session_id, message=message, token=token)
        transcript.append({"role": "apollo", "content": response.get("apollo_reply", "")})


async def _play_clarification_followups(
    persona: PersonaAttempt,
    *,
    client: ApolloClient,
    session_id: int,
    token: str,
    chat_fn: ChatFn,
    transcript: list[dict[str, Any]],
    clarification_max_turns: int,
) -> None:
    turns = 0
    while turns < clarification_max_turns:
        last_apollo = next(
            (t for t in reversed(transcript) if t.get("role") == "apollo"), None
        )
        if last_apollo is None or "?" not in str(last_apollo.get("content", "")):
            break
        message = await chat_fn(persona, tuple(transcript), None)
        transcript.append({"role": "student", "content": message})
        response = await client.chat(session_id=session_id, message=message, token=token)
        transcript.append({"role": "apollo", "content": response.get("apollo_reply", "")})
        turns += 1


async def _resolve_problem(
    persona: PersonaAttempt,
    *,
    client: ApolloClient,
    session: dict[str, Any],
    session_id: int,
    token: str,
    difficulty: str,
    max_problem_retries: int,
) -> tuple[dict[str, Any], bool, Any]:
    """Re-roll problem selection via ``POST .../next`` (NOT ``.../retry`` —
    see the module docstring deviation note) until the Overseer's
    personalized selector lands on ``persona.problem_id`` or
    ``max_problem_retries`` is exhausted. Each re-roll mints a NEW
    ``ProblemAttempt`` row, so the returned ``attempt_id`` must be threaded
    back into the caller's record instead of the session-creation one."""
    problem = session.get("problem") or {}
    attempt_id = session.get("attempt_id")
    matched = problem.get("id") == persona.problem_id
    retries = 0
    while not matched and retries < max_problem_retries:
        try:
            session = await client.next(session_id=session_id, difficulty=difficulty, token=token)
        except Exception:
            # A re-roll can legitimately fail with no more unattempted
            # problems left at this difficulty (PoolExhaustedError -> 409)
            # once a small pool has been exhausted by prior retries, or any
            # other transient route error. Per this function's own contract
            # ("a persistent mismatch is recorded... rather than silently
            # treated as a hard failure" — module docstring), that must fall
            # through to an unmatched result on the LAST successfully
            # selected problem, not propagate and fail the whole attempt.
            _LOG.warning(
                "campaign_problem_reroll_failed session_id=%s persona_problem_id=%s",
                session_id,
                persona.problem_id,
                exc_info=True,
            )
            break
        problem = session.get("problem") or {}
        attempt_id = session.get("attempt_id", attempt_id)
        matched = problem.get("id") == persona.problem_id
        retries += 1
    return problem, matched, attempt_id


async def run_attempt(
    persona: PersonaAttempt,
    *,
    client: ApolloClient,
    chat_fn: ChatFn,
    artifact_reader: ArtifactReader,
    token: str,
    search_space_id: int,
    difficulty: str = "standard",
    clarification_max_turns: int = CLARIFICATION_MAX_TURNS,
    max_problem_retries: int = DEFAULT_MAX_PROBLEM_RETRIES,
) -> AttemptRecord:
    """Drive one persona attempt end-to-end. Never raises: any failure (HTTP,
    LLM, DB-readback) is caught and recorded as ``status="error"`` so a single
    bad attempt never kills a corpus run (plan D3 Step 1)."""
    t0 = time.monotonic()
    transcript: list[dict[str, Any]] = []
    expected = persona.expected.to_ledger_dict()
    expected["expects_clarification"] = persona.expected.expects_clarification
    try:
        session = await client.create_session(
            search_space_id=search_space_id,
            hoot_transcript=build_hoot_transcript(persona),
            difficulty=difficulty,
            token=token,
        )
        session_id = session["session_id"]

        _problem, matched, attempt_id = await _resolve_problem(
            persona,
            client=client,
            session=session,
            session_id=session_id,
            token=token,
            difficulty=difficulty,
            max_problem_retries=max_problem_retries,
        )

        await _play_scripted_beats(
            persona,
            client=client,
            session_id=session_id,
            token=token,
            chat_fn=chat_fn,
            transcript=transcript,
        )

        if persona.expected.expects_clarification:
            await _play_clarification_followups(
                persona,
                client=client,
                session_id=session_id,
                token=token,
                chat_fn=chat_fn,
                transcript=transcript,
                clarification_max_turns=clarification_max_turns,
            )

        done_response = await client.done(session_id=session_id, token=token)
        canonical, pair = await artifact_reader.read(attempt_id)
        scorecard = done_response.get("scorecard") if isinstance(done_response, dict) else None

        return AttemptRecord(
            status="ok",
            persona_id=_persona_id(persona),
            subject=persona.subject,
            concept=persona.concept,
            problem_id=persona.problem_id,
            persona_archetype=persona.persona,
            expected=expected,
            session_id=session_id,
            attempt_id=attempt_id,
            problem_matched=matched,
            transcript=tuple(transcript),
            done_response=done_response,
            artifact_canonical=canonical,
            artifact_pair=pair,
            scorecard=scorecard,
            wall_time_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - one bad attempt must not kill the corpus run
        _LOG.exception("campaign_attempt_failed persona=%s", _persona_id(persona))
        return AttemptRecord(
            status="error",
            persona_id=_persona_id(persona),
            subject=persona.subject,
            concept=persona.concept,
            problem_id=persona.problem_id,
            persona_archetype=persona.persona,
            expected=expected,
            session_id=None,
            attempt_id=None,
            problem_matched=None,
            transcript=tuple(transcript),
            done_response=None,
            artifact_canonical=None,
            artifact_pair=None,
            scorecard=None,
            wall_time_ms=int((time.monotonic() - t0) * 1000),
            error=repr(exc)[:500],
        )


async def run_corpus(
    personas: Sequence[PersonaAttempt],
    *,
    client: ApolloClient,
    chat_fn: ChatFn,
    artifact_reader: ArtifactReader,
    token: str,
    search_space_id: int,
    difficulty: str = "standard",
    attempts_path: Path | str | None = None,
    clarification_max_turns: int = CLARIFICATION_MAX_TURNS,
    max_problem_retries: int = DEFAULT_MAX_PROBLEM_RETRIES,
) -> list[AttemptRecord]:
    """Drive every persona in ``personas`` sequentially (parallelism is a
    Phase F orchestration concern, not this driver's — see the plan's
    ``run_corpus(personas, parallelism=1)`` note; sequential is the only mode
    implemented here since a shared Apollo session-per-user model means
    concurrent attempts need per-persona student identities, which is Task
    F1's orchestration job, not D3's). Appends one JSONL line per attempt to
    ``attempts_path`` as each attempt completes (so a crash mid-corpus still
    leaves prior attempts durable)."""
    records: list[AttemptRecord] = []
    for persona in personas:
        record = await run_attempt(
            persona,
            client=client,
            chat_fn=chat_fn,
            artifact_reader=artifact_reader,
            token=token,
            search_space_id=search_space_id,
            difficulty=difficulty,
            clarification_max_turns=clarification_max_turns,
            max_problem_retries=max_problem_retries,
        )
        if attempts_path is not None:
            append_attempt_record(record, attempts_path)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Real seam implementations (never exercised by unit tests; Phase F only)
# ---------------------------------------------------------------------------


@dataclass
class HttpxApolloClient:
    """Real :class:`ApolloClient` — thin ``httpx.AsyncClient`` wrapper over
    the real routes (``apollo/api.py``). Constructed once per campaign run;
    ``base_url`` points at the local campaign backend (e.g.
    ``http://127.0.0.1:8000``)."""

    base_url: str
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:  # pragma: no cover - real I/O client wiring
        import httpx

        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)

    def _headers(self, token: str) -> dict[str, str]:  # pragma: no cover - trivial
        return {"Authorization": f"Bearer {token}"}

    async def create_session(  # pragma: no cover - real network call
        self, *, search_space_id: int, hoot_transcript: str, difficulty: str, token: str
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/apollo/sessions/from_hoot",
            json={
                "search_space_id": search_space_id,
                "hoot_transcript": hoot_transcript,
                "difficulty": difficulty,
            },
            headers=self._headers(token),
        )
        resp.raise_for_status()
        return resp.json()

    async def next(  # pragma: no cover - real network call
        self, *, session_id: int, difficulty: str, token: str
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/apollo/sessions/{session_id}/next",
            json={"difficulty": difficulty},
            headers=self._headers(token),
        )
        resp.raise_for_status()
        return resp.json()

    async def chat(  # pragma: no cover - real network call
        self, *, session_id: int, message: str, token: str
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/apollo/sessions/{session_id}/chat",
            json={"message": message},
            headers=self._headers(token),
        )
        resp.raise_for_status()
        return resp.json()

    async def done(self, *, session_id: int, token: str) -> dict[str, Any]:  # pragma: no cover
        resp = await self._client.post(
            f"/apollo/sessions/{session_id}/done", headers=self._headers(token)
        )
        resp.raise_for_status()
        return resp.json()


class SqlArtifactReader:
    """Real :class:`ArtifactReader` — reads the two ``GradingArtifact`` rows
    (``role="canonical"``/``role="pair"``) a Done-click persisted, straight
    off the local campaign Postgres, via the SAME model
    (``apollo.persistence.models.GradingArtifact``) the writer
    (``apollo.handlers.artifact_writer``) inserts. Rows are converted back
    into the exact payload-dict shape ``build_graph_artifact``/
    ``build_llm_artifact`` produced (the JSONB columns already ARE that
    shape; only the identity columns are dropped since the driver already
    knows them)."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _row_to_payload(row: Any) -> dict[str, Any]:
        return {
            "grader_used": row.grader_used,
            "versions": row.versions,
            "node_ledger": row.node_ledger,
            "edge_ledger": row.edge_ledger,
            "misconceptions": row.misconceptions,
            "clarification_trace": row.clarification_trace,
            "scores": row.scores,
            "abstention": row.abstention,
            "grading_latency_ms": row.grading_latency_ms,
        }

    async def read(  # pragma: no cover - real DB round-trip
        self, attempt_id: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        from sqlalchemy import select

        from apollo.persistence.models import GradingArtifact

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(GradingArtifact).where(GradingArtifact.attempt_id == attempt_id)
                )
            ).scalars().all()
        canonical = next((r for r in rows if r.role == "canonical"), None)
        pair = next((r for r in rows if r.role == "pair"), None)
        return (
            self._row_to_payload(canonical) if canonical else None,
            self._row_to_payload(pair) if pair else None,
        )


async def default_chat_fn(  # pragma: no cover - real LLM call
    persona: PersonaAttempt, transcript: Sequence[dict[str, Any]], beat: str | None
) -> str:
    """Real agent-student ``ChatFn``: one GPT-4o call constrained by the
    persona's ``system_prompt`` + (during the teaching phase) the current
    scripted beat, or (during the bounded clarification phase) the persona's
    ``clarification_policy``."""
    import os

    from openai import OpenAI

    client = OpenAI()
    model = os.getenv("MAIN_MODEL", "gpt-4o")
    if beat is not None:
        instruction = f"Teach this next, in your own words: {beat}"
    else:
        instruction = (
            "Apollo just asked a clarifying question about something you said. "
            f"Your policy is '{persona.clarification_policy}': "
            "'answer_correctly' means resolve it correctly, 'answer_wrong' means "
            "confidently assert the wrong thing, 'stay_vague' means remain "
            "non-committal without actually answering."
        )
    messages = [{"role": "system", "content": persona.system_prompt}]
    for turn in transcript:
        role = "assistant" if turn.get("role") == "apollo" else "user"
        messages.append({"role": role, "content": str(turn.get("content", ""))})
    messages.append({"role": "user", "content": instruction})
    resp = client.chat.completions.create(model=model, messages=messages)
    return resp.choices[0].message.content or ""


async def mint_student_token(  # pragma: no cover - real Supabase admin-API call
    *, email: str, password: str, supabase_url: str, service_role_key: str
) -> str:
    """Mint a local-Supabase student JWT for the campaign driver's
    ``Authorization: Bearer`` header. Creates the user via the admin API if it
    does not already exist, then signs in for a session token."""
    import httpx

    async with httpx.AsyncClient(base_url=supabase_url, timeout=30.0) as client:
        headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
        await client.post(
            "/auth/v1/admin/users",
            json={"email": email, "password": password, "email_confirm": True},
            headers=headers,
        )
        resp = await client.post(
            "/auth/v1/token?grant_type=password",
            json={"email": email, "password": password},
            headers={"apikey": service_role_key},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
