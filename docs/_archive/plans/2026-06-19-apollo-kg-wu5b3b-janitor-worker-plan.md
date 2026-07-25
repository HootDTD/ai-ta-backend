# WU-5B3b — dormant `apollo-janitor` worker process — TDD implementation plan

**Date:** 2026-06-19
**Branch:** `feat/apollo-kg-wu5b3b-janitor-worker` (already checked out — do NOT branch/switch/push/PR)
**Patch-coverage gate:** `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b3a1-janitor-drain --fail-under=95`
**Authoritative design:** the WU-5B3b section of `docs/superpowers/plans/2026-06-19-apollo-kg-wu5b3-split-proposal.md` (§3 + §7) and its ORCHESTRATOR ADJUDICATION; spec `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §6/§7.
**Runner:** `.venv/Scripts/python.exe` (py3.12).

---

## 1. Goal & one-line

**Goal:** Ship the dormant `apollo-janitor` worker process — a thin async-native poll loop that wraps the FROZEN `drain_pending_attempts` (WU-5B3a-1) behind the default-OFF `APOLLO_LEARNER_JANITOR_ENABLED` flag, with cooperative SIGTERM/SIGINT shutdown — plus the third Procfile process line, and reconcile the two owner docs in the same commit.

**One-line:** `while not stop_event: (if flag ON → await drain_pending_attempts(neo, limit=1); log DrainResult) else skip; await asyncio.sleep(POLL_SECONDS)` on a single `asyncio.run(main())` loop, scaled to 0 replicas on Railway until the flag flips.

**This unit is a THIN process wrapper.** The drain logic is FROZEN. This unit adds NO drain logic, NO claim/lease/backoff/dead-letter, NO SQL, NO migration. It owns only: the loop shell, the flag-read, the SIGTERM handler, the Neo4jClient lifetime, the Procfile line, and the doc reconcile.

**DEFERRED out of v1 (do NOT build — see §11):** the opportunistic POST-COMMIT backstop hook in `done.py` (split-proposal §3 / ADJUDICATION #2 recommended DEFER). This plan does NOT touch `apollo/handlers/done.py`.

## 2. Ground truth (verified file:line — read this pass)

**The FROZEN drain (REUSE as-is, do NOT edit `apollo/handlers/learner_janitor.py`):**
- `apollo/handlers/learner_janitor.py:310-366` — `async drain_pending_attempts(neo, *, limit=1, max_attempts=MAX_ATTEMPTS, user_id=None) -> DrainResult`. `neo` is POSITIONAL (the `Neo4jClient`). It MANAGES ITS OWN three short-lived `get_async_session()` phases internally (`:155` claim, `:200` work, `:253` record, `:299` backlog) — **the worker passes NO `db` session.** Returns a frozen `DrainResult` (`:103-114`: `claimed/succeeded/dead_lettered/retried/deferred/backlog_remaining`). The drain itself already emits `_LOG.info("learner_janitor_drain", extra={...})` at `:355-365` — so the worker logging the returned `DrainResult` is additive, not the sole observability.
- `apollo/handlers/learner_janitor.py:91-96` — module constants `MAX_ATTEMPTS` etc. are env-overridable via `_int_env`/`_float_env` (`:69-86`). The worker REUSES `MAX_ATTEMPTS` by import; it does NOT redefine the backoff constants.
- `apollo/handlers/learner_janitor.py:216` — the LAYER3 interlock (`_graph_sim_layer3_enabled()`) lives INSIDE the drain's `_work_one`. **The worker need only gate on the JANITOR flag** — the drain carries the LAYER3 guard itself. (ADJUDICATION fact #3.)

**The env-flag pattern to MIRROR (do NOT import from `done.py` — re-implement the one-liner locally to keep the worker decoupled):**
- `apollo/handlers/done.py:84-100` — `_GRAPH_SIM_LAYER3_FLAG = "APOLLO_GRAPH_SIM_LAYER3_ENABLED"` + `def _graph_sim_layer3_enabled() -> bool: return os.environ.get(_GRAPH_SIM_LAYER3_FLAG, "").lower() in ("1", "true", "yes")`. The worker mirrors this exact shape for `APOLLO_LEARNER_JANITOR_ENABLED` (default OFF). **Read the flag INSIDE the loop body each iteration** (not once at startup) so a flag flip is observed without a restart — mirrors how `done.py` reads its flags per-request.

**Neo4jClient construction & teardown (the app's own pattern — MIRROR):**
- `apollo/persistence/neo4j_client.py:15-48` — `Neo4jClient(uri,user,password,database)` + `@classmethod from_env()` (reads `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD`/`NEO4J_DATABASE`) + `async def close()`. The worker creates ONE `Neo4jClient.from_env()` for its lifetime and `await neo.close()` on shutdown (mirrors `apollo/api.py:64-79` `get_neo4j_client`/`close_neo4j_client` singleton + close-on-shutdown).

**The async-engine / per-loop registry (WHY the worker must stay on ONE loop):**
- `database/session.py:98-120` — `_get_engine_and_factory_for_current_loop()` keys the engine+sessionmaker registry by `id(asyncio.get_running_loop())`. `_build_engine()` (`:81-95`) already sets `pool_size=10, pool_pre_ping=True, pool_recycle=1800`. `get_async_session()` (`:140-145`) resolves the factory for the CURRENT loop. **Consequence:** `main()` must use a SINGLE `asyncio.run(_loop(...))` so every drain phase resolves the SAME engine. Do NOT bridge through the sync `run_async` daemon (`:70-78`) — that is for sync→async callers and would put the drain on a DIFFERENT loop than the worker's signal handlers.

**Prior-art worker (the canonical loop+entrypoint shape + the EXACT pragma precedent):**
- `teacher_upload_worker.py:1-20` — `def main(): TeacherWeeklyStorage().run_upload_worker_loop()` + `if __name__ == "__main__":  # pragma: no cover - manual entrypoint\n    main()`. This is the ONLY pragma in that file's entrypoint — the `__main__` guard line.
- `knowledge/teacher_weekly.py:348-360` — `run_upload_worker_loop`: `while True:` + `try: process_next_upload_job() except KeyboardInterrupt:  # pragma: no cover - interactive shutdown\n    raise except Exception: log.exception(...)` + `if not processed: time.sleep(self.job_poll_seconds)`. **PRECEDENT (load-bearing for §6):** the `# pragma: no cover` sits ONLY on the `except KeyboardInterrupt:` branch (`:355`) — NOT on `while True:`, NOT on `process_next_upload_job()`, NOT on the `except Exception`/`log.exception`, NOT on `time.sleep`. The loop BODY is fully tested. This sets the bar for what WU-5B3b may legitimately exempt.

