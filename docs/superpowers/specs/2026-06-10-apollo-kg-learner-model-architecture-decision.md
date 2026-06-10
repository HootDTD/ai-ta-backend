# Architecture Decision: Apollo KG → Persistent Learner Model

**Status:** Decided. Converges all seven research tracks of
`../../../../docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-research-plan.md`
into one architecture. Next artifact: phased implementation plan
(`superpowers:writing-plans`).
**Date:** 2026-06-10
**Inputs:** user interview (RQ4 + guardrails + 3 scope decisions, 2026-06-10);
RQ2/RQ5 research memos (same session); RQ3 live-GPT-4o spike
(`scripts/spikes/rq3_edge_extraction.py` + `rq3_results.json`); RQ1/RQ6/RQ7
external research memos
(`../../../../docs/superpowers/specs/2026-06-10-apollo-kg-rq1-rq6-rq7-research-memos.md`).

---

## 0. The decision in one paragraph

Apollo's knowledge graph stops being a per-attempt grading scratchpad and
becomes the evidence layer of a persistent, interpretable learner model — the
paradigm-I leg missing from Hoot's II+III hybrid. Three layers: **Layer 1**, a
curated concept ontology authored in Postgres (`apollo_kg_entities` +
prerequisite edges) and projected into Neo4j as rebuildable `:Canon` nodes;
**Layer 2**, today's per-attempt evidence graphs in Neo4j, made connected
(typed-edge extraction with cross-turn linking), persisted instead of deleted,
and resolved against Layer 1 via `RESOLVES_TO` edges at Done-time; **Layer 3**,
a per-(student, entity) **3-state Bayesian belief** `{misconception, shaky,
mastered}` living entirely in Postgres, updated by a hand-set likelihood rule
inside the existing Done transaction, with an append-only event log
(`apollo_mastery_events`) as the longitudinal record and refit corpus. The v1
consumer is **session personalization**. Because the learner model is a
persistent cognitive profile, **Phase 1 retrofits auth + course scoping onto
the existing unauthenticated `/apollo/*` surface before anything else is
built.**

```
LAYER 3  Learner model (Postgres)                       ← NEW
         belief[3] per (user, course, entity); mastery, misconception flag
         updated at Done; append-only event log
              │ aggregates evidence from
LAYER 2  Evidence graphs (Neo4j, per attempt)           ← EXISTS, being fixed
         typed nodes + extracted edges, cross-turn linked,
         PERSISTED; each node ─RESOLVES_TO→ Layer 1
              │ resolves against
LAYER 1  Curated ontology (Postgres → :Canon projection) ← NEW (seeded from
         canonical entities + prerequisite edges            existing files)
```

## 1. Product decisions (RQ4 + interview, binding)

- **v1 wedge: session personalization.** At session start Apollo reads
  Layer 3 and (a) selects the problem whose reference graph best covers the
  student's weak entities, (b) conditions the confused-AI persona ("be extra
  confused about X" when a misconception flag is active), (c) tunes
  difficulty. Teacher dashboard is the second consumer, not the first.
- **Ranked queries** the schema must serve cheaply:
  1. *Q1 personalization read:* weakest entities + misconception flags for
     (student, concept) at session init.
  2. *Q2 class mastery / stuck students:* per-entity class aggregates and
     stuck-student lists for a course.
  3. *Q3 longitudinal trend:* mastery-over-time series per (student, entity).
  Reference-step analytics deferred.
- **Guardrails confirmed:** interpretable BKT-family-or-simpler; no neural KT;
  hand-set parameters only (tiny-N pilot); no new infrastructure; one
  developer.
- **Scope decisions (user, 2026-06-10):**
  1. **Auth retrofit is Phase 1** — fix the existing `/apollo/*` no-auth gap
     and table scoping in this project, not just on the new surface.
  2. **Elo skill scalar deferred** — the event log captures enough (score,
     entity, ordering) to backfill Elo by replay when difficulty-matched
     problem selection is built.
  3. **LLM-assisted Layer-1 authoring deferred post-wedge** — v1 seeds Layer 1
     by converting the existing bernoulli files with a one-time script; the
     RQ6 extraction + teacher-approval workflow becomes its own later phase.

## 2. Storage layout (RQ2, reconciled with RQ7)

