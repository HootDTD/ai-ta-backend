# Plan: WU-3B2f — Claim/lease SKIP-LOCKED queue-drain + metered LLM client

**Goal:** Add the `apollo_provisioning_jobs` SKIP-LOCKED claim/lease drain and a per-call metered LLM client that accumulates token/cost onto the `apollo_ingest_run` row and raises `CostBudgetExceeded` at a per-document ceiling.
**Architecture:** `[indexing-complete enqueue (3B2g)] → [apollo_provisioning_jobs row] → [claim_provisioning_job FOR UPDATE SKIP LOCKED → lease] → [orchestrator (3B2g) runs stages via MeteredChat] → [usage accumulates on apollo_ingest_runs] → [complete_job | fail_job(dead-letter) | release_job]`
**Tech stack:** FastAPI + SQLAlchemy async + asyncpg / Postgres (pgvector:pg16 via Testcontainers) / OpenAI SDK (`gpt-4o` / `gpt-4o-mini`). No new packages (decision #8).

---
provides:
  - apollo/provisioning/queue.py — `claim_provisioning_job` / `complete_job` / `fail_job` / `release_job` + `ClaimedJob`
  - apollo/provisioning/metered_chat.py — `MeteredChat` + `CostBudgetExceeded` + `record_usage`
  - apollo/provisioning/cost_constants.py — `PER_DOCUMENT_TOKEN_CEILING`, `MAX_ATTEMPTS`, model→price table, `cost_usd_for`
consumes:
  - apollo.persistence.models (`ProvisioningJob`, `IngestRun`, `IngestError`) — migration 030 (WU-3B2a, already landed)
  - apollo.agent._llm (cheap_chat/main_chat are NOT mutated; the OpenAI client is re-invoked for usage)
  - the existing `chat_fn`-shaped seams consumed by apollo/provisioning/scrape.py + solution.py
depends_on:
  - WU-3B2a (migration 030 + ORM) — DONE; tables/columns verified present
  - WU-3B2e (compare branch feat/apollo-kg-wu3b2e-solution-pairing) — for diff-cover base only
  - NOT depended on by this unit: the orchestrator/worker shell/trigger (WU-3B2g) CONSUMES this unit's surface
---

## Overview

WU-3B2f delivers the **concurrency + cost spine** of the §8B materials→Apollo auto-provisioning pipeline. It is two cohesive concerns in one M-sized unit:

1. **`queue.py` — the SKIP-LOCKED claim/lease drain** over `apollo_provisioning_jobs` (migration 030, already landed). It mirrors the proven `_claim_upload_job_async` pattern in `knowledge/teacher_weekly.py:881-915`: a short `SELECT … FOR UPDATE SKIP LOCKED` transaction that flips `state='running'`, sets `lease_owner`/`lease_expires_at`, bumps `attempt_count`, and commits. Plus `complete_job` (→`completed`), `fail_job` (dead-letter to `failed` when `attempt_count >= MAX_ATTEMPTS`, else back to `pending` for retry), and `release_job` (clear the lease, return to `pending`). This unit does NOT define the trigger/enqueue (WU-3B2g), the worker-loop shell (WU-3B2g), or any stage logic.

2. **`metered_chat.py` — the per-call metered LLM client** that is the **only token signal** in the whole pipeline. `apollo/agent/_llm.py:35-95` reads `response.usage` solely to emit a log line and then DISCARDS it (returns only `str`), so there is no programmatic token count. `MeteredChat` re-invokes the OpenAI client itself, captures `response.usage.prompt_tokens`/`completion_tokens`, accumulates `llm_calls`/`llm_tokens_in`/`llm_tokens_out`/`llm_cost_usd` onto the passed `apollo_ingest_runs` row, and after each call compares the running token total against `PER_DOCUMENT_TOKEN_CEILING`; on breach it raises `CostBudgetExceeded` (the abort signal the orchestrator turns into an `apollo_ingest_errors` row + a failed run).

3. **`cost_constants.py`** — `PER_DOCUMENT_TOKEN_CEILING = 2_000_000` (a runaway circuit-breaker, NOT a tight budget; the real cost control is `APOLLO_AUTOPROVISION_ENABLED` defaulting OFF), `MAX_ATTEMPTS = 3`, and a model→price table (`gpt-4o` ≈ $2.50/$10 per 1M in/out, `gpt-4o-mini` ≈ $0.15/$0.60 — adjudication #7) with a pure `cost_usd_for(model, tokens_in, tokens_out)` helper. Pure config, env-overridable defaults, no logic beyond arithmetic (mirrors `dedup_constants.py`).

**The load-bearing deliverable is real-PG concurrency:** two genuinely independent committed connections must claim NON-OVERLAPPING jobs under contention. That is only observable with two real pooled connections (not the savepoint `db_session`), so the queue tests live under `tests/database/` and run on the session pgvector Testcontainer, mirroring `tests/database/test_learner_janitor_contention.py` exactly. The metered client is unit-tested in `apollo/provisioning/tests/` with a fake `response.usage` object — no network.

## Prior art in repo

| Pattern | Where | What this unit copies |
|---|---|---|
| SKIP-LOCKED claim/lease drain (the canonical one) | `knowledge/teacher_weekly.py:881-935` `_claim_upload_job_async` | `.with_for_update(skip_locked=True)`, `state.in_((QUEUED, PROCESSING))` + `lease_expires_at IS NULL OR < now` predicate, `order_by(created_at.asc(), id.asc())`, set lease + `attempt_count += 1`, COMMIT, return a frozen claim DTO (`ClaimedUploadJob` → our `ClaimedJob`). |
| Apollo-side SKIP-LOCKED claim | `apollo/handlers/learner_janitor.py:150-191` `_claim_due` | Short claim txn opening its OWN `get_async_session()`, bump attempts + write lease, commit immediately (Phase-A pattern) — confirms the apollo convention of claim-then-commit before any work. |
| Real-PG two-claimer concurrency test | `tests/database/test_learner_janitor_contention.py` (entire file) | The committed-connection harness: `_create_db`/`_apply_chain`/`_plain_dsn`, a session-scoped migrated DSN, a `committed_engine` fixture giving a pooled `async_sessionmaker` + a patched `get_async_session`, `asyncio.gather` of two drains, assert `{r1, r2} == {1, 0}`, plus a fresh independent asyncpg connection to prove visibility. |
| 030 migration chain selector | `tests/database/test_apollo_autoprovisioning_migration.py:54-97` | `_STUB_DDL` (auth.users + aita_search_spaces), the content-scoped `_TOUCHES_TARGETS` regex + `_EXCLUDE_FROM_CHAIN`, applying 030 LAST — reused verbatim so the queue tests get `apollo_provisioning_jobs`/`apollo_ingest_runs`/`apollo_ingest_errors`. |
| Pure constants sibling | `apollo/provisioning/dedup_constants.py` | `os.getenv(... , default)`-backed module-level constants, committed defaults pinned by tests, no imports beyond `os`. |
| Mocked-LLM unit test (no network) | `apollo/provisioning/tests/test_scrape.py`, `test_solution.py` | Inject a deterministic stub for the LLM callable; `apollo/conftest.py` re-exports `_pg_url`/`db_session`. Our metered test instead patches the OpenAI client class and feeds a fake `usage`. |
| Existing `chat_fn` seams the metered client must satisfy | `apollo/provisioning/scrape.py:141` (`chat_fn(chunk.content)`) and `apollo/provisioning/solution.py:218-244` (`chat_fn(purpose=, messages=, response_format=, temperature=)`) | `MeteredChat` must expose BOTH call conventions — a positional-string form for scrape and a cheap_chat-kwargs form for solution/pairing — each returning `str` so it is drop-in for the injected `chat_fn`. |

**This is NOT the first pipeline of its kind** — the SKIP-LOCKED + lease + dead-letter pattern is established twice (teacher uploads, learner janitor). We template on `_claim_upload_job_async` (the contract pins it) rather than inventing a scheme.

## Structural prep (from neighborhood scan)

Change path = three NEW files + their tests. Neighborhood scan of the artifacts in/adjacent to the change path:

- **`apollo/provisioning/` package** — 10 existing modules, each small and single-responsibility (scrape, solution, dedup, pairing_gate, promotion_lint, problem_hash, tag_mint, tag_mint_persist, + 2 constants). The three new files keep the one-module-one-concern shape. No god-module. CLEAN.
- **`apollo/provisioning/__init__.py`** — flat re-export hub, currently ~30 imports / 80 lines. It is a barrel, so import count is expected (it is not a CBO hotspot in the coupling sense — it has no logic). We will add the three new public names; staying a pure re-export keeps it under the file-size budget. CLEAN (no refactor needed).
- **`apollo/agent/_llm.py`** — 99 lines, 2 public functions. We do NOT touch it (contract: must not mutate). The metered client re-invokes the client rather than threading usage back through `_llm`, deliberately avoiding a change that would couple every existing `_llm` caller. CLEAN.
- **Retry/lease sprawl check** — there are now THREE SKIP-LOCKED claim sites (`teacher_weekly`, `learner_janitor`, and this new `queue.py`). They are deliberately NOT unified into one shared helper: each operates on a different table with a different lease column set and a different DTO, and the contract pins this one to mirror `_claim_upload_job_async`. A shared abstraction would be premature (the three lease shapes diverge on `attempt_count` semantics and terminal states). Documented as a known, accepted duplication — NOT debt to pay down in this unit.

**Conclusion: neighborhood is clean — no structural prep steps.** Verify the package stays a thin barrel: `python -c "import apollo.provisioning"` imports without side effects.

## Pipeline shape diagram

```
[indexing-complete enqueue — WU-3B2g, NOT this unit]
   owner: knowledge/teacher_weekly.py session + apollo/provisioning/enqueue.py
        │  writes one apollo_provisioning_jobs row (state='pending')
        ▼
[apollo_provisioning_jobs]  ── migration 030 table; partial-unique-index = one OPEN job/doc (DDL, 3B2a)
        │
        ▼
[claim_provisioning_job(session, lease_owner, lease_seconds)]   ◄── THIS UNIT (queue.py)
   owner: apollo/provisioning/queue.py
   primitive: SELECT … WHERE state='pending' OR (state='running' AND lease_expires_at < now())
              ORDER BY created_at, id  FOR UPDATE SKIP LOCKED  LIMIT 1
   on claim: state='running', lease_owner=…, lease_expires_at=now()+lease_seconds, attempt_count+=1, COMMIT
   retry behavior: lease expiry makes a stuck 'running' row re-claimable by another worker
   failure mode: two workers contend → SKIP LOCKED guarantees disjoint claims; if a worker dies
                 mid-work the lease expires and the row is re-claimed (idempotent attempt_count bump)
        │
        ▼
[orchestrator runs the 6 stages — WU-3B2g, NOT this unit]
   calls MeteredChat-backed chat_fn into scrape/solution/pairing/tag-mint
        │
        ▼
[MeteredChat.cheap(...) / .main(...)]   ◄── THIS UNIT (metered_chat.py)
   owner: apollo/provisioning/metered_chat.py
   primitive: re-invokes openai.OpenAI().chat.completions.create, captures response.usage
   on each call: ingest_run.llm_calls += 1; tokens_in/out += usage; llm_cost_usd += cost_usd_for(model,…)
   retry behavior: NONE here — a transient OpenAI error propagates to the orchestrator/queue (fail_job)
   failure mode: running token total > PER_DOCUMENT_TOKEN_CEILING → raise CostBudgetExceeded (abort)
        │
        ├── success ──► [complete_job(session, job)]  state='completed', lease cleared, COMMIT
        │                  owner: queue.py
        │
        ├── CostBudgetExceeded / terminal error ──► orchestrator writes apollo_ingest_errors row +
        │       ingest_run.status='failed'  (the WRITE is 3B2g) then calls [fail_job(session, job, error)]
        │       owner: queue.py — attempt_count>=MAX_ATTEMPTS → state='failed' (dead-letter, no further claim);
        │       else state='pending' (retry), lease cleared
        │
        └── cooperative release ──► [release_job(session, job)]  state='pending', lease cleared (no attempt change)
                owner: queue.py
```

Per-box ownership, retry, and failure modes are stated inline above. The **trigger/enqueue box and the orchestrator/observability-write box are explicitly WU-3B2g**, not this unit — this unit owns only the two ◄── boxes.

## Idempotency

The queue functions must be safe to re-run on the same input because SKIP-LOCKED + lease expiry deliberately re-deliver.

- **Idempotency key (claim):** the `apollo_provisioning_jobs.id` row that the `FOR UPDATE SKIP LOCKED` clause selects + LOCKS. Two concurrent `claim_provisioning_job` calls CANNOT both lock the same row — Postgres SKIP-LOCKED skips an already-locked row, so the second claimer either grabs a different `pending`/expired row or returns `None`. The claim is the atomic unit; `attempt_count` is the replay counter.
- **Idempotency key (cost accumulation):** the metered client is NOT idempotent on its own (each call legitimately ADDS usage). Re-running an entire job after a crash re-burns tokens — that is acceptable and is bounded by `PER_DOCUMENT_TOKEN_CEILING` per claim-attempt. The cost-accumulation idempotency lives ONE level up: the orchestrator's intra-job `(document_id, chunk_content_hash)` ON CONFLICT scrape upsert (WU-3B2d/3B2g) makes a re-run skip already-scraped chunks, so a replay does NOT re-embed unchanged content. **This unit does NOT own that guard — note it as the boundary.** What this unit guarantees: `record_usage` ADDS deltas with `+=` (never overwrites), and a fresh claim starts from whatever `ingest_run` the orchestrator hands it (a fresh run row per attempt is the orchestrator's call).
- **Duplicate handling (job level):** the partial-unique-index `apollo_provisioning_jobs_open_uniq` (migration 030, owned by 3B2a) collapses a second OPEN job for the same `document_id` at INSERT time (that INSERT is 3B2g's enqueue). `claim_provisioning_job` therefore never sees two open jobs for one document. `complete_job`/`fail_job(→failed)` move the row OUT of the open set so a later re-upload can enqueue afresh.
- **Partial-progress recovery:** if a worker crashes between claim and completion, the row is left `state='running'` with `lease_expires_at` in the past. The predicate `state='running' AND lease_expires_at < now()` makes it re-claimable; `attempt_count` is bumped again, and when it reaches `MAX_ATTEMPTS` the next `fail_job` dead-letters it to `failed` so a poison job cannot loop forever. **MUTATION-PROVE:** dropping `skip_locked=True` makes two workers grab the SAME row → the non-overlap test REDs.
- **Why row id, not a content hash, here:** the contract pins the lease shape to mirror `_claim_upload_job_async`, whose unit of work IS a queue row; content-hash idempotency is enforced upstream at enqueue (the `content_hash` short-circuit + partial-unique-index, 3B2g) and intra-job (scrape ON CONFLICT, 3B2d). Re-deriving a content hash inside the claim would duplicate that guard and break the proven pattern.

## Model & cost declaration

Models are pinned by adjudication #7 routing (scrape/judge→cheap, generate→main) and the project default models. **No deviation from the repo defaults** (`apollo/agent/_llm.py`: `APOLLO_CHEAP_MODEL` default `gpt-4o-mini`, `MAIN_MODEL` default `gpt-4o`). This unit introduces NO new model.

| Call (routed by orchestrator) | Model | Input size est. | Output size est. | Unit cost (per 1M) | Volume est. | Notes |
|---|---|---|---|---|---|---|
| Scrape (stage 1, per chunk) | gpt-4o-mini (cheap) | ~800 tok/chunk × ~40 chunks | ~300 tok | $0.15 in / $0.60 out | 1 doc | metered → ingest_run |
| Find-or-generate (stage 2) | gpt-4o (main) | ~2k tok | ~600 tok | $2.50 in / $10 out | per question | metered → ingest_run |
| Pairing gate (stage 3, judge) | gpt-4o-mini (cheap) | ~1.5k tok | ~150 tok | $0.15 in / $0.60 out | per pair | metered → ingest_run |
| Tag/mint (stage 4) | gpt-4o-mini (cheap) | ~1k tok | ~400 tok | $0.15 in / $0.60 out | per concept | metered → ingest_run |
| Dedup judge (stage 5, tiebreaker) | gpt-4o-mini (cheap) | ~600 tok | ~50 tok | $0.15 in / $0.60 out | rare | metered → ingest_run |

**Per-document circuit breaker:** `PER_DOCUMENT_TOKEN_CEILING = 2_000_000` cumulative (in+out) tokens. This is a runaway guard, NOT a tight budget — a large chapter scrapes generously below it; the call that pushes the cumulative over the line raises `CostBudgetExceeded`. **Cost math is pinned by tests** (`cost_usd_for(gpt-4o, 1_000_000, 1_000_000) == Decimal("12.50")`).

**Monthly projected cost:** Effectively **$0/month in v1** — the entire subsystem is gated behind `APOLLO_AUTOPROVISION_ENABLED` (default OFF everywhere incl. prod/staging) and the §6.7 shadow gate. When enabled for a pilot course (~50 documents/month, ~40 chunks each), a worst-case all-stages run is bounded by the $2M-token ceiling per doc but realistically ≈ $0.05–0.15/document → **≈ $3–8/month per active course**. The metered aggregate per document is committed to `apollo_ingest_runs.llm_cost_usd` as the audit trail.

**Budget ceiling from CLAUDE.md:** No hard dollar ceiling is declared in CLAUDE.md; the binding control is the flag-OFF default + the per-document token circuit breaker. The $2M token ceiling is the explicit safety bound (adjudication #7). No model deviates from the `gpt-4o`/`gpt-4o-mini` defaults.

## Failure paths

For the **OpenAI call** inside `MeteredChat`:

1. **Retry policy:** NONE inside this unit. A transient OpenAI error (rate limit, 5xx, timeout) propagates out of `MeteredChat` to the orchestrator, which fails the job; the queue's `fail_job` then either retries the WHOLE job (back to `pending`) or dead-letters it (`attempt_count >= MAX_ATTEMPTS`). Per-call retry/backoff is deliberately NOT added here (it belongs to the orchestrator's stage error handling in 3B2g, and double-retry layers would inflate `attempt_count` semantics). This unit's contribution to resilience is the job-level attempt counter + dead-letter, not call-level backoff.
2. **Fallback after exhausting retries:** `fail_job(session, job, error=...)` → when `attempt_count >= MAX_ATTEMPTS` (default 3) the job moves to terminal `state='failed'` with `last_error` set; no further claim is possible (the predicate only matches `pending`/expired-`running`). This is the dead-letter.
3. **DLQ / error table:** two surfaces. (a) Terminal queue state: `apollo_provisioning_jobs.state='failed'` + `last_error` — human-inspectable, joinable by `search_space_id`/`document_id`. (b) Per-stage diagnostic: `apollo_ingest_errors` rows (stage + error_class + context) — WRITTEN BY 3B2g's orchestrator on a survived stage error and specifically on `CostBudgetExceeded`. **This unit OWNS the `fail_job` dead-letter transition; the `apollo_ingest_errors` row WRITE on cost-abort is exercised by this unit's real-PG cost-abort test (behavior #4) but the production write site is the orchestrator (3B2g).** The cost-abort test asserts BOTH: an `apollo_ingest_errors` row exists AND `apollo_ingest_runs.status='failed'`.
4. **Observability / alert threshold:** `MeteredChat` keeps the existing `_llm` structured-log convention — one `llm_call` log line per call (purpose/model/tokens_in/tokens_out) plus a new `provisioning_cost_abort` WARNING when `CostBudgetExceeded` raises (carrying `document_id`, `ingest_run_id`, cumulative tokens, ceiling). The `claim`/`fail`/`complete` functions log one structured line each (`event=provisioning_claim|complete|fail`, carrying `job_id`, `state`, `attempt_count`). Alert threshold (for 3B2g/ops, noted here): any `apollo_provisioning_jobs` row in `state='failed'`, or `apollo_ingest_runs` where `llm_tokens_in+llm_tokens_out` approaches the ceiling, is an investigate signal. No tokens or PII are logged — only counts, ids, model names.

## Security check

- **API key source:** `MeteredChat` constructs the OpenAI client via `openai.OpenAI()` exactly as `_llm.py:27` and `apollo_llm.py:154` do — the key is read from `OPENAI_API_KEY` in the environment by the SDK. The metered client NEVER takes a key as an argument, NEVER reads it from a DB row, NEVER logs it.
- **No secrets in code/DB/logs:** the model→price table and the token ceiling are non-secret config constants. The structured logs emit purpose/model/token-counts/ids only — no prompt content, no completion content, no key.
- **No PII in metered inputs:** `MeteredChat` is a transport wrapper — it does not decide prompt content (the orchestrator/stages do). It logs token COUNTS, never the message bodies, so no course/student text is logged. No retention concern is introduced by this unit (the prompts are transient; only aggregate counts persist to `apollo_ingest_runs`).
- **No client-reachable surface:** all three modules are server-side worker code under `apollo/provisioning/`; nothing here is import-reachable from a FastAPI route or a UI. The service-role/DB session is the same `get_async_session()` the rest of the backend uses.
- **RLS awareness:** `apollo_provisioning_jobs`/`apollo_ingest_runs`/`apollo_ingest_errors` carry the migration-030 RLS stopgap (ENABLE ROW LEVEL SECURITY, no policies → default-deny to PostgREST). This unit reaches them only through the trusted async SQLAlchemy session (bypasses PostgREST), which is correct for worker code. No new policy is needed and none is added (out of scope).

No security rule is violated. PASS.

## Files to change

Scope is restricted to exactly the six files in the work-unit contract (new files in the same packages):

| File | New/Edit | Purpose |
|---|---|---|
| `apollo/provisioning/cost_constants.py` | NEW | `PER_DOCUMENT_TOKEN_CEILING`, `MAX_ATTEMPTS`, model→price table, `cost_usd_for`. Pure config. |
| `apollo/provisioning/metered_chat.py` | NEW | `MeteredChat`, `CostBudgetExceeded`, usage capture + accumulate + ceiling check. |
| `apollo/provisioning/queue.py` | NEW | `ClaimedJob`, `claim_provisioning_job`, `complete_job`, `fail_job`, `release_job`. |
| `apollo/provisioning/tests/test_cost_constants.py` | NEW | cost-math + ceiling/constant pins. |
| `apollo/provisioning/tests/test_metered_chat.py` | NEW | fake-`usage` accumulation + ceiling raise (unit, no network, no DB). |
| `tests/database/test_apollo_provisioning_queue.py` | NEW | real-PG concurrency: non-overlap, lease re-claim, dead-letter, cost-abort. |
| `apollo/provisioning/__init__.py` | EDIT | re-export the new public names (queue + metered + constants). |
| `docs/architecture/apollo.md` | EDIT | register the 3 modules under the provisioning row; bump `last_verified` to 2026-06-20. |

**Not touched (asserted):** `apollo/agent/_llm.py` (must not mutate — contract), `knowledge/teacher_weekly.py`, any migration SQL, the ORM (`models.py` already has the classes), the orchestrator/worker/enqueue (3B2g).

## Public signatures

`apollo/provisioning/cost_constants.py`:
```python
from decimal import Decimal

PER_DOCUMENT_TOKEN_CEILING: int = int(os.getenv("APOLLO_PROVISION_TOKEN_CEILING", "2000000"))
MAX_ATTEMPTS: int = int(os.getenv("APOLLO_PROVISION_MAX_ATTEMPTS", "3"))

# model -> (usd_per_1M_input, usd_per_1M_output). Keys are the resolved model strings.
MODEL_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o":      (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
}

def cost_usd_for(model: str, *, tokens_in: int, tokens_out: int) -> Decimal:
    """Decimal USD for a call. Unknown model -> Decimal('0') (counts still accrue;
    cost is best-effort, never raises) — pinned by test."""
```

`apollo/provisioning/metered_chat.py`:
```python
class CostBudgetExceeded(Exception):
    def __init__(self, *, tokens: int, ceiling: int, document_id: int | None = None) -> None: ...

@dataclass
class _Usage:               # internal duck-type accepted from response.usage
    prompt_tokens: int
    completion_tokens: int

class MeteredChat:
    """Wraps the OpenAI client to capture response.usage and accumulate token/cost
    onto a passed apollo_ingest_runs row, raising CostBudgetExceeded at the
    per-document ceiling. _llm.py is NOT used (it discards usage) — this re-invokes
    the client. The ingest_run row is MUTATED IN PLACE via SQLAlchemy attribute
    assignment (the row is the durable aggregate; this is the intended ORM write,
    not a value-object mutation — flushed/committed by the orchestrator's session)."""

    def __init__(self, *, ingest_run, client: "OpenAI | None" = None,
                 ceiling: int = PER_DOCUMENT_TOKEN_CEILING,
                 document_id: int | None = None) -> None: ...

    def cheap(self, *, purpose: str, messages: list[dict], 
              response_format: dict | None = None, temperature: float = 0.0,
              model: str | None = None) -> str:
        """cheap-tier (gpt-4o-mini default). cheap_chat-shaped kwargs (drop-in for
        the solution/pairing chat_fn). Captures usage, accumulates, checks ceiling."""

    def main(self, *, purpose: str, messages: list[dict],
             response_format: dict | None = None, temperature: float = 0.0,
             model: str | None = None) -> str:
        """main-tier (gpt-4o default). Same metering."""

    def scrape_chat_fn(self, system_prompt: str) -> Callable[[str], str]:
        """Adapter returning a positional-string chat_fn (scrape.py:141 shape):
        chat_fn(chunk_content) -> str, routed cheap."""

    def record_usage(self, *, model: str, usage) -> None:
        """Pure accumulation step (called internally; exposed for unit test):
        ingest_run.llm_calls += 1; tokens_in/out += usage; llm_cost_usd += cost_usd_for(...);
        then if tokens_in+tokens_out cumulative > ceiling: raise CostBudgetExceeded."""
```
Backward-compat note: `cheap`/`main` accept the SAME keyword shape as `_llm.cheap_chat`/`main_chat` so a stage written against `chat_fn` can be handed `metered.cheap`/`metered.main` unchanged. `scrape_chat_fn` covers the one positional-string seam. No existing signature changes.

`apollo/provisioning/queue.py`:
```python
@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    search_space_id: int
    document_id: int
    ingest_run_id: int | None
    attempt_count: int          # value AFTER the claim bump

async def claim_provisioning_job(
    session: "AsyncSession", *, lease_owner: str, lease_seconds: int,
) -> ClaimedJob | None:
    """SELECT … WHERE state='pending' OR (state='running' AND lease_expires_at < now())
    ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1. On a row: state='running',
    lease_owner, lease_expires_at=now()+lease_seconds, attempt_count+=1, updated_at,
    COMMIT, return ClaimedJob. None when nothing claimable. Caller owns the session
    (mirrors learner_janitor's claim-then-commit; the session is committed here)."""

async def complete_job(session, *, job_id: int) -> None:
    """state='completed', lease cleared, updated_at, COMMIT."""

async def fail_job(session, *, job_id: int, error: str) -> str:
    """attempt_count>=MAX_ATTEMPTS -> state='failed' (dead-letter); else state='pending'
    (retry). Always: lease cleared, last_error=error[:N], updated_at, COMMIT. Returns
    the resulting state ('failed'|'pending')."""

async def release_job(session, *, job_id: int) -> None:
    """state='pending', lease cleared, updated_at, COMMIT (no attempt_count change —
    cooperative release, e.g. shutdown)."""
```
Design decisions pinned by the contract/recon: `now()` is computed in Python (`datetime.now(UTC)`) to match `_claim_upload_job_async` and keep the lease arithmetic test-controllable; the predicate uses `state='pending'` (NOT the upload code's `QUEUED/PROCESSING` vocabulary — the provisioning `state` vocabulary is `pending/running/completed/failed`); `order_by(created_at.asc(), id.asc())` for FIFO fairness; `.with_for_update(skip_locked=True)` is the load-bearing clause.

## Step-by-step changes (TDD-ordered)

RED first for every behavior. Mock the OpenAI client deterministically; never hit the network. Real-PG tests run GREEN-not-skipped on the pgvector:pg16 Testcontainer.

- [ ] **Step 1 (RED) — cost constants + math tests.**
  - File: `apollo/provisioning/tests/test_cost_constants.py`
  - Change: write tests pinning `PER_DOCUMENT_TOKEN_CEILING == 2_000_000`, `MAX_ATTEMPTS == 3`, `MODEL_PRICES` for both models, and `cost_usd_for` arithmetic incl. the unknown-model→`Decimal('0')` branch and an env-override case.
  - Verify: `pytest apollo/provisioning/tests/test_cost_constants.py -q` → fails (module absent).

- [ ] **Step 2 (GREEN) — `cost_constants.py`.**
  - File: `apollo/provisioning/cost_constants.py`
  - Change: implement the constants + `cost_usd_for` per the signature above; `os.getenv` defaults mirror `dedup_constants.py`; no imports beyond `os`/`decimal`.
  - Verify: `pytest apollo/provisioning/tests/test_cost_constants.py -q` → green.

- [ ] **Step 3 (RED) — metered client unit tests (fake usage, no network/DB).**
  - File: `apollo/provisioning/tests/test_metered_chat.py`
  - Change: build a `_FakeIngestRun` (plain object with the five aggregate attrs initialized to 0/Decimal('0')) and a `_FakeClient` whose `chat.completions.create` returns a fake response carrying `usage=_Usage(prompt_tokens=…, completion_tokens=…)` and `choices[0].message.content`. Assert accumulation across calls, `cost_usd_for` wiring, the cheap/main model routing, the `scrape_chat_fn` positional adapter, and the ceiling raise at the boundary.
  - Verify: fails (module absent).

- [ ] **Step 4 (GREEN) — `metered_chat.py`.**
  - File: `apollo/provisioning/metered_chat.py`
  - Change: implement `MeteredChat` re-invoking `client.chat.completions.create` (client injectable; defaults to `openai.OpenAI()`), resolving model via the SAME env precedence as `_llm` (`APOLLO_CHEAP_MODEL`/`MAIN_MODEL` + explicit arg), `record_usage` doing `+=` accumulation then the cumulative ceiling check raising `CostBudgetExceeded`, the `_log_call`-style structured log + a `provisioning_cost_abort` WARNING. Does NOT import/mutate `_llm`.
  - Verify: `pytest apollo/provisioning/tests/test_metered_chat.py -q` → green.

- [ ] **Step 5 (RED) — real-PG queue concurrency tests.**
  - File: `tests/database/test_apollo_provisioning_queue.py`
  - Change: copy the `test_learner_janitor_contention.py` committed-connection harness (`_STUB_DDL`, `_plain_dsn`, `_create_db`, `_apply_chain`) but reuse the **030 chain selector** from `test_apollo_autoprovisioning_migration.py` (so `apollo_provisioning_jobs`/`apollo_ingest_runs`/`apollo_ingest_errors` exist) + apply 030 last; a session-scoped migrated DSN + a `committed_engine` fixture (pooled `async_sessionmaker`, truncates the four tables after each test). Seed committed `pending` jobs via independent asyncpg. Write the four load-bearing behaviors + the mutation-proof. These RED because `queue.py` is absent.
  - Verify: `pytest tests/database/test_apollo_provisioning_queue.py -q` → fails (import error / red), NOT skipped with Docker up.

- [ ] **Step 6 (GREEN) — `queue.py`.**
  - File: `apollo/provisioning/queue.py`
  - Change: implement `ClaimedJob` + the four functions per the signature, mirroring `_claim_upload_job_async` (SKIP-LOCKED predicate, lease set, `attempt_count += 1`, commit), with structured logs.
  - Verify: `pytest tests/database/test_apollo_provisioning_queue.py -q` → green-not-skipped.

- [ ] **Step 7 — barrel export.**
  - File: `apollo/provisioning/__init__.py`
  - Change: add `from apollo.provisioning.queue import ClaimedJob, claim_provisioning_job, complete_job, fail_job, release_job`; `from apollo.provisioning.metered_chat import MeteredChat, CostBudgetExceeded`; `from apollo.provisioning.cost_constants import PER_DOCUMENT_TOKEN_CEILING, MAX_ATTEMPTS, MODEL_PRICES, cost_usd_for`; extend `__all__`.
  - Verify: `python -c "import apollo.provisioning"` clean; `pytest apollo/provisioning -q` no regressions.

- [ ] **Step 8 — owner doc.**
  - File: `docs/architecture/apollo.md`
  - Change: add the 3 modules + their public surface to the `apollo/provisioning/` row (per the format already used for 3B2c/d/e); set `last_verified: 2026-06-20`.
  - Verify: doc lists `queue.py`/`metered_chat.py`/`cost_constants.py`; `last_verified` bumped.

- [ ] **Step 9 — full gate.**
  - Verify: `pytest tests/database -q` (real-PG green-not-skipped) + `pytest apollo -q` (no regressions) + `pytest --cov=. --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2e-solution-pairing --fail-under=95`.

## Test list

### `apollo/provisioning/tests/test_cost_constants.py` (unit, no network, no DB)

| Test | Asserts | Mocking |
|---|---|---|
| `test_per_document_token_ceiling_pinned` | `PER_DOCUMENT_TOKEN_CEILING == 2_000_000` (the committed default). | none |
| `test_max_attempts_pinned` | `MAX_ATTEMPTS == 3`. | none |
| `test_model_prices_present` | `MODEL_PRICES["gpt-4o"] == (Decimal("2.50"), Decimal("10.00"))` and `["gpt-4o-mini"] == (Decimal("0.15"), Decimal("0.60"))`. | none |
| `test_cost_usd_for_gpt4o` | `cost_usd_for("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000) == Decimal("12.50")`. | none |
| `test_cost_usd_for_mini_fractional` | `cost_usd_for("gpt-4o-mini", tokens_in=500_000, tokens_out=100_000) == Decimal("0.135")` (0.075 + 0.06). | none |
| `test_cost_usd_for_unknown_model_is_zero` | unknown model → `Decimal("0")`, never raises (counts still accrue elsewhere). | none |
| `test_cost_usd_for_zero_tokens` | `cost_usd_for(..., 0, 0) == Decimal("0")`. | none |
| `test_ceiling_env_override` | `APOLLO_PROVISION_TOKEN_CEILING=10` reimport → ceiling 10 (env-override branch). | `monkeypatch.setenv` + `importlib.reload`. |

### `apollo/provisioning/tests/test_metered_chat.py` (unit, fake usage — NO network, NO DB)

`_FakeIngestRun` = a plain object with `llm_calls=0, llm_tokens_in=0, llm_tokens_out=0, llm_cost_usd=Decimal("0")`. `_FakeResponse` carries `.usage` (an object with `prompt_tokens`/`completion_tokens`) and `.choices[0].message.content`. `_FakeClient.chat.completions.create(**kwargs)` returns a queued `_FakeResponse` and records the `model` it was called with.

| Test | Asserts | Mocking |
|---|---|---|
| `test_cheap_returns_content` | `MeteredChat(...).cheap(purpose=…, messages=…)` returns the fake `content` string. | `_FakeClient` injected. |
| `test_cheap_routes_to_mini_model` | the client was called with the resolved cheap model (`gpt-4o-mini` default, or `APOLLO_CHEAP_MODEL`). | inject client + capture kwargs. |
| `test_main_routes_to_main_model` | `.main(...)` called the client with the `gpt-4o` default (or `MAIN_MODEL`). | inject client. |
| `test_explicit_model_overrides_routing` | passing `model="gpt-4o"` to `.cheap` overrides the cheap default. | inject client. |
| `test_single_call_accumulates_counts` | after one call: `ingest_run.llm_calls == 1`, `llm_tokens_in == usage.prompt_tokens`, `llm_tokens_out == usage.completion_tokens`. | fake usage `(120, 40)`. |
| `test_cost_accumulates_via_cost_usd_for` | `ingest_run.llm_cost_usd == cost_usd_for(model, …)` for that one call (Decimal exact). | fake usage + known model. |
| `test_multiple_calls_accumulate_additively` | two calls → `llm_calls == 2`, tokens and cost are the SUM (proves `+=`, not overwrite). MUTATION: an `=` instead of `+=` REDs this. | two fake responses. |
| `test_scrape_chat_fn_positional_adapter` | `metered.scrape_chat_fn(sys_prompt)("chunk text")` returns content AND accumulates one call routed cheap (the scrape.py:141 seam). | inject client; assert chunk text reached the user message. |
| `test_ceiling_not_breached_no_raise` | a call whose cumulative tokens stay `<= ceiling` returns normally; no exception. | ceiling set high. |
| `test_ceiling_breached_raises_cost_budget_exceeded` | a call pushing cumulative `> ceiling` raises `CostBudgetExceeded` AT the boundary; the exception carries `tokens` and `ceiling`. MUTATION: dropping the ceiling check → this test REDs. | `ceiling=100`, usage that crosses it. |
| `test_counts_accrued_before_raise` | even when `CostBudgetExceeded` raises, the breaching call's counts WERE added (so the ingest_run reflects the spend that triggered the abort). | small ceiling. |
| `test_does_not_import_or_call_llm_module` | `MeteredChat` never calls `apollo.agent._llm.cheap_chat`/`main_chat` (it re-invokes the client) — patch both to raise and assert they're never hit. | `monkeypatch` `_llm.cheap_chat`/`main_chat` → raise. |
| `test_unknown_model_accrues_counts_zero_cost` | a call with an unknown model still bumps counts, adds `Decimal("0")` cost, no raise. | inject client with odd model. |

### `tests/database/test_apollo_provisioning_queue.py` (real-PG, Testcontainers pgvector:pg16 — GREEN-NOT-SKIPPED)

Harness mirrors `test_learner_janitor_contention.py` (committed connections, pooled `async_sessionmaker`, per-test truncate) + the 030 chain selector from `test_apollo_autoprovisioning_migration.py`. `pytestmark = pytest.mark.integration`. A `_seed_committed_job(plain_dsn, *, search_space_id, document_id, state="pending", attempt_count=0, lease_expires_at=None)` helper inserts + COMMITs via independent asyncpg and returns the job id; a matching `aita_search_spaces` row + an `apollo_ingest_runs` row are seeded for FK validity.

| Test | Asserts (load-bearing behavior) | Mocking / setup |
|---|---|---|
| `test_two_workers_claim_non_overlapping_jobs` | **(behavior 1)** seed TWO committed `pending` jobs; `asyncio.gather` two `claim_provisioning_job` calls on two pooled sessions → the two `ClaimedJob.job_id`s are DISTINCT and cover both rows; neither is `None`. | two committed sessions. |
| `test_skip_locked_mutation_proof` | **(behavior 1, mutation guard)** documented inverse: with `skip_locked=True` present, two concurrent claims over a SINGLE seeded job → exactly one `ClaimedJob`, the other `None` (`{c1 is None, c2 is None} == {True, False}`). The plan/test comment states that DROPPING `skip_locked=True` makes BOTH claim the same job and this assertion REDs. | single committed job, two sessions. |
| `test_claim_sets_lease_and_bumps_attempt` | a claim flips `state='running'`, sets `lease_owner`, sets `lease_expires_at ≈ now()+lease_seconds`, `attempt_count == 1`; visible to a FRESH independent asyncpg connection (committed). | one job, independent conn read. |
| `test_claim_returns_none_when_empty` | no claimable jobs → `claim_provisioning_job` returns `None`. | empty table. |
| `test_running_job_with_live_lease_not_claimed` | a `running` job whose `lease_expires_at > now()` is NOT claimable → `None`. | seed running + future lease. |
| `test_lease_expiry_makes_running_job_reclaimable` | **(behavior 2)** seed a `running` job with `lease_expires_at < now()` → a worker re-claims it, `attempt_count` bumps to 2, new `lease_owner`. | seed running + past lease, attempt_count=1. |
| `test_complete_job_terminal_and_unclaimable` | `complete_job` → `state='completed'`, lease cleared; a subsequent claim returns `None` (terminal). | claim then complete. |
| `test_fail_job_retry_below_max` | **(behavior 3, retry leg)** `fail_job` on a job with `attempt_count < MAX_ATTEMPTS` → returns `'pending'`, `state='pending'`, lease cleared, `last_error` set; re-claimable. | attempt_count=1. |
| `test_fail_job_dead_letters_at_max` | **(behavior 3)** `fail_job` when `attempt_count >= MAX_ATTEMPTS` → returns `'failed'`, `state='failed'`, and a subsequent `claim_provisioning_job` returns `None` (no further claim). MUTATION: a `>` instead of `>=` lets a job at exactly MAX re-enter → this REDs. | attempt_count=MAX_ATTEMPTS. |
| `test_release_job_returns_to_pending_no_attempt_change` | `release_job` → `state='pending'`, lease cleared, `attempt_count` UNCHANGED (cooperative release ≠ failure). | claim then release. |
| `test_cost_abort_writes_ingest_error_and_fails_run` | **(behavior 4)** drive a `MeteredChat` (real `apollo_ingest_runs` row on the committed DB, a fake client crossing a small ceiling) until `CostBudgetExceeded`; then perform the orchestrator-style terminal handling THIS TEST exercises — INSERT an `apollo_ingest_errors` row (stage='scrape', error_class='CostBudgetExceeded') + set `ingest_run.status='failed'` + `fail_job(...)` at max → assert: an `apollo_ingest_errors` row exists for the run, `apollo_ingest_runs.status=='failed'`, and the job is `state='failed'`. (The PRODUCTION write site is 3B2g; this test pins the contract the abort must satisfy end-to-end on real PG.) | committed DB; fake OpenAI client (no network); ceiling=tiny. |
| `test_claim_order_is_fifo` | with two `pending` jobs of different `created_at`, the earlier is claimed first (`order_by created_at, id`). | two jobs, staggered created_at. |

Coverage note: every branch of `fail_job` (retry vs dead-letter), `claim` (row vs None, pending vs expired-running), the metered ceiling (raise vs no-raise), and `cost_usd_for` (known vs unknown model) is hit → comfortably ≥95% patch coverage on the new lines. No `xfail`, no `skip`, no assert-nothing.

## Owner-doc updates

`docs/architecture/apollo.md` (the owner of `apollo/**`):

1. **Frontmatter:** set `last_verified: 2026-06-20` (currently `2026-06-19`) — same commit as the code.
2. **`apollo/provisioning/` row (currently line ~39):** append `queue.py`, `metered_chat.py`, `cost_constants.py` to the file list and add a **WU-3B2f** sentence describing the public surface, in the same style as the existing 3B2c/d/e sentences:
   - `claim_provisioning_job(session, *, lease_owner, lease_seconds) -> ClaimedJob | None` — the SKIP-LOCKED claim/lease drain over `apollo_provisioning_jobs` (mirrors `knowledge/teacher_weekly.py:_claim_upload_job_async`); `complete_job`/`fail_job` (dead-letter at `MAX_ATTEMPTS`)/`release_job`; the provisioning `state` vocabulary is `pending/running/completed/failed`, DISTINCT from the run `status`.
   - `MeteredChat(ingest_run, ...)` — the per-call metered LLM client that is the ONLY token signal (`_llm.py` discards `response.usage`); `.cheap`/`.main`/`.scrape_chat_fn` capture usage, accumulate `llm_calls`/`llm_tokens_in`/`llm_tokens_out`/`llm_cost_usd` onto the `apollo_ingest_runs` row, and raise `CostBudgetExceeded` at `PER_DOCUMENT_TOKEN_CEILING`. Does NOT mutate `_llm.py`.
   - `cost_constants.py` — `PER_DOCUMENT_TOKEN_CEILING=2_000_000`, `MAX_ATTEMPTS=3`, the `gpt-4o`/`gpt-4o-mini` price table + `cost_usd_for`. Routing: scrape/judge→cheap, generate→main.
3. **Data-flow / convention note:** record that the claim-then-commit-before-work discipline matches `learner_janitor._claim_due`, and that the cost aggregate per document is the `apollo_ingest_runs` audit row.

No other owner doc is in scope (no `knowledge/`, `database/`, or `indexing/` source is touched — those crossings are WU-3B2g's).

## Verification

- [ ] **Manual smoke (queue):** on a migrated local DB, INSERT a `pending apollo_provisioning_jobs` row → call `claim_provisioning_job` → assert it returns a `ClaimedJob`, the row is `running` with a lease, `attempt_count==1`; call `complete_job` → row `completed`, next claim returns `None`.
- [ ] **Manual smoke (metered):** construct `MeteredChat` with a fake client returning `usage(prompt=100, completion=50)` → after one `.cheap` call, `ingest_run.llm_calls==1`, tokens accrue, `llm_cost_usd == cost_usd_for(...)`.
- [ ] **Dry-run cost calculation:** `cost_usd_for("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)` MUST equal `Decimal("12.50")` = (1M/1M × $2.50) + (1M/1M × $10.00). Pinned by `test_cost_usd_for_gpt4o`.
- [ ] **Pipeline replay test (idempotency):** `test_lease_expiry_makes_running_job_reclaimable` + `test_two_workers_claim_non_overlapping_jobs` — the same job replayed after lease expiry is re-claimed exactly once; two workers never overlap.
- [ ] **DLQ test:** `test_fail_job_dead_letters_at_max` — a job at `MAX_ATTEMPTS` lands in `state='failed'` and is no longer claimable. Plus `test_cost_abort_writes_ingest_error_and_fails_run` proves the abort path lands an `apollo_ingest_errors` row + a failed run.
- [ ] **Backpressure / cost-ceiling test:** `test_ceiling_breached_raises_cost_budget_exceeded` — a metered run that exceeds `PER_DOCUMENT_TOKEN_CEILING` raises `CostBudgetExceeded` at the boundary rather than running unbounded.
- [ ] **Gate commands (the binding contract):**
  - `pytest tests/database -q` — real-PG GREEN-NOT-SKIPPED (Docker up; interpreter `.venv/Scripts/python.exe`).
  - `pytest apollo -q` — no regressions across the existing provisioning suite.
  - `pytest --cov=. --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2e-solution-pairing --fail-under=95`.
- [ ] **Skip-guard:** confirm the queue test FILE does not silently skip — with Docker up, `pytest tests/database/test_apollo_provisioning_queue.py -q` reports PASSED, not SKIPPED (a skip is a FAIL of the real-infra gate).

## Downstream consumers

- **WU-3B2g (orchestrator + worker shell + trigger)** is the sole direct consumer: it imports `claim_provisioning_job`/`complete_job`/`fail_job`/`release_job` to drain inside the worker loop, and constructs a `MeteredChat(ingest_run=…)` per document, handing `metered.cheap`/`metered.main`/`metered.scrape_chat_fn(...)` to the stage functions as their injected `chat_fn`. The orchestrator owns: the `ingest_run` row lifecycle, the `apollo_ingest_errors` WRITE on `CostBudgetExceeded`, flipping `ingest_run.status='failed'`, and the lease-reaper. This unit hands them a clean claim/lease + a metered transport.
- **WU-3B2h** consumes nothing from this unit directly (it joins findings/runs/attempts).

## Risks

- **[LOW] SKIP-LOCKED non-overlap correctness.** Mitigated: the pattern is copied verbatim from a proven site (`_claim_upload_job_async`) and PROVEN by the real-PG two-worker test + the mutation-proof note. Confidence HIGH it works.
- **[MEDIUM] Real-PG test runs as SKIP, not GREEN.** If Docker is down or the container fails to start, the `_pg_url` fixture skips — and a skip is a contract FAIL. Mitigated: the executor must confirm Docker is up and the test PASSES (not skips) before claiming done; the verification section pins this explicitly.
- **[MEDIUM] 030 chain selector drift in the new queue test.** The committed-connection harness must apply 030's tables. Mitigated: reuse the EXACT `_TOUCHES_TARGETS`/`_EXCLUDE_FROM_CHAIN` + apply-030-last logic already proven in `test_apollo_autoprovisioning_migration.py` (don't re-derive). Risk if the executor hand-rolls a chain that omits 030 → the queue tables won't exist → red. Copy, don't reinvent.
- **[MEDIUM] `MeteredChat` mutating the ORM `ingest_run` vs. the global immutability rule.** The CLAUDE.md/global style prefers immutable value objects, but a SQLAlchemy ORM row IS the durable aggregate and is designed to be mutated-then-flushed — this is the intended persistence write, not a value-object mutation. Documented in the docstring + this plan so a reviewer doesn't flag it as a style violation. The pure value objects in this unit (`ClaimedJob`, `_Usage`) ARE frozen.
- **[MEDIUM] Cost-surprise risk.** The $2M-token ceiling is a runaway breaker, not a tight budget; a misrouted stage (generate→main on every chunk) could spend more than expected within the ceiling. Mitigated: routing is pinned (scrape/judge→cheap, generate→main), the subsystem is flag-OFF by default, and every document's spend is committed to `apollo_ingest_runs.llm_cost_usd` for audit. The orchestrator (3B2g), not this unit, decides routing per stage.
- **[LOW] External API availability (OpenAI).** A transient OpenAI outage surfaces as an exception → `fail_job` retries the whole job up to `MAX_ATTEMPTS`, then dead-letters. No call-level retry here by design (avoids double-counting `attempt_count`). Acceptable for an async, flag-gated batch subsystem.
- **[LOW] Schema-lock during deploy.** None — this unit ships NO migration (030 already landed). Zero DDL, zero lock.
- **[LOW] `now()` clock source.** Computed in Python (`datetime.now(UTC)`) to match `_claim_upload_job_async` and keep lease arithmetic test-controllable; a multi-worker clock skew is bounded by `lease_seconds` (set generously by the caller). Acceptable.

## Out-of-scope boundaries (this unit does NOT do)

- **NO trigger/enqueue** — `enqueue_provisioning_job` and the `teacher_weekly.py:1174` hook are WU-3B2g. This unit assumes jobs already exist.
- **NO worker-loop shell** — the dormant flag-gated `apollo/provision_worker.py` (mirrors `learner_janitor_worker.py`) is WU-3B2g.
- **NO orchestrator / stage logic** — scrape/find-or-generate/pairing/tag-mint/dedup/promotion are 3B2b–e; the 6-stage sequencing is 3B2g.
- **NO `apollo_ingest_errors` PRODUCTION write site** — the orchestrator writes it; this unit only EXERCISES that contract in the cost-abort real-PG test.
- **NO migration / DDL / ORM change** — 030 + the five ORM classes already exist (WU-3B2a). This unit must NOT add or edit any migration, and must NOT apply any migration to any remote DB (local Testcontainers only).
- **NO mutation of `apollo/agent/_llm.py`** — the metered client re-invokes the OpenAI client; `_llm.py` stays byte-identical.
- **NO `write_tier1_problems` ON CONFLICT hardening / `UniqueConstraint(concept_id, problem_code)` on `ConceptProblem`** — that is the carried 3B2g/h follow-up; only NOTE it in the owner doc if natural, do not implement.
- **NO call-level retry/backoff, NO new package** (only `rapidfuzz` is pinned — decision #8); NO LangChain, NO new LLM provider, NO new vector store.
- **NO lease-reaper / terminal-status reconciliation sweep** — 3B2g.

## Deviations I'd allow the executor

- **`fail_job` return type:** returning the resulting state string is a convenience for the test/orchestrator; returning `None` and letting callers re-read the row is acceptable if the dead-letter/retry branch is still directly tested.
- **`scrape_chat_fn` shape:** if a cleaner adapter (e.g. a single `as_chat_fn(tier, system_prompt)`) covers both the positional-string (scrape) and kwargs (solution/pairing) seams, that is fine as long as BOTH existing call conventions in `scrape.py:141` and `solution.py:218` are satisfied unchanged.
- **`record_usage` visibility:** may be a private `_record_usage` if the unit test drives it through `.cheap`/`.main` instead of calling it directly — as long as the accumulation + ceiling branches are both covered.
- **Lease arithmetic source:** `now()` may be Python-side (`datetime.now(UTC)`, preferred, matches the template) OR SQL `func.now()` — but if SQL-side, the lease-expiry test must still be deterministic (seed `lease_expires_at` relative to the DB clock).
- **Cost precision:** `Decimal` is required for `llm_cost_usd` (the column is `NUMERIC(12,6)`); `float` is NOT acceptable (rounding drift breaks the exact cost-math tests).
- **Structured-log field names:** the exact `event=` tags may differ from the suggestions as long as one structured line per claim/complete/fail/cost-abort exists and carries `job_id`/`ingest_run_id` (and NEVER prompt content or the API key).
- **Test helper factoring:** the committed-connection harness may be copied inline into the new queue test OR factored into a shared `tests/database/_committed_pg.py` — copying inline (matching `test_learner_janitor_contention.py`) is the safer default to avoid touching files outside scope.
