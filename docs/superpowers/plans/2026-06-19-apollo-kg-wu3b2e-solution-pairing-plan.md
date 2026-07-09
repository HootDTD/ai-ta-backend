# WU-3B2e — FIND-OR-GENERATE (stage 2) + PAIRING/CORRECTNESS GATE (stage 3) — TDD implementation plan

**Date:** 2026-06-19
**Unit:** WU-3B2e (the §8B "solution lifecycle"). LLM stage 2 + stage 3.
**Branch (already checked out):** `feat/apollo-kg-wu3b2e-solution-pairing` — do NOT create/switch/push/PR.
**Compare branch (diff-cover):** `feat/apollo-kg-wu3b2d-scrape-tagmint`.
**Planner:** feller-planner-ai-pipeline.
**Spec authority:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §8B.2 stages 2-3 (~1268-1282), §8B.3 backstops + the recorded fabricated-solution assumption (1326-1330), §1.8 line 153 (auto-provisioning into v1). Split scope PINNED in `docs/superpowers/plans/2026-06-19-apollo-kg-wu3b2-split-proposal.md` §WU-3B2e + §9 OPS-6.
**Deliverable of THIS file:** the plan only. No code.

---

## 1. Goal & architecture (one line each)

**Goal:** For each scraped `CandidateQuestion`, retrieve-first-then-RAG-generate a reference solution
(`solution_source='extracted'|'generated'`) and validate the `(question, solution)` pairing with a
two-phase span-grounded LLM judge that **fails CLOSED** — a parse failure, a bad pairing, or an
unfaithful claim REJECTS the pair (it never becomes teachable). PASS hands a `ReferenceSolutionDraft`
to the caller (3B2g) who assembles `tag_mint.ApprovedPair`; FAIL hands a `Rejection`.

**Architecture (pipeline-shape one-liner):**
`CandidateQuestion → find_or_generate (retrieve_fn first, else chat_fn RAG-generate) → ReferenceSolutionDraft → validate_pair (Phase A pairing + Phase B claim-faithfulness, judge_fn span-grounded) → PairingVerdict → {PASS: caller builds ApprovedPair | FAIL: Rejection}`.

**Tech stack:** pure async Python over INJECTED callables — `retrieve_fn` (retrieval/ hybrid pipeline),
`chat_fn` (`apollo.agent._llm.main_chat`), `judge_fn` (`apollo.agent._llm.cheap_chat`). Pydantic types,
stdlib `json`/`hashlib`. NO new package (#8). NO DB write in this unit (compute over injected fns); the
real-PG `db_session` fixture is used ONLY if a test needs the real retrieval seam (it does not — see §6).

This unit consumes 3B2d's `CandidateQuestion` (`apollo.provisioning.scrape`) and produces inputs the
caller turns into 3B2d's `ApprovedPair` (`apollo.provisioning.tag_mint` — already DEFINED; do NOT
redefine). It does NOT mint entities (3B2d) and does NOT promote/lint or write `rejected_problems` (3B2g).

## 2. Pipeline shape (where 3B2e sits)

```
[3B2d scrape]  → CandidateQuestion (apollo.provisioning.scrape, IMPORT — do not redefine)
   │
   ▼  solution.py
[find_or_generate]
   ├─ retrieve_fn(question)  → printed solution found in course corpus?
   │      YES → solution_source='extracted', provenance=retrieved spans
   │      NO  → chat_fn(question + retrieved grounding spans)  RAG-grounded generate
   │             → solution_source='generated', provenance=the SAME retrieved spans
   ▼
ReferenceSolutionDraft  (solution_source, provenance, reference steps, grounding)
   │
   ▼  pairing_gate.py
[validate_pair]   ── judge_fn sees the SAME retrieved grounding the generator used (span-grounded)
   ├─ Phase A: pairing / answer-relevance — "does THIS solution answer THIS question?"
   ├─ Phase B: claim-decomposed faithfulness — each solution claim entailed by the grounding?
   └─ FAIL-CLOSED: malformed/unparseable judge JSON OR not-paired OR any unfaithful claim ⇒ REJECT
   ▼
PairingVerdict (paired, faithful, failed_claims, confidence)
   │
   ├─ PASS  → caller (3B2g) assembles tag_mint.ApprovedPair{problem, search_space_id, solution_source, misconceptions}
   │           from (CandidateQuestion + ReferenceSolutionDraft); 3B2e MAY provide build_approved_pair() helper
   └─ FAIL  → Rejection (THIS unit defines it); 3B2g writes apollo_rejected_problems(stage='pairing_gate')
```

Per-box owner / retry / failure mode:

| Box | Owner | Retry | Failure mode |
|---|---|---|---|
| `find_or_generate` | 3B2e `solution.py` | none in-unit (3B2f drains/retries the job) | retrieve_fn empty AND chat_fn unparseable ⇒ a `ReferenceSolutionDraft` cannot be built ⇒ raise `SolutionDraftError` (fail-closed; caller marks run failed). NEVER fabricate an empty-step draft. |
| `validate_pair` | 3B2e `pairing_gate.py` | none in-unit | judge_fn unparseable / not-paired / unfaithful ⇒ `PairingVerdict(paired=False…)` ⇒ caller builds a `Rejection`. The INVERSION of `leakage_judge`'s fail-OPEN. |
| ApprovedPair assembly | caller (3B2g) using 3B2e `build_approved_pair` helper | n/a | the assembled dict must `Problem.model_validate` — a round-trip test pins compat with `tag_mint.ApprovedPair`. |

## 3. Ground truth re-verified (file:line)

All anchors checked against the live `feat/apollo-kg-wu3b2e-solution-pairing` working tree.

- **The fail-OPEN pattern to INVERT** — `apollo/agent/leakage_judge.py:101-129`: `llm_leakage_judge`
  catches a parse error (`except Exception` :126) and returns `JudgeVerdict(leaks=False, …confidence=0.0)`
  — i.e. it lets content THROUGH on a glitch (the §6 leakage gate prefers false-negatives). Tests
  `test_malformed_json_soft_fails_open` / `test_openai_exception_soft_fails_open`
  (`apollo/agent/tests/test_leakage_judge.py:86-98,123-133`) pin that. **3B2e MUST do the opposite:** on
  a parse error / exception the verdict is `paired=False` (REJECT). The structure (try → `cheap_chat` →
  `json.loads` → except) is the template; only the except-branch's default flips.
- **The injected-judge mock template** — `apollo/agent/tests/test_leakage_judge.py:31-36`:
  `patch("…leakage_judge.cheap_chat", return_value=json.dumps(payload))`. 3B2e injects `judge_fn`/`chat_fn`
  as PARAMS (not module-level patches) — the cleaner 3B2c/3B2d convention (`_chat_returning`,
  `test_tag_mint.py:58-65`), so tests pass `judge_fn=_judge_returning({...})` directly. NO network.
