---
title: Resolver V2 → Clarification Loop Integration (VoI trigger + ranker)
status: spec / not-yet-built
author: spec-author (subagent)
date: 2026-07-07
branch: feat/apollo-resolver-v2
supersedes: none
relates_to:
  - docs/_archive/specs/2026-07-07-resolver-v2-design.md
  - docs/_archive/specs/2026-06-29-apollo-clarification-loop-design.md
  - docs/_archive/handoffs/2026-07-07-apollo-composite-score-deflation-handoff.md
  - docs/architecture/apollo.md  (Resolver V2 subsection, lines 78–126)
owns_after_build: [apollo/clarification/v2_ranker.py, apollo/clarification/v2_selection.py, apollo/clarification/v2_config.py, apollo/resolver_v2/incremental.py, apollo/resolver_v2/incremental_types.py]
migration: none (reuses migration 033 apollo_clarifications)
---

# Resolver V2 → Clarification Loop Integration

## 0. One-paragraph summary

Today the live clarification loop (`APOLLO_CLARIFICATION_ENABLED`) selects
which nodes to probe using a purely lexical/embedding v1 signal over the
**current utterance only**, ranked by a static per-node-type rubric weight.
This spec wires Resolver V2's per-node NLI scores in as both the **trigger**
(which reference nodes are weak/gray/missing) and the **ranker** (value-of-
information = importance × uncertainty), packs the winners into ≤3 answer-blind
questions of ≤3 topics each, dedups asked topics by *candidate/reference key*
(closing a v1 gap), and feeds clarification-confirmed nodes back into the
per-turn V2 incremental score. It is gated behind a new default-OFF flag
`APOLLO_CLARIFICATION_V2_RANKER`. **Every** new side effect — including the
background incremental V2 kick in `chat.py` — is gated behind that flag AND
`APOLLO_RESOLVER_V2` AND `APOLLO_CLARIFICATION_ENABLED`, so the whole feature is
a byte-identical no-op unless all three are ON (not merely the *selection*
branch — see H1 fix, §1.3/§8.2). It **fails open** to the existing v1 selection
on any V2 error *or* incremental-job exception. The per-turn incremental score is
a **conservative monotone lower bound** on the from-scratch batch grade — it is
NOT claimed byte-equal to batch (edges, gray-zone, and v1 edge-floors diverge by
design; §5.4) — and is used only to *rank questions*, never to grade. V2 remains
shadow-only for *grading*; this integration is the first place V2 scores drive a
**live, user-facing** behavior (question choice), so gating and fallback are
explicit.

---

## 1. Scope, non-goals, and the "why this is safe" argument

### 1.1 In scope

1. Run V2 internal scoring over **all reference nodes** (not just current-
   utterance parses) to obtain per-node credit + gray classification.
2. Identify weak/gray/missing nodes using the V2 gray band.
3. Rank candidates by VoI = importance × uncertainty.
4. Pack winners into ≤3 questions × ≤3 topics.
5. Mark asked topic keys; never re-ask the same topic key within an attempt.
6. Preserve answer-blindness (reuse existing hint + leak-guard machinery).
7. Feed clarification-confirmed nodes back into the running V2 per-node max.
8. Per-turn **incremental** V2 scoring (append-only windows × still-unresolved
   node views), respecting the `max_nli_pairs` budget, running in a background
   thread between turns.
9. New flag, behavior matrix, and fail-open fallback to v1 selection.

### 1.2 NON-GOALS (explicitly deferred — do NOT touch in this build)

- **Misconception penalty rework** (FINAL-VERDICT §6 blocker 1, G1/G4a,
  attempt-48 nondeterminism). Out of scope.
- **`proc.plan_*` bare-definition cap** (blocker 2, attempt 14 application/
  execution check). Out of scope.
- **Control-negative bank re-authoring** (blocker 3). Out of scope. Note the
  validation caveat in §11.
- **SymPy `parse_expr` RCE fix** (`apollo-sympy-rce-2026-07-06`). Out of scope;
  this feature adds no new parse surface.
- **95% patch-coverage polish / promotion gate re-run**. Smoke-test convention
  only (§10 test expectations); the 95% gate is enforced at PR time, not here.
- **Grading composite behavior.** V2's grading substitution path
  (`done_grading.py` step 8b) is UNCHANGED. This feature only reads V2 scores
  for question selection during chat.
- **Migration / schema changes.** Reuse 033. (§9)

### 1.3 Safety argument

- **OFF at ANY of the three flags → the entire feature is a no-op** (H1 fix).
  The gate that guards `chat.py`'s background incremental kick is the SAME
  `_v2_ranker_active(...)` predicate that guards the selection branch:
  `clarification_v2_ranker_enabled() AND resolver_v2_enabled() AND
  clarification_enabled()`. If any is OFF, `chat.py` starts NO background NLI
  thread, mutates NO session state, and spends NONE of the `max_nli_pairs`
  budget; `run_clarification_detection` runs the identical v1 pipeline;
  artifacts and chat replies are byte-identical. In particular the
  `(APOLLO_RESOLVER_V2=ON, APOLLO_CLARIFICATION_V2_RANKER=OFF)` matrix row — a
  real rollout state, since V2 grading rolls out separately — is a strict no-op
  for this feature, and the T15 golden asserts byte-identity **in that specific
  row**, not only the all-OFF row.
- V2 grading path untouched → composite/§10 gate/artifact unaffected regardless
  of this flag.
- **Fail-open covers BOTH the selection call AND the awaited/consumed
  incremental result** (H3 fix). The background incremental job is wrapped so
  any exception inside it is caught and recorded as "no snapshot this turn";
  selection then reads the most-recent *completed* snapshot (or, if none,
  v1-fallback). The teaching reply is never awaited on the *current* turn's
  incremental job (§5.5), so an incremental exception can never propagate into
  the reply. Any exception in the V2 selection path is caught and logged
  (`clarification_v2_ranker_failed_falling_back_to_v1`) and the turn continues
  on v1 selection — matching the live shadow semantics documented in apollo.md
  (flag-ON failure keeps v1, never crashes). This overrides the resolver-v2
  design card's "NO-FALLBACK" line; live behavior wins.

---

## 2. Module / file layout

New code splits across the two owning packages by responsibility. The rule:
**pure V2 scoring math lives in `apollo/resolver_v2/`; clarification-loop
orchestration/selection/packing lives in `apollo/clarification/`.** This keeps
`resolver_v2/` free of any clarification imports (preserving the current
one-way dependency: clarification may import resolver_v2, never the reverse).

### 2.1 New files

| File | Package | Responsibility | Notes |
|---|---|---|---|
| `apollo/resolver_v2/incremental.py` | resolver_v2 | Per-turn incremental scoring: given persisted `IncrementalState` + new student turns, build only the **new** windows, **reindex them into the global window space** (offset adapter — see §5.3 step 1a; `build_windows` is composed, not forked, then its 0-based output is rewritten to global `index`/`turn_index`), rescore only **still-unresolved** ref-node views, run the same **gray-zone stage** the batch engine runs (§5.3 step 4b), merge into a running per-node max AND a running per-edge relation-evidence-tier max, return updated state + `ResolverV2Result`-shaped snapshot. Pure/deterministic; injected `nli`, `grayzone_fn`, `select_fn`, `params`. | <400 lines. No DB, no env. |
| `apollo/resolver_v2/incremental_types.py` | resolver_v2 | Frozen dataclasses `IncrementalState`, `IncrementalSnapshot` (incl. `running_edge_evidence` for edge monotonicity, §5.2). | Kept separate from `types.py` to stay <400 lines; may fold into `types.py` if small. |
| `apollo/clarification/v2_ranker.py` | clarification | VoI ranking (`rank_by_voi`), candidate extraction from a V2 snapshot (`v2_gray_candidates`), and question packing (`pack_questions`, 3×3). No NLI/model. | <400 lines. |
| `apollo/clarification/v2_selection.py` | clarification | The alternate selection pipeline that `run_clarification_detection` branches into when the flag is ON: pool = reference nodes, trigger = V2 gray band, rank = `v2_ranker`, dedup = candidate-key. Returns the **same `list[str]` answer-blind hints** the v1 path returns (v1 `run_clarification_detection` returns `list[str]`, NOT `Probe` objects — the §3.1 pseudocode builds hints via `build_probe_hint`). | Thin orchestrator. |
| `apollo/clarification/v2_config.py` | clarification | Fresh-read flag + params for the ranker (mirrors `resolver_v2/config.py` idioms). | See §8. |

