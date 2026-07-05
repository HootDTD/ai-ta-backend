---
title: Emergent misconception store — design memo (D1)
status: design — human-gated (product owner rejected the prior autonomous design)
date: 2026-07-05
author: Claude (Fable), weekend campaign lane B3b step 1 (diagnosis item D1)
owner_doc: docs/architecture/apollo.md
lane: B3b · diagnosis: docs/_archive/experiments/2026-07-02-e2e-campaign-diagnosis.md §1 (D1)
migration_numbering: next free is 036 (on-disk max is 035_apollo_learner_state_space_idx.sql)
branch_target: staging (design-only; no branch cut by this memo)
---

# Emergent misconception store — design memo (D1)

> **Design only.** No code, no migration, no branch is created by this memo. It
> proposes; a human decides. It exists because the product owner **rejected the
> last autonomous design in this area** (a binary approved-only store, 2026-06-23)
> and asked for a trust-gradient / promotion pipeline. This memo is required to
> build on that record, not re-derive it.

---

## 0. Prior-art trail (what was decided, what was rejected, what is missing)

### The two 2026-06-23 misconception-bank designs are NOT in the repo — stated honestly

The task asked me to read the 2026-06-23 prior-art memo and **both** prior
misconception-bank designs. **I could not find them; they are not committed to
this repository, and I did not fabricate citations for them.** Evidence:

- The one committed pointer to them is a **user memory file**,
  `apollo-misconception-bank-decisions.md`, referenced by two committed
  handoffs. That memory file is **not present** in the memory directory
  (`~/.claude/projects/.../memory/` contains 27 files; none is the
  misconception-bank one).
- `find / -name '*misconception-bank*'` returns nothing; `git log --all` shows no
  `docs/*misconception*` doc ever committed.
- The 2026-06-29 g2 handoff says so explicitly:
  `docs/_archive/handoffs/2026-06-29-apollo-clarification-loop-g2-handoff.md:44` —
  > "User wants a **trust-gradient / promotion pipeline** for misconceptions, NOT
  > binary approved-only. ⚠️ The memory references design docs 'in
  > ai-ta-backend/docs/' but they are **not committed** (a `find` this session
  > returned none) — locate or re-derive before wiring misconception capture."

So I proceed from the **committed decision fragments** below (which are
sufficient to fix the deferred decision precisely) and the diagnosis text — not
from the missing full designs.

### What the committed record DOES establish (cited exactly)

1. **The rejected shape and the mandate.** The rejected 2026-06-23 design was a
   **binary approved-only store**; the owner wants a **trust-gradient / promotion
   pipeline** instead. Sources:
   - `docs/_archive/handoffs/2026-06-26-apollo-grader-mvp-fix-handoff.md:109` —
     "the misconception store, which the user explicitly **deferred** (wants a
     **trust-gradient/promotion pipeline**; brainstorm before code)."
   - `docs/_archive/handoffs/2026-06-29-apollo-clarification-loop-g2-handoff.md:44`
     (quoted above): "trust-gradient / promotion pipeline … **NOT binary
     approved-only**."
   - Weekend control doc `.planning/weekend/WORKER-B-integrity.md:269` names it:
     "binary approved-only store, 2026-06-23."