- **`ApprovedPair` is ALREADY DEFINED — IMPORT, do not redefine** — `apollo/provisioning/tag_mint.py:77-85`:
  `ApprovedPair(BaseModel){problem: dict, search_space_id: int, solution_source: str, misconceptions: list[dict]=[]}`.
  The docstring (`:79-80`) + `test_tag_mint.py:178` fix `solution_source ∈ {'extracted','generated'}`. The
  `problem` dict must be `Problem`-validatable (it combines the question + the reference_solution). 3B2e's
  `build_approved_pair` produces exactly this; `tag_and_mint` then consumes it (3B2g wiring).
- **`CandidateQuestion` shape (3B2d, IMPORT)** — `apollo/provisioning/scrape.py:69-84`:
  `{problem_text:str, given_values:dict[str,float], target_unknown:str, difficulty:Difficulty,
  document_id:int, page:int|None, chunk_content_hash:str, concept_slug:str}`. 3B2e reads these to build
  the `problem` dict and the retrieval query. It is a `pydantic.BaseModel`.
- **The `Problem` schema the assembled pair must satisfy** — `apollo/schemas/problem.py:33-100`:
  `EntryType = Literal['equation','definition','condition','simplification','variable_mapping','procedure_step']`;
  `Problem{id, concept_id, difficulty, problem_text, given_values: Dict[str,float], target_unknown,
  reference_solution: List[ReferenceStep] (min_length=1)}` with `ReferenceStep{step≥1, entry_type, id,
  content: dict, depends_on: list}`. The `reference_solution` 3B2e produces must satisfy these validators
  (depends_on resolution :61, procedure_step order 1..N contiguous :87-98) for the ApprovedPair round-trip
  test to pass. `Difficulty = Literal['intro','standard','hard']` (:37).
- **`retrieve_fn` real shape (for the production wiring note + the injected stub contract)** —
  `retrieval/pipeline.py:30-99`: `async retrieve_for_question(query, keywords, search_space_id, db_session,
  …) -> tuple[list[BundleSnippet], dict]`. 3B2e injects `retrieve_fn` as a NARROW async callable
  `Callable[..., Awaitable[Sequence[GroundingSpan]]]` (a thin adapter over `retrieve_for_question` that
  returns just the snippet texts) so Tier-1 mocks it with a plain list — NO `BundleSnippet`/DB coupling
  leaks into the unit. The adapter wiring is 3B2g's; 3B2e only defines the callable shape + a tiny
  `GroundingSpan` value type (text + provenance) it consumes.
- **The LLM seam returns `str` only** — `apollo/agent/_llm.py:51-95`: `cheap_chat`/`main_chat` are SYNC,
  return `resp.choices[0].message.content or ""`, accept `purpose=`/`messages=`/`response_format=`. 3B2e's
  `chat_fn`/`judge_fn` are injected SYNC `Callable[..., str]` (same convention as 3B2c/3B2d). Cost metering
  is 3B2f's `MeteredChat` — NOT this unit (these stay plain `str`-returning fns).
- **The package re-export surface** — `apollo/provisioning/__init__.py:1-55`: a flat re-export `__init__`.
  3B2e ADDS its public names here (mirrors the 3B2c/3B2d additions :22-53).
- **Test infra** — `apollo/conftest.py:24` re-exports `db_session`/`_pg_url` (real-PG, savepoint rollback,
  Docker-skip). `pytest.ini` sets `asyncio_mode=auto` (`test_tag_mint.py:49-50`), so async tests need no
  mark. THIS unit's tests are PURE (mocked fns, no DB) so they are plain sync/async unit tests that never
  request `db_session` — they run green without Docker (stronger than the 3B2c/3B2d real-PG gate).

## 4. Public signatures (the seam contract)

Backward-compat constraint: 3B2e ADDS modules + re-exports; it does NOT edit `scrape.py`/`tag_mint.py`/
`dedup.py`/`promotion_lint.py`/`_llm.py`. `ApprovedPair`/`CandidateQuestion` are IMPORTED unchanged.

### `apollo/provisioning/solution.py` (NEW)

```python
GroundingSpan = pydantic BaseModel | frozen dataclass:
    text: str                       # the retrieved passage text (NO PII — course material only)
    document_id: int | None = None
    page: int | None = None
    chunk_content_hash: str | None = None   # provenance, mirrors scrape's key

class ReferenceSolutionDraft(BaseModel):           # the stage-2 output type
    solution_source: Literal["extracted", "generated"]
    reference_solution: list[dict]                 # ReferenceStep-shaped dicts (Problem-validatable)
    grounding: tuple[GroundingSpan, ...]           # the SPANS used (extracted source OR generate context)
    provenance: dict                               # {document_id, page, chunk_content_hash, retrieval_hits:int}

class SolutionDraftError(RuntimeError):            # fail-CLOSED: cannot build a draft without guessing
    ...

async def find_or_generate(
    db,                                            # accepted for signature parity / future reads; unused in v1 compute
    question,                                      # apollo.provisioning.scrape.CandidateQuestion (duck-typed)
    *,
    retrieve_fn: Callable[..., Awaitable[Sequence[GroundingSpan]]],
    chat_fn: Callable[..., str],                   # main_chat-shaped, MOCKED Tier-1
) -> ReferenceSolutionDraft: ...
```

Contract: call `retrieve_fn(question)` first. If a retrieved span carries a printed/worked solution
(detected by a deterministic marker the retrieve adapter sets, OR a `chat_fn` extraction pass over the
retrieved spans returning a non-empty parseable `reference_solution`) → `solution_source='extracted'`,
`grounding=` the retrieved spans. Else `chat_fn(question + retrieved-spans-as-context)` RAG-generate →
`solution_source='generated'`, `grounding=` the SAME retrieved spans (so Phase B has real context to
entail against — the §8B.2 "ground both in retrieved passages" requirement). A malformed/empty
generate response with NO usable extracted solution ⇒ raise `SolutionDraftError` (never an empty-step
draft — `Problem` requires `reference_solution` min_length=1).

### `apollo/provisioning/pairing_gate.py` (NEW)

```python
class PairingVerdict(BaseModel):                   # the stage-3 output type
    paired: bool                                   # Phase A: solution answers THIS question
    faithful: bool                                 # Phase B: every claim entailed by grounding
    failed_claims: tuple[str, ...]                 # the unentailed claims (empty on PASS)
    confidence: float                              # clamped [0,1]
    # convenience: approved == (paired and faithful)

class Rejection(BaseModel):                        # the FAIL handoff (3B2g writes the rejected_problems row)
    stage: Literal["pairing_gate"] = "pairing_gate"
    reason: str                                    # 'unparseable_judge' | 'not_paired' | 'unfaithful_claims'
    diagnostic: str
    failed_claims: tuple[str, ...] = ()

async def validate_pair(
    question,                                      # CandidateQuestion (duck-typed)
    draft: ReferenceSolutionDraft,
    *,
    retrieve_fn: Callable[..., Awaitable[Sequence[GroundingSpan]]],   # to re-ground the judge identically
    judge_fn: Callable[..., str],                  # cheap_chat-shaped, MOCKED Tier-1
) -> PairingVerdict: ...

def rejection_from_verdict(verdict: PairingVerdict) -> Rejection | None:
    # None when approved; else a typed Rejection. The single fail-mapping point.
    ...
```

