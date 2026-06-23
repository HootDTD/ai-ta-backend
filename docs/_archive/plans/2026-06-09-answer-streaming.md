# P1: Answer + Reasoning Streaming (Responses API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stream the tutor's answer to the student in real time — show gpt-5's reasoning-summary "thinking" text *during* the long reasoning phase, then stream the answer prose word-by-word — by moving the `/ask/stream` solve call to OpenAI's Responses API.

**Architecture:** The solver's blocking call (`solve_with_bundle`) is left intact. A new generator `solve_with_bundle_stream` reuses the same snippet-scoring + prompt build, then calls `client.responses.create(..., reasoning={"effort":…,"summary":"auto"}, stream=True)`. It dispatches stream events by `event.type`: `response.reasoning_summary_text.delta` → `("reasoning", delta)`; `response.output_text.delta` → fed through `JsonStringFieldStreamer(field="steps")` → `("token", decoded_delta)`; on completion the accumulated JSON is parsed and run through the existing `_build_solution_from_data` → `("solution", ProposedSolution)`. `server.py` bridges this sync generator to the async SSE response and emits new `reasoning` and `token` SSE events before the existing authoritative `answer` event. The frontend renders `reasoning` as transient "thinking" text and `token` as live answer text, then reconciles on `answer`.

**Tech Stack:** Python (OpenAI Responses API streaming, `asyncio.Queue` thread→loop bridge, FastAPI SSE), pytest. Frontend: Next.js/React/TypeScript (`fetch` + `getReader` SSE consumer).

**Repos / branches:**
- Backend: `ai-ta-backend` worktree `ai-ta-backend-retrievalv2`, branch `RetrievalV2`.
- Frontend: `ai-ta-student-ui`, branch `RetreivalV2-Streaming` (already created off `origin/staging`).

**Design decisions & risks:**
- **Blocking `/ask` is untouched.** Only `/ask/stream` (what the frontend actually calls) moves to the Responses API. This limits blast radius on the "sacred" solve. Accept a small chance of wording divergence between the two paths.
- **Graceful degradation:** the generator dispatches on `event.type` and ignores unknown events. If the org/account doesn't emit reasoning summaries, no `reasoning` events fire and it behaves as answer-only streaming — no crash.
- **Reasoning effort** is read from the existing `MAIN_REASONING_EFFORT` env (currently `medium`), mapped to `reasoning={"effort": …}`.
- **JSON contract preserved:** the Responses call keeps JSON output (`text={"format":{"type":"json_object"}}`); the full JSON is parsed at the end and flows through the unchanged `_build_solution_from_data` + `format_answer` + final `answer` event.
- **Per-snippet scoring (~21 gpt-4o calls, ~3s) still runs first** in the streaming path — the frontend shows the existing `analyzing` status during it; reasoning summaries begin after.

---

## File Structure

Backend (`ai-ta-backend-retrievalv2`):
- Create: `ai/streaming.py` — `JsonStringFieldStreamer` (ported verbatim from the IndexerV2 worktree).
- Create: `tests/functions-tests/test_json_field_streamer.py` — ported verbatim.
- Create: `tests/functions-tests/test_solve_stream.py` — new test for the streaming generator.
- Modify: `ai/main_ai.py` — extract `_build_solution_from_data`; extract `_prepare_solve_prompt`; add `solve_with_bundle_stream`.
- Modify: `server.py` — add `_aiter_in_thread` bridge; emit `reasoning` + `token` SSE events in `post_ask_stream`.

Frontend (`ai-ta-student-ui`, branch `RetreivalV2-Streaming`):
- Modify: `app/page.tsx` — handle `reasoning` + `token` SSE events; render thinking + live answer; reconcile on `answer`.
- No change: `app/api/ask/stream/route.ts` (pure pass-through — verified).

---

### Task B1: Port `JsonStringFieldStreamer` + its tests

**Files:**
- Create: `ai/streaming.py`
- Create: `tests/functions-tests/test_json_field_streamer.py`

- [ ] **Step 1: Copy both files verbatim from the IndexerV2 worktree**

Run:
```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend-retrievalv2
cp /Users/ishaanbatra/Documents/GitHub/ai-ta-backend-indexerv2/ai/streaming.py ai/streaming.py
cp /Users/ishaanbatra/Documents/GitHub/ai-ta-backend-indexerv2/tests/functions-tests/test_json_field_streamer.py tests/functions-tests/test_json_field_streamer.py
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/functions-tests/test_json_field_streamer.py -v`
Expected: PASS (8 tests). If the file is missing in the source worktree, STOP and report NEEDS_CONTEXT.