**Postgres is the system of record for Layer 1 (authoring) and Layer 3
(entirely). Neo4j holds Layer 2 plus a disposable `:Canon` projection of
Layer 1 so `RESOLVES_TO` is a real edge.** Layer 1 extends the existing
migration-018 curriculum tables (`apollo_subjects` / `apollo_concepts` /
`apollo_misconceptions`) — the codebase already treats curriculum as Postgres
rows.

RQ7 mandates two corrections to the original RQ2 sketch, applied below: the
student key is a real `auth.users` UUID (not `student_id TEXT`), and
learner-model rows carry a `search_space_id` FK (per-course mastery records).

### Migration 023 (target shape; exact DDL in the implementation plan)

```sql
-- LAYER 1: skill inventory
CREATE TABLE apollo_kg_entities (
  id            BIGSERIAL PRIMARY KEY,
  concept_id    BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
  canonical_key TEXT   NOT NULL UNIQUE,   -- 'bernoulli_principle/eq.bernoulli_full'
  kind          TEXT   NOT NULL,          -- concept|equation|condition|definition|procedure
  display_name  TEXT   NOT NULL,
  payload       JSONB  NOT NULL DEFAULT '{}',  -- symbolic form, applies_when, …
  aliases       JSONB  NOT NULL DEFAULT '[]',  -- grows from the resolution log
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE apollo_entity_prereqs (      -- normalizes concept_dag.json
  from_entity_id BIGINT REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
  to_entity_id   BIGINT REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
  PRIMARY KEY (from_entity_id, to_entity_id)
);

-- LAYER 3: current snapshot, updated in place
CREATE TABLE apollo_learner_state (
  user_id            UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  search_space_id    INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  entity_id          BIGINT  NOT NULL REFERENCES apollo_kg_entities(id),
  belief             REAL[]  NOT NULL,    -- [p_misc, p_shaky, p_mastered], sums to 1
  mastery            REAL    NOT NULL,    -- 0·p_misc + 0.5·p_shaky + 1·p_mastered
  confidence         REAL    NOT NULL,    -- 1 − normalized_entropy(belief)
  misconception_code TEXT    NULL,        -- set iff p_misc is argmax and ≥ 0.5
  evidence_count     INT     NOT NULL DEFAULT 0,
  last_evidence_at   TIMESTAMPTZ,
  updated_at         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (user_id, search_space_id, entity_id)
);

-- LAYER 3: append-only event log — longitudinal record AND refit corpus
CREATE TABLE apollo_mastery_events (
  id                 BIGSERIAL PRIMARY KEY,
  user_id            UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  search_space_id    INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  entity_id          BIGINT  NOT NULL REFERENCES apollo_kg_entities(id),
  attempt_id         BIGINT  REFERENCES apollo_problem_attempts(id) ON DELETE SET NULL,
  event_kind         TEXT    NOT NULL,  -- covered|missing|partial|misconception|corrected
                                        -- (open enum; 'chat_question' reserved, RQ5)
  score              REAL    NULL,      -- continuous partial credit 0..1
  misconception_code TEXT    NULL,
  parser_confidence  REAL    NULL,      -- RQ1: needed to weight observations in refit
  grader_confidence  REAL    NULL,
  negotiation_move   TEXT    NULL,      -- challenge|paraphrase|skip|null
  reference_step_id  TEXT    NULL,      -- which authored step → per-step difficulty later
  prior_belief       REAL[]  NOT NULL,  -- replay/debug the filter
  posterior_belief   REAL[]  NOT NULL,
  mastery_after      REAL    NOT NULL,  -- Q3 trend = direct read of this column
  dt_days_since_last REAL    NULL,      -- lag feature; fit decay k later
  evidence_node_ids  JSONB   NOT NULL DEFAULT '[]',  -- Neo4j bridge (same pattern
                                                     -- as apollo_kg_negotiations)
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, entity_id, event_kind)   -- idempotent Done re-runs
);
CREATE INDEX ON apollo_mastery_events (user_id, entity_id, created_at);
CREATE INDEX ON apollo_mastery_events (entity_id, created_at);
-- plus: learner_update_pending BOOLEAN on apollo_problem_attempts (see §6)
```

Elo columns (`elo_skill_after`, `entity_difficulty`) are **omitted** — Elo is
deferred and backfillable by replaying `score` + ordering from this log.

### Neo4j