Contract: the judge sees the SAME grounding the generator used — prefer `draft.grounding`; if the gate
is asked to re-ground it calls `retrieve_fn(question)` and uses the union (span-grounded, both phases).
Phase A first (cheaper short-circuit): if `judge_fn` Phase-A response says not-paired ⇒
`PairingVerdict(paired=False, faithful=False, …)`. Else Phase B: `judge_fn` decomposes the solution into
claims and marks each entailed/not; ANY unentailed claim ⇒ `faithful=False` with `failed_claims` set.
**FAIL-CLOSED:** a malformed/non-JSON/exception judge response at EITHER phase ⇒
`PairingVerdict(paired=False, faithful=False, failed_claims=("<unparseable judge response>",),
confidence=0.0)` — the inversion of `leakage_judge`'s fail-open.

### `apollo/provisioning/solution.py` — caller helper (the ApprovedPair builder)

```python
def build_approved_pair(question, draft: ReferenceSolutionDraft) -> "tag_mint.ApprovedPair":
    # Assembles the Problem-validatable `problem` dict from (question + draft.reference_solution),
    # stamps solution_source from the draft, misconceptions=[] (3B2d/3B2g may enrich later),
    # search_space_id resolved by the caller (passed in). Returns the REAL tag_mint.ApprovedPair
    # (IMPORT it — do NOT mock/redefine). Round-trip-validated against Problem.model_validate.
```
`build_approved_pair(question, draft, *, search_space_id)` — `search_space_id` is a kwarg the caller
(3B2g) supplies from the job scope.

### Package re-exports (`apollo/provisioning/__init__.py`, EDIT — additive)

Add: `GroundingSpan`, `ReferenceSolutionDraft`, `SolutionDraftError`, `find_or_generate`,
`build_approved_pair`, `PairingVerdict`, `Rejection`, `validate_pair`, `rejection_from_verdict`
(and to `__all__`). Mirrors the 3B2c/3B2d additions.

## 5. Idempotency

This unit is PURE COMPUTE over injected callables — it writes NO database rows and holds NO state. So
idempotency is a property of its OUTPUTS, not of a persisted side-effect (the 3B2g orchestrator owns the
ON-CONFLICT Tier-1 write + the run-level no-op; see split-proposal §SPEC-1/OPS-2).

- **Idempotency key (declared for the caller):** `(chunk_content_hash, solution_hash)` where
  `solution_hash = sha256(normalize(reference_solution canonical-JSON))` — both `find_or_generate` and
  `validate_pair` are deterministic given the same inputs + the same (deterministically-mocked, or
  `temperature=0`) LLM responses. The `chunk_content_hash` comes from `CandidateQuestion` unchanged
  (3B2d's content key, survives a re-index). 3B2e exposes `solution_hash(draft) -> str` so 3B2g can key
  the downstream write on `(chunk_content_hash, solution_hash)`.
- **Duplicate handling:** a second `find_or_generate`/`validate_pair` on the same input recomputes and
  returns an EQUAL draft/verdict (no double-append, no counter, no string-concat). Re-running is safe and
  side-effect-free — 3B2g's ON-CONFLICT write absorbs the duplicate downstream.
- **Partial-progress recovery:** there is no intra-unit checkpoint to corrupt — if the worker crashed
  between `find_or_generate` and `validate_pair`, the next invocation re-runs both from the
  `CandidateQuestion` (path-independent, content-hash-keyed). No mutation of `question` or `draft` inputs
  (immutable style: builds new objects, frozen `GroundingSpan`).
- **Anti-patterns explicitly avoided:** no auto-increment id as a key (uses content hashes); no string
  append (builds a fresh `reference_solution` list each call); no counter increment; the only persistence
  (ON CONFLICT) is the CALLER's, not this unit's.

## 6. Model & cost declaration

3B2e makes LLM calls ONLY through the injected `chat_fn`/`judge_fn`. It does NOT pick or hardcode a
model — it inherits the project defaults from `apollo/agent/_llm.py` (`main_chat` → `MAIN_MODEL` default
`gpt-4o`; `cheap_chat` → `APOLLO_CHEAP_MODEL` default `gpt-4o-mini`). This MATCHES the apollo subsystem's
established tier convention (`_llm.py:23-24`, `leakage_judge` uses `cheap_chat`); no deviation. The repo
CLAUDE.md pins `GPT-4o via MAIN_MODEL` for the apollo/QA path — 3B2e conforms.

| Call | Tier / model (injected) | Input est. | Output est. | Routing rationale |
|---|---|---|---|---|
| Generate reference solution (stage 2, only on the `generated` branch) | `main_chat` → `gpt-4o` | question + ~3-6 retrieved spans (~1.5-3k tok) | ~400-800 tok | reasoning call — must produce a `Problem`-valid `reference_solution` |
| Extract solution from retrieved spans (stage 2, `extracted` branch) | `main_chat` → `gpt-4o` | ~1.5-3k tok | ~400 tok | structured extraction; reuse the reasoning tier |
| Phase A pairing judge (stage 3) | `cheap_chat` → `gpt-4o-mini` | question + solution + grounding (~2-4k tok) | ~150 tok | a yes/no relevance verdict — cheap tier suffices |
| Phase B faithfulness judge (stage 3) | `cheap_chat` → `gpt-4o-mini` | claims + grounding (~2-4k tok) | ~300 tok | claim-by-claim entailment — cheap tier |

**Per-question worst case:** 1 main_chat generate + 2 cheap_chat judges (extracted branch skips one
generate; Phase B may short-circuit if Phase A fails). **Volume is bounded by 3B2f's
`PER_DOCUMENT_TOKEN_CEILING`** (the metered abort) — 3B2e itself does NOT meter (out of scope; 3B2f wraps
`chat_fn`/`judge_fn` with `MeteredChat`). **Cost is therefore declared as per-question, attributed
upstream:** at gpt-4o $2.50/$10 per-M-in/out and gpt-4o-mini $0.15/$0.60, a worst-case question is
~$0.012 (generate) + ~$0.001 (two cheap judges) ≈ **$0.013/question**. A 200-question document ≈ **$2.6**,
well under a per-document ceiling 3B2f sets (recommend $5-10/doc — an OPEN-DECISION owned by 3B2f #7).

**Budget ceiling:** there is no per-unit dollar ceiling in CLAUDE.md; the governing ceiling is 3B2f's
`PER_DOCUMENT_TOKEN_CEILING`. 3B2e's contract is to make exactly the calls above and no retries (retry is
the queue-drain's job), so its cost is deterministic given the routing. **No model deviation to justify.**

## 7. Failure paths (fail-CLOSED — the load-bearing safety property)

This is the unit's reason for existing. The §6 `leakage_judge` fails OPEN (parse glitch → let content
through); 3B2e INVERTS that: any uncertainty REJECTS.

**Per external call:**

| Call | On parse failure / exception | On a "bad" verdict | Where it lands |
|---|---|---|---|
| `find_or_generate` generate (`chat_fn`) | no usable extracted solution + unparseable generate ⇒ raise `SolutionDraftError` | n/a (no draft produced) | caller (3B2g) marks the run failed + writes `apollo_ingest_errors(stage='find_or_generate')` |
| `validate_pair` Phase A (`judge_fn`) | `PairingVerdict(paired=False, faithful=False, failed_claims=("<unparseable judge response>",), confidence=0.0)` | not-paired ⇒ `paired=False` | caller builds `Rejection(reason='unparseable_judge'/'not_paired')` → `apollo_rejected_problems(stage='pairing_gate')` |
| `validate_pair` Phase B (`judge_fn`) | same fail-closed verdict | any unfaithful claim ⇒ `faithful=False`, `failed_claims` set | `Rejection(reason='unfaithful_claims')` → `apollo_rejected_problems(stage='pairing_gate')` |

- **Retry policy (in-unit):** NONE. Retries/backoff are the 3B2f queue-drain's job (attempt_count +
  lease + dead-letter). 3B2e makes each call once at `temperature=0`; a transient failure surfaces as a
  fail-closed reject/raise and the drain re-claims the job. This keeps the unit pure + deterministic.