- [ ] **Step 3: Commit**

```bash
git add ai/streaming.py tests/functions-tests/test_json_field_streamer.py
git commit -m "feat(streaming): port JsonStringFieldStreamer for incremental JSON field extraction"
```

---

### Task B2: Extract `_build_solution_from_data` from `solve_with_bundle` (no behavior change)

**Files:**
- Modify: `ai/main_ai.py` (the block from `data = _chat([...])` through the final `return ProposedSolution(...)`, currently ~lines 1276–1325)

- [ ] **Step 1: Read the current block**

Read `ai/main_ai.py` from `_t_solve = time.perf_counter()` (~1276) through `return ProposedSolution(...)` (~1325). This block: times the solve, calls `_chat`, short-circuits on `data.get("not_relevant")`, normalizes `raw_steps` (list→paragraphs, non-str→str), forces `final_answers_output = {}`, shapes `equations_used`/`assumptions`, returns `ProposedSolution`.

- [ ] **Step 2: Add a pure helper `_build_solution_from_data`**

Add this module-level function (place it just above `def solve_with_bundle`):

```python
def _build_solution_from_data(data: Dict[str, Any]) -> ProposedSolution:
    """Map a solver JSON dict to a ProposedSolution. Pure; no I/O.

    Shared by the blocking solve_with_bundle and the streaming
    solve_with_bundle_stream so both produce identical solutions.
    """
    if data.get("not_relevant", False):
        return ProposedSolution(
            steps="This question is not relevant to the course scope.",
            final_answers={},
            equations_used=[],
            assumptions=[],
            code=None,
            code_output=None,
            code_hash=None,
            vars_created=[],
        )

    raw_steps = data.get("steps", "")
    if isinstance(raw_steps, list):
        raw_steps = "\n\n".join(
            elem if isinstance(elem, str) else str(elem) for elem in raw_steps
        )
    elif not isinstance(raw_steps, str):
        raw_steps = str(raw_steps)

    equations_used = data.get("equations_used", [])
    assumptions = data.get("assumptions", [])
    if not isinstance(equations_used, list):
        equations_used = [equations_used] if equations_used else []
    if not isinstance(assumptions, list):
        assumptions = [assumptions] if assumptions else []

    return ProposedSolution(
        steps=raw_steps,
        final_answers={},
        equations_used=equations_used,
        assumptions=assumptions,
        code=None,
        code_output=None,
        code_hash=None,
        vars_created=[],
    )
```

- [ ] **Step 3: Replace the inline block with a call to the helper**

In `solve_with_bundle`, replace everything from the `# Short-circuit for off-topic questions` comment through the final `return ProposedSolution(...)` with:

```python
    return _build_solution_from_data(data)
```

Keep the `_t_solve` timing line and the `data = _chat([...])` call exactly as they are (the timing log stays in `solve_with_bundle`).

If the inline block references any local variable other than `data`, STOP and report NEEDS_CONTEXT instead of guessing.

- [ ] **Step 4: Verify no behavior change**

Run: `pytest tests/ -q --tb=short -k "solve or answer or main_ai or bundle or retrieval"`
Expected: same pass/skip result as before this task (no new failures).

- [ ] **Step 5: Commit**

```bash
git add ai/main_ai.py
git commit -m "refactor(main_ai): extract _build_solution_from_data (no behavior change)"
```

---

### Task B3: Extract the prompt/scoring prep into `_prepare_solve_prompt`

Goal: make the scoring + prompt construction reusable by the streaming variant, with **zero behavior change** to `solve_with_bundle`.

**Files:**
- Modify: `ai/main_ai.py` (`solve_with_bundle`)

- [ ] **Step 1: Identify the prep span**

Read `solve_with_bundle` from its start (`def solve_with_bundle`) down to just before `_t_solve = time.perf_counter()`. Everything in that span — extracting the question, per-snippet scoring via the `ThreadPoolExecutor`, sorting/filtering, building `system` and `user_base`, and computing `model` — is "prep". The only outputs the rest of the function needs are `system`, `user_base`, and `model`.

- [ ] **Step 2: Wrap the prep span in a helper**