2. **What the 2026-06-23 decision DEFERRED.** It rejected the binary store and
   mandated a trust gradient, but **deferred the actual mechanism** ("brainstorm
   before code"). Concretely, three things were left open: (a) the *accumulation
   unit* (what is counted, keyed how), (b) the *trust function* (how observations
   become a promotion signal), and (c) the *thresholds* (when an emergent
   misconception is allowed to affect a grade vs. only inform a teacher). **This
   memo resolves all three** (§6, §7).

3. **The sibling 2026-06-23 design that WAS committed** —
   `docs/design/2026-06-23-apollo-soundness-na-sentinel.md`. This is the other
   2026-06-23 decision in exactly this area and it is load-bearing here:
   - It established that an **empty misconception bank is a first-class state**,
     represented as **N/A (reason-only), not abstention**: reason
     `misconception_bank_empty` is recorded but **does not set `abstained`**
     (§3.6 of that doc; coverage still grades, Layer-3 still updates).
   - It added the `soundness_applicable` flag and the renormalize-to-coverage
     rule — the pattern of "we could not check this, and we say so honestly."
   - **My design inherits this pattern directly:** under an emergent store the
     empty bank is the *normal cold-start of every class*, and the correct
     behavior is exactly the sentinel doc's — assert no misconceptions, grade
     coverage normally, say the store was empty. Lane B3a (PR #85) has already
     moved that signal onto the artifact as
     `misconceptions_status: {assertable: false, reason: "empty_bank", …}`;
     **this design starts from that state** and never re-introduces
     abstain-on-empty.

4. **The evidence substrate the clarification loop already emits.**
   `docs/_archive/specs/2026-06-29-apollo-clarification-loop-design.md` §3 + §8:
   the bank pipeline is explicitly a "separate, not-yet-designed effort"; the loop
   emits **`refuted` clarification rows as misconception *evidence*** (student
   actively believes the opposite), in a shape that "simply does not foreclose" a
   future rollup. That is a **second write source** this store should be able to
   consume later (§10 non-goals keep it out of v1 but the schema does not
   foreclose it — mirroring the loop's own discipline).

---

## 1. Problem statement

The grader's misconception channel is backed by `apollo_misconceptions` — a
**per-concept, hand-authored** table (migration 019). Two defects flow from that:

- **It is pre-seeded, which the product owner forbids.** Misconceptions must be
  **emergent** from real student interactions, never pre-authored. The only
  populator today is `scripts/seed_apollo_misconceptions.py` (265 lines), which
  the diagnosis labels a **harness workaround — "do not productionize"**
  (D1 consequence (b)).
- **It has the wrong dimension.** `apollo_misconceptions.concept_id` keys the bank
  per **concept**, hand-authored once. The owner wants **per-class**
  (search-space) aggregation over **canonical per-subtopic scores** drawn from
  real grading artifacts, because canonical subjects are global per class but the
  *misconception population* is a property of *this class's students*.

The revealing symptom (D1) was the grader **abstaining because the bank was
empty**. Under an emergent design an empty bank is the cold-start state of every
brand-new class, so abstaining on it is categorically wrong. Lane B3a has removed
that abstention and now emits `misconceptions_status.assertable=false` on an
empty bank. **What is still missing is the store itself**: a per-class,
emergent, trust-graded accumulation of misconceptions fed by canonical grading
artifacts, feeding three consumers — the grader (`watch_out` assertions), teacher
analytics / classroom projections, and the pedagogy loop.

**Goal of this memo:** design that store, resolve the deferred 2026-06-23
trust-gradient decision, and hand humans a recommendation + numbered open
questions.

---

## 2. Code reality this design must fit (verified 2026-07-05 @ staging 2c2dc5f)

| Fact | Location | Why it matters |
|---|---|---|
| **Hand-authored bank, per-concept** — `code, description, description_embedding vector(3072), confusion_pair_a/b, trigger_phrases[], probe_question, rt_steps[]`; keyed on `concept_id` FK; **no `search_space_id`, no status/confidence/observation-count columns.** | `apollo/persistence/models.py:227` (`Misconception`), migration 019 | The thing the emergent store replaces as the *source of assertions*. It has no per-class dimension and no trust state. |
| Bank loader is **read-only**: "Authoring goes through INSERT/UPDATE on the table directly." `load_for_concept(concept_id)` → `candidates_from_misconceptions` mints `misc.*` resolution candidates (trigger_phrases as alias surfaces). | `apollo/overseer/misconception_bank.py`, `apollo/resolution/candidates.py:127` | The grader **read path** the emergent store must plug into: promoted emergent misconceptions become `misc.*` candidates the same way. |
| **Canonical grading artifact** — one immutable, append-only row per attempt per role; `UNIQUE(attempt_id, role)`. Carries `search_space_id`, `concept_id`, `misconceptions` (JSON), `node_ledger` (JSON), `scores`, `abstention`. `role='canonical'` = the served grade; `role='pair'` = shadow. | `apollo/persistence/models.py:862` (`GradingArtifact`), migration 034 | **The write source.** It already has the per-class dimension (`search_space_id`) and the per-subtopic scores (`node_ledger`), and its `misconceptions[]` is the emergent signal. Append-only + `UNIQUE(attempt_id, role)` gives idempotency for free. |
| `misconceptions[]` shape: `{canonical_key, evidence_span, confidence, opposes}` — one per CONTRADICTION finding; `opposes` is the reference node it contradicts (or `None`). | `apollo/grading/artifact_build.py:186` (`build_misconceptions`) | The **accumulation unit's identity** comes from here. `canonical_key` is a stable signature *when the misconception opposes a known reference node*; free-form ones (`opposes=None`) have no stable key. |
| `node_ledger[]` rows: `{canonical_key, status: credited\|missing\|misconception\|unresolved, method, confidence, evidence_span}` — the **canonical per-subtopic scores** the diagnosis names. | `artifact_build.py` (`build_node_ledger`) | Per-subtopic status is right here, per artifact; aggregation is a GROUP BY over these across the class. |
| Read path (projection): scorecard `_watch_out` reads `artifact["misconceptions"]`; classroom heatmaps aggregate `LearnerState` / `MasteryEvent` per `search_space_id`. | `apollo/projections/scorecard.py:106`; `models.py:515,556` | The projection consumer already reads artifacts and per-space state — the store's read API should match. |
| **Event-sourced precedent exists.** `MasteryEvent` is append-only, one row per `(attempt, entity, kind)` with `UNIQUE(attempt_id, entity_id, event_kind) NULLS NOT DISTINCT`; `event_kind` already includes **`misconception`**; `LearnerState` is the in-place rollup with `evidence_count`, `misconception_code`. Index `ix_apollo_learner_state_space_entity` (migration 035) supports classroom-wide scans. | `models.py:556,515,549` | A working template for "append-only evidence log + rolled-up snapshot" with idempotency and a class-scan index — the emergent store should mirror it, not invent a new pattern. |
| Per-class isolation invariant: every Apollo table carries a `search_space_id` FK with `ON DELETE CASCADE`; a course delete cascades its rows (`models.py:702` comment). | across `models.py` | The store MUST carry `search_space_id` and cascade, or it breaks the isolation invariant. |
| Migration numbering: on-disk max is `035_apollo_learner_state_space_idx.sql`. **Next free is 036.** | `database/migrations/` | Any table this design proposes is migration **036**. |

---

## 3. Design principles (locked before candidates)

- **P1 — Emergent, never seeded.** No row is authored by a human or a script.
  Every misconception in the store got there because ≥1 real student's *served*
  grade asserted it. The seeder is retired from any production path.
- **P2 — Grading artifacts are the source of truth.** The store is a *function of*
  `apollo_grading_artifacts` (`role='canonical'`). Anything materialized is a
  rebuildable cache; the artifacts can always regenerate it.
- **P3 — Continuous trust, never a binary gate (the owner's mandate).** Promotion
  is a **continuous trust score** with **named bands** (`candidate → observed →
  promoted`) that are *labels over thresholds*, not a boolean `approved` column.
  No single bit is allowed to gate whether a misconception can affect a grade.
- **P4 — Empty is normal, and honest (inherits the sentinel doc).** Zero
  observations → assert nothing, grade coverage normally, report
  `misconceptions_status.assertable=false`. Never abstain on emptiness.
- **P5 — Per-class isolation is non-negotiable.** `search_space_id` on every row,
  `ON DELETE CASCADE`, every read filtered by it. No cross-class bleed.
- **P6 — Idempotency comes from the artifact key.** A given attempt contributes to
  the store **at most once**; re-grades / retries must not double-count. The
  artifact's `UNIQUE(attempt_id, role)` is the anchor.

---

## 4. The accumulation unit (shared by all candidates)

All three candidates aggregate over the same unit:

> **`(search_space_id, concept_id, signature)`** → rolled-up
> `{observation_count, distinct_student_count, mean_confidence, first_seen,
> last_seen}`, from which a **trust score** and a **band** are derived.

**`signature`** identity (the crux, see OQ1):
- When the artifact's misconception row has `opposes != None` **or** a
  `canonical_key`, the signature **is that `canonical_key`** — a stable key tied
  to the reference node it contradicts. This is the strong, promotable case.
- When it is free-form (`opposes=None`, no key), v1 puts it in a per-concept
  **"unkeyed" bucket** that accumulates but **cannot promote** (you cannot assert
  what you cannot key). Embedding-clustering free-form misconceptions into stable
  signatures is deferred (OQ1, increment 2) — and the artifact already carries
  `evidence_span` text + the bank carries `description_embedding vector(3072)`, so
  the substrate for that later step exists.

**Per-subtopic scores** feed the same unit: a `node_ledger` row with
`status='misconception'` is equivalent evidence to a `misconceptions[]` entry for
the same `canonical_key`; the aggregation reads both and dedups on
`(attempt_id, signature)`.

---

## 5. Candidate architectures

### Candidate A — Materialized per-class bank, discrete-tier promotion

**Data model (migration 036).** One table `apollo_class_misconceptions`:

```
apollo_class_misconceptions
  id                     bigserial pk
  search_space_id        int   fk aita_search_spaces on delete cascade  -- P5
  concept_id             bigint fk apollo_concepts on delete set null
  signature              text  not null            -- canonical_key or 'unkeyed:<hash>'
  observation_count      int   not null default 0
  distinct_student_count int   not null default 0
  mean_confidence        real  not null default 0
  status                 text  not null default 'candidate'  -- candidate|observed|promoted
  first_observed_at      timestamptz not null
  last_observed_at       timestamptz not null
  promoted_at            timestamptz null
  exemplar_span          text  null                -- a representative student utterance
  UNIQUE(search_space_id, concept_id, signature)
```

**Write path.** A hook at the end of the Done grade path, after the
`role='canonical'` artifact is written, reads that artifact's `misconceptions[]`
+ `node_ledger[]` misconception rows and **UPSERTs** one class row per
`(space, concept, signature)`, incrementing counts. Idempotency (P6): a tiny
`apollo_class_misconception_applied(attempt_id)` marker row (or a boolean on the
attempt) guards against re-apply on re-grade; without it, counters double-count.

**Promotion mechanics (the trust gradient).** Discrete thresholds recomputed on
each write: `candidate` (≥1 obs) → `observed` (≥N obs **or** ≥M distinct
students) → `promoted` (≥K distinct students **and** mean_confidence ≥ floor).
Bands are the visible gradient; there is no `approved` bit.

**Read path.** Grader loader overlays `status='promoted'` rows as `misc.*`
candidates alongside/instead of the hand-authored bank. Projections read the full
row set (all bands) for the teacher heatmap. Pedagogy reads `promoted`+`observed`.

**Cold-start.** Empty table → no promoted rows → assert nothing (P4). ✔
**Per-class isolation.** `search_space_id` in the unique key + cascade. ✔
**Failure modes.** The counters are **derived state with no cheap source of
truth** — a lost/failed write silently under-counts and nothing reconciles it;
needs a periodic rebuild job. Discrete tiers are a *coarse* gradient (the owner
asked for a gradient; three buckets is the minimum that qualifies).
**Size.** Medium: 1 table + 1 migration, 1 write hook + applied-marker, 1 loader
overlay, 1 projection query. ~6 files.
**Where it loses:** *drift* — materialized counters diverge from the artifacts on
any lost write and there is no self-correcting source of truth without an extra
rebuild job.

---

### Candidate B — Derived-on-read over artifacts, continuous trust, no new write path

**Data model (migration 036 optional).** **No new base table.** A read-time
aggregation (a SQL view, or a `MATERIALIZED VIEW` refreshed on a schedule) over
`apollo_grading_artifacts` (`role='canonical'`):

```
GROUP BY (search_space_id, concept_id, signature)
  observation_count      = count(distinct attempt_id)
  distinct_student_count = count(distinct user_id)
  mean_confidence        = avg(confidence)
  last_seen              = max(created_at)
  trust                  = f(distinct_student_count, mean_confidence, recency)
```

Signatures are extracted from the JSON `misconceptions[]` / `node_ledger[]` at
read time.

**Write path.** **None.** The artifact write *is* the write. Idempotency is free
(artifacts are already `UNIQUE(attempt_id, role)`, append-only, immutable). This
is the strongest P2/P6 story of the three.

**Promotion mechanics.** A **continuous** `trust ∈ [0,1]`; two thresholds applied
at read — `τ_assert` (grader mints `misc.*` only above it) and `τ_project`
(teacher sees the emerging gradient, typically lower). Bands
`candidate/observed/promoted` are just labels on `trust`. This is the cleanest
expression of "trust gradient, not binary."

**Read path.** Grader loader runs the aggregation filtered to `trust ≥ τ_assert`
for the session's `(space, concept)`; projections run it (whole gradient or
`≥ τ_project`); pedagogy reads the ranked list.

**Cold-start.** Zero artifacts → empty result → assert nothing — **natively**, no
special case (converges with B3a with zero extra code). ✔
**Per-class isolation.** `WHERE search_space_id = ?`; the
`ix_grading_artifacts_space_concept_time` index exists for exactly this. ✔
**Failure modes.** (1) **Read-time cost**: aggregating JSONB over a growing
artifact table on *every* Done-grade and *every* teacher page load; a hot class
re-aggregates constantly. A materialized view mitigates but adds refresh lag /
staleness. (2) **Nowhere to hang mutable state**: there is no row to write a
teacher `mute`/`approve`/note onto, and no promotion audit trail — the store is
purely a projection.
**Size.** Smallest: 0–1 migration (the optional matview), 1 aggregation module,
loader + projection wiring. ~4 files.
**Where it loses:** *latency + no mutable surface* — pure-derived means teacher
curation and an audit trail have no home, and read cost grows with history.

---

### Candidate C — Event-sourced: append-only observation ledger + materialized bank (RECOMMENDED base)

**Data model (migration 036).** Two tables, mirroring the proven
`MasteryEvent`(log) + `LearnerState`(rollup) pattern:

```
apollo_misconception_observations           -- append-only evidence log (source of truth)
  id                bigserial pk
  search_space_id   int   fk … on delete cascade
  concept_id        bigint fk apollo_concepts on delete set null
  signature         text  not null
  user_id           uuid  not null           -- for distinct-student counting
  attempt_id        bigint fk apollo_problem_attempts on delete set null
  confidence        real  null
  evidence_span     text  null
  source            text  not null default 'grading_artifact'  -- future: 'clarification_refuted'
  created_at        timestamptz not null
  UNIQUE(attempt_id, signature)              -- P6 idempotency, mirrors MasteryEvent

apollo_class_misconceptions                  -- rolled-up bank (rebuildable cache)
  (search_space_id, concept_id, signature) pk
  observation_count / distinct_student_count / mean_confidence / first_seen / last_seen
  trust_score       real  not null           -- continuous (P3)
  status            text  not null           -- derived band label
  -- MUTABLE teacher-curation columns (the thing B cannot hold):
  muted             bool  not null default false
  teacher_note      text  null
  curated_by        uuid  null
  curated_at        timestamptz null
```

**Write path.** Done hook appends **one observation row per `(attempt,
signature)`** (idempotent via `UNIQUE(attempt_id, signature)`), then an
**incremental rollup** updates the bank counters + `trust_score`. Because the log
is the source of truth, the bank is fully **rebuildable** by replay (self-healing
— the failure mode Candidate A cannot fix cheaply). Rollup can be inline
(simplest) or a periodic janitor (mirrors migration 028's learner-model janitor;
watch the single-drainer / lease contention rule from prior Apollo ops — OQ7).

**Promotion mechanics.** Continuous `trust_score = g(distinct_student_count,
mean_confidence, recency-decay)`; `status` is the band label; `τ_assert` /
`τ_project` as in B. Teacher `muted=true` **suppresses assertion without
deleting evidence** — curation annotates, never gates emergence (P3).

**Read path.** Grader + projections + pedagogy all read the bank
(`trust_score`/`status`, honoring `muted`). Teacher curation writes back to the
bank only.

**Cold-start.** Both tables empty → nothing asserted (P4). ✔
**Per-class isolation.** `search_space_id` everywhere + cascade. ✔
**Failure modes.** Most moving parts (2 tables + rollup); the rollup is a second
place drift *could* occur — but the observation log is replayable, so the bank is
always reconstructible (drift is recoverable, unlike A). If the rollup is a
worker, it inherits Apollo's lease-contention hazard (bound scope, single
drainer).
**Size.** Largest: 2 tables + 1 migration, write hook, rollup (inline or worker),
loader overlay, projection query, teacher-curation write path. ~8 files.
**Where it loses:** *operational surface* — the most code and (if worker-based)
the most ops risk of the three.

---

### 5.4 Trade-off matrix (each candidate loses on ≥1 axis)

| Axis | A (materialized tiers) | B (derived-on-read) | C (event-sourced) |
|---|---|---|---|
| Emergent / source-of-truth (P2) | cache, no truth source ✗ | artifacts ARE truth ✔✔ | log IS truth, replayable ✔✔ |
| Trust gradient fidelity (P3) | coarse (3 tiers) ✗ | continuous ✔ | continuous ✔ |
| Idempotency (P6) | needs applied-marker ✗ | free ✔✔ | free (unique key) ✔ |
| Read latency at scale | fast (materialized) ✔ | **slow / stale** ✗ | fast ✔ |
| Mutable teacher curation + audit | possible ✔ | **no home** ✗ | first-class ✔✔ |
| Drift recovery | **needs rebuild job** ✗ | n/a (derived) ✔ | replayable ✔ |
| Implementation size | medium | **smallest** ✔ | **largest** ✗ |
| Ops surface | low | lowest ✔ | **highest** ✗ |

---

## 6. Recommendation

**Adopt Candidate C's data model, shipped in two increments — with increment 1
using Candidate B's derived-read semantics directly over C's observation ledger.**

- **Increment 1 (the D1 fix, one migration 036).** Land
  `apollo_misconception_observations` (append-only log). The Done hook appends
  observations idempotently from the `role='canonical'` artifact. The grader and
  projections read the store by **aggregating the ledger on read** (B's
  semantics) with a continuous `trust_score` and thresholds `τ_assert` /
  `τ_project`. **No materialized bank, no worker, no teacher-curation surface
  yet.** This gives: emergent (P1), per-class (P5), source-of-truth ledger (P2),
  idempotent (P6, `UNIQUE(attempt_id, signature)`), continuous trust gradient
  (P3), native empty-is-normal (P4, converges with B3a with zero special-casing),
  and it is only marginally larger than pure-B while avoiding B's "nowhere to hang
  state" dead-end.
- **Increment 2 (when demonstrated need arrives).** Materialize
  `apollo_class_misconceptions` as the rollup cache + add the mutable
  teacher-curation columns (`muted`, note, audit) + the free-form-signature
  embedding clustering. Trigger this increment on evidence (read-latency on a hot
  class, or a concrete teacher-curation ask) — not speculatively.

**Why C over A and B:**
- **Over A:** A's materialized counters are derived state with no cheap source of
  truth; a lost write under-counts silently. C's ledger *is* the truth, so the
  cache is always rebuildable — drift is recoverable, not permanent.
- **Over B:** B is the smallest but has **no home for teacher curation and no
  audit trail**, and its read cost grows unbounded with history. C's increment 1
  keeps B's clean derived-read *and* leaves a durable, curatable, replayable log
  underneath — so increment 2 is additive, not a rewrite.

### How this converges with the deferred 2026-06-23 trust-gradient decision

The 2026-06-23 decision **rejected the binary approved-only store** and mandated a
trust gradient, but **deferred** (a) the accumulation unit, (b) the trust
function, and (c) the thresholds. This design **resolves all three** and
**inherits the rejection**:

- **(a) Unit:** `(search_space_id, concept_id, signature)` over canonical grading
  artifacts (§4) — the per-class, emergent, per-subtopic unit the diagnosis asked
  for.
- **(b) Trust function:** continuous `trust_score = g(distinct_students,
  mean_confidence, recency-decay)` (OQ2), with `candidate/observed/promoted` as
  **labels over thresholds**, not states in a state machine.
- **(c) Thresholds:** two — `τ_assert` (gates grader assertion) and `τ_project`
  (gates teacher visibility, lower so teachers see the gradient forming before the
  grader acts on it).
- **Inherits the binary rejection:** there is **no `approved` bit that gates
  assertion**. Trust is continuous; the only teacher control is `muted`
  (increment 2), which *suppresses* an emergent assertion without deleting its
  evidence — curation annotates, it never gates emergence. That is precisely the
  "trust-gradient / promotion pipeline, NOT binary approved-only" the owner
  asked for.
- **Inherits the empty-bank sentinel:** empty store = `assertable=false`,
  reason-only, never abstain — the 2026-06-23 soundness-N/A pattern applied to
  cold-start, already staged by B3a.

---

## 7. Open questions for the humans (each has a default; the mandate lets me proceed on it)

1. **Free-form (`opposes=None`) signature identity.** How do we key a misconception
   that contradicts no known reference node?
   *Default:* increment 1 keys **only on `canonical_key`**; unkeyed observations
   accumulate under a per-concept `unkeyed:*` bucket but **never promote**.
   Embedding-cluster signatures (using `evidence_span` + `description_embedding`)
   are deferred to increment 2.

2. **Trust function + threshold values.** What exactly is `g()`, and where do
   `τ_assert` / `τ_project` sit?
   *Default:* `trust = min(1, distinct_students / K) · mean_confidence` with a
   recency half-life; `K = 3` distinct students, `τ_assert = 0.5`,
   `τ_project = 0.2`. Calibrate against the campaign corpus before enabling live.

3. **Does the grader ASSERT emergent misconceptions as `watch_out`, or only
   detect/count them?**
   *Default:* **yes, it asserts** — promoted (`trust ≥ τ_assert`, not `muted`)
   class-misconceptions are minted as `misc.*` candidates exactly like the
   hand-authored bank, behind a flag `APOLLO_EMERGENT_MISCONCEPTIONS_ENABLED`
   that stays **OFF** until calibration (OQ2) passes.

4. **Do shadow (`role='pair'`) artifacts feed the store?**
   *Default:* **No — `role='canonical'` only.** The store reflects grades students
   were actually served, and this avoids double-counting the paired capture.

5. **Keep or retire the hand-authored `apollo_misconceptions` bank + seeder?**
   *Default:* **keep the table** (the emergent store is additive and can co-exist
   or overlay), **stop using the seeder in any production path** now. Retiring the
   hand-authored bank is a separate decision after the emergent store proves out.

6. **Increment 1 accumulation: add the observation ledger table, or aggregate
   straight over `apollo_grading_artifacts` (pure B, no new table)?**
   *Default:* **add `apollo_misconception_observations` (migration 036).** It buys
   a stable idempotency key, a curation/audit anchor for increment 2, and
   decouples the store from artifact-JSON-schema churn — at the cost of one table.

7. **Increment-2 rollup: inline-on-write or a periodic janitor worker?**
   *Default:* **inline incremental rollup** in increment 2; only move to a worker
   if write-path latency demands it, and if so, honor the single-drainer /
   bounded-lease rule (prior Apollo drain-contention hazard).

8. **Backfill existing artifacts into the store on first deploy?**
   *Default:* **No — forward-only.** The ledger is derivable from historical
   artifacts, so a one-shot backfill script is *possible* if humans want history,
   but it is out of scope for the D1 fix.

---

## 8. Non-goals (explicit)

- **Cross-class / global aggregation.** Canonical *subjects* are global per class,
  but the misconception *population* is per-class. The store never merges
  observations across `search_space_id`. (No "this misconception is common across
  all Physics classes" rollup.)
- **Retroactive reprocessing of old artifacts.** Increment 1 is forward-only
  (OQ8). No historical backfill unless humans explicitly request it.
- **Replacing / deleting the hand-authored `apollo_misconceptions` bank in this
  change** (OQ5). Additive now; retirement is a separate decision.
- **The per-student misconception profile.** The clarification-loop design (§3,
  §8) defers it; it stays deferred here. This store is per-class, not per-student.
- **Teacher-authored / teacher-curation UI in increment 1** (mutable curation is
  increment 2; the ledger merely does not foreclose it — mirroring the
  clarification loop's own discipline).
- **Consuming `refuted` clarification rows as a second write source in v1.** The
  `source` column reserves room for it (§5.3), but wiring it is out of scope until
  the clarification loop's own G2 issues are resolved.
- **Re-introducing abstain-on-empty-bank in any form.** Empty = `assertable=false`,
  reason-only (P4, inherited from the 2026-06-23 soundness-N/A design and B3a).
