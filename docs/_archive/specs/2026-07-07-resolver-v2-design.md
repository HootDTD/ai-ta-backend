# Resolver V2 — inverted-loop node/edge recovery (design + task cards)

- **Date:** 2026-07-07 · **Status:** APPROVED FOR BUILD (tonight, parallel agents)
- **Branch:** `feat/apollo-resolver-v2` (cut from `be78660`; do NOT rebase onto ApolloV3)
- **Flag:** `APOLLO_RESOLVER_V2` (default OFF — flag-OFF must be byte-identical)
- **Research basis:** `docs/_archive/research/2026-07-07-node-resolution-prior-art/FINDINGS.md`
  (moves R1 invert-the-loop, R2 multi-view, R3 joint graded node+edge, R4 recall-first
  calibration, R9 graded credit, §5 grounded gray-zone). Memos are the drill-down.
- **Test bar TONIGHT:** smoke tests per module (happy path + one edge case, FakeNLI-injected,
  CI-safe). The repo's 95% patch gate is EXPLICITLY DEFERRED until the hypothesis is confirmed
  on the F1c replay. Say so in the PR description.

---

## 1. Problem (measured, frozen F1c corpus, 31 gradeable attempts)

The v1 resolver maps **parser-extracted student nodes → candidates** through precision-first
tiers (exact/symbolic/derived/alias/fuzzy≥0.9/NLI@0.90-entailment). Result: strong personas
that provably teach 5/5 reference nodes resolve 1/5 (node_cov 0.20); edge_coverage max 0.25
(19/31 zero — it needs BOTH endpoints resolved AND a parsed edge, so it compounds
quadratically); band agreement vs the LLM grader 4/31; strong-class mean composite (0.332)
indistinguishable from misconception-class (0.334). The parser is the recall ceiling and the
thresholds are the documented over-abstention trap (FINDINGS §1, §3-R4).

## 2. Design overview — the five moves

V2 does not touch the v1 resolver. It is a **parallel scoring engine** that answers, per
reference node: *"is this node's content entailed anywhere in the student's transcript?"*

1. **Invert the loop (R1):** for EACH reference node, score its hypothesis views against ALL
   student-turn windows; node score = MAX over (window × view). The parser no longer gates
   recall; a node taught across three turns resolves.
2. **Multi-view (R2):** 3–5 affirmative paraphrase views per node (offline LLM-generated,
   committed JSON cache; `content.label` is always view 0). Credit = max over views.
3. **Joint node+edge with graded credit (R3/R9):** edge credit is a *graded* function of
   endpoint credits + relation evidence (verbalized-edge NLI, window co-occurrence,
   endpoints-anywhere) — no binary both-endpoints gate. A direct edge entailment *pulls up*
   its endpoint node credits (ILP-Smatch spirit at our 5–20-node scale, no ILP needed).
4. **Recall-first calibration (R4):** thresholds tuned on the F1c gold expected ledgers
   (`campaign/out/f1c/attempts.jsonl` `expected.{credited,unresolved,misconceptions}` per
   persona) — maximize node recall subject to a false-credit ceiling on control negatives.
5. **Grounded gray-zone check (§5):** nodes scoring in the gray band get ONE bounded LLM
   check per attempt that must quote a verbatim span verified present in the transcript
   (fuzzy-contains). Capped credit 0.7. Candidate generator, NEVER an unconditional score
   source. Disableable (`APOLLO_RESOLVER_V2_GRAYZONE=0`, the deterministic-replay default).

**V1 floor invariant:** a node/edge v1 already credited keeps its v1 credit (node resolved in
S_norm → credit 1.0; explicit edge triple match → 1.0, inferred → 0.5). V2 node/edge coverage
is therefore ≥ v1's by construction — pure recall recovery; false-credit risk lives only in
the NEW credit, which is what calibration bounds.

**Scope guards:** misconception detection/penalty, findings, ledger, audit, abstention inputs
other than the substituted scores — ALL stay v1. V2 substitutes exactly three numbers in
`GradeResult` (`coverage_score`, `node_coverage_score`, `edge_coverage_score`) so the
existing composite (`w_n=0.706, w_e=0.294, p=0.15`), §10 composite gate, banding
(0.85/0.70/0.50), artifact, and replay metrics are apples-to-apples.

## 3. Module map

All new code in `apollo/resolver_v2/` (each file < 400 lines):

| File | Role | Task |
|---|---|---|
| `apollo/resolver_v2/__init__.py` | re-export `resolver_v2_enabled`, `run_resolver_v2` (lazy) | T1 |
| `apollo/resolver_v2/config.py` | flags + frozen `ResolverV2Params` + env overrides | T1 |
| `apollo/resolver_v2/types.py` | frozen dataclasses (the interlock contract, §5.0) | T1 |
| `apollo/resolver_v2/windows.py` | student turns → sentence-group windows | T3 |
| `apollo/resolver_v2/prefilter.py` | lexical scorer + deterministic top-K window selection | T3 |
| `apollo/resolver_v2/views.py` | committed view-cache loader + `RefNode` builder | T4 |
| `apollo/resolver_v2/nli_provider.py` | V2-owned lazy NLI singleton (deberta-v3-large, CPU) | T4 |
| `apollo/resolver_v2/scoring.py` | per-node max-aggregation, fusion, credit `g(s)` | T4 |
| `apollo/resolver_v2/edges.py` | verbalized-edge NLI, graded edge credit, node pull-up | T5 |
| `apollo/resolver_v2/aggregate.py` | winning-path node coverage + edge coverage | T5 |
| `apollo/resolver_v2/grayzone.py` | grounded gray-zone LLM check + quote verification | T6 |
| `apollo/resolver_v2/engine.py` | orchestrates windows→score→grayzone→edges→aggregate | T7 |
| `apollo/resolver_v2/integration.py` | DB turn loader, `GradeResult` substitution, trace dump | T7 |
| `apollo/resolver_v2/views/views_cache.json` | committed LLM-generated views (offline) | T2 |
| `scripts/generate_resolver_v2_views.py` | offline view generator (ONE-time LLM) | T2 |
| `scripts/resolver_v2_calibrate.py` | DB-free calibration sweep on gold ledgers | T8 |

