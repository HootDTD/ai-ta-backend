# Apollo Master Architecture & Workflow: KG → Grading Core → Learner Model

**Status:** Decided — **master document** (consolidated 2026-06-12). This
file is the single source of truth for the Apollo knowledge-graph, grading,
and learner-model build. It merges: Rev 1 of the architecture decision
(2026-06-10, converging the seven research tracks of
`../../../../docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-research-plan.md`);
Rev 2 (2026-06-12 — canonical graph-simulation grading core +
reference-anchored canonicalization); and the same-day external-review
hardening. The standalone grader spec
(`2026-06-12-apollo-canonical-graph-simulation-grader.md`) has been folded
into §6 of this document and deleted. Next artifact: phased implementation
plan (`superpowers:writing-plans`).
**Dates:** 2026-06-10 (Rev 1) · 2026-06-12 (Rev 2 + hardening + merge) ·
2026-06-15 (§8A added — the course → Apollo runtime-wiring cutover that
finishes migration 018, folding in the automatic-wiring handoff; no prior
decision changed, only the runtime read-path made explicit · §8B added
(decision §1.8) — auto-provisioning of curriculum from uploaded materials
pulled into v1, reversing decision 3's deferral and §11's auto-generation
anti-scope; fully automatic and model-gated, porting the pipeline mechanics
of the 2026-06-02 textbook-index spec onto the
Postgres/course-scoped/graph-simulation architecture)
**Inputs:** user interview (RQ4 + guardrails + 3 scope decisions,
2026-06-10); RQ2/RQ5 research memos; RQ3 live-GPT-4o spike
(`scripts/spikes/rq3_edge_extraction.py` + `rq3_results.json`); RQ1/RQ6/RQ7
external research memos (workspace
`docs/superpowers/specs/2026-06-10-apollo-kg-rq1-rq6-rq7-research-memos.md`);
grading-core design + reconciliation session (2026-06-12); external LLM
review triage (2026-06-12 — adopted: transcript audit, multi-path coverage,
abstention gates, decision table, extras-never-penalize, edge demotion,
shadow calibration, constrained diagnostics; rejected: per-equation symbol
tables, "simulation" renaming, polarity_state structures); second external
review (2026-06-12, implementation semantics — adopted nearly in full:
honest cross-store transaction story, `:Canon` key = entity id,
all-or-nothing event transactions + NULLS NOT DISTINCT, Δt anchored to Done
time, FOR UPDATE on learner rows, NaN/empty-case guards,
`comparison_confidence` defined, audit-failure → abstention, decision-table
edge row calibration-gated, alias provenance caps, reference-graph hash,
belief CHECKs, min-aggregation for gates, bounded assignment,
`search_space_id` backfill note).

---

## 0. The decision in one paragraph

Apollo's knowledge graph stops being a per-attempt grading scratchpad and
becomes the evidence layer of a persistent, interpretable learner model — the
paradigm-I leg missing from Hoot's II+III hybrid. Three layers plus a formal
grading core: **Layer 1**, a course-scoped concept ontology in Postgres
(`apollo_kg_entities` + prerequisite edges) **minted from reference
solutions** (not hand-authored) and projected into Neo4j as rebuildable
`:Canon` nodes; **Layer 2**, today's per-attempt evidence graphs in Neo4j,
made connected (typed-edge extraction with cross-turn linking), persisted
instead of deleted, and resolved against the problem's reference graph (and
through it, Layer 1) via `RESOLVES_TO` edges at Done-time; **the grading
core** (§6), an **asymmetric graph simulation over canonicalized graphs** —
`S_norm ⊑ R_norm` for soundness, `R_norm ⊑ S_norm` for coverage,
harmonic-mean near-bisimilarity — replacing per-node LLM coverage judgments
with an auditable algorithm whose findings drive both diagnostics and the
learner model; **Layer 3**, a per-(student, entity) **3-state Bayesian
belief** `{misconception, shaky, mastered}` living entirely in Postgres,
updated by a hand-set likelihood rule inside the existing Done transaction,
with an append-only event log (`apollo_mastery_events`) as the longitudinal
record and refit corpus. The v1 consumer is **session personalization**.
Because the learner model is a persistent cognitive profile, **Phase 1
retrofits auth + course scoping onto the existing unauthenticated `/apollo/*`
surface before anything else is built** (done — PRs in review as of
2026-06-11).

The division of labor, which every later decision serves:

```text
LLM for interpretation.
Normalizer for equivalence.
Graph simulation for grading.
Bayesian learner model for memory.
```

```
LAYER 3  Learner model (Postgres)                       ← NEW
         belief[3] per (user, course, entity); mastery, misconception flag
         updated at Done; append-only event log
              │ consumes events from
GRADING  Canonical graph simulation (Done-time)          ← NEW
CORE     S_norm ⊑ R_norm soundness · R_norm ⊑ S_norm coverage
         transcript-audited, abstention-gated; runs + findings
         persisted (Postgres, auditable)
              │ compares canonicalized
LAYER 2  Evidence graphs (Neo4j, per attempt)           ← EXISTS, being fixed
         typed nodes + extracted edges, cross-turn linked,
         PERSISTED; each node ─RESOLVES_TO→ reference/:Canon
              │ resolves against
LAYER 1  Course ontology (Postgres → :Canon projection) ← NEW — minted from
         entities + prereqs + misconception entities       reference solutions
```

## 1. Product decisions (binding)

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
     RQ6 extraction + teacher-approval workflow becomes its own later phase,
     reshaped as the question-bank workflow (decision 5, §8).
     *(Superseded by decision 8, 2026-06-15: the extraction workflow is now in
     v1, automatic and model-gated — §8B. The bernoulli seed survives as the
     bootstrap path, §8A.5.)*
  4. **Per-classroom isolation is an invariant** (user, 2026-06-10): the
     maximum span of correlation is the classroom. Concepts, entities,
     question banks, evidence graphs, and mastery never cross courses — the
     entire curriculum chain is scoped by `search_space_id` (§2), not just
     the learner-model rows. Two classrooms teaching the same physics
     concept have fully separate entities and statistics. Relaxing this
     (e.g., cross-class difficulty calibration) is a deliberate future
     decision, never an accident of shared rows.
  5. **Layer 1 is determined by the question bank, not the textbook**
     (user, 2026-06-10): the entity inventory emerges from the questions a
     course actually uses. Granularity follows evidence demand — a subtopic
     with no questions never becomes an entity (§8).
- **Scope decisions (user, 2026-06-12):**
  6. **Grading core is canonical graph simulation** — Done-time grading is
     an algorithm over canonicalized graphs (soundness `S ⊑ R`, coverage
     `R ⊑ S`, near-bisimilarity), not per-node LLM coverage judgment. LLMs
     are confined to interpretation (parsing, one constrained adjudication
     call, one transcript-audit call) and to *explaining* computed findings
     (diagnostics) — never to re-grading. Supersedes Rev 1 anti-scope "no
     grading redesign." Full detail: §6.
  7. **Reference-anchored canonicalization** — completing decision 5: the
     reference solutions *mint* the canonical vocabulary. Students resolve
     per-attempt against the problem's reference nodes (+ misconception
     entities); reference nodes link to Layer-1 entities once, at problem
     promotion, human-gated. No upfront manual canonical-key authoring.
- **Scope decisions (user, 2026-06-15):**
  8. **Apollo curriculum is auto-provisioned from uploaded materials in v1.**
     Reverses decision 3 (Layer-1 authoring deferred) and the §11
     "no automatic problem/reference-solution generation" anti-scope. On every
     material upload a pipeline scrapes questions, **finds-or-generates** a
     reference solution (prefer one printed in the material; RAG-ground the
     rest), and promotes problems to teachable **fully automatically** — an
     LLM pairing/correctness validator + the automated eight-gate promotion
     lint replace the teacher approval gate; no human in the loop. Stays
     course-scoped (§1.4) and Postgres-authoritative (§2). Safety rests on the
     validator plus two backstops: the §6.7 shadow/calibration gate and a new
     per-problem anomaly quarantine. v1 assumes the generation LLM does not
     fabricate a coherent-but-wrong solution; the backstops are
     defense-in-depth that do not rely on that assumption. Full pipeline: §8B.

## 2. Storage layout

**Postgres is the system of record for Layer 1 (authoring), Layer 3
(entirely), and the grading core's comparison runs/findings. Neo4j holds
Layer 2 plus a disposable `:Canon` projection of Layer 1 so `RESOLVES_TO` is
a real edge.** Layer 1 extends the existing migration-018 curriculum tables
(`apollo_subjects` / `apollo_concepts` / `apollo_misconceptions`) — the
codebase already treats curriculum as Postgres rows.

Three corrections to the original RQ2 sketch, applied below: the student key
is a real `auth.users` UUID (not `student_id TEXT`); learner-model rows carry
a `search_space_id` FK (per-course mastery records); and — per the isolation
invariant (§1.4) — **the curriculum chain itself is course-scoped**:
`apollo_subjects` gains `search_space_id INTEGER NOT NULL REFERENCES
aita_search_spaces(id) ON DELETE CASCADE`, so concepts, entities, prereqs,
and problems all inherit course ownership through existing FKs (migration-018
tables are global today). `canonical_key` uniqueness becomes per-concept, not
global.

> **Migration numbering caution:** 023 has a numbering collision between the
> prod and test projects, and 024 (textbook NUL-bytes) is written but applied
> nowhere yet. Verify the next free number before authoring DDL; the
> implementation plan owns the final numbers.
> **Backfill landmine:** `apollo_subjects.search_space_id` lands `NOT NULL`
> on a table with existing global rows — the migration must ship an explicit
> backfill (map existing subjects to their course, or nullable-then-tighten
> in two steps); as written it would fail on any populated environment.

### Learner-model migration (target shape; exact DDL in the implementation plan)

