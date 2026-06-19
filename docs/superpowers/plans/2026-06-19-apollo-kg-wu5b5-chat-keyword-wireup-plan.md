# Plan: WU-5B5 — §10 chat-keyword LIVE wire-up (server.py /ask persists bundle.found_terms via append_turn(keywords=...))

**Goal:** Thread the per-`/ask` keyword list (`bundle.found_terms`) from the LIVE `/ask` handler into the already-frozen `append_turn(keywords=...)` write path so `chat_turns.keywords` actually captures data in production, with zero behavior change to answers, retrieval, or citations.
**Architecture:** Pure call-graph wire-up inside `server.py` — add a keyword-only `keywords` param to the two assistant-turn persist helpers, extract `found_terms` robustly from the in-scope `bundle` at the two happy-path call sites, leave the error path defaulting to `[]`. No schema, no migration, no new package.
**Tech stack:** Python 3.12 / FastAPI + SQLAlchemy async (`.venv/Scripts/python.exe`), pytest. DB write target is the frozen WU-5B4 `chat_turns.keywords` JSONB column (migration 029) + `append_turn(keywords=...)`.

---
provides:
  - server.py `_append_assistant_turn_and_refresh(*, ..., keywords: Optional[List[str]] = None)`
  - server.py `_append_assistant_turn_and_refresh_async(*, ..., keywords: Optional[List[str]] = None)`
  - LIVE persistence of `bundle.found_terms` -> `chat_turns.keywords` on the assistant turn of every `/ask` (non-streaming + streaming happy paths)
