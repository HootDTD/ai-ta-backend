# Handoff — Apollo authored-set audit fix (2026-06-30 audit)

**Status as of 2026-07-01.** Companion to the audit write-up
[`2026-06-30-authored-set-dedup-false-merge.md`](./2026-06-30-authored-set-dedup-false-merge.md).

This effort fixed KG corruption found in a read-only audit of `apollo_authored_sets id=4`
(AAE 333 HW1) on **staging** (`hjevtxdtrkxjcaaexdxt`). The corruption is **not
student-visible and not crash-causing** — it silently damages the per-concept reference
knowledge graph (`apollo_kg_entities` + `apollo_entity_prereqs` + Neo4j `:Canon`) that backs
grading fidelity and personalization. Five PRs (all tagged `(WU-AAS)`) fix it: **#72, #73,
#74, #75, #77**. (`#76` is an *unrelated* NLI-resolver feature — ignore it.)

Memory files `apollo_authored_set_audit.md` and `apollo_authored_set_fixes.md` carry the bug
list, settled decisions, and per-PR status.

## What each PR does and WHY

### #72 — PR1: persist true `solution_source`
Authored-set problems whose reference solution was *extracted from the paired teacher
solution doc* were persisted as `solution_source="generated"` for all 7 (hardcoded).
Purpose: thread the real source through `promote` so a paired-extracted reference records
`"extracted"` (only when the Tier-1 row has none yet — never overwriting an ingest-stamped
source). Provenance correctness; lets extracted references be trusted/labeled properly.

### #74 — PR2: scope the dedup pool to the concept (+ exclude same-mint entities)
Root cause of the false merges. The dedup ladder's candidate pool was **course-scoped only**
(`Subject.search_space_id`), never `concept_id`, and each entity of a problem deduped against
entities minted *earlier in the same mint*. Result: distinct physics fused (hanging mass `m`
≡ block mass `M` at cosine 1.000; `pressure_A` ≡ `pressure_B`; box-1 ≡ box-2), and steps
bound to entities owned by *other concepts / earlier sets* (`proc_overall` → concept 41 from a
different set). Purpose: `_in_course_entities` now ANDs `KGEntity.concept_id == :concept_id`,
and `tag_and_mint` passes the ids resolved earlier in *this* mint as `exclude_entity_ids`, so
(a) two concepts never merge and (b) two distinct nodes of one problem can't fuse.
**Namespacing `canonical_key` was explicitly rejected** (high-churn, only fixes the slug tier,
risks silently breaking personalization). Behavioral change: cross-concept sibling-vocabulary
sharing is gone (audit-endorsed default).

### #73 — PR3: acyclicity guard at mint
There was no cycle check when inserting LLM-drafted prereq edges, so drafted (and
merge-induced) reverse edges persisted a directed cycle in `apollo_entity_prereqs` (the
audited 751↔755 2-cycle). Purpose: `_acyclic_prereq_pairs` drops any edge that would create a
self-loop or close a cycle, **resolved over the entity-id graph** (through `key_to_id`) so a
dedup merge collapsing two keys onto one id is caught as a self-loop. Drop-and-log (like the
pre-existing unminted-key drop), never raise.

### #75 — PR2b: validate prereq edge endpoints against concept scope (defense-in-depth)
Even with PR2, a prereq edge whose endpoint *merged onto a foreign-concept entity* would still
be "resolvable," so the unminted-key drop wouldn't catch it. Purpose:
`partition_prereqs_by_concept_scope` requires **both** endpoints to belong to the mint's
`concept_id`; a cross-concept edge is dropped + surfaced. It runs at the writer boundary
(`insert_prereqs`) **and** in `tag_and_mint` **before** the acyclicity guard — the ordering
matters (a foreign endpoint left in the graph could act as a *phantom bridge* faking a cycle
that discards a legit within-concept edge). `MintPlan.dropped_prereq_pairs` now surfaces all
three drop reasons (unresolvable / cross-concept / cyclic), previously log-only. With PR2 this
guard should never fire in practice.