**Procfile (add a THIRD process):**
- `Procfile:1-2` — `web: uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}` + `worker: python -m teacher_upload_worker`. The new line: `apollo-janitor: python -m apollo.learner_janitor_worker`.

**Where tests live:**
- `apollo/handlers/tests/` — the existing WU-5B3a-1 drain H1 suite is `apollo/handlers/tests/test_learner_janitor.py`. The worker is a NEW top-level module (`apollo/learner_janitor_worker.py`); its unit tests go in `apollo/tests/test_learner_janitor_worker.py` (create `apollo/tests/__init__.py` if absent — verify; `apollo/conftest.py` already provides the package-level fixtures).

**Signal-handler reality (POSIX/Railway-Linux):**
- `loop.add_signal_handler(signal.SIGTERM, …)` / `signal.SIGINT` is available on the asyncio event loop on Linux (Railway). On Windows the ProactorEventLoop raises `NotImplementedError` for `add_signal_handler` — the registration must be wrapped so local Windows dev / Windows CI does not crash on import/run (fall back to `signal.signal` or skip — see §5). Tests run on Windows here, so the handler-registration path MUST be unit-tested via a fake loop, never by sending a real OS signal.

## 3. Files to create / edit

**CREATE:**
1. `apollo/learner_janitor_worker.py` (NEW, ~110-140 lines) — the worker module. Async-native `main()`/`_loop()` + module constants + flag-read + SIGTERM handler + Neo4jClient lifetime.
2. `apollo/tests/test_learner_janitor_worker.py` (NEW) — the unit-test suite (see §7).
3. `apollo/tests/__init__.py` — **already exists** (verified this pass: `apollo/tests/` is a package with `__init__.py`, `conftest.py`, and sibling unit tests like `test_api_auth.py`). So NO new package file is needed; the new test simply drops into `apollo/tests/`.

**EDIT:**
4. `Procfile` (+1 line) — append `apollo-janitor: python -m apollo.learner_janitor_worker`.
5. `docs/architecture/apollo.md` (owner doc, `owns: apollo/**`) — register the new `learner_janitor_worker.py` module row + the `APOLLO_LEARNER_JANITOR_ENABLED` flag + the worker→drain relationship; set `last_verified: 2026-06-19`.
6. `docs/architecture/_overview.md` (owner doc, `owns: Procfile` + ops entrypoints) — register the `apollo-janitor` ops entrypoint + the scale-to-0-until-flag posture; update the Procfile landmark row; set `last_verified: 2026-06-19`.

**DO NOT TOUCH (frozen / out-of-scope):**
- `apollo/handlers/learner_janitor.py` — FROZEN (WU-5B3a-1 drain). REUSE by import only.
- `apollo/handlers/done.py`, `apollo/handlers/done_inputs.py`, `apollo/handlers/learner_update.py`, `apollo/handlers/done_grading.py` — frozen.
- `knowledge/teacher_weekly.py` — do NOT bolt the async loop into the sync `run_upload_worker_loop`.
- `database/session.py`, `apollo/persistence/neo4j_client.py` — reuse by import only.
- Any migration file — this unit adds NO migration (028 shipped in WU-5B3a-0).

## 4. Public signatures & module constants

All in `apollo/learner_janitor_worker.py`. **No backward-compat surface exists yet** (new module), so signatures are chosen for testability, not compatibility.