- **`:Canon` projection of Layer 1:** idempotent seeder (MERGE on
  `canonical_key`, uniqueness constraint), rebuildable from Postgres at any
  time, never authored in Neo4j. Sidesteps dual-write drift.
- **Layer 2 nodes** keep today's 6 labels + `:_KGNode` + 4 edge types, and
  gain properties: `user_id` (opaque UUID), `search_space_id`, `created_at`,
  `graded_at`, and resolution fields (`resolution:
  resolved|unresolved|ambiguous`, `resolved_key`, `resolution_method`,
  `resolution_confidence`). New index on `user_id`; attempt_id index stays.
- **New edge:** `(:_KGNode)-[:RESOLVES_TO {method, confidence,
  resolved_at}]->(:Canon)`.
- No student-identifying free text in node properties beyond necessity (RQ7):
  typed claims only, keyed by opaque IDs, never name/email.

### Why Layer 3 in Postgres wins (decisive, from RQ2)

1. It is not graph-shaped — keyed scalar state per (student, entity) is a row.
2. **Transactional Done-time updates:** `handlers/done.py` already writes
   grade + XP in one Postgres transaction; the learner update joins it
   atomically. Neo4j would create a cross-store two-phase problem on the most
   important write in the product.
3. Q2 joins course membership, which lives in Postgres — one GROUP BY instead
   of a cross-store join. Q1 is an index-range scan at session init. Q3 reads
   `mastery_after` off the event log.
4. The async-SQLAlchemy/migration/test machinery for `apollo_*` tables already
   exists.

Rejected: Layer 3 as Neo4j properties/edges (re-creates a relational table
inside Neo4j with worse aggregation, no FK integrity, no Done transactionality,
double Aura relationship burn); Layer 1 authored directly in Neo4j (free-tier
instance becomes a single point of loss for curated content; projection costs
one seeder).

### Temporality (RQ2)

**Bi-temporal modeling rejected.** Apollo's evidence is event-shaped, not
fact-shaped: "student asserted X during attempt 17" never becomes false.
Belief revision is a new Layer-3 event, not graph surgery. Append-only
`apollo_mastery_events` with `mastery_after` snapshots + `created_at` on
Layer-2 nodes is the entire temporal model. Replayability — the one genuinely
valuable Graphiti property — comes free.

## 3. Learner-model formalism (RQ1)

**A 3-state Bayesian belief filter per (student, entity):
`belief = [p_misconception, p_shaky, p_mastered]`, updated once per entity per
Done event by hand-set likelihood weighting.** It is a hand-parameterized
BKT-family HMM with a richer emission alphabet: interpretable, zero fitted
parameters, and the only candidate that consumes all four Apollo evidence
types (continuous coverage scores, typed misconception events, negotiation
moves, parser/grader confidence).

### Update rule (the actual math)