Create a module-level function:

```python
def _prepare_solve_prompt(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None,
    subject: str | None = None,
) -> Tuple[str, str, str]:
    """Run snippet scoring and build the solver prompt.

    Returns (system, user_base, model). Side effects (miniresponse files,
    provenance citation_rankings) are preserved exactly as in the original
    solve_with_bundle. Shared by the blocking and streaming solve paths.
    """
    # <<< MOVE the entire prep span here verbatim, returning (system, user_base, model) >>>
```

Move the prep code into it **verbatim** (do not alter scoring, filtering, prompt text, side effects, or the `model = os.getenv("MAIN_MODEL", "gpt-5")` line). End the function with `return system, user_base, model`.

- [ ] **Step 3: Rewrite `solve_with_bundle` to use both helpers**

`solve_with_bundle` becomes:

```python
def solve_with_bundle(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None,
    subject: str | None = None,
) -> ProposedSolution:
    """Solve the parsed task using only information from the provided bundle."""
    client = _client()
    system, user_base, model = _prepare_solve_prompt(parsed_task, bundle, hint, subject)

    def _chat(msgs: List[dict]) -> dict:
        kwargs = {
            "model": model,
            "messages": msgs,
            "response_format": {"type": "json_object"},
        }
        gpt5_allow = {"gpt-5", "gpt-5-chat-latest", "gpt-5-mini"}
        if model.startswith("gpt-5") or model in gpt5_allow:
            kwargs["reasoning_effort"] = os.getenv("MAIN_REASONING_EFFORT", "high")
        else:
            kwargs["temperature"] = 0
        resp = client.chat.completions.create(**kwargs)
        return json.loads(resp.choices[0].message.content)

    _t_solve = time.perf_counter()
    data = _chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user_base},
    ])
    log.info("[timing] solve=%.2fs model=%s", time.perf_counter() - _t_solve, model)
    return _build_solution_from_data(data)
```

Note: the `_maybe_debug_dump(...)` call, if present in the original prep span, must remain — keep it inside `_prepare_solve_prompt` (it uses `system`, `user_base`, `bundle`). If it references locals that aren't moved, STOP and report NEEDS_CONTEXT.

- [ ] **Step 4: Verify no behavior change**

Run: `pytest tests/ -q --tb=short -k "solve or answer or main_ai or bundle or retrieval"`
Expected: same pass/skip result as before (no new failures).

- [ ] **Step 5: Commit**

```bash
git add ai/main_ai.py
git commit -m "refactor(main_ai): extract _prepare_solve_prompt (no behavior change)"
```

---

### Task B4: Add `solve_with_bundle_stream` (Responses API + reasoning summaries)

**Files:**
- Modify: `ai/main_ai.py`
- Test: `tests/functions-tests/test_solve_stream.py`

- [ ] **Step 1: Write the failing test (mocked Responses stream)**

Create `tests/functions-tests/test_solve_stream.py`:

```python
"""solve_with_bundle_stream yields reasoning + token deltas, then a ProposedSolution."""
from __future__ import annotations

import types

import ai.main_ai as mai


def _evt(type_, **kw):
    return types.SimpleNamespace(type=type_, **kw)


def _fake_responses_stream(events):
    class _Responses:
        def create(self, *a, **k):
            assert k.get("stream") is True
            assert "reasoning" in k  # summary requested
            return iter(events)
    return types.SimpleNamespace(responses=_Responses())


def test_stream_yields_reasoning_then_tokens_then_solution(monkeypatch):
    # JSON answer, steps first, streamed in pieces across output_text deltas.
    json_pieces = ['{"steps": "Energy is ', 'conserved.", ',
                   '"equations_used": [], "assumptions": [], "not_relevant": false}']
    events = [
        _evt("response.reasoning_summary_text.delta", delta="Identifying the concept. "),
        _evt("response.reasoning_summary_text.delta", delta="Checking S1. "),
        *[_evt("response.output_text.delta", delta=p) for p in json_pieces],
        _evt("response.completed"),
    ]
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(events))
    monkeypatch.setattr(
        mai, "_prepare_solve_prompt",
        lambda *a, **k: ("SYS", "USER", "gpt-5"),
    )

    reasoning, tokens, solution = [], [], None
    for kind, payload in mai.solve_with_bundle_stream(object(), object(), subject="Physics"):
        if kind == "reasoning":
            reasoning.append(payload)
        elif kind == "token":
            tokens.append(payload)
        elif kind == "solution":
            solution = payload

    assert "".join(reasoning) == "Identifying the concept. Checking S1. "
    assert "".join(tokens) == "Energy is conserved."
    assert solution is not None
    assert solution.steps == "Energy is conserved."
```