```sql
-- apollo_subjects: + search_space_id FK (isolation invariant, §1.4)

-- LAYER 1: skill inventory (course-scoped via concept → subject → search space)
CREATE TABLE apollo_kg_entities (
  id            BIGSERIAL PRIMARY KEY,
  concept_id    BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
  canonical_key TEXT   NOT NULL,           -- 'bernoulli_principle/eq.bernoulli_full'
  kind          TEXT   NOT NULL,          -- concept|equation|condition|definition|procedure
                                          -- |misconception (canon.misc.* — §5/§6)
  display_name  TEXT   NOT NULL,
  payload       JSONB  NOT NULL DEFAULT '{}',  -- symbolic form, applies_when,
                                               -- opposes_entity_id for misconceptions, …
  aliases       JSONB  NOT NULL DEFAULT '[]',  -- grows from the resolution log
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (concept_id, canonical_key)       -- unique per concept, NOT global —
                                           -- same concept in two courses = two entities
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
  belief             REAL[]  NOT NULL CHECK (array_length(belief, 1) = 3),
                                          -- [p_misc, p_shaky, p_mastered];
                                          -- sums-to-1 validated app-side
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
                                        -- (sourced from simulation findings, §6)
  misconception_code TEXT    NULL,
  parser_confidence  REAL    NULL,      -- RQ1: needed to weight observations in refit
  grader_confidence  REAL    NULL,      -- = normalization × comparison confidence
  negotiation_move   TEXT    NULL,      -- challenge|paraphrase|skip|null
  reference_step_id  TEXT    NULL,      -- which authored step → per-step difficulty later
  prior_belief       REAL[]  NOT NULL CHECK (array_length(prior_belief, 1) = 3),
  posterior_belief   REAL[]  NOT NULL CHECK (array_length(posterior_belief, 1) = 3),
  mastery_after      REAL    NOT NULL,  -- Q3 trend = direct read of this column
  dt_days_since_last REAL    NULL,      -- lag feature; fit decay k later
  evidence_node_ids  JSONB   NOT NULL DEFAULT '[]',  -- Neo4j bridge (same pattern
                                                     -- as apollo_kg_negotiations)
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (attempt_id, entity_id, event_kind)
  -- NULLS NOT DISTINCT: a plain UNIQUE never conflicts on NULL attempt_id
  -- (deleted attempts; the reserved chat_question events), so retries could
  -- double-insert. Belt-and-braces only — the real guarantee is
  -- transactional (§3: events + belief update commit all-or-nothing;
  -- re-runs supersede).
);
CREATE INDEX ON apollo_mastery_events (user_id, entity_id, created_at);
CREATE INDEX ON apollo_mastery_events (entity_id, created_at);
-- plus: learner_update_pending BOOLEAN on apollo_problem_attempts (see §6/§7)
```

### Grading-core tables (target shape, exact DDL in the plan)