- **Fallback after a "failure":** reject (never approve). There is no "best effort" approval. A
  `SolutionDraftError` aborts the question; a fail-closed `PairingVerdict` rejects the pair.
- **DLQ / error surface:** 3B2e does NOT own a table. It hands typed values (`Rejection`,
  `SolutionDraftError`) UP; 3B2g maps them to `apollo_rejected_problems`(pairing fails) /
  `apollo_ingest_errors`(generate failures) and the run/job terminal status. This unit's job is to make
  the reject DECISION unambiguous and typed.
- **Observability:** `validate_pair` emits a structured `_LOG.info("pairing_verdict", extra={...paired,
  faithful, n_failed_claims, confidence, solution_source...})` per call (the `_llm.py:_log_call` /
  `personalized_selection` convention — one structured line). NO solution text, NO PII in the log (only
  counts + booleans). A fail-closed `unparseable_judge` logs at WARNING (mirrors
  `leakage_judge.py:127`), so a spike of unparseable-judge rejects is greppable (alert threshold: a
  human watches the `n_rejected`/`reason='unparseable_judge'` ratio in the 3B2g run aggregate during
  calibration — if it dominates, the judge prompt/model is broken, not the content).
- **The §1.8 / OPS-6 caveat (record it in the module docstring + apollo.md):** a coherent-but-WRONG
  solution that PASSES Phase A + Phase B + all 8 gates IS shown to students as teachable (shadow
  diagnostic, NO Layer-3 belief movement — `APOLLO_GRAPH_SIM_LAYER3_ENABLED` OFF). The pairing gate
  REDUCES but does NOT eliminate this. The real pre-exposure safety is the `APOLLO_AUTOPROVISION_ENABLED`
  flag-OFF default + the §6.7 calibration gate; the post-exposure catch is 3B2h's quarantine
  (RETROACTIVE). 3B2e must NOT claim to prevent the fabricated-coherent-wrong case.

## 8. Security check

- **API keys from env only.** 3B2e never touches a key — `chat_fn`/`judge_fn`/`retrieve_fn` are injected;
  the only place an `OpenAI()` client is constructed is `apollo/agent/_llm.py:27-28` (`OpenAI()` reads
  `OPENAI_API_KEY` from env). No new client, no key in any 3B2e file. PASS.
- **No secrets in code / rows / logs.** This unit writes no rows. Its log line carries only counts +
  booleans + `solution_source` — NO solution text, NO question text, NO retrieved-passage text, NO key.
  PASS.
- **No PII in LLM inputs.** The inputs are COURSE MATERIAL (the scraped question, the retrieved course
  passages) + the generated solution — academic content, no student identifiers. The unit takes NO
  `user_id` and reads NO learner state (unlike the grading path). `GroundingSpan.text` is course-material
  text. No retention/deletion policy needed beyond the course-material lifecycle the upload pipeline
  already owns. PASS — but the module docstring states explicitly "inputs are course material only; this
  unit never sees student PII."