```python
# --- module constants (env-overridable, REUSE _int_env from the frozen drain) ---
from apollo.handlers.learner_janitor import (
    MAX_ATTEMPTS,           # REUSE — do NOT redefine the backoff cap
    _int_env,               # REUSE the frozen env-parse helper (avoids a duplicate)
    drain_pending_attempts, # the FROZEN drain
)

_JANITOR_ENABLED_FLAG: str = "APOLLO_LEARNER_JANITOR_ENABLED"   # default OFF
SWEEP_LIMIT: int = _int_env("APOLLO_JANITOR_SWEEP_LIMIT", 1)    # small, sequential — bounds LLM spend
POLL_SECONDS: int = _int_env("APOLLO_JANITOR_POLL_SECONDS", 60) # >= per-row LLM time (2 LLM calls); low-throughput by design

_LOG = logging.getLogger(__name__)


def _janitor_enabled() -> bool:
    """Mirror of done.py:_graph_sim_layer3_enabled — read per-iteration so a
    flag flip is observed without a process restart. Default OFF."""
    return os.environ.get(_JANITOR_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


async def _run_one_iteration(neo, *, stop_event: asyncio.Event) -> None:
    """ONE loop pass — flag-read → (drain | skip) → sleep. Extracted so it is
    unit-testable WITHOUT a process: the loop body lives HERE, fully covered.
    Returns after one flag-gated drain (or skip) + one sleep."""
    ...


async def _loop(neo, *, stop_event: asyncio.Event) -> None:
    """The poll loop. The bare `while not stop_event.is_set():` shell line is the
    ONLY loop line that may be `# pragma: no cover`; the body is _run_one_iteration
    (fully tested). Cooperative cancel: stop_event is checked at the top of each
    pass — a set event exits AFTER the current iteration (never mid-drain, since
    the drain is awaited to completion before the check)."""
    ...


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Register SIGTERM + SIGINT to call stop_event.set() via
    loop.add_signal_handler. Wrapped in try/except NotImplementedError so Windows
    (ProactorEventLoop) and test fakes do not crash; logs a warning on fallback.
    Unit-tested with a fake loop that records the registered (sig, callback)."""
    ...


async def main() -> None:
    """Entrypoint coroutine: build ONE Neo4jClient.from_env(), one asyncio.Event,
    install signal handlers, run _loop, and ALWAYS `await neo.close()` in a finally.
    Stays on ONE asyncio loop so database.session's per-loop engine registry is
    stable."""
    neo = Neo4jClient.from_env()
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    try:
        await _loop(neo, stop_event=stop_event)
    finally:
        await neo.close()


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
```

**Design rationale for the `_run_one_iteration` extraction:** the ADJUDICATION/coverage rule says ONLY the `__main__` entrypoint and the bare `while not stop_event:` shell may be `# pragma: no cover` — the body (flag-read, drain call, sleep, cancel-check, DrainResult log) MUST be exercised. Extracting the body into `_run_one_iteration(neo, *, stop_event)` lets the tests call it directly with a real `asyncio.Event`, asserting flag-ON/OFF behaviour, the drain call args, and the log — with ZERO pragmas on any body line. `_loop` then becomes a trivial `while`-shell calling `_run_one_iteration`, and only its `while` line carries the pragma.

**`_run_one_iteration` body contract (pinned):**
```python
async def _run_one_iteration(neo, *, stop_event):
    if _janitor_enabled():
        result = await drain_pending_attempts(neo, limit=SWEEP_LIMIT, max_attempts=MAX_ATTEMPTS)
        _LOG.info(
            "apollo_janitor_sweep",
            extra={
                "claimed": result.claimed,
                "succeeded": result.succeeded,
                "dead_lettered": result.dead_lettered,
                "retried": result.retried,
                "deferred": result.deferred,
                "backlog_remaining": result.backlog_remaining,
            },
        )
    else:
        _LOG.debug("apollo_janitor_disabled")  # flag OFF → NO drain (early skip)
    # cooperative sleep: still sleeps when disabled so a flag flip is picked up
    # next pass; the sleep is interruptible-enough at POLL_SECONDS granularity.
    await asyncio.sleep(POLL_SECONDS)
```
Note: the worker does NOT pass `user_id` (global drain). `limit=SWEEP_LIMIT` (=1 default). It does NOT gate on LAYER3 — the frozen drain owns that interlock.

## 5. The `_loop()` / `main()` design (behaviour contract)

**Loop shape (cooperative cancel between rows — NEVER mid-drain):**
```python
async def _loop(neo, *, stop_event):
    _LOG.info("apollo_janitor_worker_started", extra={"poll_seconds": POLL_SECONDS, "sweep_limit": SWEEP_LIMIT})
    while not stop_event.is_set():   # pragma: no cover - loop shell (body is _run_one_iteration)
        await _run_one_iteration(neo, stop_event=stop_event)
    _LOG.info("apollo_janitor_worker_stopped")
```
- The `stop_event.is_set()` check is at the TOP of each pass. Because `_run_one_iteration` `await`s `drain_pending_attempts` to completion before returning, a `stop_event.set()` fired DURING a drain takes effect only AFTER the current iteration finishes — exactly the "exit after the current row, never mid-drain" semantics the ADJUDICATION requires. The drain's own per-row claim-lease (WU-5B3a-1) makes a row that is killed between sweeps safe to re-drain.
- The two `_LOG.info` started/stopped lines are reachable and covered by the loop tests (drive `_loop` with a stop_event pre-set or set-after-one-pass).