consumes:
  - chats/service.py `append_turn(*, ..., keywords: List[str] | None = None)` (FROZEN, WU-5B4 #45 — reuse as-is)
  - config/contracts.py `ResearchBundle.found_terms` / `ResearchMetadata.found_terms` (read-only)
  - database/models.py `ChatTurn.keywords` JSONB + database/migrations/029_*.sql (FROZEN — reuse)
depends_on:
  - feat/apollo-kg-wu5b4-chat-keyword-persist (base branch; #45 merged the write-only column + param)

---

## Overview
WU-5B4 (#45, FROZEN) added the write-only `chat_turns.keywords` JSONB column, the
`append_turn(*, keywords: List[str] | None = None)` param (coalesces `None -> []`,
`chats/service.py:110`/`:146`), and migration 029 — but made **zero** edits to the
production `/ask` handler. So in prod the column round-trips and the live path always
writes `[]`. domain-data.md:50 documents this gap verbatim: *"the production `server.py`
wire-up that fills it from `bundle.found_terms` is a documented follow-on — until it
lands the column round-trips but the live `/ask` path writes `[]`."*

**This unit IS that follow-on.** It is a surgical call-graph wire-up entirely inside
`server.py`:

1. Add a keyword-only `keywords: Optional[List[str]] = None` param to the two assistant-turn
   persist helpers — the async core `_append_assistant_turn_and_refresh_async`
   (`server.py:491`) and its sync wrapper `_append_assistant_turn_and_refresh`
   (`server.py:530`) — and forward it into the existing `append_turn(..., keywords=...)`
   call at `server.py:517`.
2. At the **two HAPPY-path call sites** — non-streaming `/ask` (`server.py:1852`) and
   streaming `/ask` (`server.py:2135`), where the populated `bundle` is in scope — extract
   the keyword list with a robust getattr chain and pass it as `keywords=`.
3. At the **ERROR-path call site** — streaming `/ask` (`server.py:2167`, `assistant_content`
   = error message, `citations=[]`, no successful bundle) — leave `keywords` at its default
   (`None -> []`).

The **user-turn** append (`server.py:451`) is UNCHANGED: keywords describe the *answered*
question and are not available until after retrieval, so they attach to the **assistant**
turn only. There is **no read path** in v1 — keywords are write-only for offline class-level
backfill (§10 RQ5 hedge).

**No behavior change:** retrieval results, answer text, and citations are byte-identical;
the only new effect is that the assistant turn's `keywords` column receives the
already-computed `found_terms` instead of `[]`.

## Spec grounding (§10 / §12)
Authoritative spec: `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`.

- **§10 "Hoot chat as evidence (RQ5)"** (spec L1453–L1466): chat evidence is **OUT for v1**
  because question-asking is sign-ambiguous. The shipped **hedge**: *"the per-`/ask`
  `extract_and_filter_keywords` output (≤8 concept terms, currently computed then discarded)
  is persisted as one JSONB column on chat turns so months of history can be backfilled
  offline."* When chat evidence lands later it is **class-level signal first, never a hard
  per-student negative.**
- **§12 phase 5 / L1534**: *"RQ5 hedge (persist chat keywords)"* is listed as part of the
  Layer-3 build. This unit delivers exactly the persistence half of that hedge on the live
  path. (WU-5B4 delivered the column + param; WU-5B5 delivers the live wire-up.)
- **§2 schema (L247)**: `'chat_question'` is a *reserved* `event_kind` for the eventual chat
  pathway — confirming chat keywords are stored now and consumed later, not wired into any
  read/learner-update path in v1.

Two spec-mandated invariants this plan enforces:
1. **Write-only, no read path** — nothing in `/ask` or downstream reads `chat_turns.keywords`.
2. **≤8 terms, NOT re-capped here** — the ≤8 bound is produced upstream by
   `extract_and_filter_keywords` and carried on the bundle; this wire-up trusts the list
   as-is (mirrors `append_turn`'s "thin persistence primitive" contract, domain-data.md:66).
   Do NOT re-slice `[:8]`.

## Prior art (existing code references)
Verified by reading the real code (all line numbers as of base branch
feat/apollo-kg-wu5b4-chat-keyword-persist):

- **Frozen write primitive** — `chats/service.py:98-149`:
  `append_turn(db, *, chat_session_id, role, content, ..., keywords: List[str] | None = None)`.
  Line 146: `keywords=keywords or []` — coalesces `None`/empty to `[]`, never writes `None`.
  REUSE as-is; do NOT modify.
- **The keyword carrier** — `config/contracts.py:90-108`: `ResearchBundle` has a top-level
  `found_terms: List[str] = field(default_factory=list)` (line 105) AND `bundle.metadata`
  (`ResearchMetadata`, line 53-86) also carries `found_terms` (line 78). The orchestrator
  populates `found_terms=keywords` (`ai/orchestrator.py:670`/`:703`). The pgvector path
  constructs both with `found_terms=[]` (`server.py:1509`/`:1536`) — so an unrouted/cache
  bundle legitimately yields `[]`. The robust getattr chain (bundle-level OR metadata-level
  OR `[]`) handles all three shapes.
- **Existing `getattr` carry pattern** — `ai/main_ai.py:1581-1582` already reads the sibling
  field with exactly this fallback idiom:
  `getattr(bundle, "not_found_terms", None) or getattr(bundle.metadata, "not_found_terms", None)`.
  The wire-up mirrors this proven pattern for `found_terms`.
- **The three persist call sites** — `server.py:1852` (non-streaming happy), `server.py:2135`
  (streaming happy), `server.py:2167` (streaming error). The non-streaming `bundle` is
  declared `bundle = None` at `server.py:1775`, populated at `:1796`
  (`_retrieve_bundle_with_router`), and in scope at `:1852`. The streaming `bundle` is in
  scope at `:2135` and is the same object passed to `_persist_router_outcome_sync(bundle=...)`
  at `:2153`.
- **Test conventions for these helpers** — existing integration tests monkeypatch
  `server._append_assistant_turn_and_refresh` to capture kwargs
  (`tests/integration/test_server_part8.py:220-224`, `:296`; `tests/integration/test_e2e_api.py:96`).
  WU-5B4's own no-behavior-change test
  (`tests/test_keywords_no_behavior_change.py`) is the style template for the unit assertions.

## Exact code changes (server.py ONLY)

Five surgical edits. All other lines untouched. Immutable style — no in-place mutation of
the bundle; we only read off it.

### Edit 1 — async core helper signature + forward (`server.py:491-498`, `:517-524`)
Add the param to the signature:
```python
async def _append_assistant_turn_and_refresh_async(
    *,
    auth: AuthContext,
    chat_id: str,
    search_space_id: int,
    assistant_content: str,
    citations: Optional[List[Dict[str, Any]]] = None,
    keywords: Optional[List[str]] = None,   # §10 RQ5 hedge — write-only chat keywords
) -> None:
```
Forward it into the existing `append_turn(...)` call (currently `server.py:517-524`):
```python
        await append_turn(
            db_session,
            chat_session_id=int(session.id),
            role="assistant",
            content=assistant_content,
            model=os.getenv("SOLVER_MODEL", "gpt-4o"),
            citations=list(citations) if citations else None,
            keywords=list(keywords) if keywords else None,  # None/empty -> [] in append_turn
        )
```
Rationale for `list(keywords) if keywords else None`: pass a defensive copy (immutable style,
no aliasing the bundle's list), and let the frozen `append_turn` apply the canonical
`None -> []` coalesce so the `[]` semantics live in exactly one place.

### Edit 2 — sync wrapper signature + forward (`server.py:530-546`)
```python
def _append_assistant_turn_and_refresh(
    *,
    auth: AuthContext,
    chat_id: str,
    search_space_id: int,
    assistant_content: str,
    citations: Optional[List[Dict[str, Any]]] = None,
    keywords: Optional[List[str]] = None,   # §10 RQ5 hedge — write-only chat keywords
) -> None:
    run_async(
        _append_assistant_turn_and_refresh_async(
            auth=auth,
            chat_id=chat_id,
            search_space_id=search_space_id,
            assistant_content=assistant_content,
            citations=citations,
            keywords=keywords,
        )
    )
```

### Edit 3 — a tiny module-level extractor helper (new, near the helpers ~`server.py:489`)
Define ONE small pure function so both happy-path call sites share identical, tested logic
(DRY; avoids duplicating the getattr chain at two sites):
```python
def _keywords_from_bundle(bundle: Any) -> List[str]:
    """Robustly pull the ≤8-term keyword list off a ResearchBundle for write-only
    persistence (§10 RQ5 hedge). Returns [] for a None/AUGMENT/cache bundle that
    carries no found_terms. Does NOT re-cap at 8 — the bound is upstream in
    extract_and_filter_keywords."""
    terms = (
        getattr(bundle, "found_terms", None)
        or getattr(getattr(bundle, "metadata", None), "found_terms", None)
        or []
    )
    return [str(t) for t in terms]
```
(`str(t)` guard mirrors the cleanliness of `extract_and_filter_keywords` output and keeps the
JSONB list homogeneous; the list is already ≤8 and pre-filtered upstream.)

### Edit 4 — non-streaming happy-path call site (`server.py:1852-1858`)
```python
        _append_assistant_turn_and_refresh(
            auth=auth,
            chat_id=chat_id,
            search_space_id=search_space_id,
            assistant_content=assistant_turn,
            citations=structured_citations,
            keywords=_keywords_from_bundle(bundle),   # write-only; [] if bundle has none
        )
```
`bundle` is in scope here (declared `:1775`, populated `:1796`). On the error sub-case where
retrieval failed, `bundle` is still `None` -> `_keywords_from_bundle(None)` returns `[]`
(safe; the getattr chain tolerates `None`).

### Edit 5 — streaming happy-path call site (`server.py:2135-2141`)
```python
                    lambda: _append_assistant_turn_and_refresh(
                        auth=auth,
                        chat_id=chat_id,
                        search_space_id=search_space_id,
                        assistant_content=answer_text.strip() or "[empty answer]",
                        citations=persist_citations,
                        keywords=_keywords_from_bundle(bundle),
                    ),
```

### NOT edited — streaming ERROR-path call site (`server.py:2167-2173`)
Leave exactly as-is. No `keywords=` kwarg -> defaults to `None` -> `append_turn` writes `[]`.
This is the desired behavior: a failed turn has no successful bundle / no answered question,
so it carries no keywords.

### NOT edited — user-turn append (`server.py:451-457`)
Untouched. Keywords attach to the assistant turn only.

## Public signatures (backward-compat)
All three are keyword-only additions with a default, so every existing caller (and the
existing monkeypatches in `test_server_part8.py` / `test_e2e_api.py` that pass `**kwargs`)
keeps working unchanged.

```python
# server.py — NEW pure helper
def _keywords_from_bundle(bundle: Any) -> List[str]: ...

# server.py — param ADDED (keyword-only, default None); was line 491
async def _append_assistant_turn_and_refresh_async(
    *, auth, chat_id, search_space_id, assistant_content,
    citations: Optional[List[Dict[str, Any]]] = None,
    keywords: Optional[List[str]] = None,
) -> None: ...

# server.py — param ADDED (keyword-only, default None); was line 530
def _append_assistant_turn_and_refresh(
    *, auth, chat_id, search_space_id, assistant_content,
    citations: Optional[List[Dict[str, Any]]] = None,
    keywords: Optional[List[str]] = None,
) -> None: ...
```

**Backward-compat guarantees:**
- Omitting `keywords` reproduces the pre-WU-5B5 behavior exactly (`None -> []` write).
- No positional-arg breakage (all params are keyword-only behind `*`).
- `chats/service.py:append_turn` signature is UNCHANGED (reused as-is).

## TDD-ordered test plan
**Write tests FIRST (RED), then make Edits 1–5 (GREEN).** No skip/xfail, no
assert-nothing. The LLM/network is never hit — we drive the helpers directly with a FAKE
bundle and SPY on `append_turn`, and the `/ask` integration test uses the existing
`TEST_FAKE_OPENAI=1` fixture with all pipeline stages monkeypatched. Every changed
`server.py` line (the 2 helper params + the 2 forwards + `_keywords_from_bundle` + the 2
happy-path call-site args) is exercised below so diff-cover >= 95% holds.

### New file: `tests/test_chat_keyword_wireup.py` (unit — helper-level, append_turn spied)
Shared fixtures in-module:
- `_fake_bundle(found_terms=..., metadata_found_terms=..., has_found_terms=True)` — builds a
  `SimpleNamespace` (or real `ResearchBundle`) carrying the requested `found_terms` and a
  `metadata` namespace; a variant with NO `found_terms` attribute at all (to exercise the
  metadata fallback and the `[]` floor).
- `_spy_append_turn(monkeypatch)` — monkeypatches `server.append_turn` with an `AsyncMock`
  that records the `keywords=` kwarg; also stubs `server.get_async_session` (async CM
  yielding a fake session), `server.get_chat_session_for_user` (returns a fake session with
  matching `search_space_id`), and `server.refresh_memory_summary` (AsyncMock no-op) so the
  async core runs without DB/infra. `run_async` is driven via a real fresh event loop
  (same `_fake_run_async` pattern as `tests/test_keywords_no_behavior_change.py:45`).

Tests:

1. `test_keywords_from_bundle_reads_top_level_found_terms`
   — asserts `_keywords_from_bundle(bundle)` returns `["momentum","impulse","force"]` when
   the bundle has a top-level `found_terms`. Covers the first getattr branch + `str()` map.

2. `test_keywords_from_bundle_falls_back_to_metadata_found_terms`
   — bundle with NO top-level `found_terms` (attr absent / `[]`) but
   `bundle.metadata.found_terms == ["energy","work"]` -> returns `["energy","work"]`.
   Covers the second getattr branch (mirrors `main_ai.py:1581-1582` idiom).

3. `test_keywords_from_bundle_none_or_empty_yields_empty_list`
   — three params: `bundle=None`; a bundle whose `found_terms` is `[]` and
   `metadata.found_terms` is `[]` (NONE/AUGMENT/cache shape, like `server.py:1509`); a bundle
   with neither attribute. ALL must return `[]`. Covers the final `or []` floor + `None`
   tolerance of the nested `getattr(getattr(bundle,'metadata',None),...)`.

4. `test_keywords_from_bundle_does_not_recap_at_8`
   — bundle with 12 `found_terms` (`t0..t11`) -> helper returns all 12 unchanged.
   Locks the spec rule "do NOT re-cap at 8 — the bound is upstream."

5. `test_happy_path_async_threads_found_terms_to_append_turn`
   — call `_append_assistant_turn_and_refresh_async(..., keywords=["a","b","c"])` with
   `append_turn` spied. Assert the spy was awaited once with `role="assistant"`,
   `keywords == ["a","b","c"]`. Covers Edit 1's forward line.

6. `test_happy_path_sync_wrapper_forwards_keywords`
   — call the sync `_append_assistant_turn_and_refresh(..., keywords=["x"])`; assert the spied
   `append_turn` received `keywords == ["x"]`. Covers Edit 2's wrapper forward + `run_async`
   path.

7. `test_omitted_keywords_writes_empty_list_semantics`
   — call the async helper WITHOUT `keywords` (default `None`). Assert the spied
   `append_turn` received `keywords is None` (the `list(keywords) if keywords else None`
   branch yields `None`, and the frozen `append_turn` coalesces to `[]`). Locks the
   backward-compat / error-path `[]` semantics at the helper boundary. Covers the `else None`
   branch of Edit 1.

8. `test_keywords_attached_to_assistant_role_only`
   — assert that in every spied `append_turn` call from helpers 5–7 the recorded `role`
   is `"assistant"` (never `"user"`); documents that the user-turn append at `server.py:451`
   is a different code path the wire-up never touches.

### New tests in `tests/integration/test_chat_keyword_wireup_ask.py` (integration — through `/ask`)
Reuse the `client_with_server` fixture pattern from
`tests/integration/test_server_part8.py:14-25` (`TEST_FAKE_OPENAI=1`, in-memory sqlite). Spy
`server._append_assistant_turn_and_refresh` to capture kwargs, and monkeypatch
`_retrieve_bundle_with_router` (or `_ask_pgvector`/`_retrieve...`) to return a fake bundle
carrying `found_terms`. Also monkeypatch `parse_question`, `solve_with_bundle`,
`format_answer`, `_load_memory_and_append_user_turn`, `_require_course_membership`,
`_get_workspace_manager`, `_get_teacher_storage`, `_save_attachments` exactly as
`test_server_part8.py:200-224` does.

9. `test_ask_happy_path_persists_bundle_found_terms` (non-streaming `/ask`)
   — fake retrieval returns a bundle with `found_terms=["bernoulli","pressure"]`. POST `/ask`.
   Assert `status_code == 200`, and the captured `_append_assistant_turn_and_refresh` kwargs
   has `keywords == ["bernoulli","pressure"]`. Covers Edit 4's call-site arg on the LIVE path.

10. `test_ask_bundle_without_found_terms_persists_empty` (non-streaming `/ask`)
    — fake retrieval returns a bundle with `found_terms=[]` (NONE/cache shape). Assert captured
    `keywords == []`. (Through the helper boundary the test asserts the value the call site
    computed, i.e. `_keywords_from_bundle(bundle) == []`.)

11. `test_ask_no_behavior_change_answer_and_citations_unchanged` (NO-BEHAVIOR-CHANGE)
    — run the SAME mocked `/ask` request twice: once via the wired code, and assert the
    response body `{"answer", "citations", "logs"}` equals a golden snapshot
    (`answer == "Memory-aware answer"`, `citations == []`) — i.e. threading keywords does not
    perturb the user-visible payload. This is the integration-level analogue of
    `tests/test_keywords_no_behavior_change.py`. (Belt-and-suspenders: also assert the
    user-turn append captured no `keywords` kwarg by spying
    `_load_memory_and_append_user_turn` / `append_turn` for the user role.)

12. `test_ask_streaming_happy_path_persists_found_terms` (streaming `/ask`, OPTIONAL but
    recommended for the `:2135` line)
    — hit the SSE `/ask` streaming endpoint with the same fake-bundle setup; spy
    `_append_assistant_turn_and_refresh`; assert the captured `keywords` equals the fake
    bundle's `found_terms`. This is the one test that covers Edit 5 (`server.py:2135`). If the
    streaming endpoint is awkward to drive in-test, fall back to a direct-helper unit test
    that replicates the `lambda: _append_assistant_turn_and_refresh(..., keywords=_keywords_from_bundle(bundle))`
    closure with a fake bundle and asserts the same — but PREFER the real endpoint so the
    `:2135` line itself is diff-covered.

### Coverage mapping (every changed line is hit)
- Edit 1 signature + forward -> tests 5, 7 (and 9–12 transitively).
- Edit 2 wrapper -> tests 6, 9–11.
- Edit 3 `_keywords_from_bundle` (all branches) -> tests 1–4.
- Edit 4 non-streaming call-site arg -> tests 9, 10, 11.
- Edit 5 streaming call-site arg -> test 12.

### Gate alignment
- **Gate 1** `pytest apollo` (or the apollo test subset) — proves no apollo regression
  (this unit does not touch `apollo/`, so it must stay green untouched).
- **Gate 3** full collection — the new `tests/test_chat_keyword_wireup.py` +
  `tests/integration/test_chat_keyword_wireup_ask.py` run here; this is where diff-cover is
  measured.
- **Real-infra gate** — NOT triggered (no migration / `tests/database` / neo4j change). Do
  NOT add a `tests/database` integration test (it would force a real-infra run); if one were
  ever added it must be green-not-skipped. The keyword write is validated at the
  `append_turn`-spy boundary, not against live Postgres.

## Owner-doc updates (same commit)
Per the drift contract, both owner docs are updated in the SAME commit as the code.

### `docs/architecture/rag-pipeline.md` (owns `server.py` /ask flow)
- In the `/ask` data-flow / persist description, add: the assistant turn now persists the
  per-`/ask` keyword list (`bundle.found_terms`, the ≤8-term `extract_and_filter_keywords`
  output) to `chat_turns.keywords` via `_append_assistant_turn_and_refresh(..., keywords=...)`.
  Note it is **write-only** (no read path; §10 RQ5 hedge), happens on BOTH happy paths
  (non-streaming + streaming), and that the error path / cache-or-`None` bundle writes `[]`.
- Document the new `_keywords_from_bundle(bundle) -> List[str]` helper (robust getattr chain,
  does not re-cap at 8).
- **`last_verified`:** the frontmatter is ALREADY `2026-06-19` (verified at line 11). The
  orchestrator's drift-contract line says set it to `2026-06-16`, but that contradicts both
  (a) the scope-files instruction's `last_verified=2026-06-19` and (b) the actual current
  value and today's date. **Resolution: keep / set `last_verified: 2026-06-19`** (today;
  `2026-06-16` would be a regression to a stale date — see Risks). If the frontmatter is
  edited at all, it stays `2026-06-19`.

### `docs/architecture/domain-data.md` (owns `chats/` + `database/`)
- Update the `ChatTurn.keywords` note at **line 50**. Today it reads (paraphrased) "...the
  production `server.py` wire-up that fills it from `bundle.found_terms` is a documented
  follow-on — until it lands the column round-trips but the live `/ask` path writes `[]`."
  Change it to state the wire-up has **landed (WU-5B5)**: the live `/ask` assistant turn now
  writes `bundle.found_terms` to `chat_turns.keywords` on both happy paths; the error path
  and cache/`None` bundles still write `[]`; still NO read path in v1; offline backfill reads
  `role='assistant'` rows. Bump this doc's `last_verified` to `2026-06-19` if it has one.
- Do NOT alter the frozen description of `append_turn` (domain-data.md:66) — it remains
  accurate.

## Verification commands (ALL local — executor runs these)
Use `.venv/Scripts/python.exe` (py3.12). No live infra, no migrations against any remote DB.

- [ ] Apollo regression gate (Gate 1):
      `.venv/Scripts/python.exe -m pytest tests -k apollo -q` — expect: all pass, none skipped
      for the wrong reason (this unit does not touch `apollo/`).
- [ ] New unit tests:
      `.venv/Scripts/python.exe -m pytest tests/test_chat_keyword_wireup.py -q` — expect: 8
      tests pass.
- [ ] New integration tests:
      `.venv/Scripts/python.exe -m pytest tests/integration/test_chat_keyword_wireup_ask.py -q`
      — expect: tests 9–12 pass.
- [ ] Full collection + whole-repo coverage (Gate 3 — server.py IS measured):
      `.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml -q`
- [ ] Patch coverage gate (>= 95% on changed lines vs base):
      `.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml --compare-branch=feat/apollo-kg-wu5b4-chat-keyword-persist --fail-under=95`
      (or `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b4-chat-keyword-persist --fail-under=95`)
      — expect: the 2 helper params + 2 forwards + `_keywords_from_bundle` + the 2 happy-path
      call-site args all covered, >= 95%.
- [ ] Lint/format (if wired): `.venv/Scripts/python.exe -m ruff check server.py tests/test_chat_keyword_wireup.py tests/integration/test_chat_keyword_wireup_ask.py`
      and `black --check` on the same — expect: no new warnings.

No `supabase db push`, no `alembic upgrade`, no `apply_migration`, no remote DDL/DML — this
unit ships no migration.

## Risks
- **[HIGH] `last_verified` date conflict in the orchestrator instructions.** The drift-contract
  line says set `rag-pipeline.md` `last_verified` to `2026-06-16`; the scope-files line says
  `2026-06-19`; the file is already `2026-06-19` and today is `2026-06-19`. Setting it back to
  `2026-06-16` would mark the doc *stale on the very commit that verifies it* — a drift-contract
  violation. **Plan resolves to `2026-06-19`.** Flagging for orchestrator confirmation; if the
  orchestrator truly wants `2026-06-16`, it must say so explicitly (the plan recommends against).
- **[MEDIUM] Streaming-endpoint test ergonomics (Edit 5, `server.py:2135`).** The SSE path runs
  the persist in `run_in_executor`; driving it end-to-end in a `TestClient` and asserting the
  spy may be flaky on event-loop teardown. Mitigation: test 12 prefers the real endpoint but
  documents a direct-closure fallback so the `:2135` line is still diff-covered without a
  brittle SSE harness. Either way the line is covered.
- **[LOW] `bundle` shape variance across NONE/AUGMENT/FRESH/cache paths.** The robust getattr
  chain + `[]` floor is designed for exactly this; tests 2–3 lock the metadata-fallback and the
  `None`/empty cases. A bundle that is a non-namespace object without `metadata` still yields
  `[]` (nested `getattr(..., None)` tolerates the missing attr).
- **[LOW] Accidental over-cap.** Re-slicing `[:8]` would silently drop terms and contradict the
  spec. The plan explicitly forbids it and test 4 (12 terms in, 12 out) guards against it.
- **[LOW] Aliasing the bundle's list.** Passing the live `found_terms` list by reference could
  let a downstream mutate the persisted value. Mitigated by `_keywords_from_bundle` returning a
  fresh `[str(t) for t in terms]` copy (immutable style).
- **[LOW] No-behavior-change regressions.** Retrieval/answer/citation code is untouched; test 11
  locks the `/ask` payload to a golden snapshot. Lock duration / DB-perf risk: none — one extra
  small JSONB value on an already-occurring INSERT; no new query, no migration.

## Out-of-scope boundaries (this unit)
- **Do NOT modify** `chats/service.py` (`append_turn`), `database/models.py` (`ChatTurn`),
  or `database/migrations/029_*.sql` — all FROZEN from WU-5B4 (#45). REUSE the
  `append_turn(keywords=...)` param and the `chat_turns.keywords` column as-is.
- **Do NOT touch `apollo/`** — this unit is purely the RAG `/ask` persist path.
- **No new package**, no new dependency, no env var.
- **No read path / no consumer.** Do NOT wire `chat_turns.keywords` into retrieval, the
  learner model, the router, memory summarization, or any analytics. It is write-only in v1.
  The eventual chat-evidence consumer (§10 "when chat evidence lands") and the `'chat_question'`
  event_kind (§2 L247) are explicitly future work.
- **Do NOT re-cap, re-filter, or re-order** the keyword list — trust the upstream
  `extract_and_filter_keywords` ≤8 bound.
- **Do NOT add the keywords to the USER turn** (`server.py:451`) — assistant turn only.
- **Do NOT apply any migration** to any remote Supabase project (test or prod). This unit
  ships no migration at all; there is no deploy-handoff DDL.
- **No streaming-pipeline refactor** — only the single `keywords=` kwarg is added at the
  streaming happy-path call site (`:2135`); the SSE machinery is untouched.
- **Branch discipline:** work only on the already-checked-out
  `feat/apollo-kg-wu5b5-chat-keyword-wireup`; do NOT create/switch branches, push, or open PRs.

## Deviations I'd allow the executor
- **Inline vs helper for the getattr chain.** If `_keywords_from_bundle` feels like overkill,
  the executor MAY inline the `getattr(bundle,'found_terms',None) or getattr(getattr(bundle,
  'metadata',None),'found_terms',None) or []` chain at both call sites — PROVIDED both sites
  stay identical and both are diff-covered. (Helper is preferred: DRY + one place to test.)
- **Test file split.** The unit + integration tests MAY live in one file or be split as
  proposed; names MAY differ. The REQUIRED assertions are: happy-path threads `found_terms`;
  bundle-without-`found_terms`/None/AUGMENT/cache -> `[]`; error-path turn -> `[]`; user-turn
  unchanged (no keywords); `/ask` answer+citations payload unchanged. All must be REAL
  (no skip/xfail), LLM/network mocked.
- **`list(keywords) if keywords else None` vs `keywords`.** The executor MAY pass `keywords`
  through directly and rely on `append_turn`'s `keywords or []`, but the defensive-copy form is
  preferred (immutable style; no bundle-list aliasing).
- **Streaming test approach** — real SSE endpoint vs direct-closure fallback (see test 12),
  executor's choice, as long as `server.py:2135` is diff-covered.
- **NOT negotiable:** scope files (server.py + the two owner docs + tests only); no
  package; no migration; no remote DB; `append_turn`/`ChatTurn`/029 frozen; `last_verified`
  stays `2026-06-19`; keywords write-only on the assistant turn; no re-cap at 8.