```sql
-- One row per Done-time comparison; makes the grader auditable
CREATE TABLE apollo_graph_comparison_runs (
  id                       BIGSERIAL PRIMARY KEY,
  attempt_id               BIGINT  NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
  user_id                  UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  search_space_id          INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  coverage_score           REAL NOT NULL,   -- max over declared paths of R_norm ⊑ S_norm
  soundness_score          REAL NOT NULL,   -- S_norm ⊑ R_norm (contradictions only)
  bisimilarity_score       REAL NOT NULL,   -- harmonic_mean(soundness, coverage)
  node_coverage_score      REAL, edge_coverage_score REAL,
  scoping_score            REAL, usage_score REAL,
  procedure_order_score    REAL, dependency_score REAL,
  contradiction_score      REAL,
  normalization_confidence REAL NOT NULL,
  abstained                BOOLEAN NOT NULL DEFAULT false,
  abstention_reasons       JSONB   NOT NULL DEFAULT '[]',
  comparison_version       TEXT NOT NULL,   -- algorithm version, for replays
  reference_graph_hash     TEXT NOT NULL,   -- the reference graph AS GRADED —
                                            -- teacher edits after grading must
                                            -- not make old runs unexplainable
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, comparison_version)
  -- Re-run semantics: a re-run at the same version SUPERSEDES — delete the
  -- prior run + its findings (and its events, if any committed) and
  -- re-insert, in one transaction. The constraint guards accidental
  -- double-execution; it must never crash a legitimate retry.
);

-- Structured evidence behind every score; diagnostics read THIS, not the transcript
CREATE TABLE apollo_graph_comparison_findings (
  id                 BIGSERIAL PRIMARY KEY,
  run_id             BIGINT NOT NULL REFERENCES apollo_graph_comparison_runs(id) ON DELETE CASCADE,
  entity_id          BIGINT REFERENCES apollo_kg_entities(id),
  finding_kind       TEXT NOT NULL,  -- covered_node|missing_node|matched_edge|missing_edge
                                     -- |unsupported_extra|contradiction|unresolved|alternative_path
  score              REAL, confidence REAL,
  student_node_ids   JSONB NOT NULL DEFAULT '[]',
  reference_node_ids JSONB NOT NULL DEFAULT '[]',
  student_edge_ids   JSONB NOT NULL DEFAULT '[]',
  reference_edge_ids JSONB NOT NULL DEFAULT '[]',
  evidence_spans     JSONB NOT NULL DEFAULT '[]',  -- quoted transcript spans (audit/resolver)
  message            TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The **reference-node → Layer-1 entity link** (assigned at promotion, §8) is
persisted alongside the problem (per-problem mapping table or keys embedded
in `reference_solution` — planner picks; it must survive problem edits and be
queryable for the selection join in §1-Q1). The reference format also
declares **acceptable solution paths** (v1: one path per problem; coverage =
max over paths, §6) and each misconception entity carries an **opposes-link**
to the entity it contradicts (column or payload field on
`apollo_kg_entities` — planner picks) so the event layer can detect
conflicting evidence about one concept.

Elo columns (`elo_skill_after`, `entity_difficulty`) are **omitted** — Elo is
deferred and backfillable by replaying `score` + ordering from this log.

### Neo4j

- **`:Canon` projection of Layer 1:** idempotent seeder — MERGE on a single
  unique property `key = <apollo_kg_entities.id>` (the Postgres surrogate
  id; `search_space_id`, `concept_id`, and `canonical_key` carried as
  properties). Postgres uniqueness is `(concept_id, canonical_key)`, so a
  key synthesized from `search_space_id:canonical_key` alone could fuse two
  concepts' same-named entities (two fluid concepts both minting
  `eq.continuity`) into one node and cross-contaminate every RESOLVES_TO
  edge — the concept-prefixed-slug naming convention would usually mask
  this, but the entity id makes it impossible and survives key renames.
  Rebuildable from Postgres at any time, never authored in Neo4j. Sidesteps
  dual-write drift. `:Canon` nodes also carry `search_space_id` so every
  Cypher filter can enforce the isolation invariant. The projection includes
  misconception entities (`canon.misc.*`, sourced from
  `apollo_misconceptions`) — they are competing resolution targets (§5).
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
   grade + XP in one Postgres transaction; the comparison run and learner
   update join it (with the §6 split for retry semantics). Neo4j would create
   a cross-store two-phase problem on the most important write in the product.
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

**Evidence provenance:** the per-entity evidence items below are not LLM
coverage verdicts; they are **simulation findings converted to events through
the §6 decision table**. Every event is backed by a persisted finding row
with node/edge IDs and (where applicable) quoted transcript spans.

### Update rule (the actual math)

- **Cold start:** `belief = [0.20, 0.60, 0.20]` (mastery 0.40 — "assume
  nothing, slightly pessimistic"; BKT-analogue P(L0)≈0.3–0.4).
- **Step 0 — between-session decay toward the prior:**
  `belief ← (1−w)·belief + w·prior` where `w = 1 − e^(−k·Δt_days)`, `k = 0.05`
  (≈14-day half-life; Pelánek's k≈0.1 halved because Apollo evidence is
  weekly/sparse). Unseen entities drift to "unknown," never to "confidently
  wrong." No spacing-effect/DAS3H modeling. **Δt is anchored to the
  attempt's freeze/Done timestamp, never `now()`** — a janitor retry three
  days later must decay exactly as the original run would have.
- **Step 1 — evidence likelihood vector** `L = [L_misc, L_shaky, L_mastered]`,
  start `[1,1,1]`, multiply per evidence item:

  | Evidence (per entity, from §6 findings) | L_misc | L_shaky | L_mastered |
  |---|---|---|---|
  | covered, score `s ∈ [0,1]` | `(1−s)^γ` | `1 − \|2s−1\|^γ` | `s^γ` (γ = 1.5; mid scores land on "shaky") |
  | missing | 0.7 | 1.0 | 0.4 |
  | misconception (code c) | 3.0 | 1.0 | 0.2 (sets `misconception_code = c`) |
  | corrected | 0.5 | 1.5 | 1.2 |
  | negotiation move | — | ×1.2 | ×1.1 |

  **Blank cells are ×1.0 (no effect) — never 0.** A multiplicative zero
  would annihilate a belief component permanently and unrecoverably.

- **Step 2 — confidence damper:** `q = parser_confidence · grader_confidence`,
  where `grader_confidence = normalization_confidence ×
  comparison_confidence` from the comparison run. **`comparison_confidence`
  is defined as 1.0 in v1** (the comparison algorithm is deterministic once
  resolution is fixed), reduced below 1.0 only when a finding depends on an
  ambiguous global assignment or a path tie — so in practice v1 grader
  confidence equals the event's resolution confidence;
  `L ← q·L + (1−q)·[1,1,1]`. A low-confidence resolution barely moves belief;
  a high-confidence contradiction (resolved to `canon.misc.*` by symbolic or
  alias match) moves it hard. Confidence caps by resolution method:
  exact 1.00 · symbolic 0.98 · alias 0.92 · fuzzy 0.80 · LLM-adjudicated
  0.75 · transcript-audit 0.75 · unresolved 0.00 (no event). Damping handles
  noise only — *biased* evidence is handled upstream by the §6 abstention
  gates, which withhold the Layer-3 update entirely rather than shrink it.
- **Step 3 — Bayes + renormalize:** `belief ← normalize(belief ⊙ L)`;
  `mastery = 0.5·p_shaky + p_mastered`;
  `confidence = 1 − entropy(belief)/log 3`.
- **Step 4 — append the event row** (prior/posterior belief, score,
  confidences, `mastery_after`).

Pure arithmetic over Postgres rows inside the Done transaction — no LLM call,
no Neo4j write, fires once per Done episode (never per turn).

**Concurrency + retry rules (binding):** the learner-state rows are read
with `SELECT … FOR UPDATE` (or a per-(user, entity) advisory lock) — the
update is read-modify-write, and a janitor retry racing a live Done would
otherwise clobber a posterior. Events + belief updates for an attempt commit
in **one all-or-nothing Postgres transaction**: a partially-applied learner
update cannot exist, so a retry either re-runs the whole step (nothing
committed) or skips it (everything committed). This is also what makes
"the retry produces a different event_kind than the first run" safe — the
first run's events never committed, so nothing double-counts.

### Readouts

| Consumer | Readout |
|---|---|
| Session personalization (v1) | `mastery` + `misconception_code` from `apollo_learner_state`. Persona: misconception flag active → "be extra confused about {code}". Problem selection: prefer entities with mastery 0.3–0.7 (teachable edge) whose prerequisites are mastered (`apollo_entity_prereqs`, in-memory). Low `confidence` → re-probe. |
| Teacher dashboard | `AVG(mastery)` and `% students with p_misc ≥ 0.5` per entity per course; "stuck" = p_misc ≥ 0.5 for ≥2 consecutive episodes or flat/declining mastery across last 3 events. |
| Longitudinal | `SELECT mastery_after, created_at FROM apollo_mastery_events …` — a direct time series. |

The belief vector renders directly as an open learner model (three stacked
bars) — the interpretability the MATHia argument demands. Every bar is
drill-down-able to the comparison findings that moved it.

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

## 4. Extraction (RQ3 — spike-verified; hard prerequisite of the grading core)

**One-call extraction.** The per-turn parser call extracts nodes AND typed
edges in a single GPT-4o strict-structured-outputs call (`json_schema`), with
the edge vocabulary (`EDGE_ALLOWED_PAIRS`) in the prompt and the existing
attempt graph (node ids + types + labels) passed as context so new entries
link across turns. No second canonicalize call per turn — resolution happens
once per attempt at Done (§5).

**Parser recall is the grading ceiling.** The graph simulation can only
grade what was extracted and resolved; a student statement the parser
dropped would otherwise be graded "missing" (where the old LLM diff might
have credited it from the transcript). Two consequences: the extraction
upgrade is a **hard prerequisite** of the grading core (orphan-node
reduction, 20 → 6 in the spike, directly raises the grading ceiling), and
the **transcript auditor (§6) gates every `missing` event** so parser recall
alone never decides absent knowledge. The unresolved rate per run is
monitored as both a parser-recall and curriculum-gap metric.

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
dropped edges are logged with reason). Parser edges carry a **provenance
tag**: `explicit` (directly supported by wording) vs `inferred` — consumed by
the §6 edge weighting.

## 5. Resolution — reference-anchored, shared by grading and learner model

There is **one resolver**. It serves both the grading core (§6) and the
learner-model bridge; it is never implemented twice with different tier
tables.

### Targets — reference-anchored canonicalization (decision §1.7)

Per attempt, student evidence nodes resolve against a **closed candidate
set**: *this problem's reference nodes* + *the course's misconception
entities* (`canon.misc.*`). This makes per-attempt resolution a small
matching problem (~15–25 candidates) instead of a search over a global
ontology, and it removes upfront canonical-key authoring entirely.

Cross-problem identity — which Layer 3 requires ("incompressible" in problem
5 and problem 12 must be the same entity row) — comes from a second,
amortized link: **at problem promotion** (once per problem, human-gated —
question-bank workflow, §8), each reference node is linked to a Layer-1
entity: a new term mints a new entity; a term matching an existing course
entity merges with it (LLM drafts the merge, teacher approves). The
composition `student node → reference node → Layer-1 entity` gives the
learner model its stable keys. The reference graph is therefore canonical by
construction — `normalize(R)` in v1 is identity plus a **validation pass**
(every reference node must carry an entity link and paths must be declared;
failure blocks grading) and becomes real fuzzy work only when references are
machine-generated (v2).

### Matching order — content seeds, structure corroborates

1. **Content tiers seed anchor matches** (node content is the strong
   signal): exact key → SymPy structural equivalence (reuses
   `parse_zero_form`; sign-exact; runs under the concept's canonical symbol
   context and declared mappings like `d = 2r`) → normalized alias /
   `normalization_map` match → RapidFuzz ≥ 0.9.
2. **Structural propagation narrows the remainder:** if student node X
   matched reference node Y, X's edge-neighbors are prioritized against Y's
   corresponding edge-neighbors. Edges suggest candidates; they never assert
   a match by position alone (student graphs are edge-sparse — anchoring
   identity on edges would invert the reliability order).
3. **Structure as confidence modifier and veto:** neighborhood agreement
   boosts confidence; structurally incoherent mappings are rejected; **type
   compatibility is a hard constraint** (condition ↔ condition only).
4. **Global consistency:** the mapping is solved jointly (maximize total
   match score). Many student nodes may merge into one reference node
   (paraphrase evidence); one student node never splits across several.
   **Bounded:** greedy assignment in descending match-score order is
   sufficient at v1 scale (~15–25 candidates); cap the student nodes
   considered (~150) and route pathological graphs to abstention rather
   than letting an unbounded solve hang the Done path.
5. **One LLM adjudication call** for the remaining ambiguity, candidate list
   = the constrained set from steps 2–4, "return empty when unsure";
   hallucinated keys → hard `ResolutionInvalidOutputError`.

### Anti-over-normalization guardrails

- **Misconception entities compete in every resolution.** A polar near-miss
  ("pressure *increases* with speed") scores higher against
  `canon.misc.pressure_velocity_inverted` than against the lexically-close
  reference node — wrong claims are out-competed, not merely thresholded.
  This is also how contradiction detection stays algorithmic (§6).
- **Polarity/direction screen** on fuzzy matches above threshold (symbolic
  matching is already sign-exact).
- **Do not over-normalize variants:** `bernoulli_full`,
  `bernoulli_horizontal`, `bernoulli_no_loss`, `bernoulli_with_head_loss`
  stay distinct entities; assumptions are represented by `SCOPES` edges, not
  collapsed into one generic node.
- **Below threshold → `unresolved`, never snap.**

### Mechanics

- **Key:** `canonical_key` slug with a Neo4j uniqueness constraint on the
  `:Canon` projection. Never MERGE-on-UUID (degenerates to CREATE). Evidence
  nodes keep `CREATE` — episodic events are intentionally never deduped;
  dedup happens by convergence of `RESOLVES_TO` edges onto `:Canon`.
- **When:** at Done, batched per attempt — chat-loop latency untouched; one
  LLM resolution call per attempt max (plus the §6 transcript-audit call).
- **NO-FALLBACK, two cases:** per-node non-match is **data, not an error**
  (`resolution: 'unresolved'`, no edge, an `unresolved` finding, no mastery
  event, logged — the unresolved rate is itself a parser-recall and
  curriculum-gap metric and an alias source). Infrastructure failure raises
  named `ResolutionUnavailableError` and must NOT void the earned grade:
  grade commits, `learner_update_pending = true` on the attempt,
  janitor/next-session retry; the update is idempotent (MERGE edges + the
  `(attempt_id, entity_id, event_kind)` uniqueness key).
- Confidence caps by method feed the §3 damper: exact 1.00 · symbolic 0.98 ·
  alias 0.92 · fuzzy 0.80 · LLM 0.75 · transcript-audit 0.75 ·
  unresolved 0.00.

## 6. The grading core: canonical graph simulation at Done

### 6.1 Goal and the comparison

Grading should not depend on asking an LLM whether each student node
"covers" a reference node. Raw student and reference nodes differ even when
they mean the same thing (`A = πr²` vs `A = πd²/4`); normalization (§5) maps
both into canonical space, and then the comparison is an algorithm:

```
S_norm = canonicalized student graph     R_norm = canonicalized reference graph