### 2.2 Modified files

| File | Change |
|---|---|
| `apollo/clarification/turn.py` | **Signature change (enumerated, A-MINOR fix):** `run_clarification_detection` gains keyword-only params `snapshot: IncrementalSnapshot \| None = None`, `reference_graph=None`, `problem_payload=None`, and `resolved_candidate_keys: frozenset[str] = frozenset()` — all defaulted so existing v1 callers/tests are unchanged. Branches at the top: if `_v2_ranker_active(...)` (all three flags) AND `snapshot is not None` → `v2_selection.select(snapshot, candidates, db, attempt_id, ...)`; else v1 path unchanged. Wrap the V2 branch in try/except → fall back to `_v1_select(...)`. Return type stays `list[str]`. |
| `apollo/handlers/chat.py` | (a) **Gated on `_v2_ranker_active(...)` (all three flags — H1 fix): only then** kick the background per-turn V2 incremental score (see §5), enforcing **one in-flight job per attempt** (skip the kick if the prior turn's job is still running — M2/M1 fix), and stash the resulting `IncrementalState` + latest *completed* `IncrementalSnapshot` on session state. When the gate is OFF, chat.py does nothing new (byte-identical). (b) Pass the **most-recent completed** snapshot (never blocking on the current turn's job — §5.5) into `run_clarification_detection`. (c) On the *next* turn, after `resolve_pending_clarifications` records a **content-verified `confirmed`** outcome, seed that node's V2 view (§6). No change to the v1-OFF code path. |
| `apollo/clarification/store.py` | Add `load_asked_candidate_keys(db, attempt_id) -> set[str]` (query distinct `candidate_key` where state != vague-terminal-noise, i.e. any asked row) for topic-key dedup. Optionally extend `write_asked_waiting` to also carry the reference/candidate key already stored. |
| `apollo/handlers/done_grading.py` | No behavioral change to grading. Optionally: if a live `IncrementalState` exists on session at Done, log a trace comparing it to the from-scratch V2 result (observability only, §7). Default: leave grading path exactly as-is. |

**No changes** to `resolver_v2/engine.py`, `scoring.py`, `edges.py`,
`aggregate.py`, `integration.py`, `windows.py`, `grayzone.py` (grading path
stays intact). `incremental.py` *composes* the existing pure sub-stage functions
(`build_windows`, `score_nodes`, `apply_grayzone`, `select_windows`,
`score_edges`, `aggregate`) on suffixes/subsets. It does **not** fork them, but
because `build_windows(suffix)` re-indexes its output from 0 (windows.py:130
sets `Window(index=len(windows), turn_index=turn_index)` from the *passed*
sequence), `incremental.py` owns a thin **offset adapter** that rewrites the
returned windows into the global index/turn space (§5.3 step 1a). Composing +
adapting the output is not forking the function — the source of `build_windows`
is untouched.

---

## 3. Data flow

### 3.1 Per chat turn (flag ON, clarification ON, resolver_v2 available)

All three of the branches below are gated on `_v2_ranker_active(...)` = all of
`APOLLO_CLARIFICATION_ENABLED`, `APOLLO_RESOLVER_V2`, `APOLLO_CLARIFICATION_V2_RANKER`
being ON (H1). When the gate is OFF the `(new)` branches DO NOT RUN AT ALL — no
thread, no session mutation, no budget spend.

```
student message
  │
  ├─(existing) resolve_pending_clarifications  ── records outcome via content-verified LLM judge
  │        └─► §6 SEED (only on outcome == "confirmed", i.e. the judge ruled the committed
  │            clarification correctly expresses the target idea — NOT self-report):
  │            set V2 running-max[node]=1.0 (source="clarification"), freeze node.
  │            Does NOT auto-lift neighbor nodes (edge pull-up requires real ENTAIL evidence — §6 / M5).
  │
  ├─(new, gated + one-in-flight-per-attempt) INCREMENTAL V2 SCORE  [background asyncio.to_thread]
  │        NOTE: this job feeds the NEXT turn's selection, not this one (§5.5). It never blocks the reply.
  │        input:  IncrementalState (from session, best-effort) + all student turns so far
  │        build:  new windows over appended turns only (cursor) + overlap tail; REINDEX to global space (§5.3 1a)
  │        score:  score_nodes over ref-nodes whose running credit is NOT resolved (credit < t_high)
  │        gray:   apply_grayzone stage (same grayzone_fn as batch; §5.3 4b) — conservative if skipped
  │        merge:  running_max[node]      = max(old, new fused)            ── append-only ⇒ monotone
  │                running_edge_evidence  = max_tier(old, this-turn tier)  ── monotone r(e) (A-MAJOR-1 fix)
  │        edges:  recompute edge credit from running node credits × running_edge_evidence tier
  │        output: IncrementalSnapshot (node credits, edge_scores, node_cov, edge_cov,
  │                winning_path_index, gray set, pair_count_this_turn)
  │        persist: new IncrementalState + this completed snapshot onto session state (single writer)
  │        on-exception: caught → recorded as "no completed snapshot this turn" (H3); never reaches reply
  │
  └─(new, gated) run_clarification_detection(snapshot=<most-recent COMPLETED>, ...)
           if _v2_ranker_active AND completed snapshot present:
             pool      = v2_gray_candidates(snapshot)              # weak/gray/missing ref nodes
             minus     = load_asked_candidate_keys(db, attempt_id) # topic-key dedup + per-attempt cap (§10)
             ranked    = rank_by_voi(pool, snapshot, weights)      # §4
             questions = pack_questions(ranked, max_q=3, max_topics=3)
             for each selected topic: build_probe_hint + write_asked_waiting(candidate_key)
           else / on error / no completed snapshot:
             v1 path unchanged (find_residual_nodes → detect_ambiguous_nodes → select_probes)
        │
        └─► clarification_hints → draft_reply (Apollo voice) → guard_clarification_reply (leak backstop)
```