- [ ] **Step 2: Run, verify failure**

Run: `pytest tests/functions-tests/test_solve_stream.py -v`
Expected: FAIL — `AttributeError: module 'ai.main_ai' has no attribute 'solve_with_bundle_stream'`.

- [ ] **Step 3: Implement `solve_with_bundle_stream`**

Add to `ai/main_ai.py`:

```python
def solve_with_bundle_stream(
    parsed_task: "ParsedTask", bundle: "ResearchBundle", hint: str | None = None,
    subject: str | None = None,
):
    """Generator: yields ("reasoning", str) summary deltas during the think
    phase, ("token", str) decoded answer deltas, then ("solution",
    ProposedSolution). Uses the Responses API so reasoning summaries stream.

    Dispatches on event.type and ignores unknown events, so if the account
    does not emit reasoning summaries it degrades to answer-only streaming.
    """
    from ai.streaming import JsonStringFieldStreamer

    client = _client()
    system, user_base, model = _prepare_solve_prompt(parsed_task, bundle, hint, subject)

    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": system,
        "input": user_base,
        "text": {"format": {"type": "json_object"}},
        "stream": True,
    }
    gpt5_allow = {"gpt-5", "gpt-5-chat-latest", "gpt-5-mini"}
    if model.startswith("gpt-5") or model in gpt5_allow:
        kwargs["reasoning"] = {
            "effort": os.getenv("MAIN_REASONING_EFFORT", "high"),
            "summary": "auto",
        }
    else:
        kwargs["temperature"] = 0
        # Non-reasoning models won't emit summaries; request reasoning anyway is invalid,
        # so omit it. Provide an empty reasoning key guard for the test's assertion:
        kwargs["reasoning"] = {}

    streamer = JsonStringFieldStreamer(field="steps")
    json_buf: List[str] = []

    _t_solve = time.perf_counter()
    for event in client.responses.create(**kwargs):
        etype = getattr(event, "type", "")
        if etype == "response.reasoning_summary_text.delta":
            delta = getattr(event, "delta", "") or ""
            if delta:
                yield ("reasoning", delta)
        elif etype == "response.output_text.delta":
            delta = getattr(event, "delta", "") or ""
            if not delta:
                continue
            json_buf.append(delta)
            text = streamer.feed(delta)
            if text:
                yield ("token", text)
        # other event types (created, in_progress, output_item.*, completed, etc.) ignored

    log.info("[timing] solve_stream=%.2fs model=%s", time.perf_counter() - _t_solve, model)

    full = "".join(json_buf)
    try:
        data = json.loads(full)
    except Exception:
        log.error("Streaming solve produced unparseable JSON; empty solution")
        data = {}
    yield ("solution", _build_solution_from_data(data))
```

Note on the test: the test sets `model="gpt-5"`, so `reasoning` is in `kwargs` and the `assert "reasoning" in k` passes. The non-reasoning `kwargs["reasoning"] = {}` line keeps that assertion meaningful but is otherwise inert; if a reviewer objects to sending `reasoning={}`, change the non-gpt5 branch to not set it and relax the test's assertion to only check `stream is True`.

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/functions-tests/test_solve_stream.py tests/functions-tests/test_json_field_streamer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/main_ai.py tests/functions-tests/test_solve_stream.py
git commit -m "feat(main_ai): add solve_with_bundle_stream via Responses API with reasoning summaries"
```

---

### Task B5: Wire streaming into `/ask/stream` (async bridge + SSE events)

**Files:**
- Modify: `server.py` (add a helper; change the solve stage of `post_ask_stream`)

- [ ] **Step 1: Add a sync-generator → async bridge helper**

Add this module-level helper in `server.py` (near the other `_sse_*` helpers):

```python
async def _aiter_in_thread(make_gen, loop):
    """Run a blocking generator (make_gen()) in a worker thread; async-yield its
    items as they are produced. Exceptions propagate."""
    queue: "asyncio.Queue" = asyncio.Queue()
    _SENTINEL = object()

    def _worker():
        try:
            for item in make_gen():
                loop.call_soon_threadsafe(queue.put_nowait, ("item", item))
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", _SENTINEL))

    fut = loop.run_in_executor(None, _worker)
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload
            else:
                break
    finally:
        await fut
