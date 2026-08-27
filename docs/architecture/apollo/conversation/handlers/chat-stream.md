---
doc: apollo/conversation/handlers/chat-stream
description: apollo/handlers/chat_stream.py — SSE view over a teaching turn (POST /sessions/{id}/chat/stream), its event contract, and the disconnect invariant
owns:
  - apollo/handlers/chat_stream.py
related:
  - apollo/conversation/handlers/chat
  - apollo/conversation/routing/router
  - apollo/conversation/routing/errors
  - database/session
  - platform/http-server
last_verified: 2026-08-23
stub: false
---

# handlers/chat-stream — the streaming teaching turn

`POST /apollo/sessions/{id}/chat/stream` (2026-08-23 study-prep §B.1 Tier 1).
Same body, same turn, same final payload as the BLOCKING `.../chat` route —
delivered as SSE phase events so the student sees activity immediately instead
of a 10-17s dead spinner. The blocking route is untouched and remains the
fallback / kill switch; the student UI picks between them behind a flag.

## Interface

- `stream_chat_turn(*, request, neo, open_session, session_id, message,
  ask_hoot=False) -> AsyncIterator[str]` — the SSE generator `routing/router`
  hands to `StreamingResponse`.
- `get_turn_session_opener() -> TurnSessionOpener` — FastAPI dependency giving
  the detached turn task its own session context manager.
- `sse_frame(event, data) -> str`; `MEDIA_TYPE`, `STREAM_HEADERS`;
  `EVENT_RECEIVED` / `_WORKING` / `_REPLY` / `_COMPLETE` / `_ERROR`;
  `STAGE_ACCEPTED`.

## Event contract (the student UI types against this)

Media type `text/event-stream`; headers `Cache-Control: no-cache` +
`X-Accel-Buffering: no` — identical to `/ask/stream` (`platform/http-server`),
whose reader the student UI reuses. Frames are `event: <name>\ndata: <one-line
JSON>\n\n`; every `data` object repeats its own name in an `"event"` key so it
also parses standalone.

| Event | Data | When |
|---|---|---|
| `received` | `{session_id}` | first frame, before any turn work |
| `working` | `{stage, message}` | `accepted` immediately, then `reading` / `thinking` / `grading` from the turn's own phases |
| `reply` | `{apollo_reply}` | Apollo's student-visible text is final |
| `complete` | `{payload}` | terminal — `payload` is the blocking route's JSON body |
| `error` | `{status, body, message}` | terminal — `body` rendered by the SAME registered exception handler the blocking route would hit; `message` mirrors its student-facing text at top level |

Ordering:

```
received → working(accepted) → working* → reply → working* → (complete | error)
```

`working` events appear on BOTH sides of `reply`: an auto-done turn emits
`working(grading)` AFTER the reply, because the reply is released before the
grading run (see `handlers/chat`). Exactly one `reply` always precedes
`complete`. **Exactly one terminal event, always** — `complete` or `error`,
never both, never neither (the `_KIND_END` branch emits the generic `error`
rather than closing silently, so the guarantee survives a turn task killed by
a BaseException). On `error`, `reply` may or may not already have been sent.
There is no heartbeat: the widest gap is the ~8-12s unified call, well inside
proxy idle timeouts (`/ask/stream` runs 30-60s gaps on the same chain).

## Invariants & gotchas

- **The stream is a VIEW, never a second writer.** The turn runs as a detached
  `asyncio.Task` pushing onto an UNBOUNDED queue; the generator only drains it.
  A client disconnect cancels the generator, not the task (a bare
  `asyncio.create_task` escapes anyio's cancel scope), and the task can never
  block on an unread queue — so the turn completes and commits exactly as the
  blocking route would. NEVER add a `task.cancel()` to the generator, and never
  bound the queue.
- **`_BACKGROUND_TURNS` is load-bearing**: asyncio only weak-refs tasks and the
  creating frame dies on disconnect — dropping the strong ref would let the GC
  kill the very turn this design exists to protect. Its done-callback is
  `_release_background_turn`, not `set.discard`: it also RETRIEVES the task's
  exception and logs it at ERROR (`apollo_stream_turn_task_failed`), which is
  the one server-side signal for the failure mode `_KIND_END` covers on the
  wire. It checks `cancelled()` first, because `Task.exception()` raises on a
  cancelled task and worker shutdown cancels in-flight turns.
- **The turn owns its own DB session** (`get_turn_session_opener`, an
  `asynccontextmanager` over `get_db_session`). It must NOT borrow the
  request-scoped session: FastAPI closes dependency sessions once the response
  finishes, which on a disconnect is mid-turn. Wrapping `get_db_session` keeps
  DB-08b RLS identical — `create_task` copies the context, so the
  `current_request_user_id` the owner gate resolved is still visible.
  `get_turn_session_opener` is a FastAPI dependency purely so route tests can
  redirect it; overriding `get_db_session` alone would not reach the task.
- **The route releases the gate's connection** (`await db.rollback()` in
  `apollo/api.py`): the owner gate is the only request-scoped-session use, and
  holding it for the whole stream would cost two pool slots per turn where the
  blocking route costs one.
- **Pre-turn failures stay real HTTP** (401/403/404 from the owner gate, 422
  from body validation) because they precede the 200. Only failures raised
  inside the turn become `error` events — headers are already sent by then.
- **Errors are rendered by the registered handler**, not re-mapped here, so
  `error.body` is byte-equal to the blocking route's body and the FE's existing
  409 `session_frozen` / 503 handling works unchanged. `error.message` is a
  TOP-LEVEL mirror of that body's student-facing text (`body.message`, else
  `body.detail`, else the generic envelope's), because the `/ask/stream` reader
  the UI is built around does `payload.message || …` — without it, a reader
  reusing that shape renders nothing.
- **A terminal event is structural, not best-effort.** `_run_turn` catches
  `Exception`, so a BaseException (worker-shutdown `CancelledError`,
  `SystemExit`, an escaping `BaseExceptionGroup`) reaches only its `finally`
  and the sentinel. The generator's `_KIND_END` branch therefore emits the
  generic 500 `error` and logs `apollo_chat_stream_turn_vanished` — never
  close a stream with no terminal event; a client waiting on one would hang on
  its own state machine.
- **`complete.payload` runs through `jsonable_encoder`** — the encoder FastAPI
  applies to the blocking route's return value — so the two are equal, not
  merely similar.
- **The phase vocabulary is CLOSED**: a `TURN_PHASE_*` with no entry in
  `_WORKING_MESSAGES` is dropped (logged), never forwarded without copy.
- SSE, not literal NDJSON: the plan said "NDJSON mirroring `/api/ask/stream`",
  but that endpoint is SSE. Reader reuse won; see the module docstring.

## Related

Turn machinery + the `on_phase` sink: `handlers/chat`. Route wiring:
`routing/router`. Error taxonomy: `routing/errors`. Session/RLS:
`database/session`.