**Driving `_loop` in tests WITHOUT a process (the testability hinge):**
- Pre-set `stop_event` → `_loop` logs started, the `while` guard is False immediately, logs stopped, returns. Covers the started/stopped lines and proves the shell does not run the body when already stopped.
- A fake `_run_one_iteration` (patched) that calls `stop_event.set()` after N calls → drives the shell N times then exits. Asserts `_run_one_iteration` called exactly N times. (This keeps the `while` shell itself driven by tests even though its single line is pragma'd — belt-and-braces.)
- The BODY behaviours are tested by calling `_run_one_iteration` DIRECTLY (no loop), so the pragma on the `while` line never hides logic.

**SIGTERM/SIGINT handler:**
```python
def _install_signal_handlers(loop, stop_event):
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows ProactorEventLoop / non-main-thread: signals unsupported.
            _LOG.warning("apollo_janitor_signal_handler_unsupported", extra={"signal": sig})
```
- `stop_event.set` (the bound method, no-arg) is the callback — `add_signal_handler` calls it with no args. The handler does cooperative cancel only; it never cancels the in-flight drain task.
- Unit-tested with a FAKE loop object recording `add_signal_handler(sig, cb)` calls — assert both SIGINT+SIGTERM registered and that invoking the recorded callback sets the event. Also test the `NotImplementedError` branch (fake loop whose `add_signal_handler` raises) logs the warning and does NOT propagate. **Never send a real OS signal in a test** (Windows + flakiness).

**`main()` lifetime:**
- ONE `Neo4jClient.from_env()` for the whole process; `await neo.close()` in a `finally` so a crash or a stop still releases the driver. Mirrors `apollo/api.py:74-79`.
- `main()` runs entirely inside the single `asyncio.run(main())` loop (the `__main__` guard), so `database.session`'s `id(loop)`-keyed engine registry resolves ONE engine for every drain phase. **Do NOT create a second loop or use `run_async`.**
- `main()` is unit-testable: patch `Neo4jClient.from_env` → a fake with an `AsyncMock close`, patch `_loop` → an `AsyncMock`, patch `_install_signal_handlers`, then `await main()` and assert: `_loop` awaited once with the fake neo + an `asyncio.Event`; `neo.close()` awaited once; and (separately) that `close()` is still awaited when `_loop` raises (the `finally`).

## 6. Coverage / pragma rules

**The ONLY two lines that may carry `# pragma: no cover`:**
1. The `if __name__ == "__main__":` block's `asyncio.run(main())` (+ the `basicConfig`/guard) — `# pragma: no cover - manual entrypoint` (mirrors `teacher_upload_worker.py:19`).
2. The bare `while not stop_event.is_set():` shell line in `_loop` — `# pragma: no cover - loop shell (body is _run_one_iteration)`.

**Everything else MUST be exercised by asserting unit tests — NO pragma:**
- `_janitor_enabled()` — both True and False return paths.
- `_run_one_iteration` — flag-ON (drain called + DrainResult logged) AND flag-OFF (drain NOT called, debug log, then sleep) AND the `await asyncio.sleep` (patched to assert the call / no real wait).
- `_loop`'s `_LOG.info` started/stopped lines and the body-dispatch.
- `_install_signal_handlers` — the happy registration path, the callback-sets-event behaviour, AND the `NotImplementedError` fallback branch.
- `main()` — the build/run/close happy path AND the `finally`-closes-on-error path.
- the module constants / flag string.

**Precedent enforced (NOT exceeded):** `teacher_weekly.py:355` pragmas ONLY `except KeyboardInterrupt`, not the loop body. WU-5B3b pragmas ONLY the `__main__` entrypoint + the `while` shell line — strictly less than what the prior art's body exposes, and the body is fully tested. Do NOT pragma the flag-read, the drain call, the sleep, the cancel-check, or the DrainResult log.

**`asyncio.sleep` in tests:** patch `apollo.learner_janitor_worker.asyncio.sleep` (or pass a real `0`-delay via a monkeypatched `POLL_SECONDS`) so tests do not actually wait `POLL_SECONDS`. Assert it was awaited (e.g. `AsyncMock` with `assert_awaited_once`). This covers the sleep line without a 60s test.

**No real-infra gate expected.** The drain's real-PG/Neo4j behaviour is already proven in WU-5B3a-1; this unit MOCKS `drain_pending_attempts`. If an integration test is added it must run green-not-skipped with Docker UP — but none is required here.

## 7. TDD test list (write tests FIRST)

File: `apollo/tests/test_learner_janitor_worker.py`. All pure-unit (no DB, no Neo4j, no network). `drain_pending_attempts` is MOCKED throughout (`AsyncMock` returning a canned `DrainResult`). `asyncio.sleep` patched to an `AsyncMock`. Mark `@pytest.mark.unit` (or unmarked — mirror the existing apollo unit tests; do NOT mark `integration` since no infra is touched).

**Shared fixtures/helpers (top of file):**
- `_FAKE_RESULT = DrainResult(claimed=1, succeeded=1, dead_lettered=0, retried=0, deferred=0, backlog_remaining=0)` — imported `DrainResult` from `apollo.handlers.learner_janitor`.
- `_FakeNeo` — a trivial object (or `object()`); the worker never calls methods on `neo` directly (only hands it to the mocked drain).
- `monkeypatch.delenv(_JANITOR_ENABLED_FLAG, raising=False)` / `setenv(..., "1")` to drive the flag. Use `monkeypatch` so env is restored.

### Flag-read tests
1. **`test_janitor_enabled_default_off`** — with the env var unset, `_janitor_enabled()` is `False`. Asserts the default-OFF contract.
2. **`test_janitor_enabled_truthy_values`** — `"1"/"true"/"TRUE"/"yes"` → `True`; `"0"/"false"/""/"no"` → `False`. Mirrors `done.py`'s truthy set exactly.

### `_run_one_iteration` body tests (the coverage core)
3. **`test_iteration_flag_off_skips_drain`** — flag OFF; patch `drain_pending_attempts` (`AsyncMock`) + `asyncio.sleep` (`AsyncMock`). Await `_run_one_iteration(neo, stop_event=Event())`. Assert: `drain_pending_attempts` **NOT** awaited (`assert_not_awaited`); `asyncio.sleep` awaited once with `POLL_SECONDS`. (Proves the flag-OFF early-skip + still-sleeps.)
4. **`test_iteration_flag_on_calls_drain_with_limit_1`** — flag ON; mocked drain returns `_FAKE_RESULT`. Assert: `drain_pending_attempts` awaited **once** with `(neo,)` positional and `limit=SWEEP_LIMIT, max_attempts=MAX_ATTEMPTS` kwargs (assert `limit == 1` via the default), and NO `user_id`; then `asyncio.sleep` awaited once. (Proves flag-ON drains with `limit=1`.)
5. **`test_iteration_logs_drain_result`** — flag ON; use `caplog` at INFO. Assert an `apollo_janitor_sweep` record is emitted carrying the six `DrainResult` fields (`claimed/succeeded/dead_lettered/retried/deferred/backlog_remaining`) from `_FAKE_RESULT`. (Proves the DrainResult is logged.)
6. **`test_iteration_drain_exception_propagates_or_is_handled`** — DECISION-PIN: the drain is awaited un-wrapped, so an exception propagates out of `_run_one_iteration`. Assert that a raising mocked drain raises out of `_run_one_iteration` (the loop's own resilience is tested separately in #9 if a try/except is added). **If the executor chooses to wrap the drain in `try/except Exception: _LOG.exception(...)` inside `_run_one_iteration`** (recommended, mirrors `teacher_weekly.py:357` so one bad sweep does not kill the worker), then instead assert: the exception is swallowed-to-log, `asyncio.sleep` is STILL awaited, and `_run_one_iteration` returns normally. **Pick the wrapped variant** (resilient worker) and test THAT — see §10 risk R3.

### `_loop` shell tests
7. **`test_loop_exits_when_stop_event_preset`** — `stop_event` pre-`set()`; patch `_run_one_iteration` (`AsyncMock`). Await `_loop(neo, stop_event=stop_event)`. Assert `_run_one_iteration` **NOT** awaited (guard False on first check) and the started+stopped INFO logs are present (`caplog`). (Covers the started/stopped lines + the don't-run-when-stopped path.)
8. **`test_loop_runs_body_then_stops`** — patch `_run_one_iteration` with an `AsyncMock` side-effect that calls `stop_event.set()` on its first call. Await `_loop`. Assert `_run_one_iteration` awaited **exactly once** (one pass, then the top-of-loop guard sees the set event and exits). (Proves cooperative exit-after-current-iteration + drives the `while` shell.)

### Cooperative-cancel semantics test
9. **`test_stop_event_set_mid_iteration_finishes_current_drain`** — flag ON; the mocked `drain_pending_attempts` side-effect calls `stop_event.set()` BEFORE returning `_FAKE_RESULT` (simulating SIGTERM arriving mid-drain). Drive via `_loop` with the real `_run_one_iteration`. Assert: the drain completed (returned its result, was awaited once — NOT cancelled mid-call) AND the loop exits after that single iteration (`drain_pending_attempts` awaited exactly once). (Proves "exit AFTER the current row, never mid-drain".)

### Signal-handler tests
10. **`test_install_signal_handlers_registers_sigint_sigterm`** — a `_FakeLoop` recording `add_signal_handler(sig, cb)` calls. Call `_install_signal_handlers(fake_loop, stop_event)`. Assert both `signal.SIGINT` and `signal.SIGTERM` registered; invoke each recorded `cb` and assert `stop_event.is_set()` becomes True. (Proves the handler sets the stop event.)
11. **`test_install_signal_handlers_tolerates_not_implemented`** — `_FakeLoop.add_signal_handler` raises `NotImplementedError` (the Windows/Proactor case). Assert `_install_signal_handlers` does NOT raise and logs `apollo_janitor_signal_handler_unsupported` (`caplog` WARNING). (Covers the fallback branch — required since the suite runs on Windows.)

### `main()` lifetime tests
12. **`test_main_builds_runs_and_closes`** — patch `Neo4jClient.from_env` → returns a fake with `close = AsyncMock()`; patch `_loop` → `AsyncMock`; patch `_install_signal_handlers`. `await main()`. Assert: `from_env` called once; `_install_signal_handlers` called once with a loop + an `asyncio.Event`; `_loop` awaited once with `(fake_neo,)` + `stop_event=` that Event; `fake_neo.close` awaited once.
13. **`test_main_closes_neo_on_loop_error`** — same patches but `_loop` raises `RuntimeError`. Assert `main()` re-raises (or the chosen behaviour) AND `fake_neo.close` is STILL awaited once (the `finally`). (Proves the driver is always released.)

### Procfile guard test (cheap, prevents the wiring regressing)
14. **`test_procfile_declares_apollo_janitor_process`** — read the repo `Procfile`, assert a line `apollo-janitor: python -m apollo.learner_janitor_worker` is present and that the module path is importable (`importlib.util.find_spec("apollo.learner_janitor_worker")` is not None). (Locks the +1 process line + that the module name matches.) Use an absolute path resolved from this test file's location.

**Mocking summary (deterministic, no live API / no infra):**
- `drain_pending_attempts` → `AsyncMock` returning a canned `DrainResult` (or a side-effect that sets the stop event / raises).
- `asyncio.sleep` → `AsyncMock` (no real wait).
- `Neo4jClient.from_env` → fake object with `AsyncMock close`.
- signal registration → `_FakeLoop` recording calls; NEVER a real OS signal.
- env flag → `monkeypatch.setenv/delenv`.

**~14 tests.** Every non-pragma line in the module is hit by at least one asserting test → ≥95% patch coverage with margin.

## 8. Owner-doc updates (same commit)

### `docs/architecture/apollo.md` (owner of `apollo/**`)
- **Frontmatter:** keep `last_verified: 2026-06-19` (already there — re-affirm it for this change).
- **Module map:** add a NEW row (or extend the existing landmark text) for `apollo/learner_janitor_worker.py`:
  > **WU-5B3b** adds `learner_janitor_worker.py` — the dormant `apollo-janitor` Procfile process. An async-native poll loop (`main()` → single `asyncio.run`; `_loop` → `while not stop_event: await _run_one_iteration(...)`) that, when `APOLLO_LEARNER_JANITOR_ENABLED` is ON (default OFF EVERYWHERE), calls the FROZEN `drain_pending_attempts(neo, limit=SWEEP_LIMIT=1, max_attempts=MAX_ATTEMPTS)` once per pass and logs the returned `DrainResult` (`apollo_janitor_sweep`), then `await asyncio.sleep(POLL_SECONDS=60)`; when OFF it skips the drain and just sleeps. It owns ONE `Neo4jClient.from_env()` for its lifetime (closed in a `finally`) and gates ONLY on the janitor flag — the LAYER3 interlock lives inside the frozen drain. SIGTERM/SIGINT register an `asyncio.Event` via `loop.add_signal_handler` (tolerating `NotImplementedError` on Windows) for cooperative cancel BETWEEN drains (never mid-row; a killed-between-sweeps row re-drains via the drain's claim-lease). Adds NO drain logic / SQL / migration — a thin process wrapper around WU-5B3a-1. The opportunistic POST-COMMIT backstop is DEFERRED (not built; see `downstream_followups`).
- Note the new env flag `APOLLO_LEARNER_JANITOR_ENABLED` + the `APOLLO_JANITOR_SWEEP_LIMIT`/`APOLLO_JANITOR_POLL_SECONDS` overrides alongside the existing `APOLLO_JANITOR_*` drain constants.

### `docs/architecture/_overview.md` (owner of `Procfile` + ops entrypoints)
- **Frontmatter:** bump `last_verified: 2026-06-15` → `2026-06-19`. Add `apollo/learner_janitor_worker.py` to the `owns:` list? — **NO**: `apollo/**` is owned by `apollo.md`; `_overview.md` owns `Procfile` only. Leave `owns:` as-is; register the entrypoint as ops surface, not as an owned source file.
- **Module map row** for `Procfile` (`:46`): update to `web: uvicorn server:app` + `worker: python -m teacher_upload_worker` + `apollo-janitor: python -m apollo.learner_janitor_worker` (Railway deploys web+worker; **apollo-janitor is scaled to 0 replicas until `APOLLO_LEARNER_JANITOR_ENABLED` is flipped**).
- **Ops entrypoints / process section** (near `:44` and `:134`): add a sentence registering the `apollo-janitor` process — "a dormant async-native learner-update retry janitor; scaled to 0 while dormant so it costs no replica; flip `APOLLO_LEARNER_JANITOR_ENABLED` (and ensure `APOLLO_GRAPH_SIM_LAYER3_ENABLED` for belief writes) to activate."

**Drift-contract note:** both doc edits land in the SAME commit as the code (per CLAUDE.md drift contract). The prompt's instruction mentions `last_verified=2026-06-16` in one clause and `2026-06-19` elsewhere; the STALE-NOTE in the recon facts resolves this — **use `2026-06-19`** for both docs (today's date and the value already in `apollo.md`).

## 9. Build order (TDD-ordered steps)

Strict RED→GREEN→REFACTOR. Tests are written FIRST and must fail before the module exists.

- [ ] **Step 0 — confirm imports.** `apollo/tests/` already exists as a package (verified — no `__init__.py` to add). Confirm `apollo/handlers/learner_janitor.py` exports `drain_pending_attempts`, `DrainResult`, `MAX_ATTEMPTS`, `_int_env` (it does — §2). No code yet.
- [ ] **Step 1 — RED: write `apollo/tests/test_learner_janitor_worker.py`** with all ~14 tests (§7). They import from `apollo.learner_janitor_worker`, which does not exist → collection/import error (RED). Run: `.venv/Scripts/python.exe -m pytest apollo/tests/test_learner_janitor_worker.py -q` → expect ImportError.
- [ ] **Step 2 — GREEN: write `apollo/learner_janitor_worker.py`** (§4/§5): constants, `_janitor_enabled`, `_run_one_iteration` (wrapped-drain variant per test #6), `_loop`, `_install_signal_handlers`, `main`, the `__main__` guard. Add the two `# pragma: no cover` markers (§6). Run the suite → all green.
- [ ] **Step 3 — EDIT `Procfile`** — append the `apollo-janitor:` line. Test #14 goes green.
- [ ] **Step 4 — REFACTOR** — confirm the module is <200 lines, no mutation (the worker holds only the `stop_event`/`neo` locals; `DrainResult` is frozen), small focused file. Run `ruff`/`black`/`isort` if wired (pre-commit).
- [ ] **Step 5 — owner docs** (§8) — edit `apollo.md` (module row + flag) and `_overview.md` (Procfile row + ops entrypoint + `last_verified` bump). Same commit as code.
- [ ] **Step 6 — full gates** (§12) — `pytest apollo -q` (no regressions), `pytest --cov=apollo --cov-report=xml -q`, then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b3a1-janitor-drain --fail-under=95`.
- [ ] **Step 7 — sanity-import the entrypoint** — `.venv/Scripts/python.exe -c "import apollo.learner_janitor_worker as w; print(w.main, w._JANITOR_ENABLED_FLAG)"` (does NOT run the loop — proves the module imports clean and the flag string is correct). Do NOT run `python -m apollo.learner_janitor_worker` to completion (it would start the real loop / try Neo4j).

## 10. Risks

- **[HIGH] Coverage gaming temptation on the loop shell.** The `while`/`__main__` lines are un-driveable-to-completion, BUT the body is fully testable via the `_run_one_iteration` extraction. Mitigation: the §4 design FORCES the body out of the pragma'd shell; tests #3-#6 + #9 hit every body line. Do NOT collapse `_run_one_iteration` back into `_loop` (that would put the body under the `while` pragma). **Confidence: HIGH this is avoidable; the extraction is the whole mechanism.**
- **[HIGH] Wrong loop / `run_async` bridge breaks the per-loop engine registry.** `database/session.py` keys engines by `id(loop)` (`:98-120`). If `main()` ran the drain on a different loop than its own (e.g. via `run_async`), the drain's `get_async_session()` would build a SECOND engine, defeating pool reuse and risking cross-loop asyncpg errors (the textbook-class failure mode). Mitigation: ONE `asyncio.run(main())`; everything awaited inline; no `run_async`. Pinned in §5. **Confidence: HIGH if the design is followed.**
- **[MEDIUM] Signal handlers on Windows.** `add_signal_handler` raises `NotImplementedError` on the ProactorEventLoop; the test suite runs on Windows. Mitigation: the try/except fallback (§5) + test #11 covering it. The real SIGTERM path runs on Railway-Linux (untested by unit tests, by design — never send a real signal in a test). **Confidence: HIGH.**
- **[MEDIUM] Worker resilience — one bad sweep must not kill the process.** A transient `drain_pending_attempts` raise (e.g. a Neo4j blip) should NOT terminate the worker. Mitigation (R3): wrap the drain call in `_run_one_iteration` in `try/except Exception: _LOG.exception("apollo_janitor_sweep_failed")` then still sleep — mirrors `teacher_weekly.py:357`. Test #6 (wrapped variant) asserts this. Do NOT let `KeyboardInterrupt`/`SystemExit` be swallowed (catch `Exception`, not bare `except`). **Confidence: HIGH.**
- **[MEDIUM] Sleep granularity vs shutdown latency.** With `POLL_SECONDS=60` and the stop-check only at the top of each pass, a SIGTERM that arrives during the `sleep` waits up to ~60s before exit — within Railway's grace only if grace is generous; Railway's default SIGTERM→SIGKILL is ~30s. ACCEPTED for v1 (the drain's claim-lease makes a SIGKILL-interrupted sweep safe to re-drain; the worker is dormant in prod anyway). OPTIONAL hardening (note, not required): race the sleep against the stop_event (`asyncio.wait({sleep_task, stop_task}, return_when=FIRST_COMPLETED)`) so shutdown is prompt. If the executor adds this, it must be unit-tested (the sleep-vs-event race) and stays within the no-new-package rule (stdlib `asyncio`). **Confidence: MEDIUM — accept the simple sleep for v1; document the hardening.**
- **[LOW] POLL_SECONDS too small re-triggers the rate-limit that created the backlog.** `limit=1` + `POLL_SECONDS≥`per-row LLM time keeps the worker low-throughput. Defaults (1, 60s) are conservative. Env-overridable for tuning. **Confidence: HIGH.**
- **[LOW] Dormancy replica cost.** A Procfile entry means Railway will run a process unless scaled to 0. Mitigation: the `_overview.md` ops note pins the "scale to 0 until flag flips" posture; activation is a human deploy step (§13). **Confidence: HIGH.**
- **[LOW] `_int_env` reuse couples the worker to the frozen drain module.** Importing `_int_env`/`MAX_ATTEMPTS`/`drain_pending_attempts`/`DrainResult` from `apollo.handlers.learner_janitor` is intentional (single source of truth) and does NOT edit the frozen file. **Confidence: HIGH.**

**CARRIED heads-up (do NOT touch):** the H2 contention test `#19` from WU-5B3a-1 is a known CI-flakiness watch — it is NOT in this unit's scope; do not modify it.

## 11. Out-of-scope boundaries

**Explicitly NOT in this unit (touching any of these violates the scope contract):**
- **The drain logic.** `apollo/handlers/learner_janitor.py` is FROZEN (WU-5B3a-1). No edits, no re-implementation of claim/lease/backoff/dead-letter/LAYER3-interlock/clear-flag. REUSE by import.
- **The opportunistic POST-COMMIT backstop hook in `apollo/handlers/done.py`.** DEFERRED out of v1 (split-proposal §3 + ADJUDICATION #2 recommended DEFER). This plan does NOT edit `done.py`. It lands as a `downstream_followup` if a future unit decides to ship it under the four POST-COMMIT/own-session/`wait_for`-bounded/separately-controllable constraints.
- **Any migration.** Migration 028 shipped in WU-5B3a-0. This unit adds NO DDL and NEVER applies migrations to any remote DB.
- **Bolting into the sync upload worker.** Do NOT add the async drain into `knowledge/teacher_weekly.py::run_upload_worker_loop` — it is SYNC and `time.sleep` would starve the async drain. The `apollo-janitor` is a SEPARATE process.
- **Railway scaling / replica config.** The plan declares the scale-to-0 posture in the ops doc; actually scaling the Railway process is a HUMAN deploy step (§13), not an executor task.
- **Flipping `APOLLO_LEARNER_JANITOR_ENABLED` or `APOLLO_GRAPH_SIM_LAYER3_ENABLED` anywhere (test/prod env).** Both stay default OFF in code; activation is a human calibration/deploy decision.
- **New packages.** Only stdlib `asyncio`/`signal`/`os`/`logging` + the existing `apollo`/`database` imports. No queue library, no `tenacity`.
- **Touching the H2/H3 WU-5B3a-1 tests** (incl. the flaky contention test #19) or any other frozen file.

## 12. Verification commands (all local)

Run from `ai-ta-backend/` with `.venv/Scripts/python.exe` (py3.12; bare `python` lacks deps).

- [ ] **RED proof:** `.venv/Scripts/python.exe -m pytest apollo/tests/test_learner_janitor_worker.py -q` BEFORE writing the module → expect ImportError (tests written first).
- [ ] **Worker suite green:** `.venv/Scripts/python.exe -m pytest apollo/tests/test_learner_janitor_worker.py -q` → all ~14 pass, none skipped/xfail.
- [ ] **No apollo regressions:** `.venv/Scripts/python.exe -m pytest apollo -q` → green (the frozen drain's H1 suite + everything else unaffected).
- [ ] **Coverage XML:** `.venv/Scripts/python.exe -m pytest --cov=apollo --cov-report=xml -q`
- [ ] **Patch-coverage gate (the binding gate):** `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b3a1-janitor-drain --fail-under=95` (the `diff-cover` console script is on PATH; if the venv lacks it, `.venv/Scripts/python.exe -m diff_cover diff coverage.xml --compare-branch=...`) → ≥95% on changed lines, ONLY the `__main__` + `while`-shell pragmas excluded.
- [ ] **Lint (if pre-commit wired):** `ruff check apollo/learner_janitor_worker.py apollo/tests/test_learner_janitor_worker.py` + `black --check` + `isort --check` → no new warnings. No `print()` (use `logging`).
- [ ] **Entrypoint import sanity (NOT a run):** `.venv/Scripts/python.exe -c "import apollo.learner_janitor_worker as w; print(w._JANITOR_ENABLED_FLAG)"` → prints `APOLLO_LEARNER_JANITOR_ENABLED`. Do NOT execute `python -m apollo.learner_janitor_worker` (starts the real loop).

**STOP — the executor's job ends here.** No remote deploy, no env-flag flip, no Railway scaling.

## 13. Deploy handoff (HUMAN/CI only — never executed by feller agents)

The code ships dormant. To ACTIVATE the janitor in any environment, a human/CI does (in order):
1. Ensure migration 028 (WU-5B3a-0) has been rehearsed on the TEST Supabase project and applied to the target project by the human/CI migration step (NOT by feller). The worker reads/writes the 028 columns via the frozen drain.
2. On Railway (`hoot-ai-ta`), confirm the `apollo-janitor` process appears (from the Procfile) and is scaled to **0 replicas** by default.
3. To dry-run drain WITHOUT belief writes: set `APOLLO_LEARNER_JANITOR_ENABLED=1` on the `apollo-janitor` process and LEAVE `APOLLO_GRAPH_SIM_LAYER3_ENABLED` OFF — the drain re-runs the shadow (supersede-idempotent) and DEFERS belief writes (rows stay pending; watch the `apollo_janitor_layer3_deferred` warnings + the backlog gauge). Scale the process to 1 replica.
4. To activate belief writes: additionally set `APOLLO_GRAPH_SIM_LAYER3_ENABLED=1` (a separate human calibration decision). Watch `apollo_janitor_sweep` logs for `succeeded`/`dead_lettered`/`backlog_remaining` and confirm the backlog drains.
5. Tune `APOLLO_JANITOR_POLL_SECONDS` / `APOLLO_JANITOR_SWEEP_LIMIT` per observed LLM latency if needed.
6. Rehearse on the Railway STAGING backend (→ TEST Supabase) before prod (→ prod Supabase). Promotion to prod follows the `staging → ApolloV3` PR path.

**Sanity queries (human, against the real DB at deploy time):** `SELECT count(*) FROM apollo_problem_attempts WHERE learner_update_pending AND NOT learner_update_failed_permanently;` (backlog) and the oldest-pending `created_at` (backlog age) before vs after enabling.

---

## Open decisions inherited (already adjudicated — recorded, do NOT re-litigate)
- Opportunistic backstop: **DEFERRED** (ADJUDICATION #2). Not built here.
- SWEEP_LIMIT=1 / POLL_SECONDS=60 / increment-at-claim+lease: inherited from WU-5B3a-1 + ADJUDICATION #5/#7. The worker reuses `MAX_ATTEMPTS`; it adds only `SWEEP_LIMIT`/`POLL_SECONDS` (env-overridable). If the orchestrator wants different worker defaults, change the two `_int_env` defaults — they are the only new tunables.