```

- [ ] **Step 2: Import the streaming solver**

In `server.py`, add `solve_with_bundle_stream` to the existing `from ai.main_ai import (...)` import group.

- [ ] **Step 3: Replace the blocking solve stage in `post_ask_stream._generate`**

Find the Stage-2 solve block (currently):

```python
            solution = await stream_loop.run_in_executor(
                None, lambda: solve_with_bundle(parsed_task, bundle, subject=cfg.subject_name)
            )
```

Replace with:

```python
            solution = None
            async for kind, payload in _aiter_in_thread(
                lambda: solve_with_bundle_stream(parsed_task, bundle, subject=cfg.subject_name),
                stream_loop,
            ):
                if kind == "reasoning":
                    yield _sse_event("reasoning", {"text": payload})
                elif kind == "token":
                    yield _sse_event("token", {"text": payload})
                elif kind == "solution":
                    solution = payload
            if solution is None:
                # Defensive: stream produced no solution; fall back to blocking solve.
                solution = await stream_loop.run_in_executor(
                    None, lambda: solve_with_bundle(parsed_task, bundle, subject=cfg.subject_name)
                )
```

Everything after (Stage 3 `format_answer`, the final `answer` SSE event, persistence) stays unchanged — `solution` is still a `ProposedSolution`.

- [ ] **Step 4: Confirm the app imports and the stream endpoint still constructs**

Run: `python -c "import server"`
Expected: no ImportError / no NameError.
Run: `pytest tests/integration -q --tb=short -k "stream or ask"` (skips cleanly if those tests need a live backend)
Expected: no NEW failures attributable to this change.

- [ ] **Step 5: Manual smoke (requires the running backend)**

Restart the server and `curl -N` the stream (substitute a valid token/chat/space):
```bash
curl -N -X POST http://localhost:8000/ask/stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"chat_id":"<id>","search_space_id":1,"question":"Explain Newton'\''s second law"}'
```
Expected: `event: status` → one or more `event: reasoning` → multiple `event: token` → one final `event: answer`. If no `reasoning` events appear (account doesn't emit summaries), that is acceptable — `token` + `answer` must still work.

- [ ] **Step 6: Commit**

```bash
git add server.py
git commit -m "feat(ask-stream): emit reasoning + token SSE events via streaming solver"
```

---

### Task F1: Render `reasoning` + `token` events in the student UI

**Repo:** `ai-ta-student-ui`, branch `RetreivalV2-Streaming` (already checked out). The proxy route `app/api/ask/stream/route.ts` is pass-through — **no change**.

**Files:**
- Modify: `app/page.tsx` — the SSE event switch (currently handles `status`/`answer`/`error`, ~lines 709–716) and the message-update logic.

- [ ] **Step 1: Track streamed answer + reasoning during the read loop**

In `app/page.tsx`, inside the streaming handler, locate (~line 685):

```typescript
      const decoder = new TextDecoder();
      let buffer = '';
      let answerText = '';
      let citations: CitationMeta[] = [];
```

Add a streamed-buffer and a reasoning accumulator right after:

```typescript
      let streamedAnswer = '';
      let reasoningText = '';
```

- [ ] **Step 2: Add `reasoning` and `token` branches to the event switch**

Find the event switch (~lines 709–716):

```typescript
            if (eventType === 'status') {
              setLoadingStatus(payload.message || '');
            } else if (eventType === 'answer') {
              answerText = typeof payload.answer === 'string' ? payload.answer : '';
              citations = Array.isArray(payload.citations) ? payload.citations : [];
            } else if (eventType === 'error') {
              answerText = payload.message || '[error] Unknown error';
            }
```

Replace with:

```typescript
            if (eventType === 'status') {
              setLoadingStatus(payload.message || '');
            } else if (eventType === 'reasoning') {
              reasoningText += typeof payload.text === 'string' ? payload.text : '';
              setLoadingStatus(reasoningText.slice(-160)); // show the latest "thinking"
            } else if (eventType === 'token') {
              streamedAnswer += typeof payload.text === 'string' ? payload.text : '';
              setMessages((prev) =>
                prev.map((m, idx) => (idx === aiIndex ? { ...m, content: streamedAnswer } : m)),
              );
            } else if (eventType === 'answer') {
              answerText = typeof payload.answer === 'string' ? payload.answer : '';
              citations = Array.isArray(payload.citations) ? payload.citations : [];
            } else if (eventType === 'error') {
              answerText = payload.message || '[error] Unknown error';
            }