**Existing files touched (the ONLY ones):**
- `apollo/handlers/done_grading.py` — ~12-line hook after step 8 + one new defaulted
  `ShadowGradeResult` field (`resolver_v2_trace: dict | None = None`).
- `apollo/grading/artifact_build.py` — ~5 lines: nest `scores.resolver_v2` summary when the
  trace is present (flag-OFF artifact byte-identical).
- `docs/architecture/apollo.md` — owner-doc reconciliation (T7, same commit).
- `campaign/replay.py` — **ZERO changes.** The flag flows through `run_graph_simulation`.

**Never touch:** `campaign/out/f1c/**` (frozen), `apollo/subjects/**/problems/*.json`
(problem_02–05 fluid JSONs carry pre-existing uncommitted diffs — views live in the separate
cache file, NOT in problem files), ApolloV3, remote Supabase.

## 4. Data flow (flag ON)

```
done click / replay attempt
  └─ run_graph_simulation (done_grading.py)
       steps 1–7 unchanged (candidates, validate, v1 resolve, RESOLVES_TO, canonicalize)
       step 8   grade = grade_attempt(...)            # v1 scores, v1 findings
       step 8b  [APOLLO_RESOLVER_V2=1]
                turns  = load_student_turns(db, attempt_id)        # role == "student"
                grade, trace = apply_resolver_v2(grade, turns, student_canonical,
                                                 reference_graph, problem_payload)
                # inside (run in asyncio.to_thread — CPU-bound):
                #   windows   = build_windows(turns)
                #   ref_nodes = build_ref_nodes(reference_graph, problem_payload, views)
                #   nodes     = score_nodes(windows, ref_nodes, nli, v1_keys, select_fn)
                #   nodes     = apply_grayzone(nodes, transcript, fn)   # if GRAYZONE=1
                #   edges, pullups = score_edges(ref_edges, nodes, windows, nli, v1_edges)
                #   node_cov, edge_cov, path_idx = aggregate(reference_graph, nodes, edges)
                #   grade' = replace(grade, coverage_score=node_cov,
                #                    node_coverage_score=node_cov, edge_coverage_score=edge_cov)
       steps 9–13 unchanged — audit, nc, persist, rubric, artifact all read grade'
```

Substitution happens BEFORE `build_audited_grade`, so the §10 composite abstention gate
(`node_coverage=grade.node_coverage_score`) and the artifact composite both read V2 numbers
when the flag is on. Findings stay v1-binary (audit upgrades operate on them unchanged); the
documented consequence is that V2's winning path index may differ from the findings' winning
path — scores-only substitution, recorded in the trace.

## 5. Scoring math

### 5.0 Shared types (`types.py` — verbatim contract, all frozen dataclasses)

```python
@dataclass(frozen=True)
class Window:
    index: int          # 0-based transcript order
    turn_index: int     # source student-turn ordinal
    text: str           # premise text, <= max_window_words

@dataclass(frozen=True)
class RefNode:
    canonical_key: str
    node_type: str              # ontology NodeType string
    label: str                  # problem step content.label (fallback: canonical_key)
    views: tuple[str, ...]      # affirmative views; views[0] == label ALWAYS

@dataclass(frozen=True)
class PairScore:
    window_index: int
    view_index: int
    lexical: float              # [0,1]
    entailment: float           # [0,1]; 0.0 when NLI skipped
    contradiction: float        # [0,1]
    fused: float                # §5.4

@dataclass(frozen=True)
class NodeScore:
    canonical_key: str
    score: float                # max fused over pairs
    credit: float               # g(score) after grayzone + floors, in [0,1]
    source: str                 # "nli"|"lexical_skip"|"v1_floor"|"grayzone"|"edge_pullup"|"zero"
    best: PairScore | None      # argmax pair (None when skipped)

@dataclass(frozen=True)
class EdgeScore:
    edge_type: str              # "USES"|"DEPENDS_ON"|"SCOPES"|"PRECEDES"
    from_key: str
    to_key: str
    credit: float               # [0,1]
    relation_evidence: str      # "entail"|"cooccur"|"endpoints"|"v1_explicit"|"v1_inferred"|"none"

@dataclass(frozen=True)
class ResolverV2Result:
    node_scores: tuple[NodeScore, ...]   # one per distinct ref key (union over paths), key-sorted
    edge_scores: tuple[EdgeScore, ...]   # one per reference edge, in reference order
    node_coverage: float                 # winning-path mean credit
    edge_coverage: float                 # mean edge credit over ALL reference edges
    winning_path_index: int
    grayzone_used: bool
    pair_count: int                      # NLI pairs actually run (budget audit)
    def trace(self) -> dict: ...         # JSON-safe: {"summary": {...}, "nodes": [...], "edges": [...]}
```

Type-selector callable (breaks the T3↔T4/T5 import dependency; tests inject fakes):

```python
SelectFn = Callable[[Sequence[Window], str, int], tuple[tuple[int, float], ...]]
# (windows, view_text, k) -> ((window_index, lexical_score), ...) top-k, ties -> lowest index
```

### 5.1 Windows (`windows.py`)

Student turns ONLY (`role == "student"`, ordered by `turn_index`). Each turn is split into
sentences (deterministic regex on `.!?` + newline; LaTeX-safe: never split inside `\[ \]`
or `$...$`); windows = sliding groups of `window_sentences=3` with
`window_overlap_sentences=1` overlap, hard-capped at `max_window_words=120` (an over-long
sentence becomes its own window, truncated at the cap). Expected volume: ~10 turns →
~30–50 windows per attempt. Deterministic: same turns → same windows.

### 5.2 Views (offline, `views_cache.json` + `views.py`)

Committed single JSON generated ONCE by `scripts/generate_resolver_v2_views.py`:

```json
{ "<concept_id>/<problem_id>": { "<entity_key>": ["view 1", "view 2", "view 3"] } }
```

Per reference step the generator emits **3–4 AFFIRMATIVE paraphrases** (one definition-style,
one application/causal-style, one plain-language restatement; for equations: one spoken form,
e.g. "the product of area and velocity is the same at both sections"). Generation rules
(NLI-memo hard lessons): simple declarative present tense; NO negation, NO litotes, NO
hedges; ≤ 25 words. The generator VALIDATES each view with
`apollo.resolution.polarity.polarity_allows_match(view, view)`-style negation heuristics and
drops/regenerates offenders. Runtime loader: `load_views(concept_id, problem_id)` →
`{entity_key: (views...)}`; a missing problem/key degrades to label-only views (log once,
never raise). `build_ref_nodes(reference_graph, problem_payload, views_by_key)` prepends the
payload step's `content.label` as view 0 and returns one `RefNode` per distinct key on the
union of declared paths. Misconception candidates are NEVER views/RefNodes — V2 scores
reference content only.