S_norm ⊑ R_norm   → soundness   (is everything the student said supported?)
R_norm ⊑ S_norm   → coverage    (did the student cover the reference solution?)
bisimilarity_score = harmonic_mean(soundness, coverage)
```

Harmonic mean because a strong answer must be both not-wrong AND
not-incomplete. Four outcomes fall out — sound-but-incomplete ·
complete-but-unsound · complete-and-sound · incomplete-and-wrong — mapping
directly onto the §3 states.

Degenerate cases are defined, not discovered: `harmonic_mean(a, b) = 0`
whenever `a + b = 0` (never NaN — a NaN written to a `REAL NOT NULL` column
either crashes the insert or poisons every downstream aggregate). An empty
student graph (student says nothing, hits Done) grades coverage 0, soundness
1 (vacuously — nothing claimed, nothing wrong), bisimilarity 0. An empty
declared-path list is a reference-validation failure that blocks grading at
pipeline step 3 — which is why the §8 seed script must declare paths for the
bernoulli problems, or the day-one fixtures fail there.

> **Terminology (binding for implementers):** the implementation primitive
> is **weighted asymmetric canonical coverage with confidence** —
> "simulation" names the conceptual frame and "near-bisimilarity" names the
> summary score, not a formal transition-system algorithm. Do not build
> strict graph matching; student explanations are sparse, reordered, and
> linguistically messy by design of the task. Raw graphs preserve the
> student's wording (they are evidence); comparison operates only on
> canonical graphs.

### 6.2 Scoring rules that are binding, not tunable

- **Coverage = max over declared acceptable paths.** A reference graph
  declares one or more acceptable solution paths (v1 authors a single path —
  the degenerate case); a student solving via a valid alternative route
  (energy conservation instead of Bernoulli) is graded against the path they
  took, never punished against the authored one. The schema supports
  multiple paths from day one because retrofitting path structure later is
  expensive; teachers add paths when calibration surfaces false-missings.
- **Soundness penalizes only resolved contradictions:**
  `soundness_score = 1 − penalty(contradictions)`. Contradiction is detected
  by resolution to `canon.misc.*` (negative knowledge), not by LLM judgment.
  Unsupported extras and unresolved nodes carry **zero** soundness penalty —
  they cannot be classified structurally, and penalizing them punishes
  strong students for knowing more than the reference. Richer labels for
  extras (valid extension / irrelevant / unknown) may be produced by the
  diagnostic LLM for feedback wording only — never for scoring or events. A
  student node matching nothing is `unsupported_extra` — honestly distinct
  from `contradiction` (an unknown wrong claim is NOT detected; only
  enumerated negative knowledge is).
- **A `missing` event requires a negative transcript audit** (§6.4,
  component 7). Parser recall alone never decides absent knowledge.
- **Edges are diagnostic-grade in v1.** Learner-model events come from node
  findings and misconception resolutions ONLY; edge findings (missing
  SCOPES/USES/PRECEDES) feed rubric sub-scores and the diagnostic ("connect
  this assumption more clearly"), never Layer 3 — students imply relations
  without stating them, and edge recall is unproven. `explicit` edges
  outweigh `inferred`; `dependency_score` (loose any→any DEPENDS_ON grammar)
  gets the lowest weight of all. Revisit edge-driven events only after
  calibration proves edge recall.
- **Procedure order is non-strict:** `procedure_order_score` penalizes only
  *inversions* of true reference PRECEDES dependencies, never the absence of
  a stated order. Students legitimately teach steps out of sequence.
- **Findings convert to events through the fixed decision table** (§6.5) —
  never ad hoc in code.
- **Abstention gates protect Layer 3 from biased evidence** (§6.6).
- **Tier distribution is tracked per run.** If many nodes resolve at tiers
  4–5 (fuzzy/LLM), LLM judgment has merely moved from comparison to
  resolution — still a win (a closed candidate list is a far more
  constrained task than open coverage judgment), but the claim stays honest
  only if measured.

### 6.3 Components (`apollo/graph_compare/`)

| Component | Job |
|---|---|
| `validator.py` | Graph grammar only (not physics truth): node/edge types, endpoint pairs (reuses `EDGE_ALLOWED_PAIRS` — PRECEDES: step→step; USES: step→equation; SCOPES: condition/simplification→equation; DEPENDS_ON: any→any; RESOLVES_TO: evidence→Canon), scoping consistency (`attempt_id`/`user_id`/`search_space_id`), no invalid PRECEDES cycles, required fields. Example rejection: `equation SCOPES condition` (SCOPES must originate from a condition/simplification). |
| `normalizer.py` / `symbolic.py` / `resolver.py` | The §5 shared resolver — reference-anchored targets, content-first tiers, structural corroboration, misconception competition, global assignment. |
| `canonical_graph.py` | Build S_norm/R_norm: merge student nodes resolving to the same target (preserving source raw node IDs as evidence — e.g. "density stays the same" + "constant density" become one canonical node with two supporting spans), normalize edges after endpoint resolution, drop unresolved edges from comparison, retain unresolved nodes as findings. |
| `simulation.py` | Coverage (`R ⊑ S`, per path, max) and soundness (`S ⊑ R`, contradictions-only) passes. Sub-scores: node/edge coverage, scoping, usage, procedure order, dependency, contradiction. |
| `bisimilarity.py` | Harmonic mean of soundness and coverage. |
| `transcript_audit.py` | **The missing-node gate.** One batched Done-time LLM call (never per-node): input = the simulator-flagged missing reference entities (display names + aliases) + the raw attempt transcript; output per entity = supporting span or null. Constrained *verification*, not re-grading — never scores, never sees reference structure. Span found → finding converts to `partial`/`covered` at `method = transcript_audit`, confidence ≤ 0.75, span persisted as quoted evidence AND as an **alias candidate**. No span → `missing` confirmed. **Failure mode (binding):** audit infrastructure failure (timeout/error) → suppress ALL `missing` events this run via the abstention gate, named error, diagnostic-only for those entities — NEVER "skip audit, emit missing"; that try/except default is exactly the biased-evidence path abstention exists to prevent. **Context budget:** long sessions are chunked (per-turn windows, entities re-asked per chunk, spans deduped) so the call cannot blow context. **Alias provenance (anti-laundering):** span-derived alias candidates enter the §8 teacher approval queue; until approved, a learned alias resolves at the transcript-audit cap (0.75), never the alias tier (0.92) — a wrong span must not upgrade its own future confidence. |
| `findings.py` / `events.py` | Findings (`covered_node`, `missing_node`, `matched_edge`, `missing_edge`, `unsupported_extra`, `contradiction`, `unresolved`, `alternative_path`) → learner-model events (`covered|partial|missing|misconception|corrected`) via the §6.5 decision table. |

`done.py` orchestrates; it contains no graph-comparison logic.

### 6.4 Done-time pipeline

```
1.  Freeze attempt.
2.  Load frozen student graph from Neo4j.
3.  Load authored reference graph (entity links + declared paths validated; failure blocks grading).
4.  Validate raw student graph.
5.  Resolve student nodes (§5).
6.  Reference normalization = identity + validation (real fuzzy work only when references are machine-generated, v2).
7.  Write RESOLVES_TO edges for student evidence nodes.
8.  Build canonical student graph S_norm.
9.  Build canonical reference graph R_norm (per declared path).
10. Coverage simulation per path: coverage_score = max over paths of R_norm ⊑ S_norm.
11. Soundness simulation: S_norm ⊑ R_norm (contradictions only penalize).
12. Transcript audit: one batched check of missing reference nodes against the raw transcript.
13. Compute near-bisimilarity score.
14. Apply abstention gates (§6.6).
15. Persist comparison run + findings (always — even on abstained runs).
16. Convert findings into learner-model events (decision table, §6.5).
17. Update Layer-3 learner model (§3; skipped if abstained).
18. Generate diagnostic from structured findings + quoted spans only (§6.8).
```

**Transaction story (binding — there is no cross-store transaction):**
Neo4j and Postgres cannot commit atomically, so the pipeline is staged:
(1) the **grade** (rubric/XP — the student-facing result) commits first, in
Postgres, before any cross-store work; (2) all Neo4j writes (step 7
RESOLVES_TO) are **idempotent MERGEs** — re-running them is always safe, and
edges orphaned by a mid-pipeline crash are harmless and reconciled on retry;
(3) the comparison run + findings (step 15) commit in one Postgres
transaction (supersede semantics on re-run, §2); (4) events + learner update
(steps 16–17) commit in one all-or-nothing Postgres transaction (§3
concurrency/retry rules). Any failure from step 5 onward sets
`learner_update_pending = true` and the retry re-runs **from resolution**
idempotently — the pending flag covers the whole cross-store window, not
just 16–17. The student's grade is never voided by grading-pipeline
infrastructure failure (§5 NO-FALLBACK).
**Rubric mapping:** `compute_rubric` currently consumes the LLM coverage
verdict map; the implementation plan must define the findings → rubric input
mapping (or adapt the rubric to consume the sub-scores directly) — explicit
task, not discovered mid-implementation.

### 6.5 Finding → event decision table (binding)

Without this table, "partial or missing" gets resolved ad hoc inside
`events.py`. The mapping is fixed:

| Findings for entity E | Event | Score / notes |
|---|---|---|
| `covered_node`, required edges present | `covered` | s ≈ 1.0 at deterministic tiers, scaled by resolution confidence |
| `covered_node`, required edge missing | `covered` (v1) | full resolution score; the edge gap is a diagnostic flag only. The `partial` (s ≈ 0.5–0.7) variant of this row is **calibration-gated**: enabling it before edge recall is proven would let an edge gap halve a student's score — exactly the edge-driven Layer-3 bias the §6.2 demotion rule forbids |
| `missing_node` AND transcript audit negative | `missing` | s = 0.0 — a `missing` event REQUIRES a negative audit |
| `missing_node` BUT transcript audit finds a span | `partial` or `covered` | method = `transcript_audit`, confidence ≤ 0.75; span persisted + alias candidate |
| `contradiction` (resolved to `canon.misc.*` at ≥ gate confidence) | `misconception` | s = 0.0; `misconception_code` from the entity |
| `contradiction` in an earlier turn, `covered_node` later on the opposed entity | `corrected` | turn order from node `created_at` |
| `covered_node` earlier, `contradiction` later | `misconception` | last position wins |
| both present, order ambiguous | `partial` (low confidence) | plus a `mixed-understanding` diagnostic flag |
| `unsupported_extra` | **no event** | diagnostic may label it (extension / irrelevant) for wording only |
| `unresolved` | **no event** | finding only; counts toward the abstention gate |

**Opposes-links make the conflict rows detectable.** Each misconception
entity carries an `opposes` link to the entity it contradicts. Merging never
collapses conflicting statements (they resolve to *different* canonical
targets — the entity vs `canon.misc.*`), but without the opposes-link the
event layer cannot see that two findings concern the same concept and apply
the order rule.

### 6.6 Abstention gates (hard, not damped)

Confidence damping handles *noisy* evidence; abstention handles *biased*
evidence. Parser misses skew systematically toward `missing`, so many small
damped updates still accumulate into confident wrong beliefs. Some runs must
produce **no learner update at all**, not a weak one.

Gates (hand-set v1 values, tuned from calibration data):

```text
unresolved_rate > 0.35
→ no learner update; diagnostic-only run

parser_confidence < 0.6
→ suppress `missing` events (transcript audit may still upgrade to partial/covered)

reference graph fails validation (missing entity links, undeclared paths)
→ block grading entirely; named error (NO-FALLBACK)

misconception resolution confidence < 0.8
→ no misconception event; finding persists for diagnostic review

transcript-audit call fails (timeout/error)
→ suppress all `missing` events this run (the audit is load-bearing for
  them); named error; covered/partial/misconception events unaffected