The incremental score and the selection are **decoupled**, not sequenced on the
current turn: to guarantee "never block the teaching reply on V2" (§5.5),
selection consumes the **most-recent completed** snapshot (produced by a prior
turn's background job on this worker), NOT a snapshot it waits for this turn.
Turn 1 (no completed snapshot yet) → v1 fallback. This one-turn lag is
intentional and documented; it removes the latency-blocking and thread-pool
contention risks (M1) and, with the one-in-flight-per-attempt cap, the
read-modify-write race (M2).

### 3.2 At Done-grading

**Unchanged.** `done_grading.py` step 8b still calls `apply_resolver_v2` which
runs the full from-scratch V2 (batch) over all turns and substitutes grade
coverages when `APOLLO_RESOLVER_V2` is ON. The incremental per-turn state is a
chat-time convenience for question selection and is NOT the grading source of
truth. Rationale: the from-scratch batch is the calibrated, tested grading
path; the incremental snapshot is monotone-approximate and must not silently
become the grade. (Optional observability diff only — §7.)

---

## 4. VoI ranking

### 4.1 Signature

```python
# apollo/clarification/v2_ranker.py

@dataclass(frozen=True)
class VoICandidate:
    canonical_key: str
    node_type: str
    node_credit: float          # current running V2 credit c_u (∈[0,1])
    is_gray: bool
    incident_edges: tuple[EdgeRef, ...]   # ref edges touching this node
    best_window_index: int | None         # from NodeScore.best (co-occurrence hint)

@dataclass(frozen=True)
class VoIScore:
    candidate: VoICandidate
    importance: float           # expected composite gain if node resolves
    uncertainty: float          # P(a question flips it)
    voi: float                  # importance * uncertainty

def rank_by_voi(
    pool: Sequence[VoICandidate],
    snapshot: IncrementalSnapshot,
    weights: CompositeWeights,          # from apollo/grading/composite.load_weights()
    params: ClarificationV2Params,
) -> list[VoIScore]:
    ...
```

### 4.2 Formula (real names)

`weights` are the **live grading composite weights** read fresh via
`apollo.grading.composite.load_weights()` — so VoI is expressed in the same
currency as the grade the student is optimizing:

```
w_n = weights.w_n   # _DEFAULT_W_NODE = 0.706
w_e = weights.w_e   # _DEFAULT_W_EDGE = 0.294
```

**Importance (expected composite gain if node u resolves to full credit).**
Model the resolve as raising u's credit from its current running value `c_u`
to a target `c_u'`. Target = `t_high`-tier credit `1.0` (a confirmed
clarification credits @0.90 via the v1 method, but for *ranking* we use the
optimistic full-resolution ceiling so hub value isn't undercounted; the exact
constant is a param `voi_target_credit`, default 1.0).

```
Δnode_cov(u) = (c_u' - c_u) / |winning_path|          # winning path node count

Δedge_cov(u) = Σ_{e ∈ incident(u)}  edge_gain(e, c_u', c_v)
               ───────────────────────────────────────────
                              |all_ref_edges|

importance(u) = w_n * Δnode_cov(u) + w_e * Δedge_cov(u)
```

**`edge_gain` — corrected reconstruction (A-MAJOR-5 fix).** `EdgeScore` has **no
numeric `r` field**; it carries `credit: float` and `relation_evidence: str` ∈
`{entail, cooccur, endpoints, v1_explicit, v1_inferred, none}` (types.py:62-69).
The earlier draft's "reuse the `relation_evidence` → tier map (ENTAIL/COOCCUR/
ENDPOINTS/0.0)" left **3 of 6** evidence values (`v1_explicit`, `v1_inferred`,
`none`) with no tier — so a gray hub whose incident edges were credited via v1
floors would map to `r=0` and contribute **zero** gain, undercounting exactly
the high-value hubs the model is supposed to reward. And for v1-floored edges
`edges.py` sets a **flat** credit (`_V1_EXPLICIT_FLOOR=1.0` / `_V1_INFERRED_FLOOR
=0.5`), not `r·sqrt`, so the "exactly edges.py's `r·sqrt`" claim was false.

`edge_gain(e, c_u', c_v)` instead **mirrors edges.py's final-credit computation**
using only snapshot data, and returns the *non-negative* rise in that edge's
credit if `u` resolves to `c_u'` (with `c_v` held at its current running value):

1. Recover the recorded NLI/co-occurrence tier from `e.relation_evidence` via a
   **complete 6-entry map** (no case undefined):
   `entail→1.0, cooccur→0.7, endpoints→0.4, v1_explicit→1.0, v1_inferred→0.5,
   none→0.0` (constants imported/mirrored from `edges.py`
   `_R_ENTAIL/_R_COOCCUR/_R_ENDPOINTS`).
2. Recompute the **deterministic** tiers under the hypothesis: if
   `min(c_u', c_v) >= _ENDPOINTS_MIN_CREDIT (0.7)`, the ENDPOINTS tier (0.4) is
   newly available — so resolving a gray hub to ≥0.7 *unlocks* endpoint credit
   on every incident edge whose other endpoint is already ≥0.7 (this is the
   real hub reward, and it works even for edges currently tagged `none`). The
   ENTAIL/COOCCUR tiers are **not** recomputed (they need NLI/window argmax we
   will not re-run at ranking time) — the recorded tier is monotone and kept.
   `r = max(recorded_tier, endpoints_tier_if_applicable)`.
3. `graded = r * sqrt(c_u' * c_v)`.
4. `new_credit = max(graded, v1_floor(e.relation_evidence))` where
   `v1_floor = 1.0` for `v1_explicit`, `0.5` for `v1_inferred`, else `0.0`.
5. `edge_gain = max(0.0, new_credit - e.credit)`.

Consequences: a `v1_explicit` edge (already credit 1.0) yields gain 0 (correct —
already maxed, nothing to buy by re-probing). A `v1_inferred` edge (0.5) that a
hub resolution lifts above 0.5 via ENTAIL/ENDPOINTS yields positive gain. A gray
hub with several incident edges to already-credited neighbors gets large
`Δedge_cov` → **hubs rank high naturally**, as required, without the
under-counting hole.

- `c_v` = current running credit of the *other* endpoint (from the snapshot).
  Do NOT recompute NLI.
- `|winning_path|` and `|all_ref_edges|` come from the snapshot's aggregate
  metadata (winning_path_index → path length; total ref edge count).

**Uncertainty (P a question flips it).** A monotone bump function of how deep in
the gray band the node sits — nodes near `t_mid` are nearly resolved (low flip
value); nodes near/below `t_low` are the sweet spot; already-`t_high` nodes are
excluded upstream. Missing nodes (`credit==0`, source `"zero"`) get a fixed
`p_missing`.

```
if c_u == 0:                      uncertainty = params.p_missing        # default 0.6
elif c_u >= t_mid:                uncertainty = params.p_near_resolved  # default 0.2
else:  # gray band [t_low, t_mid)
    frac = (t_mid - c_u) / (t_mid - t_low)      # 0 at t_mid, 1 at t_low
    uncertainty = params.p_gray_min + frac*(params.p_gray_max - params.p_gray_min)
    # defaults p_gray_min=0.3, p_gray_max=0.8
```

Equation-cap nodes (`source=="equation_cap"`, gray *unconditionally*) get an
uncertainty floor `params.p_equation_floor` (default 0.7) — the "can you write
out the equation you used?" recovery is known to be high-yield (FINAL-VERDICT
§6.3: personas teach omitted beats when asked). This is the designed recovery
mechanism for eqgate-capped equation nodes.

**VoI** = `importance * uncertainty`. Ties broken by `(voi desc, node_credit
asc, canonical_key asc)` for determinism.

### 4.3 Why this displaces `rubric_weight_for`

`pacing.rubric_weight_for` ranks by static node-*type* axis weight only. VoI
subsumes it: node-type importance enters through `w_n`/`w_e` and path/edge
structure, and adds per-node uncertainty and hub-edge leverage — exactly the
"importance × uncertainty = value-of-information" locked decision.

---

## 5. Incremental scoring mechanics

### 5.1 What persists, and where

**Session state, NOT the DB.** The clarification loop already runs inside chat
with a per-request DB session; the incremental score is a within-attempt
in-memory accelerator whose loss is harmless (Done-grading re-derives from
scratch). Persisting it to the DB would add a migration and a write on the hot
teaching path for zero grading benefit → violates NON-GOALS. Store it on the
in-process per-attempt holder keyed by `attempt_id`.

**Single-writer + multi-worker reality (M2 fix).**

- **Single writer per attempt.** At most ONE incremental job is in flight per
  `attempt_id` at any time (§2.2 / §5.5): a new turn does not kick a second job
  while the prior one runs. Because only that one job ever writes the attempt's
  state, the read-modify-write is serialized and the lost-update / cursor
  regression race is structurally impossible. State updates are also
  monotone-guarded: a write whose `window_cursor` is not strictly greater than
  the stored one is discarded (defensive against reordering).