### #77 — PR4: full KG teardown for orphaned concepts on authored-set delete
A plain `delete_authored_set` removed only the `ConceptProblem`s + docs + set row, leaving the
per-concept KG (`apollo_kg_entities`, `apollo_entity_prereqs`, `apollo_dedup_decisions`,
`:Canon`) behind. So `delete → re-provision` re-upserted onto the *same corrupt entities*
(idempotent on `concept_id`+`canonical_key`) and validation could never get a clean graph.
Purpose: tear the KG down for concepts the set fully **orphaned** — under a **strict
conservative guard** (see warnings): only if 0 remaining problems AND no Postgres student/seed
footprint (`_protected_concepts`: sessions, learner_state, mastery_events, misconceptions,
inbound cross-concept prereq from a surviving concept) AND no `:Canon` RESOLVES_TO. PG deletes
`apollo_dedup_decisions` then `apollo_concepts` (cascades entities → prereqs); post-commit
Neo4j `DETACH DELETE`s `:Canon` guarded by `NOT (c)<-[:RESOLVES_TO]-()`.

## ⚠️ Warnings & gotchas

- **Do NOT patch cp242 in code.** cp242's dropped minus sign (slope→acceleration relation) is
  a **DATA artifact from OCR/extraction, not a code bug** — there is no code reference to
  "fix." Re-provisioning the set after PR5's normalization corrects it. Special-casing cp242
  in code is wrong.
- **Residual false-merge (tracked, NOT a regression, do not "re-fix" as if new):** PR2 reduces
  but doesn't eliminate a *cross-problem, same-concept* thin-`scope_summary` false-merge — a
  genuinely new variable in problem 2 can still embedding-merge against problem 1's
  already-**persisted** sibling (prior-call, so not excluded by same-mint exclusion). Root
  cause is the thin `_scope_summary_for` (`tag_mint.py`), because `variable_mapping` entities
  carry an empty payload so the summary can't discriminate. Deliberate future root-cause work
  (enrich scope_summary or wire a real dedup-band judge), separate from #72–#77 and PR5.
- **PR4 deliberately OVER-spares.** A concept with *any* student/seed footprint (session,
  learner_state, mastery_events, misconceptions, inbound cross-concept prereq from a survivor,
  or `:Canon` RESOLVES_TO) is **not** torn down. This is intentional: the naive "0 problems +
  no RESOLVES_TO" guard the plan first specified was unsafe — it would **500** on the
  `apollo_sessions.concept_id` `ON DELETE RESTRICT` FK (making student-used sets permanently
  undeletable) and **silently CASCADE-destroy** `apollo_learner_state`/`apollo_mastery_events`
  (student data written even on the all-`missing` grading path, which mints no RESOLVES_TO
  edge). Don't "simplify" the guard back.
- **PR4 known limitation (safe direction):** a minted-but-*rejected* problem recorded with
  `concept_problem_id: None` carries no concept id in `result_summary`, so its KG is **not**
  torn down (under-teardown, never data loss). Documented in code + `apollo.md`.
- **Concepts bind by SLUG.** `resolve_or_create_concept` resolves by `(search_space_id, slug)`
  and returns an *existing* concept if the slug already exists — so an authored-set problem can
  attach to a pre-existing shared/§8-seed concept. This is why PR4's teardown must be strict;
  don't assume a set "owns" the concepts its problems touch.
- **Validation is gated.** The clean-graph validation requires **#74 + #73 + #77 MERGED *and
  deployed* to staging**. #75 and #77 may still be open when you start — verify with
  `gh pr view 75 77 --json state` first.
- **STAGING ONLY, never prod.** Supabase staging = `hjevtxdt…`; prod = `uduxdn…` / project
  "Apollo". Verify `get_project_url` before any write. Neo4j from this machine needs
  `SSL_CERT_FILE=$(python3 -m certifi)`; creds in Railway `hoot-ai-ta` → service
  `ai-ta-backend` → env `staging`. Aura `NEO4J_DATABASE=791f9ced`.
- **Never merge PRs; base every PR on `origin/staging`; PR targets `staging`.** Ishaan merges.
  `ApolloRun` is stale — do not base off it.

## Current state