```

`parser_confidence` is per-turn; the gate compares the **min over the
attempt's turns** — one unparseable turn should trip the gate, and a mean
would hide it.

An abstained run still persists its comparison run + findings (the grader
stays auditable) and still produces the diagnostic; it only withholds
Layer-3 updates. Abstention reasons are recorded on the comparison run.

### 6.7 Calibration gate (before Layer 3 trusts the grader)

`compute_coverage` is not deleted when the grading core lands — it runs in
**shadow** alongside the simulator during the pilot window, and Layer-3
updates from simulation events are enabled only after calibration review.
Tracked per run:

```text
false-missing rate            (vs transcript audit + spot human labels)
false-misconception rate
resolution tier distribution  (how much falls to fuzzy/LLM tiers)
unresolved rate
edge extraction reliability   (explicit vs inferred)
agreement: simulator verdicts vs old LLM diff vs human labels
```

Until the gate opens, simulation findings drive the diagnostic and rubric
display, and events are emitted diagnostic-only/low-confidence. One bad
grade becoming persistent memory is the failure this prevents.

### 6.8 Diagnostics constraints (binding)

The diagnostic LLM *explains* computed findings; it must be structurally
unable to re-grade. If it reads the raw answer freely, it will eventually
contradict the findings ("you correctly used continuity" against a `missing`
finding) and destroy trust in the whole pipeline.

- **Input = findings only**, with the raw transcript present *only* as
  quoted evidence spans attached to individual findings (from the resolver
  and transcript auditor) — never as free text inviting re-interpretation.
- **Post-check before the diagnostic is returned:** it must not claim
  covered what findings mark missing; must not introduce misconceptions not
  in the findings; must reference evidence spans where they exist. A failed
  post-check regenerates once, then falls back to a template rendering of
  the findings (NO-FALLBACK: logged, visible).

### 6.9 Worked example (Bernoulli)

Reference graph (single declared path): conditions C1 steady flow ·
C2 incompressible · C3 negligible losses · C4 same streamline ·
C5 horizontal pipe; equations E1 continuity · E2 circular area ·
E3 full Bernoulli · E4 horizontal Bernoulli; procedure P1 area ratio ·
P2 compute v2 · P3 apply Bernoulli · P4 cancel elevation · P5 solve P2.

Student: *"Density is constant, so use Bernoulli. Area is pi r squared, so
the smaller pipe has four times the speed. Pressure is lower at the narrow
part."*

Resolution: "density is constant" → `canon.cond.incompressible` (alias);
"A = πr²" → `canon.eq.circular_area` (symbolic, mapping d = 2r);
"use Bernoulli" → `canon.eq.bernoulli_full`; "four times the speed" →
`canon.proc.compute_v2`; "pressure is lower at narrow part" →
`canon.def.pressure_velocity_tradeoff`.

Coverage: covered = incompressible, circular area, Bernoulli, compute v2;
missing (audit-confirmed) = steady flow, negligible losses, same streamline,
horizontal simplification, explicit continuity, final pressure solve.
Soundness: no contradiction (nothing resolved to `canon.misc.*`). Verdict:
high soundness, medium coverage, medium near-bisimilarity — **sound but
incomplete**. Events: covered ×2 (incompressibility, circular area),
partial (velocity computation), missing (assumptions, final solve).

### 6.10 Grading-core risks

1. **Parser recall is the grading ceiling.** Mitigations (all mandatory):
   transcript auditor gates every `missing` event; the §4 extraction upgrade
   lands first; unresolved nodes surface as findings, never disappear;
   abstention gates stop biased low-quality runs from updating Layer 3; the
   confidence damper handles residual noise; unresolved rate monitored.
2. **Resolver tier drift.** If many nodes fall to fuzzy/LLM tiers, LLM
   judgment has moved rather than vanished. Tracked per run (§6.7) so the
   claim stays honest.
3. **Edge sparsity.** Handled by edge demotion (no Layer-3 events from
   edges) and provenance-aware weights; revisit post-calibration.
4. **Unknown misconceptions are not detected as contradictions** — only
   enumerated `canon.misc.*` entities are. Honest limitation; unknown wrong
   claims land as `unsupported_extra` (no penalty, no event) and surface in
   diagnostics for teacher review, which is also the feed for growing the
   misconception bank.

### 6.11 Adversarial test fixtures (required by the implementation plan)

Each fixture ships with expected findings AND expected learner events, so
the spec is executable:

```text
valid alternative solution path (energy conservation, not Bernoulli)
→ covered via path B; zero false missings

correct final answer, thin explanation
→ low coverage, high soundness; no misconception events

wrong final answer, mostly correct concepts
→ covered nodes + contradiction on the final relation

polar near-miss ("pressure increases with speed")
→ resolves to canon.misc.*, never to the lexically-close reference node

conflicting statements, misconception first then correct
→ corrected (decision table, turn order)

conflicting statements, correct first then misconception
→ misconception (last position wins)

vague pronouns ("it increases there")
→ unresolved; no event; counts toward abstention

nonstandard notation / heavy paraphrase
→ resolved at alias or symbolic tier; covered

parser misses a key sentence
→ transcript audit finds the span; partial/covered at ≤0.75; NO false missing

reference omits a valid assumption the student states
→ unsupported_extra; zero soundness penalty

misconception not present in canon.misc.*
→ unsupported_extra (honest: NOT detected as contradiction)