### 5.3 Lexical prefilter (`prefilter.py`) — the runtime guard

NO new packages; NO sentence-transformers (not installed); NO small-NLI fallback (only the
large checkpoint is cached under `HF_HUB_OFFLINE=1`). The prefilter is pure lexical:

```
lex(window, view) = 0.5 * token_set_ratio(window, view)/100          # rapidfuzz, already a dep
                  + 0.5 * |content(window) ∩ content(view)| / max(1, |content(view)|)
```

`content(t)` = lowercased tokens, len > 2, stripped of punctuation (mirror
`nli_resolution._content_tokens`). `select_windows(windows, view_text, k=top_k_windows=3)`
returns the top-K by `lex`, ties broken by lowest window index. **Node skip rule:** if
max lex over ALL (window, view) pairs of a node < `lex_floor=0.10`, the node runs NO NLI
(`source="lexical_skip"`, score = that max lex, credit via `g` — effectively 0.0 unless
v1-floored). Deterministic and free.

### 5.4 NLI scoring + fusion (`scoring.py`)

NLI = the existing `TransformersNLIAdjudicator` with `active_nli_model()`
(cross-encoder/nli-deberta-v3-large), CPU, loaded via V2's OWN lazy process singleton
(`nli_provider.get_adjudicator()`) so V2 works even when `APOLLO_NLI_ENABLED=0`.
Premise = window text; hypothesis = view. Per (node, view): score the top-K prefilter
windows. Screens, per pair:

- **Polarity screen:** reuse `apollo.resolution.polarity.polarity_allows_match(window_text,
  view)`; disallowed → pair contributes 0.