- **Cold start:** `belief = [0.20, 0.60, 0.20]` (mastery 0.40 — "assume
  nothing, slightly pessimistic"; BKT-analogue P(L0)≈0.3–0.4).
- **Step 0 — between-session decay toward the prior:**
  `belief ← (1−w)·belief + w·prior` where `w = 1 − e^(−k·Δt_days)`, `k = 0.05`
  (≈14-day half-life; Pelánek's k≈0.1 halved because Apollo evidence is
  weekly/sparse). Unseen entities drift to "unknown," never to "confidently
  wrong." No spacing-effect/DAS3H modeling.
- **Step 1 — evidence likelihood vector** `L = [L_misc, L_shaky, L_mastered]`,
  start `[1,1,1]`, multiply per evidence item:

  | Evidence (per entity, from Done diff) | L_misc | L_shaky | L_mastered |
  |---|---|---|---|
  | covered, score `s ∈ [0,1]` | `(1−s)^γ` | `1 − \|2s−1\|^γ` | `s^γ` (γ = 1.5; mid scores land on "shaky") |
  | missing | 0.7 | 1.0 | 0.4 |
  | misconception (code c) | 3.0 | 1.0 | 0.2 (sets `misconception_code = c`) |
  | corrected | 0.5 | 1.5 | 1.2 |
  | negotiation move | — | ×1.2 | ×1.1 |

- **Step 2 — confidence damper:** `q = parser_confidence · grader_confidence`;
  `L ← q·L + (1−q)·[1,1,1]`. A low-confidence diff barely moves belief; a
  high-confidence misconception moves it hard. This is where Apollo's typed
  metadata pays off — no rejected alternative can absorb grader confidence.
- **Step 3 — Bayes + renormalize:** `belief ← normalize(belief ⊙ L)`;
  `mastery = 0.5·p_shaky + p_mastered`;
  `confidence = 1 − entropy(belief)/log 3`.
- **Step 4 — append the event row** (prior/posterior belief, score,
  confidences, `mastery_after`).

Pure arithmetic over Postgres rows inside the Done transaction — no LLM call,
no Neo4j write, fires once per Done episode (never per turn).

### Readouts

| Consumer | Readout |
|---|---|
| Session personalization (v1) | `mastery` + `misconception_code` from `apollo_learner_state`. Persona: misconception flag active → "be extra confused about {code}". Problem selection: prefer entities with mastery 0.3–0.7 (teachable edge) whose prerequisites are mastered (`apollo_entity_prereqs`, in-memory). Low `confidence` → re-probe. |
| Teacher dashboard | `AVG(mastery)` and `% students with p_misc ≥ 0.5` per entity per course; "stuck" = p_misc ≥ 0.5 for ≥2 consecutive episodes or flat/declining mastery across last 3 events. |
| Longitudinal | `SELECT mastery_after, created_at FROM apollo_mastery_events …` — a direct time series. |

The belief vector renders directly as an open learner model (three stacked
bars) — the interpretability the MATHia argument demands.

### Upgrade path

The filter is a special case of a fittable 3-state HMM; the event log stores
raw evidence (typed kind, score, confidences, prior/posterior, lag) sufficient
to EM-fit emissions, grid-search decay `k`, or backfill Elo offline once any
entity passes ~200 graded episodes. Parameter swap, not architecture change;
the live read path stays the interpretable filter.

### Rejected (RQ1, cited in the memo)

Classic binary BKT (collapses continuous/typed evidence; partial-credit
extensions need fitting we can't do at tiny-N); plain EMA (cannot represent
misconception distinctly from "low score," no uncertainty); pure Elo (1-D, no
typed misconception state — deferred as the difficulty-matching sub-component);
EM-fitted HMM (no data; degeneracy); **all neural KT** (DKT/DKVMN/SAINT —
data-hungry, fails on small dialogue-KT datasets, uninterpretable;
LAK 2025 dialogue-KT result is directly analogous and damning).

## 4. Extraction (RQ3 — spike-verified)

**One-call extraction.** The per-turn parser call extracts nodes AND typed
edges in a single GPT-4o strict-structured-outputs call (`json_schema`), with
the edge vocabulary (`EDGE_ALLOWED_PAIRS`) in the prompt and the existing
attempt graph (node ids + types + labels) passed as context so new entries
link across turns. No second canonicalize call per turn — resolution happens
once per attempt at Done (§5).

Spike evidence (`scripts/spikes/rq3_edge_extraction.py`, live GPT-4o, 5
synthetic multi-turn transcripts grounded in the real problem bank; prod-replay
was impossible — both Supabase projects had zero Apollo messages):

- 24 edges proposed, **88% valid** against `EDGE_ALLOWED_PAIRS`; all 3 invalid
  edges were deterministically rejectable (and traced to one transcript where
  plan-speak typing made targets procedure_steps — correct behavior).
- **Cross-turn linking works:** 11/21 valid edges spanned turns, including
  late-arriving conditions correctly SCOPES-linked to equations from earlier
  turns. SCOPES comes alive (it is dead code today).
- Orphan nodes: 20 → 6 vs the current deterministic-only rules.
- Cost/latency: ~$0.004/turn, median 2.45 s — acceptable in the chat loop.

Deterministic validation (`EDGE_ALLOWED_PAIRS`) + logging is the rejection
point for invalid edges — `write_edges` stops silently dropping (NO-FALLBACK:
dropped edges are logged with reason).

## 5. Resolution: `RESOLVES_TO` (RQ2)

- **Key:** human-authored `canonical_key` slug with a Neo4j uniqueness
  constraint. Never MERGE-on-UUID (degenerates to CREATE). Evidence nodes keep
  `CREATE` — episodic events are intentionally never deduped; dedup happens by
  convergence of `RESOLVES_TO` edges onto `:Canon`.
- **When:** at Done, after grading, batched per attempt — chat-loop latency
  untouched; one LLM resolution call per attempt max.
- **Two-stage pipeline:** (1) deterministic — normalized alias match against
  `apollo_kg_entities.aliases` + `normalization_map`; SymPy structural
  equivalence for equations (reuses `parse_zero_form`); RapidFuzz ≥ 0.9.
  (2) one JSON-mode LLM adjudication call for the unmatched remainder,
  candidate list = the concept's full inventory, "return empty when unsure";
  hallucinated keys → hard `ResolutionInvalidOutputError`.
- **NO-FALLBACK, two cases:** per-node non-match is **data, not an error**
  (`resolution: 'unresolved'`, no edge, no mastery event, logged — the
  unresolved rate is itself a curriculum-gap metric and an alias source).
  Infrastructure failure raises named `ResolutionUnavailableError` and must
  NOT void the earned grade: grade commits, `learner_update_pending = true`
  on the attempt, janitor/next-session retry; the update is idempotent
  (MERGE edges + the `(attempt_id, entity_id, event_kind)` uniqueness key).

## 6. Retention (RQ2)

**Per-attempt subgraphs stay (no merged per-student graph); persist instead of
delete.** `handle_end` stops deleting; Done stamps `graded_at`.
`restart_problem` keeps deleting (explicit student wipe). The
`attempt_id < 0` test-cleanup convention stays. `delete_subgraph` remains in
`KGStore` as the future janitor's primitive. Cross-attempt connectivity comes
free: two attempts' nodes resolving to the same `:Canon` node are two hops
apart.

Aura capacity: ~50 students × 2 attempts/week × 36 weeks ≈ 90k nodes / 170k
relationships per year — within the 200k/400k free-tier limits (~2 years
headroom; verify the figures in the Aura console for instance 791f9ced, legacy
pages say 50k/175k). Janitor later: prune evidence subgraphs older than N
months, oldest first — **pruning Layer 2 loses only drill-down; Layer 3 and
the event log live in Postgres.** Operational risk: Aura Free pauses after
days of inactivity and may be deleted after ~30 days paused — school breaks
are real; schedule a weekly keep-alive or budget Aura Professional.

## 7. Layer-1 authoring (RQ6 — deferred workflow, v1 seed)

**v1: a one-time conversion script** turns the existing hand-authored
bernoulli files into Layer-1 rows — `concept_dag.json` (14 nodes / 16 edges) →
`apollo_kg_entities` (kind=concept) + `apollo_entity_prereqs`;
`canonical_symbols.json` (7 symbols) → entities (kind=variable);
`normalization_map.json` (23 mappings) → `aliases`. Session machinery
(`parser_prompt_template.md`, `solver_hints.json`, `forbidden_named_laws.json`)
and `problems/*.json` stay hand-authored files, untouched.

**Deferred (own phase, post-wedge, spec = the RQ6 memo):** LLM-assisted
expansion — extraction pass over the teacher's RAG corpus drafts entities,
aliases, and confidence-ranked prerequisite-edge candidates; a teacher
approval queue (checklist UX, no graph editor) publishes to
`apollo_kg_entities` and triggers the `:Canon` rebuild. Trust boundary from
the literature: LLM drafts concept lists + aliases (high precision, cheap to
prune) and suggests prerequisite edges (F1 ≈ 0.5–0.77; human prunes); humans
exclusively author `reference_solution` math, `scope_boundary`,
`forbidden_named_laws`, `solver_hints`, parser prompts. Honest economics:
ontology authoring drops ~2.5h → ~1h review, but problems (~3–5h) remain the
per-concept bottleneck — automating them is anti-scope.

## 8. Security, privacy, multi-tenancy (RQ7)

**Two existing defects must be fixed, not inherited** (both verified in repo):
`/apollo/*` endpoints perform no auth (body-supplied identity —
`security.md` "Known gaps"), and `apollo_*` tables key off `student_id TEXT`
with no course scoping.

- **Phase 1 retrofit (user decision):** wire `/apollo/*` through
  `resolve_auth_context` + `_require_course_membership`; migrate Apollo tables
  to `user_id UUID` + `search_space_id`; update the student UI to
  authenticated calls. No new endpoint or table may use the old pattern.
- **Scoping rules (enforceable):** student sees own mastery + own evidence
  only (server-injected `user_id` filter; RLS self-read policy as
  defense-in-depth); teacher sees students in their own courses only
  (`_require_course_membership` before any read; `search_space_id` filters in
  both stores). Mastery records are **per-course**; cross-course aggregation
  is out of scope for v1.
- **Aura placement: approved with mandatory mitigations.** Backend-mediated
  access only (creds already backend-only); **every** Cypher read/write
  filters by `user_id` AND `search_space_id`, both server-injected from the
  validated AuthContext — enforced by a shared scoping helper wrapping the
  driver so an unscoped query is impossible by construction; no
  student-identifying free text in properties; deletion fans out from
  Postgres (`ON DELETE CASCADE`) to Aura nodes.
- **Student rights (v1):** view — the open learner model ships eventually
  (belief bars); contest — negotiation moves (`apollo_kg_negotiations`)
  already exist and serve as the FERPA "seek to amend" mechanism; deletion —
  per-student delete supported; course-bound retention recommended (purge at
  term end / enrollment removal), no indefinite default. FERPA skim: a
  persistent inferred mastery profile + stored teaching transcripts is an
  education record; persistent inferring AI tutors are the "high-risk"
  category; vendor duties include direct control, purpose limitation, and
  access/amend/destroy rights.

## 9. Hoot chat as evidence (RQ5)

**OUT for v1.** Question-asking is sign-ambiguous per the literature
(Graesser & Person: frequency uncorrelated with achievement; Aleven/Koedinger:
interpreting a help request requires already knowing mastery — circular for a
cold-start model). Apollo's graded explanations are sign-unambiguous; diluting
them risks corrupting the estimates teachers see.

**Hedges shipped in v1:** `event_kind` is an open enum with `chat_question`
reserved (already true of the §2 schema), and the per-`/ask`
`extract_and_filter_keywords` output (≤8 concept terms, currently computed
then discarded) is persisted as one JSONB column on chat turns so months of
history can be backfilled offline. When chat evidence lands, it is
class-level signal first (validated use), never a hard per-student negative.

## 10. Consolidated anti-scope

No neural KT; no EM/parameter fitting in v1; no Elo until problem selection
needs it; no bi-temporal edges or LLM contradiction-invalidation; no
Graphiti-the-library; no MinHash/LSH; no GraphRAG retrieval machinery; no
grading redesign (diff-at-Done stays; the learner model consumes grading
output); no merged per-student graph; no automatic problem/reference-solution
generation; no skill discovery from response data; no graph-editing UI; no
cross-course concept merging; no chat evidence; no per-turn learner updates;
no new infrastructure.

## 11. Phasing (input to the implementation plan)

1. **Auth & scoping retrofit** — `/apollo/*` through
   `resolve_auth_context` + `_require_course_membership`; `user_id UUID` +
   `search_space_id` on Apollo tables; student-UI auth. Independently
   shippable; closes a documented security gap.
2. **Make the evidence graph real (RQ3)** — one-call typed-edge extraction
   with graph context + strict outputs; wire SCOPES; cross-turn linking;
   `write_edges` logs instead of silently dropping. Immediately improves
   existing grading.
3. **Layer 1 + resolution + persistence** — migration 023; bernoulli seed
   script; `:Canon` projection seeder; two-stage Done-time resolution +
   `RESOLVES_TO`; stop deleting graphs at session end; new named errors.
4. **Layer 3 learner model** — the 3-state filter in the Done transaction;
   decay; event log; `learner_update_pending` retry path; RQ5 hedge (persist
   chat keywords).
5. **Session personalization wedge** — session-init Q1 read; problem
   selection over weak entities; persona conditioning; difficulty.
6. **Later, in rough order** — teacher dashboard (Q2); LLM-assisted Layer-1
   authoring (RQ6 memo is the spec); student-facing OLM; Hoot-chat evidence
   (RQ5 memo is the spec); Elo for difficulty matching; Layer-2 janitor.

Each phase is independently shippable and none requires revisiting a prior
phase's decisions.

## 12. Source documents

- Parent plan + RQ1/RQ6/RQ7 memos + RQ1–RQ7 briefs: workspace
  `docs/superpowers/specs/2026-06-10-apollo-kg-*.md`
- RQ2/RQ5 full memos: research session 2026-06-10 (key content reproduced
  above; citations in the memos)
- RQ3 spike: `scripts/spikes/rq3_edge_extraction.py`, `rq3_results.json`
- System ground truth: `docs/architecture/apollo.md` (verified 2026-06-10),
  `docs/shared-architecture/security.md`, migrations 006/018/019/021/022