high-unresolved-rate transcript (>0.35)
→ abstention: findings persisted, diagnostic produced, no Layer-3 update
```

### 6.12 Relationship to prior decisions

Supersedes Rev 1 anti-scope "no grading redesign." Partially amends
diff-at-Done v1 (workspace
`docs/superpowers/specs/2026-06-09-apollo-diff-at-done-design.md`): SymPy
and canonicalization return, but **Done-time only, batched, against a closed
candidate list** — the per-turn loop (nodify + dumb reply, no output filter)
is untouched and the `bernulis` per-turn string-matching bug class stays
dead. v1's §9 explicitly anticipated SymPy's return.

## 7. Retention (RQ2)

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
months, oldest first — **pruning Layer 2 loses only drill-down; Layer 3, the
event log, and the comparison runs/findings live in Postgres.** Pruning also
dangles the Neo4j node IDs stored in findings JSONB: **`evidence_spans` are
the durable provenance; node IDs are best-effort** — OLM drill-down for
pruned attempts degrades to quoted spans, not to nothing. Operational
risk: Aura Free pauses after days of inactivity and may be deleted after ~30
days paused — school breaks are real; schedule a weekly keep-alive or budget
Aura Professional.

## 8. Layer-1 authoring: minted from references (RQ6, revised twice)

### Granularity rule

Layer 1 is **not** the textbook's table of contents. An entity exists only if
Apollo can collect evidence about it — equations, conditions, definitions,
variables, and procedure steps that appear in reference solutions and that a
student articulates while teaching. The determination rule (decisions §1.5 +
§1.7): **the question bank determines the ontology, and the reference
solutions mint the entities.** Layer 1 = the union of entities linked from
the course's promoted reference solutions, plus prerequisite closure.
Granularity follows evidence demand, bottom-up. A subtopic with no question
touching it never becomes an entity.

### The minting step (replaces upfront key authoring)

At problem promotion (Tier-2, below), every reference node must be linked to
a Layer-1 entity:

- **New term** → mint a new `apollo_kg_entities` row (key drafted from the
  reference node's content; kind from its node type).
- **Term matching an existing course entity** → merge: the same tiered
  matcher from §5, run reference-against-course-inventory, drafts the link;
  **the teacher approves merges**, not keys ("is your 'constant density' the
  same as problem 5's 'incompressible'? → yes/no"). This is the RQ6 trust
  boundary (LLM suggests, teacher prunes) applied to entity identity.

The manual cost per problem drops to approving a handful of merge prompts;
no one ever authors `canon.cond.incompressible` in the abstract.

**Promotion lint (hard gate):** a reference graph cannot reach Tier 2
unless: every reference node carries an entity link; acceptable solution
paths are declared (one is fine); assumptions are explicitly SCOPES-scoped;
misconception competitors (`canon.misc.*` + opposes-links) are attached for
the concept's high-risk relations; equation entities pass symbol/alias
validation against the concept's canonical symbol context; and the minting
matcher's duplicate check has run (so `constant_density` /
`incompressible` / `rho_constant` never become three entities by accident).
Reference-graph validation failure at Done-time blocks grading (§6.6), so
this gate is what keeps the grader consistent across problems and authors.

### v1 seed

**A one-time conversion script** turns the existing hand-authored bernoulli
files into Layer-1 rows — `concept_dag.json` (14 nodes / 16 edges) →
`apollo_kg_entities` (kind=concept) + `apollo_entity_prereqs`;
`canonical_symbols.json` (7 symbols) → entities (kind=variable);
`normalization_map.json` (23 mappings) → `aliases`; `apollo_misconceptions`
rows → entities (kind=misconception, `canon.misc.*`, with opposes-links);
and the script **assigns the reference-node → entity links AND declares the
(single) acceptable solution path for the existing bernoulli problems** so
the grading core has a complete, validation-passing fixture from day one
(an undeclared path list blocks grading at pipeline step 3, §6.1).
Session machinery (`parser_prompt_template.md`, `solver_hints.json`,
`forbidden_named_laws.json`) and `problems/*.json` stay hand-authored,
untouched.

### The question-bank workflow — now in v1 (see §8B)

> **Pulled into v1 (decision §1.8, 2026-06-15).** This was a deferred
> post-wedge phase; it is now built in v1 as the automatic materials → Apollo
> provisioning pipeline (§8B), with an LLM pairing/correctness validator + the
> automated promotion lint replacing the teacher approval queue. The five-step
> shape below still describes the workflow; **§8B is the authoritative
> automated implementation** and overrides the "teacher approval" / "deferred"
> language here.

Reshapes the RQ6 extraction workflow around questions instead of concepts:

1. **Scrape questions** from the course's approved materials (the RAG corpus
   is already chunked/embedded) into the question bank — extends the existing
   `apollo_concept_problems` table (migration 018) with source provenance
   (document/page) and an LLM-assigned `difficulty`.
2. **Tag each question** with the concepts/entities it exercises (a
   question↔entity mapping table). The entity inventory emerges as the union
   of tags; prerequisite-edge candidates are drafted per the RQ6 trust
   boundary (LLM suggests, teacher prunes — F1 ≈ 0.5–0.77).
3. **Teacher approval queue** (checklist UX, no graph editor) publishes
   entities/aliases/edges to `apollo_kg_entities` and triggers the `:Canon`
   rebuild — including the reference-node → entity merge approvals from the
   minting step above.
4. **Two-tier bank — the grading trust boundary:** a scraped+tagged question
   (Tier 1) is usable for coverage analysis and inventory, but Apollo's
   selector only picks from **Tier 2: teachable problems**, i.e. questions
   promoted by adding an authored or LLM-drafted-then-teacher-verified
   `reference_solution` **with complete entity links, passing the promotion
   lint**. The grading core treats the promoted reference graph as truth;
   one hallucinated formula or mis-linked entity silently corrupts grading
   for every student, so promotion is always human-gated.
5. **Selection becomes a join:** weak entities + misconception flags from
   `apollo_learner_state` × question→entity tags × difficulty → next problem.
   Difficulty-tagged questions also remove most of the hand-authoring cost
   that motivated deferring Elo (§1.2).

Honest economics: ontology authoring drops ~2.5h → ~1h review per concept
(mostly merge approvals), but verified reference solutions (~3–5h per
concept's problem set) remain the bottleneck — full problem automation stays
anti-scope; the queue makes the drafting cheap and keeps the verification
human.

## 8A. Course → Apollo wiring: runtime cutover (finishing migration 018)

§8 decides how the curriculum is *authored* and *minted*; this section
decides how the runtime *finds* it for a given course with no hard-coded map
and no code deploy. It is the missing implementation half of the isolation
invariant (§1.4) and the unfinished half of migration 018: the `concept_id`
FK that 018 added to `apollo_sessions` exists but is **never populated** — the
runtime still writes the legacy `concept_cluster_id` TEXT and resolves
curriculum through three hard-codings that bypass the DB tables 018 created.

### 8A.1 The gap (verified in repo, 2026-06-15)

Three hard-codings, all bypassing tables that already exist, plus one missing
structural link:

| Location | Hard-coded thing | Should be |
|---|---|---|
| `apollo/hoot_bridge/session_init.py:23` | `_AVAILABLE_CLUSTERS = ["fluid_mechanics"]` | the concepts this course teaches, from `apollo_concepts WHERE …search_space_id = :sid` |
| `apollo/overseer/problem_selector.py:23` | `_CLUSTER_TO_CONCEPT = {"fluid_mechanics": ("fluid_mechanics", "bernoulli_principle")}` | resolve via DB FKs; no map |
| `apollo/subjects/__init__.py:108` | `load_concept()` reads filesystem JSON | a `ConceptDefinition` built from the `apollo_concepts` row (018's JSONB/TEXT columns) |
| *(structural)* | no link from a course to its curriculum | `apollo_subjects.search_space_id` (§1.4/§2); every course implicitly gets the one global `fluid_mechanics` cluster today |

Consequence today: every course resolves to the same single global cluster,
`apollo_sessions.concept_id` is NULL, and adding a class would require editing
`_AVAILABLE_CLUSTERS` + `_CLUSTER_TO_CONCEPT` and shipping filesystem content —
a code deploy. That violates §1.4.

### 8A.2 What "wired" means (the target invariant)

> A course is wired to Apollo when, given only its `search_space_id`, the
> runtime resolves **entirely from the DB, scoped to that course, with no
> hard-coded map and no code deploy** — the concepts it teaches, the problems
> for each, and the per-concept session machinery (symbols, parser prompt,
> solver hints, misconceptions). Adding or changing a class is a data
> operation, never a code change.

This is "automatic" in two complementary senses, split across two sections.
**§8A (this section) is the *read* side** — the runtime auto-discovers a
course's curriculum from the DB at session time. **§8B is the *write* side** —
the curriculum is itself auto-derived from uploaded materials and written to
those same DB rows. §8A makes the runtime DB-driven; §8B makes the DB
self-populating. Both are v1.

### 8A.3 Half 1 — course-scoped data model (already decided)

The structural link is **not new work for this section** — it is the
`apollo_subjects.search_space_id INTEGER NOT NULL REFERENCES
aita_search_spaces(id) ON DELETE CASCADE` migration already binding in §1.4
and §2, with per-course `canonical_key`/slug uniqueness and the explicit
backfill landmine (§2: map existing global rows to their owning course, or
nullable-then-tighten in two steps — a bare `NOT NULL` add fails on any
populated DB). Concepts → subjects → search_space, so concepts, problems,
misconceptions, and (later) Layer-1 entities all inherit course ownership
through existing FKs. The wiring cutover **rides that same migration and the
bernoulli seed of §8** — it does not add a parallel one. This subsection
exists only to name the dependency; the binding text is §1.4 + §2 + §8.

### 8A.4 Half 2 — runtime cutover to DB (the new work)

The read path becomes a single scoped DB resolution, every hop filtered by
`search_space_id` (isolation invariant, enforced in tests):

```text
search_space_id
  └─► apollo_subjects        (WHERE search_space_id = :sid)
        └─► apollo_concepts          ← concept_inference candidate list = these rows
              ├─► apollo_concept_problems   ← problem_selector reads these (by difficulty)
              └─► apollo_misconceptions     ← already concept_id-scoped
```

Concrete per-file changes (binding; exact signatures owned by the
implementation plan):

| File / symbol | Change |
|---|---|
| `session_init.py` | Build the available-concept list from the course's `apollo_concepts` rows (`WHERE …search_space_id = :sid`), not `_AVAILABLE_CLUSTERS`. Pass it to `infer_concept_cluster` as the candidate set. **Populate `ApolloSession.concept_id` (the 018 FK); stop writing `concept_cluster_id`.** Delete `_AVAILABLE_CLUSTERS`. |
| `overseer/concept_inference.py` | `available_clusters` becomes the course's concept rows (id + display name); the LLM picks among *this course's* concepts. Inference stays an isolated call; only its candidate list changes from a constant to a scoped query. |
| `problem_selector.py` | `list_problems_for_cluster` / `select_problem` query `apollo_concept_problems` by `concept_id` + `difficulty`. **Delete `_CLUSTER_TO_CONCEPT` and `cluster_to_concept` entirely.** |
| `subjects/__init__.py` `load_concept` | Returns a `ConceptDefinition` built from the `apollo_concepts` row's JSONB/TEXT columns (018), not from disk. The filesystem `subjects/**` layout becomes the **authoring source format only** — a seeder converts files → DB rows (§8 v1 seed); runtime never touches the filesystem. |
| Consumers `next.py:67`, `chat.py:223`, `done.py:199`, `lifecycle.py:89` | Read `sess.concept_id` (FK) directly instead of `sess.concept_cluster_id`; drop the `cluster_to_concept` indirection. |
| `ProblemAttempt.problem_id` | Resolves to a **DB** problem (`problem_code` stays the stable author id). The `reference_solution` lives in the problem payload (`apollo_concept_problems.payload`) — this is what the §6 grading core / Layer-1 consume, so it MUST be in the DB, not only on disk (acceptance criterion 7). |
| `persistence/models.py:166` + migration | `concept_cluster_id` is the legacy column 018 said is "dropped in 022" but is still present and nullable. Final cutover step: drop it once nothing writes or reads it. The implementation plan owns the migration number (§2 numbering caution). |

**Sync → async wrinkle (call out, do not discover mid-build).** `load_concept`,
`select_problem`, and `infer_concept_cluster`'s candidate load are sync
filesystem reads today, called from async handlers (`session_init` already
holds an `AsyncSession`). Cutting over to DB makes the curriculum reads async
(or take pre-fetched rows threaded from the handler). This signature change
ripples through every consumer in the table above — the plan must pick one
shape (async loaders vs. handler-fetched `ConceptDefinition`/problem rows
passed in) and apply it uniformly. This reconciles 018's "data not code" with
the §1 guardrail "session machinery stays hand-authored": authored as files,
seeded into DB columns, **read from DB at runtime.**

### 8A.5 Provisioning — how a course's rows get written (all v1)

The runtime (§8A.4) only *reads* curriculum rows; three v1 paths *write* them,
none requiring a code deploy:

- **Auto from materials (§8B) — the primary path.** On every material upload
  the §8B pipeline scrapes questions, finds-or-generates reference solutions,
  and writes course-scoped concept/problem/entity rows fully automatically.
  This is the "derive concepts from course materials" capability — **in v1**
  (decision §1.8), no longer deferred.
- **Bernoulli seeder — the bootstrap.** The §8 one-time script converts the
  hand-authored bernoulli files into the same rows, tagged to a real course's
  `search_space_id`, so there is a complete validation-passing fixture from
  day one independent of any upload.
- **Console `INSERT`s — manual override.** When the teacher console lands a
  teacher can author or correct rows directly (e.g. fix a quarantined
  problem, §8B.3).

All three write the identical row shape the runtime reads, so they are
interchangeable and composable. The isolation invariant (§1.4) holds for all
three — every written row carries `search_space_id`.

### 8A.6 Done when (acceptance criteria)

1. `apollo_subjects.search_space_id` exists, `NOT NULL`, backfilled;
   `canonical_key`/slug uniqueness is per-course, not global (§1.4/§2).
2. `_AVAILABLE_CLUSTERS`, `_CLUSTER_TO_CONCEPT` / `cluster_to_concept`, and
   filesystem reads in the selection path are **deleted** — `grep` for them is
   clean.
3. Two courses can each have their own concept(s) — even the same physics —
   and a student in course X is offered only course X's concepts/problems,
   proven by a test with two scoped courses.
4. `session_init` populates `apollo_sessions.concept_id`; `concept_cluster_id`
   is no longer written (and is droppable).
5. A new course is fully wired with **zero code changes** (seeder run or
   console `INSERT`s only).
6. Every curriculum read filters by `search_space_id` (isolation invariant),
   enforced in tests.
7. Seeded reference solutions live in `apollo_concept_problems.payload` so the
   §6 grading-core / Layer-1 tracks consume them from the DB.

### 8A.7 Relationship to existing decisions

Sits inside **§12 phase 3** (Layer 1 + resolution + persistence) — it shares
that phase's `apollo_subjects.search_space_id` migration and bernoulli seed.
It depends on **Phase 1** (auth + `user_id`/`search_space_id` scoping, done)
and is independent of the **grading core** (phase 4) and **Layer 3**
(phase 5), except that criterion 7 (reference solutions in the DB) is the
contract those tracks consume. The bernoulli **seeder** converts
*hand-authored* content into the course-scoped rows; **automatic**
provisioning from uploaded materials is now §8B (pulled into v1 by decision
§1.8), which writes those same rows automatically at upload time —
superseding §11's former "no automatic problem/reference-solution generation"
anti-scope.

## 8B. Materials → Apollo auto-provisioning (the question-bank workflow, in v1)

**Decision §1.8 (2026-06-15) pulls §8's deferred question-bank workflow into
v1 and makes it fully automatic.** On every material upload, Apollo scrapes
questions from the material, finds-or-generates a reference solution for each,
and promotes problems to teachable with **no human in the loop** — an LLM
pairing/correctness validator plus the automated promotion lint stand in for
the teacher. This reverses decision 3 (Layer-1 authoring deferred) and the
§11 "no automatic problem/reference-solution generation" anti-scope. It does
**not** relax §1.4 (course isolation) or §2 (Postgres is the system of
record): provisioning writes the same course-scoped Postgres rows the §8 seed
script writes, only automatically and at upload time.

The pipeline mechanics below are **ported from the 2026-06-02 textbook
problem-index spec** (six staged modules, the eight validation gates, the
dedup ladder, the observability namespace, the tiered tests). That spec's
*storage* and *scope* choices are **not** adopted — it predates this document
and chose Neo4j-authoritative storage and global dedup-merged concepts, both
overruled here by §2 (Aura Free is a single point of loss for curated
content) and §1.4 (the classroom is the maximum span of correlation). §8B.6
states the port boundary explicitly.

### 8B.1 Trigger & scope

Eager, per material, **fully automatic**. When a document finishes the
existing `teacher_pdf_ingestion`/indexing pass, it enqueues an Apollo
provisioning job scoped to that document's `search_space_id`. No teacher
button, no approval queue. Re-uploading N materials runs N jobs; idempotency
is per `(document_id, chunk_id)` so re-ingest of an unchanged document is a
no-op. The corpus is already chunked and embedded by the upload — the pipeline
reads those chunks, it does not re-index.

### 8B.2 Pipeline (Postgres-authoritative, course-scoped)

Six stages, each with an explicit Pydantic input/output type so it is testable
and replaceable in isolation. Ordered **question-first** to honor decision
§1.5 (the question bank determines the ontology, not the textbook TOC):

1. **Scrape questions.** LLM pass over the document's chunks → candidate
   questions (worked examples + end-of-chapter exercises), each with source
   provenance (`document_id`, `page`, `chunk_id`) and an LLM-assigned
   `difficulty`. Written to `apollo_concept_problems` as **Tier 1** (usable
   for inventory/coverage, not yet teachable), scoped by `search_space_id`.
2. **Find-or-generate reference solution.** For each question, retrieve over
   the course corpus for a solution already printed in the material (a
   worked-example solution, a solutions-manual passage). Found → extract it,
   `solution_source = extracted`, provenance recorded. Not found → LLM-generate,
   **RAG-grounded in the course corpus**, `solution_source = generated`.
   (Preferring the material's own solution is the 2026-06-15 decision: an
   authoritative printed solution beats a generated one, and grounding both in
   retrieved course passages is the cheapest defense against the mispairing
   failure mode.)
3. **Model approval — pairing & correctness gate.** One LLM validator per
   `(question, reference_solution)` pair — extracted *and* generated — checks,
   in priority order, **correct pairing** ("does this solution actually answer
   this question?"), then basic correctness, RAG-grounded in retrieved
   passages. This is the automated stand-in for §8's teacher approval queue;
   mispairing is the failure it exists to catch.
4. **Concept/entity tagging + minting (question-driven).** From the scraped
   questions and their approved solutions, tag the concepts/entities each
   exercises; the course's concept inventory is the **union of tags** (§1.5),
   prerequisite-edge candidates drafted by LLM. Reference nodes are **minted
   into Layer-1 entities** (§5/§8 minting) — model-approved, not
   teacher-approved — and misconception competitors (`canon.misc.*` +
   opposes-links) attached for the concept's high-risk relations so the §6
   grading core can detect contradictions. Writes
   `apollo_concepts`/`apollo_kg_entities`/`apollo_misconceptions`, all
   `search_space_id`-scoped.
5. **Course-local dedup.** Resolve concept/entity candidates against **this
   course's** existing inventory — slug match → `scope_summary` embedding
   similarity → LLM-judge tiebreaker (the textbook spec's ladder, re-scoped).
   Crucially **never global**: two courses' "incompressible" stay separate
   entities (§1.4). First-writer-wins on a concept's established vocabulary —
   a later material may add problems to a course concept but not rewrite its
   canonical symbols / normalization map.
6. **Automated promotion lint → Tier 2 + `:Canon` rebuild.** Promotion to
   **teachable** runs §8's promotion lint, implemented as the **ported
   eight-gate validator** (§8B.4). On pass: the problem's `reference_solution`
   is stored in `apollo_concept_problems.payload` (§8A criterion 7), the
   problem flips to Tier 2 (selectable), and the course's `:Canon` projection
   rebuilds from the new Postgres rows. On fail: rejected + logged, never
   reaches a student.

### 8B.3 Backstops (no human gate ⇒ the automated net must be stronger)

Removing the teacher means three mechanisms carry the trust the human used to:

- **The §6 grading core runs in shadow** behind the §6.7 calibration gate: an
  auto-provisioned problem can be taught and can show a student a diagnostic,
  but its grades do **not** move Layer-3 beliefs until the shadow window's
  false-missing / false-misconception rates pass review.
- **§6.6 abstention gates** unchanged — biased/low-quality runs withhold the
  learner update regardless of problem source.
- **NEW — per-problem anomaly quarantine.** A problem whose **class-wide
  coverage is abnormally low** (most students "miss" the *same* reference
  node) is auto-pulled from the selectable pool and flagged: that pattern is
  the signature of a wrong or mispaired reference solution. This is the
  automated replacement for a teacher noticing a bad problem — and unlike
  global calibration it is **per-problem**, so one bad reference among many
  cannot hide in an average.

**Recorded assumption (decision §1.8):** v1 assumes the generation LLM does
not fabricate a *coherent-but-wrong* solution that also survives the pairing
validator. The three backstops above are defense-in-depth that do **not**
depend on that assumption holding — they are what catches the case where it
doesn't.

### 8B.4 The promotion lint — eight gates (ported, binding)

Run in order, short-circuit on first failure; any failure drops the problem to
a logged `rejected_problems` row. This is the safety layer of the
fully-automatic design and must be exercisable on hand-written fixtures with
no LLM stage running.

| # | Gate | Checks |
|---|---|---|
| 1 | Schema | Pydantic validation of the extracted problem, every node, every edge; edge types within `EDGE_ALLOWED_PAIRS` |
| 2 | Reference closure | every `depends_on` and every edge endpoint resolves (no dangling refs) |
| 3 | DAG | dependency graph acyclic; every node reachable from a root (no orphan islands) |
| 4 | Symbol consistency | every symbol in equations / `given_values` / `target_unknown` is in the concept's canonical symbols or normalizes to one |
| 5 | Procedure coherence | `:ProcedureStep` nodes form one `PRECEDES` chain; step equation refs resolve; terminal step computes `target_unknown` |
| 6 | SymPy parse | `sympify(symbolic)` succeeds for every equation |
| 7 | Equation-system closure (closure-only, v1) | every symbol is a given, the target, or claimed-cancelled by a `:Simplification` — a *paper* closure check, **not** an end-to-end solve (the solver doesn't apply simplifications yet; end-to-end is a follow-on) |
| 8 | Duplicate detection | `sha256(normalize(problem_text)+canonical(given_values)+target_unknown)` not already present **for this course's concept** |

Gate 7's honest limit (carried from the source spec): some reference graphs
whose equations parse but don't actually produce the claimed answer slip
through; the per-problem quarantine (§8B.3) is the runtime catch until gate 7
is promoted to a real solve.

### 8B.5 Storage & observability (Postgres, not Neo4j)

Concepts, problems, solutions, entities, misconceptions, and the ingest
observability rows all live in **Postgres** (migration-018 lineage). The only
Neo4j artifact remains the rebuildable `:Canon` projection (§2). The source
spec's `:Concept`/`:Problem`/`:ClusterAlias`/`:_IngestEvent` Neo4j nodes are
**not** built. Observability is ported as Postgres tables — `ingest_runs`
(one per document: counts, LLM call/token/cost aggregates), `rejected_problems`
(gate failed + diagnostic + payload), `dedup_decisions` (method + similarity +
verdict), `ingest_errors` (stage + class + context) — the same evidence,
course-scoped and joinable with the rest of the system.

### 8B.6 Port boundary — from the 2026-06-02 textbook-index spec

**Adopted:** the six-stage decomposition with per-boundary Pydantic types; the
eight validation gates; the dedup ladder (slug → embedding → LLM-judge); the
observability namespace; the tiered test strategy; fully-automatic /
model-as-judge; eager-at-upload extraction.
**Rejected / changed:** Neo4j-authoritative concept+problem storage → Postgres
(§2); **global** dedup-merged concepts → **course-scoped** (§1.4); the old
`coverage.py`/`rubric.py` grader → the §6 graph-simulation core; **extraction
only** → **find-or-generate** solutions; **no misconceptions** → misconception
minting + opposes-links (required by §6.5); no backstop → shadow/calibration
(§6.7) + per-problem quarantine. Stale infra references in the source spec
(Heroku log drain) are dropped — the platform is Railway.

### 8B.7 Testing

Port the source spec's three tiers; the CLAUDE.md 95%-patch-coverage contract
applies to all new code:

- **Tier 1 (every PR, mocked LLMs):** per-stage unit tests; the **eight-gate
  validator** suite — positive fixtures (the seeded bernoulli problems pass
  all gates) and one adversarial fixture per gate (foreign symbol → gate 4,
  cycle → gate 3, dangling edge → gate 2, malformed SymPy → gate 6, broken
  procedure chain → gate 5, duplicate → gate 8); course-local dedup tests with
  deterministic mock embeddings (must prove two courses do **not** merge).
- **Tier 2 (nightly/on-demand, real LLMs):** end-to-end against a synthetic
  mini-textbook — assert ≥1 concept, ≥2 problems from worked examples, reject
  rate below threshold, and that one extracted problem grades a hand-crafted
  good-student attempt as passing through the §6 core.
- **Tier 3 (release gate, real textbook):** statistical assertions (concept
  count band, per-concept problem band, reject-rate threshold), the
  per-problem quarantine exercised on a deliberately-mispaired fixture, and a
  curated "known-good worked examples must appear" list. Output: the
  `ingest_runs` summary committed as the release audit trail.

### 8B.8 Phasing

Lands as a **follow-on to phase 3** (it needs Layer 1 + resolution +
persistence + the §8A wiring in place) and **before** the §6 personalization
wedge needs a populated multi-concept bank. It depends on phase 2 extraction
quality only for the *student* side; its own scraping/authoring LLM calls are
independent. Ships behind the §6.7 shadow gate like the grading core, so
auto-provisioned problems accumulate calibration evidence before their grades
are trusted to update learner models.

## 9. Security, privacy, multi-tenancy (RQ7)

*(Referenced as "§8" by migration 023's header comment — Rev 1 numbering.)*

**Two existing defects must be fixed, not inherited** (both verified in repo):
`/apollo/*` endpoints perform no auth (body-supplied identity —
`security.md` "Known gaps"), and `apollo_*` tables key off `student_id TEXT`
with no course scoping.

- **Phase 1 retrofit (user decision):** wire `/apollo/*` through
  `resolve_auth_context` + `_require_course_membership`; migrate Apollo tables
  to `user_id UUID` + `search_space_id`; update the student UI to
  authenticated calls. No new endpoint or table may use the old pattern.
  *(Status 2026-06-11: built; backend PR #12 + student-UI PR #4 open and
  green; prod migration user-gated.)*
- **Scoping rules (enforceable):** student sees own mastery + own evidence
  only (server-injected `user_id` filter; RLS self-read policy as
  defense-in-depth); teacher sees students in their own courses only
  (`_require_course_membership` before any read; `search_space_id` filters in
  both stores). Mastery records are **per-course**; cross-course aggregation
  is out of scope for v1. The isolation invariant (§1.4) extends this beyond
  learner data: curriculum, entities, graphs, **and the comparison
  runs/findings tables** are course-scoped too — the classroom is the maximum
  span of any correlation in the system.
- **Aura placement: approved with mandatory mitigations.** Backend-mediated
  access only (creds already backend-only); **every** Cypher read/write
  filters by `user_id` AND `search_space_id`, both server-injected from the
  validated AuthContext — enforced by a shared scoping helper wrapping the
  driver so an unscoped query is impossible by construction; no
  student-identifying free text in properties; deletion fans out from
  Postgres (`ON DELETE CASCADE`) to Aura nodes.
- **Student rights (v1):** view — the open learner model ships eventually
  (belief bars, findings-backed and auditable); contest — negotiation moves
  (`apollo_kg_negotiations`) already exist and serve as the FERPA "seek to
  amend" mechanism; deletion — per-student delete supported; course-bound
  retention recommended (purge at term end / enrollment removal), no
  indefinite default. FERPA skim: a persistent inferred mastery profile +
  stored teaching transcripts is an education record; persistent inferring
  AI tutors are the "high-risk" category; vendor duties include direct
  control, purpose limitation, and access/amend/destroy rights.

## 10. Hoot chat as evidence (RQ5)

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

## 11. Consolidated anti-scope

No neural KT; no EM/parameter fitting in v1; no Elo until problem selection
needs it; no bi-temporal edges or LLM contradiction-invalidation; no
Graphiti-the-library; no MinHash/LSH; no GraphRAG retrieval machinery; no
merged per-student graph; automatic problem/reference-solution generation is now in v1, model-gated
(§8B) — this anti-scope item was reversed by decision §1.8;
no skill discovery from response data; no graph-editing UI; no cross-course
concept merging; no chat evidence; no per-turn learner updates; no new
infrastructure.

Grading-core anti-scope: **no strict raw-graph bisimulation** (weighted
asymmetric canonical coverage only); **no per-turn grading or per-turn
resolution** (the grading core runs once, at Done); **no LLM re-grading in
diagnostics** (the diagnostic explains computed findings under §6.8
constraints); **no global/cross-course canonical vocabulary** (entities are
minted per course from its references — isolation invariant §1.4); **no
per-equation symbol tables / physical binding contexts** (symbolic
equivalence runs under the concept's canonical symbol context and declared
mappings; full binding models are v2 if Apollo expands to domains where
variable-meaning collisions actually bite). LLM calls in the Done path are
limited to three **constrained-verification roles** — per-turn parsing, one
resolution adjudication call (closed candidate list), one transcript-audit
call (span or null) — each returns structured choices or quotes, never a
grade. (Rev 1's "no grading redesign" was removed by decision §1.6.)

## 12. Phasing (input to the implementation plan)

1. **Auth & scoping retrofit** — `/apollo/*` through
   `resolve_auth_context` + `_require_course_membership`; `user_id UUID` +
   `search_space_id` on Apollo tables; student-UI auth. *(Built 2026-06-11;
   PRs #12/#4 open & green; prod migration user-gated.)*
2. **Make the evidence graph real (RQ3)** — one-call typed-edge extraction
   with graph context + strict outputs; wire SCOPES; cross-turn linking;
   explicit/inferred edge provenance; `write_edges` logs instead of silently
   dropping. **Hard prerequisite for phase 4** (parser recall is the grading
   ceiling, §4).
3. **Layer 1 + resolution + persistence** — learner-model migration (incl.
   the `apollo_subjects.search_space_id` scoping FK, §1.4); bernoulli seed
   script including misconception entities, opposes-links, and
   reference-node → entity links (§8); `:Canon` projection seeder; the §5
   shared resolver (reference-anchored, content-first + structural
   corroboration) + `RESOLVES_TO`; stop deleting graphs at session end; new
   named errors. **Includes the §8A course → Apollo runtime cutover** (finish
   migration 018): delete `_AVAILABLE_CLUSTERS` / `_CLUSTER_TO_CONCEPT` /
   filesystem `load_concept`, resolve curriculum from DB scoped by
   `search_space_id`, populate `apollo_sessions.concept_id`, reference
   solutions into `apollo_concept_problems.payload` — proven by the
   two-scoped-courses isolation test (§8A.6).
   - **3B — Materials → Apollo auto-provisioning (§8B), v1 follow-on to this
     phase:** scrape questions → find-or-generate reference solution → LLM
     pairing/correctness gate → question-driven concept/entity minting →
     course-local dedup → automated eight-gate promotion lint → Tier-2
     teachable + `:Canon` rebuild. Fully automatic (no human gate); ships
     behind the §6.7 shadow gate with a per-problem anomaly quarantine.
4. **Graph-simulation grading core** — `apollo/graph_compare/`: validator,
   canonical graph builder, coverage (max over paths) + soundness
   (contradictions-only) simulations, sub-scores, near-bisimilarity,
   transcript auditor, abstention gates, comparison runs + findings tables;
   `done.py` re-orchestrated to the §6.4 pipeline; rubric input mapping;
   diagnostic switched to constrained findings-explanation (§6.8).
   **Ships in shadow mode**: runs alongside `compute_coverage`, emitting
   diagnostic-only events while calibration metrics accumulate (§6.7). The
   adversarial fixture suite (§6.11) is part of this phase's test gate.
5. **Layer 3 learner model** — findings → events via the decision table
   (§6.5); the 3-state filter in the Done transaction; decay; event log;
   `learner_update_pending` retry path; RQ5 hedge (persist chat keywords).
   **Entry is gated by the §6.7 calibration review** — learner updates from
   simulation events are enabled only after the shadow window's
   false-missing / false-misconception rates pass review.
6. **Session personalization wedge** — session-init Q1 read; problem
   selection over weak entities; persona conditioning; difficulty.
7. **Later, in rough order** — teacher dashboard (Q2); *(the question-bank
   workflow is no longer here — **pulled into v1 as §8B / phase 3B**);*
   student-facing OLM (findings-backed); Hoot-chat evidence (RQ5 memo is the
   spec); Elo for difficulty matching; Layer-2 janitor; reference
   normalization goes fuzzy when references are machine-generated; edge-driven
   learner events if calibration proves edge recall.

Each phase is independently shippable and none requires revisiting a prior
phase's decisions.

## 13. Source documents

- Parent plan + RQ1/RQ6/RQ7 memos + RQ1–RQ7 briefs: workspace
  `docs/superpowers/specs/2026-06-10-apollo-kg-*.md`
- RQ2/RQ5 full memos: research session 2026-06-10 (key content reproduced
  above; citations in the memos)
- RQ3 spike: `scripts/spikes/rq3_edge_extraction.py`, `rq3_results.json`
- Diff-at-Done v1 (partially amended by §6.12): workspace
  `docs/superpowers/specs/2026-06-09-apollo-diff-at-done-design.md`
- Textbook problem-index spec (2026-06-02): pipeline mechanics ported into
  §8B; its Neo4j-authoritative + global-concept storage rejected (predates
  and contradicts §2 + §1.4). `2026-06-02-apollo-textbook-problem-index-design.md`
- The standalone grader spec
  (`2026-06-12-apollo-canonical-graph-simulation-grader.md`) was merged into
  §6 of this document on 2026-06-12 and deleted.
- System ground truth: `docs/architecture/apollo.md` (verified 2026-06-10),
  `docs/shared-architecture/security.md`, migrations 006/018/019/021/022