- **Multi-worker (Railway web replicas).** The in-process holder is **NOT**
  shared across workers. Consecutive turns of one attempt may land on different
  replicas → the new replica sees cold state. This is explicitly **NOT** patched
  with a shared cache in this build; instead: a cold turn simply has **no
  completed snapshot** → it **falls back to v1 selection** (fail-open, §8.3) and
  its background job rebuilds from `window_cursor=0` (bounded by
  `max_nli_pairs`). The process-local `dict[attempt_id, IncrementalState]` LRU
  is therefore an *intra-worker* accelerator only; it is **rejected as a
  correctness mechanism** and must never be assumed shared. **Efficacy caveat
  (feeds §11 gate):** without worker/session affinity the snapshot hit-rate — and
  hence how often the VoI ranker (vs v1) actually drives selection in production
  — may be low. This must be **measured** before flag-ON (§11); a DB-backed or
  sticky-session store is a documented follow-up, out of scope here.

### 5.2 `IncrementalState` (frozen)

```python
@dataclass(frozen=True)
class IncrementalState:
    window_cursor: int                       # number of student turns already windowed
    global_window_count: int                 # windows emitted so far (global index offset, §5.3 1a)
    running_node_max: Mapping[str, float]    # canonical_key -> best fused score so far
    node_source: Mapping[str, str]           # canonical_key -> source of that best
    running_edge_evidence: Mapping[str, str] # edge_key "TYPE|from|to" -> best relation-evidence
                                             #   tier seen so far (A-MAJOR-1: monotone r(e))
    seeded_keys: frozenset[str]              # keys pinned by clarification (§6)
    pair_count_total: int                    # cumulative NLI pairs used (budget guard)
```

`running_edge_evidence` is the fix for the **edge non-monotonicity** defect
(A-MAJOR-1): entailment/co-occurrence evidence can appear in windows from *any*
turn, so "new windows only" would lose evidence found in earlier turns. Keeping
a per-edge running **max of the relation-evidence tier** (`entail` > `cooccur` >
`endpoints` > v1 tiers > `none`, ranked by the §4.2 r-value) makes `r(e)`
monotone non-decreasing across turns; combined with monotone node credits, the
recomputed edge credit `r·sqrt(c_u·c_v)` is monotone too. The tie-break when two
tiers share an r-value (`v1_explicit` vs `entail`, both 1.0) keeps whichever was
stored (both yield the same edge credit ceiling). This does not require
persisting prior window *text* — only the winning tier per edge.

Immutable: each turn returns a **new** `IncrementalState` (repo immutability
rule).

### 5.3 Per-turn algorithm (`incremental.score_turn`)

```python
def score_turn(
    state: IncrementalState,
    *, all_student_turns, reference_graph, problem_payload,
    v1_resolved_keys: frozenset[str],                       # from the turn's v1 resolution
    nli, grayzone_fn, select_fn, params, ref_nodes,         # ref_nodes precomputed once
    v1_explicit_triples: frozenset = frozenset(),           # A-MAJOR-4: empty by default (conservative)
    v1_inferred_triples: frozenset = frozenset(),
) -> tuple[IncrementalState, IncrementalSnapshot]:
```

**On the edge v1-triple inputs (A-MAJOR-4 fix).** `score_edges` takes
`v1_explicit_triples`/`v1_inferred_triples` as **required** args (edges.py:140);
in batch these come from `v1_inputs_from_canonical(student_canonical)`, and the
student canonical is built only at Done — it is NOT available on the hot chat
turn. Rather than add a full canonical-build stage to the teaching path (an
integration cost we refuse), the incremental scorer passes **empty** v1 edge
triples by default. Consequence: incrementally, an edge that would earn a v1
floor at grading is NOT floored in the *selection* snapshot → it stays eligible
in the pool and may be probed. This is **conservative** (the safe direction:
selection never wrongly drops a weak edge; see §5.4). If a future build cheaply
has the v1 triples from the turn's candidate assembly, it may pass them; it must
never build the canonical inline. `v1_resolved_keys` (for `score_nodes`) IS
available at turn time — it is the set of candidate keys the v1 resolution
already matched this turn (chat.py has `inputs.candidates`), so node v1 floors
are applied exactly as batch.

1. **New windows only.** `new_turns = all_student_turns[state.window_cursor:]`.
   Build windows over the appended turns *plus the last `window_overlap` turns*
   of the prior slice so the sliding overlap is honored at the seam (compute
   from `params.window_overlap_sentences`; the overlap tail text is re-derived
   from `all_student_turns` — no prior text needs persisting).
1a. **Reindex to global space (A-MAJOR-2 fix).** `build_windows(suffix)`
   re-indexes from 0 and resets `turn_index` (windows.py:130). The scorer's
   offset adapter rewrites each returned window to
   `Window(index=state.global_window_count + local_i,
   turn_index=state.window_cursor + local_turn_offset, text=...)` so
   `winning_path_index` / `best.window_index` / cursor semantics live in one
   continuous global space, matching batch. `global_window_count` advances by
   the number of *new* (non-overlap) windows emitted.
2. **Unresolved views only.** Candidate ref-nodes = those with
   `running_node_max[key] < t_high` AND `key not in seeded_keys`. Resolved /
   seeded nodes are frozen — no new pairs spent on them.
3. **Score.** `score_nodes(new_windows, candidate_ref_nodes, ...)` with
   `select_fn` prefiltering windows per view (top_k_windows). Respect the
   budget: stop issuing pairs when `state.pair_count_total + pairs_this_turn`
   would exceed `params.max_nli_pairs` (default 200) — degrade gracefully
   (skip remaining low-priority views, record `budget_truncated=True` in trace).
4. **Merge (append-only max).** `new_max[key] = max(old, this_turn_fused)`.
   Because windows are append-only and node score is `max` over pairs, running
   max is **monotone non-decreasing** — a score can only rise per turn.
4a. **Apply v1 floors + equation gate** to the merged maxes exactly as batch
   does (equation_cap before v1_floor). Seeded keys (§6) override to 1.0.
4b. **Gray-zone stage (A-MAJOR-3 fix).** The batch engine runs `apply_grayzone`
   between node scoring and edges (engine.py:147-155); omitting it made every
   gray node's credit differ (raw NLI 0.3 vs upgraded 0.7) and shifted the pool.
   The incremental scorer runs the **same** `apply_grayzone(gray_nodes,
   transcript, grayzone_fn, params, ...)` with the **same injected `grayzone_fn`
   the batch path uses**. In the default deployment `APOLLO_RESOLVER_V2_GRAYZONE`
   is OFF ⇒ `grayzone_fn=None` in BOTH paths ⇒ no divergence. When grayzone is
   ON, to avoid a per-turn LLM call on the hot path the build MAY pass
   `grayzone_fn=None` here; the resulting gray node stays at 0.3 instead of the
   batch's ≤0.7 — a **conservative under-credit** (it keeps the node in the pool,
   the safe direction; §5.4). Whichever choice the build makes MUST be
   conservative (never credit a gray node *higher* than batch would).
5. **Edges (monotone, A-MAJOR-1).** For the new windows, compute this-turn edge
   relation-evidence tiers via the existing `score_edges` ladder; **merge each
   edge's tier into `running_edge_evidence` by max r-value** (§5.2). Then
   recompute every reference edge's credit from the *running* node credits ×
   *running* evidence tier (deterministic; ENTAIL/COOCCUR pairs run this turn
   count against the budget). This yields edge coverage that is monotone across
   turns and equals batch on the pure ladder (v1 edge floors excepted — see
   A-MAJOR-4). `aggregate` over the running node/edge credits produces coverages.
6. Return new `IncrementalState` (cursor → `len(all_student_turns)`,
   `global_window_count` advanced, `running_edge_evidence` merged, pair_count
   updated) + `IncrementalSnapshot`.

### 5.4 Correctness note vs. batch — conservative monotone lower bound