- Merged to staging: **#72, #73, #74** (+ unrelated #76).
- Open, awaiting merge: **#75, #77** (both green/CI-clean when authored).

## Remaining to be done

### 1. PR5 — extraction/OCR quality (lowest priority, but the last code PR)
Branch off `origin/staging` (`git checkout -b fix/apollo-scrape-ocr-normalize origin/staging`).
Two independent, pure/deterministic, no-network parts. Follow the standard workflow: **TDD
(failing test first → RED → GREEN)**, run the full `apollo/provisioning` suite, dispatch a
`code-reviewer` (+ `apollo-specialist`) adversarial pass **before committing**, update
`docs/architecture/apollo.md` in the same commit (drift contract; bump `last_verified`). Gates:
ruff + mypy on added files, `diff-cover --fail-under=95` — **Docker must be UP** (co-located
tests use a real pgvector Testcontainer `db_session`, or they skip and the gate fails on skip).
Inject stubs; no network.

**Part A — normalize `problem_text` at the scrape chokepoint.**
- Add a pure `_normalize_problem_text(text) -> str` and apply it in
  `apollo/provisioning/scrape.py` `_coerce_candidate` (~line 110; `problem_text=raw.get(
  "problem_text", "")` ~line 124), and in the sibling coerce path (second
  `problem_text=raw.get(...)` ~line 196).
- Normalize: collapse whitespace, unicode minus `U+2212` → ASCII hyphen, common OCR
  confusables (e.g. `"PPPs"`→`Pa·s`, `"sss/ft3"`→`slug/ft³`). **NEVER drop content** — bias to
  *flag-for-review* over silent rewrite.
- Why the chokepoint: `problem_text` feeds BOTH the persisted payload (retrieval + student
  display) AND the gate-8 dedup key `problem_dup_hash` (`apollo/provisioning/problem_hash.py:46`
  re-normalizes `problem_text`). Normalizing once at coerce keeps write and hash consistent.
- Do NOT conflate with the existing `scrape._normalize` (line 66, whitespace-for-hashing) or
  `problem_hash._normalize_text` — `_normalize_problem_text` is a new *semantic* pass; keep
  them separate.
- Tests (`apollo/provisioning/tests/test_scrape.py`): pure unit tests per confusable/minus/
  whitespace case + a `_coerce_candidate` test proving the stored `problem_text` is normalized.

**Part B — force the OCR cross-check when the page contains math.**
- `apollo/provisioning/authored_sets/verification.py` `verify_against_generated` (~line 62)
  early-returns `base` (`review_required=False`, no cross-check) when `not low_confidence`
  (~lines 73–80). So a high-confidence-but-garbled page (min_conf 0.95) skips the
  independent-generate cross-check — that's how cp242's dropped minus sign slipped past the 0.6
  gate (`_DEFAULT_CONF_THRESHOLD = 0.6`, `authored_sets/orchestrator.py:53`).
- Fix: when `problem_text`/the reference contains math, do NOT early-return — force the
  cross-check, or flag-for-review. Conservative (flag over silent trust).
- Tests (`apollo/provisioning/tests/test_authored_verification.py`): high-conf page *with* math
  triggers the cross-check/review; high-conf page *without* math still early-returns (no
  behavior change).
- Remember: **do not patch cp242 in code** (data artifact — see warnings).

### 2. Validation (the payoff; after #74+#73+#77 merged AND deployed — PR5 not strictly required)
Delete `authored_sets id=4` via the endpoint (now with full teardown), re-run provisioning
once, re-run the audit doc's SQL/Cypher. Expect: zero cross-concept/foreign prereq edges, zero
2-cycles, no false merges (`m≠M`, `pressure_A≠pressure_B`, `p1≠p2`), `solution_source=
"extracted"` on all 7, and `:Canon` count == `apollo_kg_entities` count for live concepts with
no concept-45 orphans. (If set 4 turns out to have any student footprint, PR4 will over-spare
it — do a targeted manual teardown of set 4's concepts after verifying no student data.)

Start by confirming #75/#77 are merged and `origin/staging` is current, then do PR5 Part A
first (smallest, self-contained).