- **Service-role / client-reachable:** this is server-side worker-path code (3B2g's worker drains it),
  never client-reachable. No RLS surface (no table). PASS.
- **Input validation at the boundary:** `find_or_generate` validates the generated `reference_solution`
  by attempting `Problem`-shaped construction before returning a draft (a malformed generate ⇒
  `SolutionDraftError`, not a half-built draft). `validate_pair` validates the judge JSON (fail-closed on
  invalid). Never trusts the LLM output shape. PASS.

## 9. Structural prep (neighborhood scan)

Scanned the change-path artifacts + one ring out (`apollo/provisioning/__init__.py` is the only EDITED
file; `scrape.py`/`tag_mint.py`/`leakage_judge.py` are read-only dependencies / pattern sources).

| Artifact | Lines | Direct imports | Verdict |
|---|---|---|---|
| `apollo/provisioning/__init__.py` (EDIT) | 54 | flat re-export | clean — additive only; will grow ~10 lines |
| `apollo/provisioning/scrape.py` (import only) | 292 | 12 | within thresholds |
| `apollo/provisioning/tag_mint.py` (import only) | 301 | 10 | within thresholds |
| `apollo/agent/leakage_judge.py` (pattern source, NOT edited) | 151 | 7 | within thresholds |
| `apollo/provisioning/promotion_lint.py` | 383 | — | sibling; not in change path |

**Result: neighborhood is CLEAN.** No file exceeds the 800-line / >8-import / >5-responsibility
thresholds. The two new modules are deliberately SMALL + single-responsibility (`solution.py` ≈ stage 2
+ the ApprovedPair builder ≈ ~180 lines; `pairing_gate.py` ≈ stage 3 + the fail-mapping ≈ ~160 lines),
matching the repo's "many small files" rule and the 3B2c/3B2d sibling sizes. No structural-prep step is
required before the feature work.

- [ ] None — neighborhood is clean.
- Verify: `wc -l apollo/provisioning/solution.py apollo/provisioning/pairing_gate.py` (each < 250 lines
  after build); `grep -cE "^(from|import) " apollo/provisioning/solution.py` (< 9).

## 10. Files to create / edit

**Prior art copied (document 2-3 references):**
- `apollo/agent/leakage_judge.py:101-129` + `tests/test_leakage_judge.py:86-98` — the LLM-judge
  try→parse→except shape. 3B2e copies the structure and INVERTS the except-branch default (fail-open →
  fail-closed). The canonical pattern this unit exists to mirror-and-invert.
- `apollo/provisioning/tag_mint.py` + `tests/test_tag_mint.py` — the injected-`chat_fn` convention
  (`_chat_returning`, `_parse_tag` fail-closed `TagMintError`, `ApprovedPair` shape, the
  `test_approvedpair_and_mintplan_shapes` round-trip). 3B2e's `SolutionDraftError`/`Rejection` mirror
  `TagMintError`'s NO-FALLBACK convention.
- `apollo/provisioning/dedup.py` + `tests/test_dedup.py` — the injected-`judge_fn`/`embed_fn` SYNC-stub
  convention + the deterministic-stub test idiom; and the package re-export `__init__.py` pattern.

**Create:**
- `apollo/provisioning/solution.py` (NEW) — `GroundingSpan`, `ReferenceSolutionDraft`,
  `SolutionDraftError`, `find_or_generate`, `solution_hash`, `build_approved_pair`. Module docstring:
  the §1.8/OPS-6 caveat + "inputs are course material only, no student PII".
- `apollo/provisioning/pairing_gate.py` (NEW) — `PairingVerdict`, `Rejection`, `validate_pair`,
  `rejection_from_verdict`. Module docstring: the FAIL-CLOSED convention (the explicit inversion of
  `leakage_judge`'s fail-open, with the file:line reference).
- `apollo/provisioning/tests/test_solution.py` (NEW) — Tier-1, mocked `chat_fn`/`retrieve_fn`, no DB.
- `apollo/provisioning/tests/test_pairing_gate.py` (NEW) — Tier-1, mocked `judge_fn`/`retrieve_fn`, the
  fail-closed safety tests, no DB.

**Edit:**
- `apollo/provisioning/__init__.py` (EDIT, additive ~10 lines) — re-export the new public names + extend
  `__all__`.
- `docs/architecture/apollo.md` (EDIT, owner doc) — register `solution.py`/`pairing_gate.py` in the
  module map + Public interfaces, the fail-closed convention, the §1.8/OPS-6 caveat;
  `last_verified: 2026-06-19`.

**Explicitly NOT edited:** `scrape.py`, `tag_mint.py`, `dedup.py`, `promotion_lint.py`, `_llm.py`,
`leakage_judge.py`, any migration, any model, `problem.py`. No new package.

## 10b. Out-of-scope confirmation for the file set
No `apollo_*` write (no ORM, no migration); no `done.py`/grading-core touch; no minting (3B2d); no
promote/lint (3B2g); no queue/metering (3B2f); no quarantine (3B2h).

## 11. TDD step-by-step (RED → GREEN, tests FIRST)

Each step writes REAL tests FIRST (they fail to import / fail red), then the minimal implementation to
GREEN. No skip/xfail/assert-nothing. Mocks are deterministic injected callables (no network, no DB).

- [ ] **Step 0 — pin the seam imports (RED first).** Write `test_solution.py` + `test_pairing_gate.py`
  top-matter importing the not-yet-existing public names from `apollo.provisioning.solution` /
  `apollo.provisioning.pairing_gate` AND the REAL `apollo.provisioning.tag_mint.ApprovedPair` +
  `apollo.provisioning.scrape.CandidateQuestion`. Run: RED (ImportError on the new modules).
  - Verify: `.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_solution.py -x` → ImportError.

- [ ] **Step 1 — `GroundingSpan` + `ReferenceSolutionDraft` types + `solution_hash` (stage-2 shell).**
  Tests: `test_reference_solution_draft_shape`, `test_solution_hash_deterministic`. Implement the Pydantic
  types + `solution_hash`. GREEN.

- [ ] **Step 2 — `find_or_generate` extracted branch.** Test `test_find_or_generate_extracted_branch`
  (retrieve_fn returns a span carrying a printed solution → `solution_source='extracted'`, grounding =
  the retrieved spans, NO generate call). Implement the retrieve-first path. GREEN.

- [ ] **Step 3 — `find_or_generate` generated branch.** Test `test_find_or_generate_generated_branch`
  (retrieve_fn returns context spans but NO printed solution → `chat_fn` RAG-generate →
  `solution_source='generated'`, grounding carries the SAME retrieved spans). Implement the fallback.
  GREEN. ALSO `test_generated_branch_carries_retrieved_grounding` (Phase B precondition).

- [ ] **Step 4 — `find_or_generate` fail-closed.** Test `test_find_or_generate_unparseable_generate_raises`
  (retrieve empty + chat_fn returns non-JSON garbage → `SolutionDraftError`, NOT an empty-step draft).
  Implement the raise. GREEN. This is a fail-closed property at stage 2.

- [ ] **Step 5 — `PairingVerdict` + `Rejection` + `rejection_from_verdict` (stage-3 shell).** Tests:
  `test_pairing_verdict_shape`, `test_rejection_from_verdict_none_on_approved`,
  `test_rejection_from_verdict_typed_on_fail`. Implement types + the mapping. GREEN.

- [ ] **Step 6 — `validate_pair` happy path (paired + faithful).** Test
  `test_validate_pair_approves_good_pair` (Phase A says paired, Phase B says all claims entailed →
  `paired=True, faithful=True, failed_claims=()`). Implement the two-phase flow over `judge_fn`. GREEN.

- [ ] **Step 7 — `validate_pair` Phase A reject (mispairing — the failure it exists to catch).** Test
  `test_validate_pair_rejects_mispaired_solution` (Phase A judge says the solution answers a DIFFERENT
  question → `paired=False`; Phase B is short-circuited/irrelevant). GREEN.

- [ ] **Step 8 — `validate_pair` Phase B reject (unfaithful claim).** Test
  `test_validate_pair_rejects_unfaithful_claim` (Phase A paired, Phase B marks one claim NOT entailed →
  `faithful=False`, `failed_claims=(that claim,)`). GREEN.

- [ ] **Step 9 — THE LOAD-BEARING FAIL-CLOSED test.** Tests
  `test_validate_pair_fails_closed_on_unparseable_judge` (judge_fn returns non-JSON → `paired=False,
  faithful=False, confidence=0.0`, NOT an approval) and `test_validate_pair_fails_closed_on_judge_exception`
  (judge_fn raises → same). Implement the inverted except-branch. GREEN. **This is the mutation the
  orchestrator will revert (fail-closed → fail-open) to prove the test RED-discriminates.**

- [ ] **Step 10 — span-grounding (judge sees the generator's grounding).** Test
  `test_validate_pair_judge_sees_same_grounding` (assert the grounding text passed to `judge_fn` equals
  `draft.grounding` — captured via a recording stub). GREEN. Pins the "judge uses the SAME context the
  generator used" failure-mode defense.

- [ ] **Step 11 — `build_approved_pair` round-trip against the REAL tag_mint.ApprovedPair.** Test
  `test_build_approved_pair_validates_against_tag_mint` (build from an approved (question, draft) →
  IMPORT the REAL `tag_mint.ApprovedPair`, assert the result IS one + its `problem` dict passes
  `apollo.schemas.problem.Problem.model_validate`; assert `solution_source` carried from the draft). And
  `test_build_approved_pair_extracted_vs_generated_source` (the source discriminates). GREEN. Do NOT mock
  `ApprovedPair`/`Problem`.

- [ ] **Step 12 — re-export surface.** Test `test_solution_pairing_public_api_reexport` (the
  `apollo.provisioning.<name>` paths resolve to the SAME objects as the modules). Edit `__init__.py`.
  GREEN. DISCRIMINATING: dropping a re-export REDs it (mirrors `test_tag_mint_public_api_reexport`).

- [ ] **Step 13 — coverage sweep.** Add the small branch-completing tests (§12 "coverage closers") so
  diff-cover ≥95%: the `confidence` clamp, the Phase-A short-circuit (Phase B not called when not paired),
  the extracted-branch detection edge, the empty-`failed_claims` path. GREEN.

- [ ] **Step 14 — owner-doc reconcile (same work).** Update `docs/architecture/apollo.md` (§13) +
  `last_verified: 2026-06-19`.

- [ ] **Step 15 — gate.** Run the full §14 verification (pytest + diff-cover ≥95% vs
  `feat/apollo-kg-wu3b2d-scrape-tagmint`).

## 12. Full test list (name + assertion + mocking)

**Shared deterministic stubs (NO network, NO DB)** — defined once per test module, mirroring
`test_tag_mint.py:58-65` / `test_dedup.py`:
- `_chat_returning(payload: dict | str)` → a sync `chat_fn` returning `json.dumps(payload)` (or a raw
  string for the non-JSON case).
- `_judge_returning(*phase_payloads)` → a sync `judge_fn` that returns successive JSON strings per call
  (Phase A then Phase B), or raises / returns garbage for the fail-closed cases. A RECORDING variant
  captures the messages/grounding it was handed (for the span-grounding assertion).
- `_retrieve_returning(spans)` → an async `retrieve_fn` returning a fixed `list[GroundingSpan]` (empty
  list for the generate branch; a span flagged as a printed solution for the extracted branch).
- `_candidate(...)` → a `CandidateQuestion` fixture (IMPORT the real type from `apollo.provisioning.scrape`).
- `_draft(...)` → a `ReferenceSolutionDraft` fixture with a `Problem`-valid `reference_solution`
  (bernoulli-shaped, copied from `test_tag_mint.py:120-165`).

### `test_solution.py`

| Test | Asserts | Mocking |
|---|---|---|
| `test_reference_solution_draft_shape` | `ReferenceSolutionDraft` pydantic round-trips; `solution_source` constrained to `extracted`/`generated`; bad source raises | none |
| `test_solution_hash_deterministic` | `solution_hash(draft) == solution_hash(equal draft)`; differs for a different `reference_solution` | none |
| `test_find_or_generate_extracted_branch` | retrieve_fn returns a span with a printed solution → `solution_source=='extracted'`, `grounding` == the retrieved spans, `provenance.retrieval_hits>0`; chat_fn NOT used for generation | `_retrieve_returning([printed_solution_span])`, `_chat_returning` (extraction pass returns the parsed solution) |
| `test_find_or_generate_generated_branch` | retrieve_fn returns context but NO printed solution → chat_fn RAG-generate → `solution_source=='generated'` | `_retrieve_returning([context_span])`, `_chat_returning(valid_solution_json)` |
| `test_generated_branch_carries_retrieved_grounding` | the `generated` draft's `grounding` equals the retrieved spans (so Phase B has real context); chat_fn was called with the span text in its context | recording `_chat_returning` |
| `test_find_or_generate_unparseable_generate_raises` | retrieve empty + chat_fn non-JSON → `SolutionDraftError` (NOT an empty-step draft) | `_retrieve_returning([])`, `_chat_returning("not json {")` |
| `test_find_or_generate_empty_reference_solution_raises` | chat_fn returns valid JSON but EMPTY `reference_solution` → `SolutionDraftError` (Problem requires min_length=1) | `_chat_returning({"reference_solution": []})` |
| `test_build_approved_pair_validates_against_tag_mint` | `build_approved_pair(q, draft, search_space_id=…)` returns the REAL `tag_mint.ApprovedPair`; its `problem` passes `Problem.model_validate`; `solution_source` carried | IMPORT real `ApprovedPair`/`Problem` — NOT mocked |
| `test_build_approved_pair_extracted_vs_generated_source` | an extracted draft → pair.solution_source=='extracted'; a generated draft → 'generated' (the two paths DISCRIMINATE) | two `_draft(...)` fixtures |
| `test_find_or_generate_provenance_records_chunk_hash` | `draft.provenance['chunk_content_hash'] == question.chunk_content_hash` (the idempotency key threads through) | stubs |

### `test_pairing_gate.py`

| Test | Asserts | Mocking |
|---|---|---|
| `test_pairing_verdict_shape` | `PairingVerdict` round-trips; `confidence` clamped to [0,1]; `failed_claims` is a tuple | none |
| `test_rejection_from_verdict_none_on_approved` | `rejection_from_verdict(paired&faithful verdict) is None` | none |
| `test_rejection_from_verdict_typed_on_fail` | a not-paired verdict → `Rejection(stage='pairing_gate', reason='not_paired')`; an unfaithful → `reason='unfaithful_claims'` with `failed_claims` carried | none |
| `test_validate_pair_approves_good_pair` | Phase A paired + Phase B all-entailed → `paired=True, faithful=True, failed_claims=()` | `_judge_returning({paired:true,...}, {claims:[{entailed:true},...]})` |
| `test_validate_pair_rejects_mispaired_solution` | **Phase A says the solution answers a DIFFERENT question → `paired=False`**; Phase B not consulted (short-circuit) | `_judge_returning({paired:false,...})`; assert judge called ONCE |
| `test_validate_pair_rejects_unfaithful_claim` | Phase A paired, Phase B marks one claim NOT entailed → `faithful=False`, the claim in `failed_claims` | `_judge_returning({paired:true}, {claims:[{entailed:true},{entailed:false, claim:"X"}]})` |
| **`test_validate_pair_fails_closed_on_unparseable_judge`** | **judge_fn returns non-JSON → `paired=False, faithful=False, confidence=0.0`, failed_claims carries the unparseable marker — NEVER an approval.** THE load-bearing safety test (inverts leakage_judge fail-open). | `_judge_returning("garbage {")` |
| **`test_validate_pair_fails_closed_on_judge_exception`** | judge_fn raises → same fail-closed verdict (not an approval) | `judge_fn` `side_effect=RuntimeError("API down")` |
| `test_validate_pair_fails_closed_on_phase_b_unparseable` | Phase A paired, Phase B unparseable → `faithful=False` (the fail-closed default applies per-phase, not just Phase A) | `_judge_returning({paired:true}, "garbage")` |
| `test_validate_pair_judge_sees_same_grounding` | the grounding text handed to judge_fn equals `draft.grounding` text (span-grounded; judge uses the generator's context) | recording `_judge_returning` |
| `test_validate_pair_confidence_clamped` | a judge confidence of 5.7 → clamps to 1.0; -0.4 → 0.0; non-numeric → 0.0 (mirrors leakage_judge) | `_judge_returning({paired:true, confidence:5.7})` |
| `test_solution_pairing_public_api_reexport` | `from apollo.provisioning import find_or_generate, validate_pair, ReferenceSolutionDraft, PairingVerdict, Rejection, SolutionDraftError, build_approved_pair, rejection_from_verdict` resolve to the SAME module objects. DISCRIMINATING: drop a re-export → REDs | none |

**Coverage closers (Step 13):** `test_validate_pair_phase_b_not_called_when_not_paired` (asserts the
Phase-A short-circuit branch — judge invoked exactly once when not paired), `test_grounding_span_optional_provenance`
(a `GroundingSpan` with only `text` constructs), `test_extracted_branch_no_solution_marker_falls_to_generate`
(retrieve returns spans none flagged as a solution → falls to the generate branch — the branch boundary).

**Independent-mutation discipline (orchestrator runs these to validate the suite):**
- Revert the fail-CLOSED default (parse failure → approve) ⇒ `test_validate_pair_fails_closed_on_*`
  MUST go RED. This is the pinned safety mutation.
- Collapse `solution_source` to a constant ⇒ `test_find_or_generate_extracted_branch` /
  `test_find_or_generate_generated_branch` / `test_build_approved_pair_extracted_vs_generated_source`
  discriminate (each path is asserted).
- Drop the span-grounding (judge re-retrieves instead of using `draft.grounding`) ⇒
  `test_validate_pair_judge_sees_same_grounding` REDs.
- Mock `tag_mint.ApprovedPair`/`Problem` instead of importing the real ones ⇒ forbidden; the round-trip
  test imports the REAL types so it catches a shape drift in `tag_mint`.

## 13. Owner-doc updates (drift contract)

Owner doc: `docs/architecture/apollo.md` (its `owns:` includes `apollo/**`). Reconcile in the SAME
commit; set `last_verified: 2026-06-19` (line 13).

- **Module map (line 39, the `apollo/provisioning/` row):** append the two new files to the file list
  (`solution.py`, `pairing_gate.py`) and a **WU-3B2e** clause describing: `find_or_generate` (retrieve-
  first then RAG-grounded generate; `solution_source` extracted/generated; `SolutionDraftError`
  fail-closed); `validate_pair` (two-phase span-grounded — Phase A pairing, Phase B claim-faithfulness;
  **FAIL-CLOSED, the explicit inversion of `leakage_judge`'s fail-open at `leakage_judge.py:126-129`** —
  a parse failure / not-paired / unfaithful claim REJECTS); `build_approved_pair` (assembles the REAL
  `tag_mint.ApprovedPair`, Problem-validated); injected `retrieve_fn`/`chat_fn`/`judge_fn` (network-free
  Tier-1). State it does NOT mint (3B2d), promote/lint (3B2g), meter/queue (3B2f), or write any row.
- **Public interfaces (after line 80, the 3B2d entries):** add bullet lines for
  `apollo.provisioning.find_or_generate(...) -> ReferenceSolutionDraft`,
  `apollo.provisioning.validate_pair(...) -> PairingVerdict`,
  `apollo.provisioning.build_approved_pair(...) -> tag_mint.ApprovedPair`, and the types
  (`ReferenceSolutionDraft`, `GroundingSpan`, `PairingVerdict`, `Rejection`, `SolutionDraftError`), each
  noting fail-closed + the re-export.
- **Non-obvious conventions (line 141 section):** add the **fail-CLOSED convention** as a named apollo
  convention: "the §8B pairing gate inverts the §6 leakage-judge fail-open default — auto-provisioning
  prefers a false-reject to a false-approve; an unparseable judge response is a REJECT." Plus the
  **§1.8/OPS-6 caveat**: a coherent-but-wrong solution that passes pairing + all 8 gates IS shown in
  shadow (no Layer-3 belief movement); the real safety is `APOLLO_AUTOPROVISION_ENABLED` flag-OFF +
  §6.7 calibration, with 3B2h quarantine the retroactive catch — the pairing gate reduces, not
  eliminates, this case.
- **No drift to `domain-data.md` / `indexing.md`:** this unit writes no rows + reads no chunks directly
  (it consumes the already-typed `CandidateQuestion` + injected `retrieve_fn`), so only `apollo.md`
  changes.

## 14. Verification commands

Interpreter: `.venv/Scripts/python.exe` (a bare-`python` ImportError is interpreter selection, not a
blocker). All tests are Tier-1 (mocked fns, NO DB, NO Docker, NO network) so they run green without the
Postgres container.

```bash
# 1. Unit tests green (both new modules)
.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_solution.py \
    apollo/provisioning/tests/test_pairing_gate.py -v

# 2. The full provisioning package still green (no regression to 3B2a-d)
.venv/Scripts/python.exe -m pytest apollo/provisioning/ -q

# 3. Patch coverage >= 95% on changed lines vs the 3B2d compare branch
.venv/Scripts/python.exe -m pytest apollo/provisioning/ --cov=. --cov-report=xml -q
.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml \
    --compare-branch=feat/apollo-kg-wu3b2d-scrape-tagmint --fail-under=95
# (or the installed `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2d-scrape-tagmint --fail-under=95`)

# 4. Re-export resolves
.venv/Scripts/python.exe -c "from apollo.provisioning import find_or_generate, validate_pair, \
    ReferenceSolutionDraft, PairingVerdict, Rejection, SolutionDraftError, build_approved_pair, \
    rejection_from_verdict; print('ok')"

# 5. File-size / import-count guard (neighborhood stays clean)
wc -l apollo/provisioning/solution.py apollo/provisioning/pairing_gate.py
```

**Manual smoke (the pipeline-replay + fail-closed + DLQ proofs, expressed as the tests above):**
- *Replay / idempotency:* `test_solution_hash_deterministic` + running `find_or_generate`/`validate_pair`
  twice on the same input returns equal values (no side effect).
- *Fail-closed (the safety smoke):* `test_validate_pair_fails_closed_on_unparseable_judge` /
  `_on_judge_exception` — inject a malformed judge response, assert `paired=False` (a REJECT, never an
  approval).
- *Bad-pairing reject (the DLQ-equivalent):* `test_validate_pair_rejects_mispaired_solution` — a solution
  that answers a DIFFERENT question yields a `Rejection` the caller routes to `apollo_rejected_problems`.
- *Backpressure:* N/A in-unit (no batching/rate-limit here — 3B2f's `PER_DOCUMENT_TOKEN_CEILING` + the
  queue drain own backpressure). State this explicitly so the executor does not invent an in-unit limiter.

## 15. Out-of-scope boundaries (held firmly)

- **NO entity minting / no `apollo_kg_entities` write** — that is 3B2d's `tag_and_mint`. 3B2e produces
  the `ReferenceSolutionDraft`/`ApprovedPair` that 3B2g feeds INTO `tag_and_mint`.
- **NO promotion / 8-gate lint / `:Canon` / `apollo_rejected_problems` write** — 3B2g. 3B2e returns a
  typed `Rejection`; the orchestrator does the DB write.
- **NO `ApprovedPair` redefinition** — IMPORT `apollo.provisioning.tag_mint.ApprovedPair` (3B2d owns it).
- **NO metering / queue-drain / cost ceiling** — 3B2f's `MeteredChat` + `claim_provisioning_job`. 3B2e's
  `chat_fn`/`judge_fn` are plain `str`-returning injected fns.
- **NO quarantine / shadow verification** — 3B2h.
- **NO trigger / worker shell / orchestrator** — 3B2g.
- **NO migration, NO ORM, NO model edit, NO `done.py`/grading-core touch, NO new package.**
- **NO real OpenAI / `embed_text` / live retrieval call in tests** — everything injected + mocked Tier-1.
- **NO in-unit retry/backoff/rate-limit** — the queue-drain (3B2f) owns those; 3B2e calls once at temp 0.
- **NOT solving the fabricated-coherent-wrong case** (§1.8/OPS-6) — explicitly documented as a residual
  the pairing gate reduces but does not eliminate; the flag-OFF default + calibration + 3B2h are the
  defense-in-depth.

## 16. Risks (confidence-rated)

- **[HIGH confidence it's correct] Fail-closed inversion is the single most important property.** The
  test suite pins it two ways (unparseable JSON + exception, both phases) and the orchestrator's pinned
  mutation (fail-closed → fail-open) must RED them. Residual risk: a future refactor adds a third judge
  call without the fail-closed wrapper. Mitigation: a single private `_judge_or_fail_closed(judge_fn, …)`
  helper that ALL judge invocations route through (one place to be correct) — make the helper the only
  caller of `judge_fn`.
- **[MEDIUM] The extracted-vs-generated branch detection is heuristic.** "Does a retrieved span carry a
  printed solution?" has no crisp signal in v1. Plan: the retrieve adapter (3B2g) flags solution-bearing
  spans, OR an extraction `chat_fn` pass returns a non-empty parseable solution → extracted. The UNIT
  tests both branches deterministically via the stub; the real discrimination quality is a 3B2g/
  calibration concern, not a unit-correctness one. Document the heuristic in the docstring.
- **[MEDIUM] Cost surprise.** The `generated` branch is a `gpt-4o` reasoning call per un-extracted
  question; a document with many novel exercises burns the most. Bounded by 3B2f's
  `PER_DOCUMENT_TOKEN_CEILING` (NOT this unit). Risk is that the ceiling is set too high; flagged to 3B2f
  (OPEN-DECISION #7). 3B2e's per-question cost (~$0.013) is declared in §6.
- **[MEDIUM] External-API availability.** A judge/generate outage surfaces as `SolutionDraftError` /
  fail-closed reject → the question is rejected/aborted, the drain re-claims (3B2f). No data corruption;
  the cost is a re-burn on re-claim. Acceptable for an OFF-by-default v1 pipeline.
- **[LOW] `Problem`-validity of the generated `reference_solution`.** The generate prompt must yield
  `ReferenceStep`-shaped steps (procedure_step order 1..N contiguous, depends_on resolvable). If it does
  not, `build_approved_pair`/the round-trip test fails — caught at stage 2 (`SolutionDraftError`) or by
  gate 1/2 downstream (3B2b). The unit validates the shape before returning a draft, so a malformed
  generate never reaches the gate as a half-valid pair.
- **[LOW] No schema-lock / deploy risk** — no migration, no DDL, no remote DB. Pure additive code on an
  OFF-by-default pipeline.

## 17. Deviations I'd allow the executor

- **Type backing for `GroundingSpan`/verdicts:** Pydantic `BaseModel` OR frozen dataclass — either is
  fine; prefer `BaseModel` for the LLM-facing types (validation at the boundary) and a frozen dataclass
  for `GroundingSpan`. Executor's call.
- **One module vs two:** `solution.py` + `pairing_gate.py` is the recommended split (mirrors the
  stage-2/stage-3 seam and keeps each < 250 lines). If the executor finds the ApprovedPair builder reads
  more naturally in `pairing_gate.py` (co-located with the PASS handoff), that is acceptable as long as
  the re-export surface in §4 is preserved.
- **Phase A/B as one judge call vs two:** the plan assumes two `judge_fn` calls (cheaper short-circuit on
  Phase A). The executor MAY combine them into one structured call IF the fail-closed property and the
  `failed_claims` decomposition are preserved AND `test_validate_pair_phase_b_not_called_when_not_paired`
  is adapted accordingly. The two-call shape is preferred for cost + the short-circuit test.
- **Extracted-branch detection mechanism:** flag-on-span vs extraction-`chat_fn`-pass — either, as long
  as both `test_find_or_generate_extracted_branch` and `_generated_branch` discriminate deterministically.
- **`confidence` semantics:** the exact threshold (if any) at which low confidence forces a reject is
  NOT pinned here — v1 may treat confidence as observability-only (the boolean `paired`/`faithful` drive
  the reject). If the executor wants a confidence floor, it must add a discriminating test; absent that,
  booleans govern.
- **NOT negotiable:** the fail-CLOSED default (parse failure ⇒ reject), importing the REAL
  `tag_mint.ApprovedPair`/`Problem` (no mock), injected-and-mocked LLM/retrieval (no network/DB), no
  edit to `scrape.py`/`tag_mint.py`/`_llm.py`/migrations, the §1.8/OPS-6 caveat in the docstring + doc,
  and the ≥95% diff-cover gate vs `feat/apollo-kg-wu3b2d-scrape-tagmint`.