The incremental snapshot is **NOT** claimed byte-equal to batch. It is a
**conservative monotone lower bound**: for every reference node and edge,
`incremental_credit ≤ batch_credit`, and both are monotone non-decreasing across
turns. "Conservative" is load-bearing for safety — under-crediting keeps a node
in the clarification pool (we might probe something batch thinks is fine: mild
over-interrogation, capped by §10), whereas over-crediting would drop a genuinely
weak node from the pool (missed remediation). The design forces every divergence
into the under-crediting direction. Exact equality holds only on the
**pure-ladder monotone fixture** (no gray-zone upgrade, no v1 edge floors, no
budget truncation) — that is the equality T3 asserts.

The four enumerated, intentional divergences (all conservative):

1. **Nodes:** incremental max over (prior ∪ new windows) == batch max over all
   windows (max is associative; windows append-only). Frozen resolved/seeded
   nodes cannot lower a max. → **exact** for nodes.
2. **Edges (A-MAJOR-1):** monotone via `running_edge_evidence`; equals batch on
   the NLI/co-occurrence/endpoints tiers.
3. **v1 edge floors (A-MAJOR-4):** omitted incrementally (empty triples) → an
   edge batch would floor stays lower incrementally → **under-credit**.
4. **Gray-zone (A-MAJOR-3):** if `grayzone_fn=None` on the hot path while batch
   runs it, a gray node stays 0.3 vs batch ≤0.7 → **under-credit**. (When both
   paths share the same `grayzone_fn`, no divergence.)
5. **Budget truncation:** per-turn `max_nli_pairs` may skip low-priority pairs
   (batch has its own 200 cap too) → **under-credit**.

This is why grading still uses batch — incremental is a **selection-time
approximation**, exact on the pure monotone case and conservatively low
otherwise. The optional Done diff-trace (§7) and the T14 integration assertion
(L2 fix) enforce `incremental_cov ≤ batch_cov + ε` so a *non-conservative*
divergence (over-credit) is caught as a test failure, not merely observed.

### 5.5 Latency budget (M1 + H3)