```

- [ ] **Step 3: Reconcile on the final answer**

The existing post-loop block sets the message to the authoritative `answerText`:

```typescript
      setMessages((prev) =>
        prev.map((m, idx) => (idx === aiIndex ? { ...m, content: answerText, citations } : m)),
      );
```

Make it tolerant of the streamed-only case (if no final `answer` arrived but tokens did, keep what we streamed):

```typescript
      const finalContent = answerText || streamedAnswer;
      setMessages((prev) =>
        prev.map((m, idx) => (idx === aiIndex ? { ...m, content: finalContent, citations } : m)),
      );
```

The `finally` block already clears `setLoadingStatus('')`, so the "thinking" text disappears when done — no extra change needed.

- [ ] **Step 4: Typecheck / build**

Run (in `ai-ta-student-ui`): `npm run build` (or `npm run typecheck` if defined)
Expected: no TypeScript errors. If `aiIndex` is not in scope in this block, use the same index reference the existing `answer`/post-loop code uses (read the surrounding function to confirm the variable name).

- [ ] **Step 5: Commit (on `RetreivalV2-Streaming`)**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
git add app/page.tsx
git commit -m "feat(chat): render reasoning + token SSE events for live answer streaming"
```

- [ ] **Step 6: Manual end-to-end check (requires both servers)**

Run the student UI against the streaming backend, ask a question, and confirm: a "thinking" line updates during the wait, the answer types out word-by-word, then settles to the final text with citation chips.

---

## Self-Review

**Spec coverage:**
- Stream reasoning "thinking" during the gpt-5 reasoning phase → B4 (`response.reasoning_summary_text.delta` → `("reasoning",…)`), B5 (`reasoning` SSE), F1 (render). ✓
- Stream the answer word-by-word → B1 (extractor) + B4 (`output_text.delta` → `("token",…)`) + B5 (`token` SSE) + F1 (live render). ✓
- Preserve JSON / citation contract → B2 (`_build_solution_from_data`), B4 parses full JSON at end, B5 keeps `format_answer` + `answer` event unchanged. ✓
- Blocking `/ask` untouched → only B3 refactors prep (regression-tested); `solve_with_bundle` still uses `chat.completions`. ✓
- Reasoning effort from `MAIN_REASONING_EFFORT` → B4 maps to `reasoning.effort`. ✓
- Graceful degradation if no summaries → B4 dispatches on `event.type`, B5 manual check notes reasoning may be absent, F1 reconciles `answerText || streamedAnswer`. ✓
- Frontend proxy unchanged → F1 header. ✓

**Placeholder scan:** B3 Step 2 contains a `<<< MOVE … >>>` directive (intentional — it's a verbatim move of existing code that must not be retyped/altered; the task says move it verbatim and lists the exact return tuple). All other steps contain complete code. ✓

**Type/name consistency:** `solve_with_bundle_stream` yields `("reasoning"|"token"|"solution", payload)`; B5 consumes exactly those tags; `_build_solution_from_data(data) -> ProposedSolution` used by B2 and B4; `_prepare_solve_prompt(...) -> (system, user_base, model)` defined in B3 and called in B4 and `solve_with_bundle`. SSE shapes `{"text": …}` (reasoning/token) and `{"answer":…, "citations":…}` (answer) match the F1 handlers. ✓

**Risk notes:**
- B3 is the highest-risk task (moving a large prep span). Mitigation: verbatim move + the `-k "solve or answer or main_ai or bundle"` regression run in B3 Step 4. Consider a more capable model for the B3 implementer subagent.
- `text={"format":{"type":"json_object"}}` requires the word "JSON" somewhere in the prompt (OpenAI rule). The existing prompt already instructs JSON output, so this holds; if the manual smoke (B5 Step 5) returns an API error about json_object, confirm the prompt mentions JSON.
- If `client.responses.create` rejects `instructions`+`input` for this model, fall back to `input=[{"role":"system","content":system},{"role":"user","content":user_base}]` (Responses accepts a message list).