- **Contradiction veto:** `P(contradiction) > max_contradiction=0.30` → pair contributes 0
  (recall-first: looser than v1's 0.05 — calibration owns the final value).

Fusion (MENLI-style linear interpolation, lexical as the second signal since no embedder):

```
fused(pair) = alpha * P(entailment) + (1 - alpha) * lex(pair)        # alpha = 0.85 default
score(node) = max over views, over top-K windows of fused(pair)
```

In-attempt memo cache on `(window_index, view_text)` — a view shared across nodes is never
re-scored. Per-attempt hard budget `max_nli_pairs=200`: nodes are scored in
declared-path order; when the budget exhausts, remaining nodes fall back to lexical-only
(`source="lexical_skip"`). `pair_count` is recorded in the trace.

### 5.5 Node credit `g(s)` (graded, R9)

```
g(s) = 1.0                                  if s >= t_high   (0.90)
       0.7                                  if t_mid <= s < t_high   (0.75)
       GRAY                                 if t_low <= s < t_mid    (0.40)
       0.0                                  if s < t_low
GRAY = 0.7 if gray-zone check verifies (§6), else 0.3
     = 0.3 when APOLLO_RESOLVER_V2_GRAYZONE=0 (deterministic default)
```

Floors applied after `g` (max wins, `source` records which):
- **v1 floor:** `canonical_key ∈ v1_resolved_keys` (S_norm non-misconception keys) →
  credit = 1.0. Guarantees V2 ≥ V1 and keeps equation strength (v1 exact/symbolic/derived
  already handle equations well; NLI on symbolic text is weak).
- **edge pull-up (§5.6):** a direct edge entailment ≥ `t_edge` floors both endpoint credits
  at `edge_pullup_floor=0.6`.

`t_low/t_mid/t_high/alpha/max_contradiction` are the calibration surface (§7). Defaults above
are pre-calibration placeholders — T8 replaces them with the fitted values.

### 5.6 Graded edge credit `g_e` (`edges.py`) — kills the quadratic starvation

For each reference edge `e = (type, u, v)` with endpoint credits `c_u, c_v`:

```
r(e) = max of the applicable evidence tiers:
  1.0   ENTAIL    max over top-2 windows of P(entailment | window, verbalize(e)) >= t_edge (0.85)
                  (NLI run ONLY when min(c_u, c_v) >= 0.3 OR the endpoints' best windows
                   co-occur — budget guard; counts against max_nli_pairs)
  0.7   COOCCUR   |best_window(u) - best_window(v)| <= 1   (both endpoints' argmax windows
                  in the same or adjacent window — the ALA-Reader/GIKS co-occurrence move)
  0.4   ENDPOINTS c_u >= 0.7 and c_v >= 0.7 anywhere in the transcript
  0.0   otherwise

edge_credit(e) = r(e) * sqrt(c'_u * c'_v)
  where c'_x = max(c_x, edge_pullup_floor) when r(e) came from ENTAIL, else c_x

v1 floor: edge_credit(e) = max(edge_credit(e), 1.0 if v1 explicit triple match,
                               0.5 if v1 inferred-only match)      # v1 scores.py semantics
```

Verbalization templates (deterministic f-strings over payload labels; direction conventions
match `build_reference_canonical`):

- USES (proc → eq): `"{u_label} uses {v_label}."`
- DEPENDS_ON (dep → step): `"{v_label} requires {u_label}."`
- PRECEDES (prev → next): `"{u_label} happens before {v_label}."`
- SCOPES (cond → target): `"{v_label} applies when {u_label}."`

**Node pull-up (the R3 "joint" move, ILP-free):** when `r(e)` = ENTAIL, both endpoints' node
credits are floored at 0.6 (`source="edge_pullup"`) BEFORE path aggregation — a clearly-taught
relation pulls an otherwise-unresolvable node onto the reference.

### 5.7 Aggregation (`aggregate.py`) — winning path preserved

```
node_cov(path)  = mean over path keys k of credit(k)          # graded, not binary
node_coverage   = max over reference.paths of node_cov(path)  # ties -> lowest path_index
edge_coverage   = mean over ALL reference.edges of edge_credit(e)   # v1 denominator parity
```

Mirrors `graph_compare/coverage.py` winner selection exactly (`min` on
`(-score, path_index)`). Empty-path guard: vacuous 1.0 (v1 parity).

## 6. Gray-zone check contract (`grayzone.py`)

- **Trigger:** nodes with `t_low <= score < t_mid` after §5.4 (the gray band), at most
  `max_grayzone_nodes=8` per attempt (descending score; the rest default to 0.3).
- **Bound:** exactly ONE LLM call per attempt (all gray nodes batched). Model/client: mirror
  `apollo.grading.transcript_audit.main_chat_auditor`'s client plumbing.
- **Prompt contract:** input = full student-only transcript + per node `(canonical_key, label,
  views)`. Output (strict JSON): `[{"canonical_key", "taught": true|false, "quote": "<verbatim
  span>"}]`. The model is instructed the quote MUST be copied character-for-character.
- **Verification (the hard gate):** `verify_quote(quote, transcript)` — normalize both
  (casefold, collapse whitespace); pass iff normalized quote is a substring OR
  `rapidfuzz.fuzz.partial_ratio >= 95` and `len(quote) >= 15` chars. `taught=true` WITHOUT a
  verified quote is an auto-NO (FINDINGS §5: unverifiable quote = auto-reject).
- **Credit:** verified YES → credit `grayzone_credit=0.7` (CAPPED — never 1.0, never above
  the NLI-high tier). NO / unverified / malformed JSON / LLM error → 0.3 (the deterministic
  gray default). The check can only move a node WITHIN the gray band's allowed range — it is
  a candidate generator, never an unconditional score source.
- **Modes (both first-class):** `APOLLO_RESOLVER_V2_GRAYZONE=0` (default) → no LLM call,
  gray = 0.3, fully deterministic (replay/calibration mode). `=1` → live check (bounded,
  logged, trace records per-node verdict + verified flag). Engine takes `grayzone_fn:
  Callable | None` — tests inject a fake; `None` = disabled.

## 7. Flags & env (all read fresh per call, `config.py`)

| Env | Default | Meaning |
|---|---|---|
| `APOLLO_RESOLVER_V2` | `0` | master switch; OFF = byte-identical pipeline |
| `APOLLO_RESOLVER_V2_GRAYZONE` | `0` | gray-zone LLM check on/off |
| `APOLLO_RESOLVER_V2_TRACE_DIR` | unset | when set, integration dumps full per-attempt trace JSON to `<dir>/attempt_<id>.json` (calibration input; new files only) |
| `APOLLO_RESOLVER_V2_T_LOW / T_MID / T_HIGH` | 0.40 / 0.75 / 0.90 | credit thresholds |
| `APOLLO_RESOLVER_V2_ALPHA` | 0.85 | fusion weight on entailment |
| `APOLLO_RESOLVER_V2_MAX_CONTRADICTION` | 0.30 | pair contradiction veto |
| `APOLLO_RESOLVER_V2_TOP_K` | 3 | windows per (node, view) sent to NLI |
| `APOLLO_RESOLVER_V2_MAX_PAIRS` | 200 | per-attempt NLI budget |
| `APOLLO_RESOLVER_V2_T_EDGE` | 0.85 | direct edge-entailment bar |

`ResolverV2Params` is a frozen dataclass; `load_params()` = defaults + env overrides
(mirror `nli_config.load_nli_params` style). NLI checkpoint: `active_nli_model()` unchanged
(`APOLLO_NLI_MODEL` env already exists) — under `HF_HUB_OFFLINE=1` only deberta-v3-large is
cached; never design against a small-model fallback.

## 8. Runtime estimate (CPU, 31-attempt replay budget: well under 1 h)

Per attempt: windows ≈ 30–50; ref nodes 5–8; views ≤ 5; NLI pairs ≤ nodes×views×K =
8×5×3 = 120, + edges ≤ 10×2 = 20, hard-capped at 200. deberta-v3-large on CPU with short
sequences (≤ ~200 tokens) at batch-16 (`pipeline` accepts a list — batch ALL pairs of an
attempt in one call): ~3–6 pairs/s conservative → 25–70 s/attempt → **13–36 min for 31
attempts**, single process. Levers if over budget: `TOP_K=2`, views→3, `MAX_PAIRS=120` —
lexical-skip + the pair cache typically cut the real pair count 30–50%. Prefilter and
aggregation are microseconds. Gray zone OFF in replay (deterministic) = zero LLM latency.

## 9. Calibration procedure (T8, `scripts/resolver_v2_calibrate.py` — DB-free)

**Gold labels from the frozen corpus (read-only):** each `attempts.jsonl` record carries
`transcript` (list of `{role, content}`), `problem_id`, `subject`, `concept`, and
`expected.{credited, unresolved, misconceptions}` (mirrored in
`campaign/cast/personas/<subject>/*.json`). Per attempt, per reference key `k` on the union
of declared paths:

- `k ∈ expected.credited` → **positive** (persona provably taught it)
- `k ∈ expected.unresolved` → **negative** (persona deliberately omitted/mangled it)
- on control personas (`misconception`, `vague_then_clarifies`): any path key ∉
  expected.credited → **negative**

**Harness:** load problem payload from
`apollo/subjects/<subject>/concepts/<concept>/problems/<problem_id>.json` (READ-only),
`build_reference_canonical(payload)`, run the V2 engine directly on the transcript's
student turns — no DB, no Neo4j, no v1 floors (pure V2 signal), grayzone OFF. Deterministic.

**Objective (recall-first, R4):** detection := credit ≥ 0.7. Grid-search
`(t_low, t_mid, t_high, alpha, max_contradiction)` over
t_high ∈ {0.80,0.85,0.90,0.95}, t_mid ∈ {0.60,0.70,0.75}, t_low ∈ {0.30,0.40,0.50},
alpha ∈ {0.75,0.85,1.0}, max_contradiction ∈ {0.20,0.30,0.50}:

```
maximize   node recall on positives
subject to false-credit rate on negatives <= 0.05        # X = 5%
tie-break: lower false-credit rate, then higher mean margin (score_pos - t_mid)
```

Report per-persona-class recall/FCR + score distributions. **Output:**
`campaign/out/resolver-v2/calibration-2026-07-07.json` (full sweep) + the winning params
written into `config.py` defaults (one commit). Edge thresholds are NOT swept tonight (no
edge gold) — report the edge-credit distribution per class instead.

**Then end-to-end replay (local Docker stack: DB :57322, Neo4j :57687, `.env.campaign`,
`HF_HUB_OFFLINE=1`, `APOLLO_NLI_GRADING_MAX_NODES=40` per `campaign/infra/env.campaign.example`):**

```
python -m campaign.replay --run-dir campaign/out/f1c --out campaign/out/resolver-v2/replay-off-2026-07-07.json   # flag OFF baseline
APOLLO_RESOLVER_V2=1 python -m campaign.replay --run-dir campaign/out/f1c --out campaign/out/resolver-v2/replay-on-2026-07-07.json
```

Replay appends `apollo_graph_comparison_runs` rows + MERGEs Neo4j (same idempotent writes
the retry janitor makes — local stack only, acceptable). All NEW output files under
`campaign/out/resolver-v2/`; `campaign/out/f1c/**` is never written.

**Success criteria (the hypothesis test):** strong-class mean node_coverage ≥ 0.6 (was 0.20);
strong vs misconception composite separation ≥ 0.15 (was 0.002); edge_coverage nonzero on
≥ 24/31 (was 12/31); `control_credit_leak` count ≤ baseline; band agreement vs LLM grader
improves from 4/31.

## 10. Risks

1. **False credit on controls (the over-correction failure).** Cross-encoder entailment on
   short windows over-fires on topically-adjacent-but-wrong statements; misconception
   personas teach 4/5 beats CORRECTLY, so their negatives are per-node, not per-attempt.
   Mitigation: contradiction veto + polarity screen + calibration constraint FCR ≤ 5% on
   labeled negatives + replay `control_credit_leak` as the hard regression check.
2. **CPU runtime blow-up.** If un-batched or the cache misses, 31 attempts could exceed the
   budget. Mitigation: single batched pipeline call per attempt, pair budget hard cap with
   deterministic lexical-only degradation, `pair_count` in every trace; T8 measures wall time
   on 2 attempts before the full run.
3. **View quality / negation brittleness.** A negated or hedged view false-vetoes on
   polarity or false-fires entailment. Mitigation: generator validation pass (affirmative
   grammar rules + polarity self-check), label always view 0, calibration sweeps alpha so a
   bad view can be out-voted by max-aggregation.
4. **Scores-vs-findings divergence (accepted, documented).** With flag ON, artifact scores
   are graded V2 while the node ledger/findings stay v1-binary — a scorecard could show
   coverage 0.8 with 3 "missing" ledger rows. Acceptable for the shadow/replay phase; ledger
   unification is explicitly out of scope tonight.
5. **`content.label` drift between payload and cache.** A reseeded problem changes labels but
   not entity keys. Mitigation: cache keyed by entity_key; loader degrades to label-only
   views; the generator records the source label per view for later staleness checks.

## 11. What surprised us in the existing code (design-relevant)

- `build_audited_grade` never re-grades: `AuditedGrade.grade` is carried UNCHANGED, so
  substituting scores BEFORE the audit is safe and flows into the §10 gate + artifact with
  zero further edits. (The audit rewrites findings only.)
- `attempts.jsonl` `transcript` is a real JSON list of `{role, content}` (roles `student` /
  `apollo`) — calibration can run fully DB-free. `artifact_*` fields are stringified Python
  reprs; don't parse them, we don't need them.
- `ReferenceGraph`'s `CanonicalNode` has NO label field — labels/views must come from
  `problem_payload["reference_solution"]` steps, keyed by `entity_key`.
- Misconception-persona `expected.credited` contains 4/5 nodes — controls are per-node
  negatives, not whole-attempt zeros.
- `sentence-transformers` is NOT installed; the only cached checkpoint under
  `HF_HUB_OFFLINE=1` is `cross-encoder/nli-deberta-v3-large`. Prefilter must be lexical.
- Table name is `apollo_problem_attempts`; messages live in `apollo_messages` with a `role`
  Text column — student-turn filtering is a one-line WHERE.

---

# TASK CARDS

Execution order: **Group A** (T1 ∥ T2) → **Group B** (T3 ∥ T4 ∥ T5 ∥ T6, all depend only on
T1) → **Group C** (T7, sequential) → **Group D** (T8, sequential). Branch:
`feat/apollo-resolver-v2`. Every card: smoke tests ONLY (happy path + one edge case,
deterministic, FakeNLI/fake-fn injection, no network, no model load in tests); the 95% patch
gate is deferred — state this in commit messages. NEVER touch `campaign/out/f1c/**`,
`apollo/subjects/**/problems/*.json` (pre-existing dirty diffs on problem_02–05), or any
remote DB. All dataclasses frozen; no mutation; files < 400 lines.

---

## T1 — resolver_v2 core: types + config  [Group A, no deps]

**Create:** `apollo/resolver_v2/__init__.py`, `apollo/resolver_v2/types.py`,
`apollo/resolver_v2/config.py`, `apollo/resolver_v2/tests/__init__.py`,
`apollo/resolver_v2/tests/test_config_types.py`.

**Interface (copy §5.0 verbatim):** `Window`, `RefNode`, `PairScore`, `NodeScore`,
`EdgeScore`, `ResolverV2Result` (+ `.trace() -> dict`, JSON-safe via `dataclasses.asdict`
reshaping), `SelectFn` type alias. `config.py`: `RESOLVER_V2_FLAG = "APOLLO_RESOLVER_V2"`,
`GRAYZONE_FLAG = "APOLLO_RESOLVER_V2_GRAYZONE"`, `def resolver_v2_enabled() -> bool`
(default False, truthy = `1|true|yes`, mirror `nli_config.nli_enabled` but DEFAULT-OFF),
`def grayzone_enabled() -> bool` (default False), `@dataclass(frozen=True) class
ResolverV2Params` with every §7 field + defaults, `def load_params() -> ResolverV2Params`
(env overrides `APOLLO_RESOLVER_V2_<FIELD>`, malformed → default; mirror
`nli_config._env_float/_env_int`). `__init__.py` exports `resolver_v2_enabled` eagerly and
`run_resolver_v2` lazily (module `__getattr__` — engine.py lands in T7).

**Smoke tests:** flag default-off + env truthiness; `load_params()` env override + malformed
fallback; `ResolverV2Result.trace()` returns JSON-serializable dict
(`json.dumps` round-trip).

**DONE:** `pytest apollo/resolver_v2/tests/ -q` green; `python -c "import apollo.resolver_v2"`
works with no heavy imports (no transformers at import time); flag off by default.

---

## T2 — view-cache generator + committed views JSON  [Group A, no deps]

**Create:** `scripts/generate_resolver_v2_views.py`,
`apollo/resolver_v2/views/views_cache.json` (the generated, committed output).

**Behavior:** enumerate `apollo/subjects/*/concepts/*/problems/*.json` (READ-only — the
files stay byte-identical; problem_02–05 fluids carry uncommitted diffs, generate FROM the
working tree but never write to them). For each reference step emit 3–4 affirmative
paraphrase views per §5.2 (definition-style, application/causal-style, plain-language;
equations get one spoken form) via ONE OpenAI chat call per problem (strict JSON output;
reuse the repo's OpenAI client pattern — see `apollo/agent/_llm.py`; key from env/
`.env.campaign`). Validation pass per view: affirmative (reject if it matches the negation
markers in `apollo/resolution/polarity.py`), ≤ 25 words, non-empty; offenders dropped (do
NOT retry forever — 1 regeneration round, then keep what passed). Output shape per §5.2:
`{"<concept_id>/<problem_id>": {"<entity_key>": [views...]}}` + a top-level `"_meta"` block
(model, date, source label per key). Deterministic file layout: `json.dumps(...,
indent=2, sort_keys=True)`. Script is idempotent (`--only <problem_id>` and `--dry-run`
supported).

**Smoke tests:** none required for the script (offline tool) — instead the card's gate is
the ARTIFACT: cache parses, covers every problem file found, every key list non-empty,
no view contains ` not ` / `never` / `n't`.

**DONE:** `views_cache.json` committed, covering all problems under `apollo/subjects/`
(fluid_mechanics 5, macroeconomics, linear_motion — whatever `problems/*.json` exists);
validation gate above passes via a one-off `python - <<...` check pasted into the commit
message.

---

## T3 — windows + lexical prefilter  [Group B, depends: T1]

**Create:** `apollo/resolver_v2/windows.py`, `apollo/resolver_v2/prefilter.py`,
`apollo/resolver_v2/tests/test_windows_prefilter.py`.

**Interface:**
```python
# windows.py
def split_sentences(text: str) -> list[str]                    # regex; LaTeX-safe (§5.1)
def build_windows(student_turns: Sequence[str], params: ResolverV2Params) -> tuple[Window, ...]
# prefilter.py
def lexical_score(window_text: str, view_text: str) -> float   # §5.3 formula, [0,1]
def select_windows(windows: Sequence[Window], view_text: str, k: int) -> tuple[tuple[int, float], ...]
```
`select_windows` satisfies `SelectFn`: top-k `(window.index, lex)` sorted by `(-lex, index)`.
Uses `rapidfuzz` (existing dep) + the content-token overlap per §5.3. Pure, deterministic,
no imports beyond stdlib/rapidfuzz/types/config.

**Smoke tests:** (1) happy path — 2 multi-sentence turns → expected window count/overlap,
`select_windows` ranks the on-topic window first; (2) edge — empty turns → `()`; single
120+-word sentence → one truncated window; zero-overlap view → scores 0.0 and still returns
deterministically ordered results.

**DONE:** tests green; `build_windows` output stable across two calls (equality assert).

---

## T4 — node scoring engine: views loader, NLI provider, fusion + credit  [Group B, depends: T1]

**Create:** `apollo/resolver_v2/views.py`, `apollo/resolver_v2/nli_provider.py`,
`apollo/resolver_v2/scoring.py`, `apollo/resolver_v2/tests/test_scoring.py`.

**Interface:**
```python
# views.py
VIEWS_CACHE_PATH: Path   # apollo/resolver_v2/views/views_cache.json
def load_views(concept_id: str, problem_id: str) -> dict[str, tuple[str, ...]]
    # {} / missing key degrades to empty (caller falls back to label-only); logs once; never raises
def build_ref_nodes(reference_graph: ReferenceGraph, problem_payload: dict,
                    views_by_key: Mapping[str, tuple[str, ...]]) -> tuple[RefNode, ...]
    # one RefNode per distinct key on the union of reference_graph.paths, key-sorted;
    # label from payload reference_solution step content.label (fallback canonical_key);
    # views = (label,) + cached views (dedup, order-preserving)
# nli_provider.py
def get_adjudicator() -> NLIAdjudicator      # process-lived lazy TransformersNLIAdjudicator
                                             # (active_nli_model(), device=NLI_DEVICE); import-
                                             # error degrades to None + one warning (mirror
                                             # done_grading._log_nli_import_failure_once)
# scoring.py
def fuse(entailment: float, lexical: float, params: ResolverV2Params) -> float   # §5.4
def credit_for_score(score: float, params: ResolverV2Params) -> tuple[float, bool]
    # (credit, is_gray) — §5.5 WITHOUT grayzone/floors (0.3 for gray band)
def score_nodes(windows: Sequence[Window], ref_nodes: Sequence[RefNode], *,
                nli: NLIAdjudicator | None, params: ResolverV2Params,
                v1_resolved_keys: frozenset[str], select_fn: SelectFn,
                ) -> tuple[tuple[NodeScore, ...], int]        # (scores, pair_count)
```
`score_nodes` implements §5.3 skip rule, §5.4 screens (import
`apollo.resolution.polarity.polarity_allows_match`), pair memo cache, `max_nli_pairs` budget
(path order = `ref_nodes` order), v1 floor (credit 1.0, `source="v1_floor"`), gray nodes get
credit 0.3 + `source="nli"` here (grayzone upgrade happens in engine, T7). `nli=None` →
lexical-only for everything (deterministic degrade).

**Smoke tests (FakeNLIAdjudicator from `apollo.resolution.nli_adjudicator`):** (1) happy —
scripted entailment 0.95 on the right (window, view) → credit 1.0, `best` records the pair;
multi-view: only view 2 entails → still credited (max-over-views); (2) edge — contradiction
0.9 → pair vetoed → credit 0.0; `v1_resolved_keys` floors an otherwise-zero node to 1.0;
budget=1 → second node degrades to lexical_skip; `load_views` on a missing problem returns
`{}` without raising.

**DONE:** tests green; no transformers import at module import time (lazy inside
`get_adjudicator`).

---

## T5 — graded edge credit + aggregation  [Group B, depends: T1]

**Create:** `apollo/resolver_v2/edges.py`, `apollo/resolver_v2/aggregate.py`,
`apollo/resolver_v2/tests/test_edges_aggregate.py`.

**Interface:**
```python
# edges.py
def verbalize_edge(edge_type: str, from_label: str, to_label: str) -> str   # §5.6 templates
def score_edges(ref_edges: Sequence[CanonicalEdge],
                node_scores: Mapping[str, NodeScore],
                windows: Sequence[Window], *,
                labels: Mapping[str, str],                    # canonical_key -> label
                nli: NLIAdjudicator | None, params: ResolverV2Params,
                v1_explicit_triples: frozenset[tuple[str, str, str]],   # (type, from, to)
                v1_inferred_triples: frozenset[tuple[str, str, str]],
                select_fn: SelectFn, pair_budget_left: int,
                ) -> tuple[tuple[EdgeScore, ...], dict[str, float], int]
    # returns (edge_scores in input order, node pull-up floors {key: 0.6}, pairs_used)
# aggregate.py
def aggregate(reference_graph: ReferenceGraph,
              node_scores: Mapping[str, NodeScore],
              edge_scores: Sequence[EdgeScore]) -> tuple[float, float, int]
    # (node_coverage winning-path mean credit, edge_coverage mean over ALL ref edges,
    #  winning_path_index) — §5.7; tie -> lowest path_index; empty path -> 1.0 vacuous
```
`score_edges` implements the full §5.6 ladder (ENTAIL via top-2 windows of the verbalized
edge selected by `select_fn`; COOCCUR via `NodeScore.best.window_index` distance ≤ 1;
ENDPOINTS; v1 floors; the `sqrt(c'_u * c'_v)` scaling; ENTAIL-gated NLI budget guard).
`nli=None` → ladder without the ENTAIL tier. Import `CanonicalEdge`/`ReferenceGraph` from
`apollo.graph_compare.canonical` (read-only).

**Smoke tests (FakeNLI):** (1) happy — scripted entailment on a verbalized USES edge →
`relation_evidence="entail"`, pull-up floors both endpoints at 0.6, edge_credit =
`sqrt(0.6*0.6)*1.0`; aggregate picks the higher-credit path and reports its index; (2) edge —
both endpoints credited 0.8 but non-adjacent windows and no entailment → ENDPOINTS tier
0.4·sqrt(0.64); v1 explicit triple floors credit to 1.0; zero ref edges → edge_coverage
vacuous 1.0 (v1 parity).

**DONE:** tests green; `aggregate` reproduces v1 `coverage.py` winner-selection semantics on
a binary-credit fixture (credit ∈ {0,1} → same winner + same score as set-membership math).

---

## T6 — grounded gray-zone check  [Group B, depends: T1]

**Create:** `apollo/resolver_v2/grayzone.py`, `apollo/resolver_v2/tests/test_grayzone.py`.

**Interface:**
```python
@dataclass(frozen=True)
class GrayzoneQuery:   canonical_key: str; label: str; views: tuple[str, ...]
@dataclass(frozen=True)
class GrayzoneVerdict: canonical_key: str; taught: bool; quote: str | None; verified: bool
GrayzoneFn = Callable[[tuple[GrayzoneQuery, ...], str], tuple[GrayzoneVerdict, ...]]

def verify_quote(quote: str, transcript: str) -> bool          # §6 normalization + partial_ratio>=95, len>=15
def apply_grayzone(gray: Sequence[NodeScore], transcript: str, fn: GrayzoneFn | None,
                   params: ResolverV2Params) -> dict[str, float]
    # {canonical_key: new_credit}; fn None -> {} (all stay 0.3); truncates to
    # max_grayzone_nodes by descending score; verified YES -> grayzone_credit (0.7);
    # everything else -> unchanged; LLM/JSON errors inside fn are caught -> {} for the batch
def main_chat_grayzone(queries, transcript) -> tuple[GrayzoneVerdict, ...]
    # the live impl: ONE chat call, strict JSON, §6 prompt contract; client plumbing mirrors
    # apollo/grading/transcript_audit.py's main_chat_auditor; runs verify_quote itself and
    # sets .verified (a YES with an unverifiable quote comes back verified=False)
```

**Smoke tests (fake `GrayzoneFn`, no LLM):** (1) happy — verified YES upgrades exactly that
key to 0.7, others untouched; `verify_quote` passes an exact quote and a
whitespace/case-mangled quote; (2) edge — fabricated quote (not in transcript) → verdict
verified=False → no upgrade; > max_grayzone_nodes gray nodes → only top-8 by score queried;
fn raising → caught, `{}` returned.

**DONE:** tests green; `main_chat_grayzone` importable without an OpenAI key (lazy client).

---

## T7 — integration: engine + done_grading hook + artifact trace  [Group C, depends: T3+T4+T5+T6]

**Create:** `apollo/resolver_v2/engine.py`, `apollo/resolver_v2/integration.py`,
`apollo/resolver_v2/tests/test_engine_integration.py`.
**Modify:** `apollo/handlers/done_grading.py`, `apollo/grading/artifact_build.py`,
`docs/architecture/apollo.md`.

**Interface:**
```python
# engine.py — pure orchestration (NLI + grayzone injected); §4 sequence:
def run_resolver_v2(*, student_turns: Sequence[str], reference_graph: ReferenceGraph,
                    problem_payload: dict, v1_resolved_keys: frozenset[str],
                    v1_explicit_triples: frozenset[tuple[str, str, str]],
                    v1_inferred_triples: frozenset[tuple[str, str, str]],
                    nli: NLIAdjudicator | None, grayzone_fn: GrayzoneFn | None,
                    params: ResolverV2Params) -> ResolverV2Result
# order: build_windows -> load_views/build_ref_nodes -> score_nodes -> apply_grayzone ->
#        score_edges (pull-ups applied to node credits AFTER grayzone) -> aggregate
# integration.py
async def load_student_turns(db: AsyncSession, attempt_id: int) -> tuple[str, ...]
    # SELECT content FROM apollo_messages WHERE attempt_id=? AND role='student' ORDER BY turn_index
def v1_inputs_from_canonical(student_canonical: CanonicalGraph) -> tuple[frozenset[str], frozenset, frozenset]
    # resolved keys (EXCLUDING misc.* via graph_compare.soundness.is_misconception_key),
    # explicit triples, inferred triples
def substitute_scores(grade: GradeResult, v2: ResolverV2Result) -> GradeResult
    # dataclasses.replace(grade, coverage_score=v2.node_coverage,
    #                     node_coverage_score=v2.node_coverage,
    #                     edge_coverage_score=v2.edge_coverage)   # NOTHING else changes
async def apply_resolver_v2(db, *, attempt_id: int, grade: GradeResult,
                            student_canonical: CanonicalGraph,
                            reference_graph: ReferenceGraph,
                            problem_payload: dict) -> tuple[GradeResult, dict]
    # loads turns; builds nli via nli_provider.get_adjudicator(); grayzone_fn =
    # main_chat_grayzone if grayzone_enabled() else None; runs engine in
    # asyncio.to_thread (CPU-bound); dumps full trace JSON to
    # $APOLLO_RESOLVER_V2_TRACE_DIR/attempt_<id>.json when set (new files only);
    # returns (substitute_scores(grade, result), result.trace())
```

**done_grading.py diff shape (the ONLY logic change; place after step 8
`grade = grade_attempt(...)`, inside the existing try):**
```python
resolver_v2_trace: dict | None = None
if resolver_v2_enabled():                     # lazy import inside the branch
    from apollo.resolver_v2.integration import apply_resolver_v2
    grade, resolver_v2_trace = await apply_resolver_v2(
        db, attempt_id=int(attempt.id), grade=grade,
        student_canonical=student_canonical, reference_graph=reference_graph,
        problem_payload=problem_payload,
    )
```
plus `ShadowGradeResult` gains `resolver_v2_trace: dict | None = None` (defaulted LAST so
every existing construction stays valid) and the constructor call passes it. A V2 failure is
NOT special-cased — it follows the existing broad-except NO-FALLBACK contract.
`resolver_v2_enabled()` import at module top is fine (tiny, no heavy deps).

**artifact_build.py diff shape (in `build_graph_artifact`, after the `scores` block):**
```python
if shadow.resolver_v2_trace is not None:
    artifact["scores"]["resolver_v2"] = shadow.resolver_v2_trace.get("summary")
```

**Owner doc:** append a short "Resolver V2 (flagged, shadow)" subsection to
`docs/architecture/apollo.md` module map + bump `last_verified` — same commit as the code
(drift contract).

**Smoke tests:** (1) engine happy path with FakeNLI + fake selector + fake grayzone → full
`ResolverV2Result` with expected coverages; (2) `substitute_scores` changes exactly the three
fields (assert every other `GradeResult` field equal); (3) **flag-OFF byte-identity**: with
`APOLLO_RESOLVER_V2` unset, `run_graph_simulation`'s existing tests still pass UNMODIFIED
(run `pytest apollo/handlers/tests/ -q`) and `build_graph_artifact` output on a fixture
`ShadowGradeResult` (default trace None) is byte-identical to before (golden-dict assert);
(4) `load_student_turns` filters role + orders by turn_index (sqlite fixture, mirror existing
handler-test fixtures).

**DONE:** all resolver_v2 tests + `pytest apollo/handlers/tests apollo/grading/tests -q`
green; flag-OFF golden assert present; owner doc updated in the same commit.

---

## T8 — calibration sweep + F1c replay ON/OFF  [Group D, depends: T2+T7]

**Create:** `scripts/resolver_v2_calibrate.py`,
`campaign/out/resolver-v2/calibration-2026-07-07.json`,
`campaign/out/resolver-v2/replay-off-2026-07-07.json`,
`campaign/out/resolver-v2/replay-on-2026-07-07.json`,
`campaign/out/resolver-v2/REPORT.md`.
**Modify:** `apollo/resolver_v2/config.py` (fitted defaults ONLY — the `ResolverV2Params`
field values; nothing else).

**Steps (exactly):**
1. Calibration per §9: DB-free, reads `campaign/out/f1c/attempts.jsonl` (READ-only) +
   `campaign/cast/personas/**` + `apollo/subjects/**/problems/*.json` (READ-only; do NOT
   `git add` the pre-existing dirty problem_02–05 diffs), engine with `nli=real adjudicator`
   (`HF_HUB_OFFLINE=1`), `grayzone_fn=None`, `v1_*` inputs empty. Grid per §9; emit the
   sweep JSON + pick the winner (recall-first, FCR ≤ 5%).
2. Write winning params into `config.py` defaults; commit with the sweep evidence.
3. Timing probe: run 2 attempts, extrapolate; if > 90 s/attempt, drop `TOP_K` to 2 and note
   it in REPORT.md.
4. Replay baseline + V2 per §9 commands (local Docker stack :57322/:57687, `.env.campaign`,
   `APOLLO_NLI_GRADING_MAX_NODES=40`). New output files only.
5. `REPORT.md`: §9 success-criteria table (baseline vs V2 per metric), per-persona
   node_cov/edge_cov/composite/band, `control_credit_leak` diff, wall time, pair-count stats,
   and an explicit verdict line: HYPOTHESIS CONFIRMED / PARTIAL / REFUTED.

**Smoke tests:** the calibration script gets one test
(`apollo/resolver_v2/tests/test_calibrate_labels.py` or `scripts/`-adjacent): label
derivation from a 2-record fixture jsonl (positive/negative sets per §9 rules, control
per-node negatives included).

**DONE:** all four output artifacts committed under `campaign/out/resolver-v2/`; `config.py`
defaults = fitted values; REPORT.md verdict present; `campaign/out/f1c/**` untouched
(`git status` proof in the report).

---

## Task-card dependency summary

| id | group | depends_on | parallel with |
|---|---|---|---|
| T1 | A | — | T2 |
| T2 | A | — | T1 |
| T3 | B | T1 | T4, T5, T6 |
| T4 | B | T1 | T3, T5, T6 |
| T5 | B | T1 | T3, T4, T6 |
| T6 | B | T1 | T3, T4, T5 |
| T7 | C | T3, T4, T5, T6 | — (sequential) |
| T8 | D | T2, T7 | — (sequential) |