- **Selection NEVER blocks on the current turn's incremental job.** The reply
  path reads only the **most-recent completed** snapshot (produced by a prior
  turn's job on this worker). There is no `asyncio.wait_for` on the current
  turn's job in the reply path, so the ≤1500 ms block the earlier draft added on
  top of `draft_reply` is **removed** — the teaching reply cannot be delayed by
  V2 at all. (`incremental_deadline_ms` is repurposed to a watchdog for the
  background job only; it does not gate the reply.)
- **One in-flight job per attempt.** The incremental step runs in
  `asyncio.to_thread` (CPU-bound NLI), started right after
  `resolve_pending_clarifications` **only if no prior job for this attempt is
  still running**. This bounds thread-pool contention and the single NLI model's
  contention, and — since `asyncio` cannot cancel an OS thread — avoids the
  accumulation of timed-out-but-still-running jobs the earlier draft risked. A
  still-running job simply means this turn kicks nothing; the running job's
  result becomes available to a later turn.
- **Exceptions are contained (H3).** The background job body is wrapped in
  try/except; on any exception it records "no completed snapshot this turn" and
  the exception never escapes into the awaited reply path (there is no such await
  — see first bullet). Selection then uses the prior snapshot or v1.
- If NO completed snapshot exists yet (turn 1, or a cold worker — §5.1) → v1
  selection for that turn.
- Model singleton (`nli_provider.get_adjudicator`) is process-lived; no per-turn
  load cost.

---

## 6. Clarification answers → V2 feedback

**Confirmation is content-verified — precondition for seeding (B-HIGH-2).**
Seeding is triggered **only** by a `confirmed` outcome from
`resolve_pending_clarifications`, and `confirmed` is **not** a student
self-report. It is produced by `default_clarification_judge`
(`clarification/rescorer.py`), an LLM that judges whether the student's
*committed clarification text* correctly expresses the target idea
("confirmed = the clarification correctly expresses the target idea … Judge
meaning, not wording"); a wrong claim → `refuted`, noncommittal → `vague`, and a
judge failure leaves the row `asked_waiting` (never credit on failure). So the
"student says yes → 0.90 in the grade" leak does not apply: the content is
adjudicated. **This is a hard precondition** — if a future change let
`confirmed` be reached by bare affirmation without content adjudication, seeding
MUST be disabled, because (a) selection seeds the node to **1.0** and (b) the VoI
ranker deliberately steers confirmations at the **highest-composite-gain hub
nodes**, so any weakness in confirmation adjudication is amplified at exactly the
nodes with maximum grade blast radius. The build MUST assert the seed path is
gated on `outcome == "confirmed"` and add a comment pinning this precondition to
`rescorer.py`'s judge. Residual risk (stated, not a blocker): credit correctness
is bounded by that judge's quality; VoI hub-targeting concentrates exposure on
it.

**A clarification-confirmed node gets its V2 running view credited.** When
`resolve_pending_clarifications` records a content-verified `confirmed` outcome
on the next turn (node_id → candidate_key, method `"clarification"`, v1
confidence 0.90):

- Seed the V2 incremental state: `running_node_max[candidate_key] = 1.0`,
  `node_source[candidate_key] = "clarification"`, add to `seeded_keys`.
- Rationale for **1.0 in the running max, not 0.90**: the running max is the
  V2 *fused-score* space feeding the credit tiers; a clarification confirmation
  is an authoritative resolution, so it should freeze the node at full credit
  and stop further NLI spend on it. The *grading* credit is unaffected — Done
  grading applies the 0.90 clarification method through the v1 resolver as
  today; the batch V2 grade path likewise gets the confirmation through
  `v1_inputs_from_canonical` (clarification-resolved keys arrive as v1 floors).
  So: **selection-space = 1.0 (freeze + hub edge lift); grading-space = 0.90 via
  existing v1 path.** Document this two-space distinction in the trace.
- `refuted` → do NOT seed; leave the node in the unresolved pool (it may be
  re-scored but its topic key is already asked → dedup prevents re-probe). The
  refutation remains misconception evidence via the existing v1 path.
- `vague` → terminal no-credit; topic key stays asked (no re-probe).

**Seeding freezes the NODE only; it does NOT auto-resolve neighbor nodes
(M5 fix).** Raising `running_node_max[hub]=1.0` does raise the *credit* of
edges incident to the hub (edge credit = `r·sqrt(c_hub·c_v)`), and that is fine
— it feeds VoI *ranking* and the snapshot's edge coverage. But the **node
pull-up floor** (edges.py's `edge_pullup_floor` applied to the *other* endpoint)
MUST NOT fire as a consequence of seeding: in batch, pull-up only fires on real
ENTAIL evidence earned from actual student windows. Seeding creates no window
evidence, so the neighbor node `v` must NOT be lifted out of the gray pool by a
hub confirmation it never actually taught. Concretely, the incremental scorer
applies pull-up floors **only** from `relation_evidence == "entail"` earned this
attempt, never from a seed. This keeps a genuinely-weak neighbor edge/node in the
pool (so it can still be probed) even after an adjacent hub is confirmed —
preventing the "selection believes an edge is fine, student is still scored low
at Done, and never got the remediating question" harm. Confirming a hub still
answers the hub's own edges (their credit rises), consistent with the VoI model
that ranked it — it just does not silently mark the hub's *neighbors* resolved.

---

## 7. Observability / tracing

Extend the existing clarification trace (do NOT invent a new sink). Add, under
the chat-turn clarification log and — when `APOLLO_RESOLVER_V2_TRACE_DIR` is set
— appended to the per-attempt V2 dump:

- `clarification_v2.enabled` (bool), `clarification_v2.snapshot_source`
  (`"this_turn" | "prior_turn" | "none_v1_fallback"`).
- `clarification_v2.pool` — list of `{canonical_key, node_type, node_credit,
  is_gray, source}`.
- `clarification_v2.ranked` — top-N `{canonical_key, importance, uncertainty,
  voi}` (N = `params.trace_top_n`, default 10).
- `clarification_v2.questions` — packed structure `[{question_index,
  topic_keys:[...], hint_dims:[...]}]` where `hint_dims` are the hint **dimension
  types** (e.g. `"direction" | "variable" | "relationship"`) NOT the rendered
  hint strings (L1 fix). The trace persists to disk/logs, so it must never record
  the phrased hint text that `build_probe_hint` produces — only topic keys +
  dimension type. A leak-guard assertion in T13 checks no candidate-derived or
  rendered-hint text appears anywhere in the serialized trace.
- `clarification_v2.asked_dedup_skipped` — candidate keys filtered by dedup.
- `clarification_v2.budget` — `{pair_count_this_turn, pair_count_total,
  budget_truncated}`.
- `clarification_v2.seeded` — keys pinned from confirmations this attempt.
- On fallback: single structured log `clarification_v2_ranker_failed_falling_
  back_to_v1` with exception class + attempt_id (no transcript text).

Trace must be JSON-safe (`json.dumps` round-trip), matching the resolver_v2
convention. All new dataclasses frozen; `.trace()`/`asdict` serializable.

Optional (default off, `APOLLO_CLARIFICATION_V2_DIFF_TRACE`): at Done, log
`{incremental_node_cov, batch_node_cov, max_abs_node_delta}` to validate the
monotone approximation against batch. Observability only — never alters grade.

---

## 8. Flags, config, behavior matrix

### 8.1 New config (`apollo/clarification/v2_config.py`, mirrors resolver_v2/config idioms)

- **`APOLLO_CLARIFICATION_V2_RANKER`** — master flag for this feature. Default
  OFF. Truthy = `1|true|yes`, read **fresh per call** (no caching), malformed →
  OFF. `clarification_v2_ranker_enabled() -> bool`.
- Frozen `ClarificationV2Params` + `load_clarification_v2_params()` with
  `APOLLO_CLARIFICATION_V2_<FIELD>` env overrides (malformed → default), fields:
  - `max_questions=3`, `max_topics_per_question=3`
  - `max_questions_per_attempt=12` (M4 cumulative cap, §10)
  - `voi_target_credit=1.0`
  - `p_missing=0.6`, `p_near_resolved=0.2`, `p_gray_min=0.3`, `p_gray_max=0.8`,
    `p_equation_floor=0.7`
  - `incremental_deadline_ms=1500`
  - `trace_top_n=10`
- **Gray band source.** The trigger band `[t_low, t_mid)` is read from the
  active `ResolverV2Params` (`load_params()`), so trigger tracks grading
  calibration automatically. **Discrepancy to resolve at build time:** the
  resolver_v2 recon reports shipped code defaults `t_low=0.30, t_mid=0.70,
  t_high=0.90` (`config.py:94-96`), while the design-doc/locked-decision text
  says `t_low=0.40, t_mid=0.75`. **Decision: the ranker does NOT hardcode
  either — it consumes `params.t_low/t_mid/t_high` verbatim from
  `load_params()`.** Whatever the calibrated code default is at build time is
  what the trigger uses; the locked 0.40/0.75 numbers are advisory and, if the
  team wants them, are set via `APOLLO_RESOLVER_V2_T_LOW/_T_MID` env, not by
  forking constants here. The build MUST NOT change `resolver_v2/config.py`
  defaults (that would perturb the shadow grade). Flag this line in the build
  handoff.
  - **Calibration pin (M3).** The VoI uncertainty band and the `p_*` constants
    are heuristic and were reasoned against a *specific* gray band. Because the
    §10 composite gate is under active recalibration (project state 2026-07-07),
    `load_params()` may return a *different* band at flag-ON time than the one
    the ranker was validated against — silently shifting the pool. Therefore the
    §11 pre-flag-ON gate MUST **record the exact `t_low/t_mid/t_high` and all
    `p_*`/`voi_*` values the offline selection-quality comparison was run
    against**, and flag-ON is only valid while the live `load_params()` band
    matches that recorded band (or the comparison is re-run). This does not fork
    constants — it pins the *validation context* so a calibration drift can't
    quietly invalidate the ranker.

### 8.2 Behavior matrix

| `APOLLO_CLARIFICATION_ENABLED` | `APOLLO_RESOLVER_V2` | `APOLLO_CLARIFICATION_V2_RANKER` | Behavior |
|:---:|:---:|:---:|---|
| OFF | any | any | No clarification at all. Byte-identical baseline. |
| ON | any | OFF | v1 clarification selection (find_residual → detect_ambiguous → select_probes). **Current live behavior.** **H1:** `chat.py` starts NO background incremental job, mutates no session state, spends no NLI budget — this includes the `(ENABLED=ON, RESOLVER_V2=ON, RANKER=OFF)` sub-case, which is a strict byte-identical no-op for this feature (asserted by the T15 golden in that exact row). |
| ON | OFF | ON | V2 ranker requires V2 scores. `APOLLO_RESOLVER_V2` OFF ⇒ no snapshot is produced ⇒ **fall back to v1 selection**, log `clarification_v2_no_resolver_v2`. (We do NOT force-enable V2 scoring from the clarification flag — keep flags orthogonal and V2 opt-in.) |
| ON | ON | ON | **New behavior.** Per-turn incremental V2 → VoI rank → 3×3 packing → candidate-key dedup. Fail-open to v1 on any error/timeout/empty-snapshot. |

Key rule: `APOLLO_CLARIFICATION_V2_RANKER` is a **no-op unless BOTH**
`APOLLO_CLARIFICATION_ENABLED` and `APOLLO_RESOLVER_V2` are ON. Gating check
sits at the top of the V2 branch in `run_clarification_detection` and re-reads
all three flags fresh.

### 8.3 Fail-open contract

**Two independent guards** (H3 — selection AND the background job):

1. **Selection branch** — every entry is wrapped:
```python
try:
    return v2_selection.select(snapshot, ...)
except Exception:
    log.warning("clarification_v2_ranker_failed_falling_back_to_v1", ...)
    return _v1_select(...)          # the existing pipeline, unchanged
```
2. **Background incremental job** — the `to_thread` body is wrapped so an
   exception NEVER escapes into the reply path (there is no await on the current
   turn's job — §5.5). It is recorded as "no completed snapshot this turn"; state
   is left at its last good value:
```python
async def _incremental_bg(...):
    try:
        new_state, snap = await asyncio.to_thread(score_turn, ...)
        _store_completed(attempt_id, new_state, snap)   # single-writer, monotone-guarded
    except Exception:
        log.warning("clarification_v2_incremental_failed", ...)  # snapshot stays as-is → v1 next
```
Empty V2 pool (nothing gray/missing) → return no probes (valid outcome, not an
error). Missing/failed snapshot → v1 fallback. Both guards nest inside the
existing `db.begin_nested()` savepoint + catch-all in `chat.py`, so even a
fallback failure cannot block teaching.

---

## 9. Migration

**None.** Reuse migration 033 (`apollo_clarifications`). The topic-key dedup
uses the existing `candidate_key` column via a new SELECT
(`load_asked_candidate_keys`) — no DDL. The `UNIQUE (attempt_id, node_id)`
constraint stays; we add an application-level dedup on `candidate_key` (query
distinct asked candidate keys, subtract from the V2 pool before packing). If a
future hard DB guarantee on `(attempt_id, candidate_key)` is wanted, that is a
separate migration and is a NON-GOAL here.

---

## 10. Question packing (3×3) + answer-blind + asked-key dedup

### 10.1 Packing (`v2_ranker.pack_questions`)

- Input: VoI-ranked candidates (desc).
- Remove candidates whose `candidate_key ∈ load_asked_candidate_keys` (dedup).
- Greedily fill up to `max_questions=3` questions, each holding up to
  `max_topics_per_question=3` topic keys. Grouping heuristic: co-locate topics
  that share an incident edge or `best_window_index` proximity (so one Apollo
  question can naturally touch related ideas) — but grouping is cosmetic;
  correctness only requires ≤3×≤3 and no dupes. Default: fill question 1 with
  the top 3, question 2 with next 3, question 3 with next 3 (simple chunking) if
  no edge-affinity grouping is implemented in v1 of this build.
- Output: `list[PackedQuestion]` where `PackedQuestion.topic_keys: tuple[str,...]`
  (≤3). Total topics per turn ≤ 9.

**Per-attempt cumulative cap (M4).** The 3×3 bound is per-*turn*; without a
cumulative cap a 10-turn attempt could interrogate ~30 distinct topics
(over-interrogation UX harm, amplified by any pool over-breadth). Add
`params.max_questions_per_attempt` (default 12). Before packing,
`v2_selection` counts topics already asked this attempt via
`len(load_asked_candidate_keys(db, attempt_id))`; the number of NEW topics it may
pack is `max(0, max_questions_per_attempt - already_asked)`. When the cap is
reached, selection returns no new probes for the rest of the attempt (valid
outcome; logged as `clarification_v2_attempt_cap_reached`). The cap counts
distinct reference topic keys, so dedup and the cap share the same counter.

### 10.2 Answer-blind preservation

- For each topic key, produce a hint via the **existing**
  `build_probe_hint(node_type, candidate)` — which by construction names only
  the *dimension* (direction/variable/relationship), never the concept. The
  candidate arg is never rendered. For equation nodes the hint is the designed
  "commit to / write out the relationship" steering (do NOT name the equation).
- Hints flow through the **existing** `draft_reply(clarification_hints=...)`
  and **existing** `guard_clarification_reply` leak backstop unchanged. We add
  no new LLM prompt surface. The only new thing crossing to the LLM is *which*
  hints (chosen by VoI) — the phrasing/leak machinery is identical, so
  answer-blindness is inherited, not re-implemented.
- `probe_question` continues to persist as `""` (DB never stores phrased text).

### 10.3 Asked-key dedup (closing the v1 gap)

- v1 dedups on **student `node_id`** (`UNIQUE(attempt_id, node_id)`), so the
  same *reference topic* can be re-probed via a different student node.
- V2 selection is **reference-node-centric**, so the natural key is the
  reference/`candidate_key`. `load_asked_candidate_keys` returns all
  candidate_keys ever asked this attempt; `pack_questions` filters them out
  BEFORE packing. On write, `write_asked_waiting` records the row with its
  `candidate_key` (already a column), and — because V2's node_id may differ —
  the build must ensure the write carries a stable node_id for the reference
  node (use the reference canonical key as the node identity in the V2 path, or
  the mapped student node when one exists). Net effect: **a reference topic is
  asked at most once per attempt.**

---

## 11. Validation strategy (replay can't exercise this)

The replay corpus passes `clarification_trace=[]`, so it CANNOT exercise the
clarification loop (confirmed verbatim in the deflation handoff lane B). Do NOT
claim replay coverage. Validate via:

1. **Deterministic unit/smoke tests** (FakeNLI + injected `SelectFn`/callables,
   no network, no model load at import) — the resolver_v2 convention. Cover:
   incremental == batch on the monotone case; VoI ranking order on a hand-built
   snapshot; 3×3 packing bounds; candidate-key dedup; fail-open; flag matrix;
   seeding at 1.0.
2. **Scripted multi-turn integration test** with a `FakeNLIAdjudicator` and a
   fake `draft_reply`/judge: 3-turn attempt where turn 1 leaves an equation node
   gray, Apollo asks "write out the relationship", turn 2 confirms → node freezes
   at 1.0, incident edge (own) lifts, node not re-asked, and a neighbor node NOT
   auto-resolved by the seed (M5). Assert trace fields. **Also assert the
   conservative bound `incremental_cov ≤ batch_cov + ε` (L2 fix)** — the
   equivalence check is a test assertion here, not merely the optional runtime
   diff flag, so a non-conservative (over-credit) divergence fails CI.
3. **Offline selection-quality gate — REQUIRED before flag-ON (M3).** A labeled
   fixture of multi-turn transcripts with human/oracle "which reference nodes
   were genuinely weak" labels. Compare, on the SAME snapshots: VoI ranking vs
   the v1 `rubric_weight_for` ranking it replaces, on precision@3 /
   questions-spent-on-genuinely-weak-nodes. Flag-ON is gated on VoI ≥ v1 on this
   metric; otherwise the feature adds latency/concurrency/credit-leak surface for
   an unproven ranking benefit. Record the exact param band the comparison ran
   against (§8.1 calibration pin). This is task T16.
4. **Live/staging manual smoke** with flags ON on the test Supabase project
   (human/CI step, per repo rules — agents never flip prod flags). Also
   **measure the completed-snapshot availability rate** across turns (the
   multi-worker cold-state caveat, §5.1) — if the ranker rarely has a snapshot in
   production, its live value is limited regardless of offline quality. The
   empirical basis that clarification questions flip nodes (FINAL-VERDICT §6.3,
   5/7 flips followed a clarification question) is the expected signal to
   reproduce.
5. Byte-identity golden: flag OFF → chat replies + artifacts unchanged, **and
   the `(RESOLVER_V2=ON, RANKER=OFF)` row specifically** (H1) — no background job,
   no session mutation, no budget spend.

Caveat inherited from FINAL-VERDICT §6.3 / blocker 3: personas teach omitted
beats when asked, so a live A/B will *look* strongly positive partly because
role-players comply — interpret live wins with that bias in mind. Not a blocker
for shipping the flag OFF.

---

## 12. Task-by-task build plan

Each task is independently testable, TDD (RED→GREEN), smoke-test convention,
frozen dataclasses, files <400 lines, CPU work in `asyncio.to_thread`. The flag
ships default-OFF and stays OFF through the whole build; turning it ON in ANY
environment is additionally gated on the T16 offline efficacy gate passing and
the §8.1 calibration band being recorded/matched.

| # | Task | Deliverable | Test expectation |
|---|---|---|---|
| **T1** | Config + flag | `apollo/clarification/v2_config.py`: `clarification_v2_ranker_enabled()`, `ClarificationV2Params`, `load_clarification_v2_params()`. | Fresh-read per call; truthy parsing; malformed env → default; default OFF. |
| **T2** | Incremental types | `apollo/resolver_v2/incremental_types.py`: `IncrementalState` (incl. `global_window_count`, `running_edge_evidence`), `IncrementalSnapshot` (frozen, JSON-safe). | Construct/asdict/`json.dumps` round-trip; immutability (new instance per update). |
| **T3** | Incremental scorer | `apollo/resolver_v2/incremental.py`: `score_turn(...)` composing build_windows(suffix)+**global-space reindex adapter**+score_nodes(subset)+**apply_grayzone stage**+score_edges+aggregate; monotone node merge; **monotone edge-evidence merge** (`running_edge_evidence`); budget guard; seed override; empty v1 edge triples (conservative). | **Incremental==batch on the pure-ladder monotone fixture** (no grayzone, no v1 edge floors); **conservative `incremental_cov ≤ batch_cov`** on the general fixture (A-MAJOR-1/3/4); global window `index`/`turn_index` continue across turns (A-MAJOR-2); edge coverage monotone non-decreasing; unresolved-only rescoring (resolved node spends 0 pairs); budget truncation sets `budget_truncated`; overlap seam correct. |
| **T4** | Seeding path | `score_turn` honors `seeded_keys`→1.0 + source `"clarification"`; helper `seed(state, keys)->state`. | Seeded node frozen at 1.0, edges lift on next snapshot, no pairs spent on it. |
| **T5** | VoI ranker | `apollo/clarification/v2_ranker.py`: `VoICandidate`, `VoIScore`, `rank_by_voi` using `composite.load_weights()`; **`edge_gain(edge_score, c_u', c_v, params)`** mirroring edges.py final-credit with the complete 6-entry `relation_evidence`→r map + endpoints-tier promotion + v1-floor clamp (A-MAJOR-5). | Hub with 3 incident credited edges outranks isolated equal-credit node; **v1_explicit edge gives gain 0, v1_inferred/none edges are NOT silently r=0** (all 6 evidence values mapped); a gray hub resolving to ≥0.7 unlocks endpoints-tier gain on incident edges; equation_cap node gets `p_equation_floor`; deterministic tie-break; importance uses live w_n/w_e. |
| **T6** | Candidate extraction | `v2_gray_candidates(snapshot, params)` — pool = gray∪missing ref nodes (source∈{nli,lexical_skip,equation_cap} & is_gray, plus credit==0). | Correct pool from a fixture snapshot; excludes t_high nodes and misconceptions. |
| **T7** | Packing + per-attempt cap | `pack_questions(ranked, max_q, max_topics, remaining_budget)` → ≤3×≤3 `PackedQuestion`, honoring the M4 cumulative `max_questions_per_attempt`. | Bounds enforced (≥9 candidates → exactly 3×3); <9 packs fewer; topic keys unique; order by VoI; **cumulative cap: once `max_questions_per_attempt` topics asked, packs 0 new**. |
| **T8** | Dedup store query | `store.load_asked_candidate_keys(db, attempt_id)`; ensure `write_asked_waiting` carries reference candidate_key + stable node identity. | Returns distinct asked candidate keys; `on_conflict_do_nothing` idempotent; a topic asked once never re-appears in pool; **count drives both dedup and the M4 attempt cap**. |
| **T9** | V2 selection pipeline | `apollo/clarification/v2_selection.py`: pool→dedup→rank→pack→hints→write, returning v1-shaped probe result. | End-to-end on fake snapshot; equals expected probes; produces existing `build_probe_hint` outputs; answer-blind (no candidate text in hints). |
| **T10** | turn.py branch + fail-open | `run_clarification_detection` gains defaulted keyword params (`snapshot`, `reference_graph`, `problem_payload`, `resolved_candidate_keys`); branches on `_v2_ranker_active(...)` (all 3 flags) AND snapshot present; try/except → v1 fallback; empty-pool → no probes; missing snapshot → v1. Return type stays `list[str]`. | Flag matrix (all 4 rows §8.2); exception in V2 path → v1 result + warning log; empty/missing snapshot → v1; OFF → byte-identical v1; existing v1 callers (no new kwargs) unchanged. |
| **T11** | chat.py incremental wiring | **Gate the whole kick on `_v2_ranker_active(...)` (H1)**; enforce **one in-flight job per attempt** (M1/M2); run `score_turn` in `asyncio.to_thread` wrapped so exceptions can't escape (H3); persist `IncrementalState`+completed snapshot single-writer/monotone-guarded; pass the **most-recent completed** snapshot to detection (never block the reply — §5.5). | Gate OFF (incl. RESOLVER_V2 ON, RANKER OFF) → no thread, no session mutation, no budget spend (H1); reply never awaits current-turn job; second concurrent turn does not start a 2nd job; background exception → `snapshot_source` stays prior/`none`, reply unaffected (H3); cold/turn-1 → v1; cursor advances across turns on a warm worker. |
| **T12** | Feedback seeding wiring | After `resolve_pending_clarifications`, seed keys **only on content-verified `outcome=="confirmed"`** (B-HIGH-2) into `IncrementalState` (1.0/selection-space); refuted/vague not seeded; **seed freezes the node only — no neighbor pull-up (M5)**. | Confirmed next-turn → node frozen 1.0 in snapshot, its own edges lift, not re-asked; **a gray neighbor is NOT auto-resolved by the seed** (stays in pool); refuted stays in pool but topic-deduped; grading credit still 0.90 via v1 path; assert seed path guarded on `"confirmed"`. |
| **T13** | Trace + observability | Emit §7 trace fields; JSON-safe; fallback/no-v2/budget/attempt-cap logs; optional Done diff trace behind `APOLLO_CLARIFICATION_V2_DIFF_TRACE`. | Trace round-trips `json.dumps`; contains pool/ranked/questions(**topic keys + `hint_dims` only, L1**)/dedup/budget/seeded; **leak-guard assertion: no rendered-hint or candidate text in the serialized trace**; fallback log fires. |
| **T14** | Scripted multi-turn integration test | 3-turn attempt (equation gray → asked → confirmed → frozen); FakeNLI + fake draft/judge. | Reproduces the flip; asserts no re-ask, own-edge lift, **neighbor NOT auto-resolved (M5)**, trace; **asserts conservative bound `incremental_cov ≤ batch_cov + ε` (L2)**; no network/model. |
| **T15** | Flag-OFF byte-identity golden + docs | Golden asserts OFF chat reply + artifact unchanged, **including the `(RESOLVER_V2=ON, RANKER=OFF)` row (H1)**; update `docs/architecture/apollo.md` Resolver V2 + clarification subsections (drift contract) with the new flag, matrix, two-space credit note, conservative-bound note, `last_verified` bump. | Both OFF golden rows pass; apollo.md `owns` frontmatter/globs updated; matrix + non-goals recorded. |
| **T16** | Offline selection-quality + calibration gate (M3) | Labeled multi-turn fixture; harness comparing VoI vs v1 `rubric_weight_for` on precision@3 / questions-on-genuinely-weak-nodes; records the exact `t_*`/`p_*`/`voi_*` param band used (§8.1 pin). **Pre-flag-ON gate, not a runtime dependency.** | VoI ≥ v1 on the metric (else flag-ON blocked); param band recorded; deterministic (fixtures, no network). |

Dependencies: T1–T2 first; T3←T2; T4←T3; T5–T7 parallel after T2; T8 independent;
T9←(T5,T6,T7,T8); T10←T9; T11←T3; T12←(T4,T11); T13←T10–T12; T14←all; T15←T14;
T16←(T5,T9) — the efficacy gate needs the ranker + selection but is otherwise
independent; it must PASS before the flag is turned ON in any environment.

---

## 13. Repo-convention checklist (must hold at PR)

- Immutability: every state update returns a new frozen dataclass.
- Files <400 lines; split if approaching.
- No env reads or DB inside `resolver_v2/` pure functions; flags read fresh in
  `*_config.py` at the boundary.
- No `resolver_v2 → clarification` import (one-way dependency preserved).
- CPU-bound NLI in `asyncio.to_thread`; per-turn respects `max_nli_pairs=200`;
  one in-flight incremental job per attempt (H3/M1/M2); reply never awaits the
  current turn's V2 job (H3).
- **Every** new side effect (incl. the chat.py incremental kick) gated behind
  all three flags via one shared `_v2_ranker_active(...)` predicate (H1).
- Incremental snapshot is a conservative monotone lower bound on batch; edge
  coverage monotone via `running_edge_evidence`; windows reindexed to the global
  space; gray-zone stage never over-credits vs batch (A-MAJOR-1/2/3/4/5).
- Seeding only on content-verified `confirmed`; seed freezes the node, never
  pulls up neighbors (B-HIGH-2 / M5).
- Trace stores topic keys + hint-dimension types only, no rendered hint/candidate
  text (L1); JSON-safe; artifact/grade byte-identical when flag OFF (incl. the
  RESOLVER_V2-ON/RANKER-OFF row).
- Drift contract: `apollo.md` reconciled in the same PR (T15).
- No new secrets; no new parse/eval surface (RCE non-goal untouched).
- Flag-ON gated on T16 offline efficacy gate + recorded calibration band (M3).
- Patch coverage: smoke-test convention now; 95% patch gate is the PR-time CI
  step (not relaxed — just enforced at PR, per the OFF-by-default rollout).
