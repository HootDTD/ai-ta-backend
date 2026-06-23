# Apollo Graph-Grading — Edge-Fidelity Findings: resolution is the leaky layer

**Date:** 2026-06-23
**Status:** Diagnosis complete, evidence-backed on the LOCAL econ run (33 attempts).
No code changed. Feeds the Direction-B design (`docs/superpowers/specs/`).
**Continues:** `APOLLO-USES-EDGE-RESOLUTION-HANDOFF.md`,
`APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md`, `APOLLO-GRAPH-GRADING-HANDOFF.md`.

---

## Problem statement

**The grader discards ~40% of the student's structural claims before it scores
them, because it requires every edge's two endpoints to be individually *named*
(resolved to a reference entity) — and the naming step systematically fails on the
exact forms students legitimately produce.**

1. **The edges *are* what is graded.** A student's edges are their structural
   claims — *"this procedure step USES this equation," "this simplification SCOPES
   this equation."* The `usage`, `scoping`, and `edge_coverage` scores exist for
   exactly these claims.
2. **Naming is a hard precondition for crediting a claim.** Every student node is
   first *resolved* in isolation to one reference entity (`resolve_attempt`); then
   `build_student_canonical` keeps an edge **only if both endpoints resolved**
   (`canonical.py:275`). One unnamed endpoint ⇒ the whole claim is dropped.
3. **Naming fails on two whole classes of correct content** — **1a** derived /
   solved / numeric equation forms (not symbolically equal to the base form) and
   **1b** free-text conditions / simplifications / definitions (reference
   candidates carry no aliases, `candidates.py:101`). Both land `unresolved`.
4. **Consequence (measured):** 40 / 100 student edges dropped (`SCOPES` 59%,
   `USES` 35%). The student made the correct claim, but an unnamed endpoint makes
   it invisible to the scorer; `edge_coverage` / `usage` collapse to 0 and the
   attempt abstains.
5. **Essence:** the pipeline conflates *"Can I **name** this node?"* (resolution)
   with *"Is this structural **claim** correct?"* (grading), and uses the first —
   decided per-node in isolation — to gate the second. But the second has more
   evidence available: the edge, the already-resolved neighbor, and the reference
   structure. **An edge can be a correct structural claim even when one of its
   endpoints cannot be precisely named.**

---

## TL;DR

The handoff series treats the dead `edge_coverage`/`usage` scores as **one niche
bug** (case-3: a derived equation form fails to resolve, so its `USES` edge is
dropped). Measuring the actual local graph (33 econ + fluid attempts, 100 student
edges) shows a bigger, simpler truth:

1. **40 of 100 student edges (40%) never reach scoring** because an endpoint did
   not resolve. This single failure mode is **93% of all edge-fidelity loss**.
2. It splits in two, and the handoff only names half of it loudly:
   - **1a — derived/variant EQUATION forms unresolved (31 endpoint-incidents).**
     This is case-3. The biggest single bucket. Kills `USES` + eq→eq `DEPENDS_ON`.
   - **1b — non-equation nodes unresolved (17): Condition 11, Simplification 3,
     Definition 2, ProcedureStep 1.** Reference conditions/simplifications carry
     **no aliases** (`candidates.py:101`), so they can only resolve via exact /
     fuzzy≥0.9 / one LLM call. This is why `SCOPES` is the **worst-hit edge type
     (59% dropped)**.
3. The "DEPENDS_ON type/direction drift" hypothesis is **real but marginal** —
   2 type-drift + 1 direction-drift events. Do not prioritize it.
4. Two **new resolution-quality bugs** surfaced from reconstructing S_norm:
   - **Over-merge → `PRECEDES` self-loop** (resolver many-to-one is type-blind).
   - **Cross-type resolution** (LLM adjudicator bypasses the type gate), which
     also violates `canonical.py`'s "merged members share one type" invariant.
5. **The "messy/overflow" you see in `CALL db.schema.visualization()` is an
   artifact, not pollution.** Per-attempt instance graphs are clean and sparse.
   Do not spend time "pruning edges."

**The scorer and the edge model are not at fault. Resolution is. 40% of edges
die at the resolution gate before any score runs.**

---

## How this was measured

Read-only probes against the LOCAL stack (`hoot-neo4j-local` :7687, healthy),
project venv `.venv/Scripts/python.exe`. No writes, no migrations. The graph held
**33 attempts, 147 `:_KGNode` nodes, 100 student edges**. Reproduction Cypher is
in the Appendix; the `inspect_attempt.py` connection pattern was reused.

Key DB facts:
- Every node has exactly **two labels**: `:_KGNode` + its kind label
  (`Equation` / `Condition` / `Simplification` / `Definition` / `VariableMapping`
  / `ProcedureStep`). No multi-label explosion.
- All attempts share one database, scoped only by an `attempt_id` **property**.
- **Cross-attempt contamination edges: 0.**

---

## The quantification

### Three failure modes (100 student edges)

| Mode | What | Count | Share of loss |
|---|---|---:|---:|
| **1 — endpoint unresolved** | edge dropped, ≥1 endpoint `unresolved` | **40** | **93%** |
| 2 — type drift | surviving `DEPENDS_ON` sitting in a typed edge's territory | 2 | 5% |
| 3 — direction drift | `DEPENDS_ON` key-pair seen in both directions | 1 | 2% |

### Drop rate by edge type

| edge type | total | survived | **dropped** | drop rate |
|---|---:|---:|---:|---:|
| `SCOPES` | 27 | 11 | **16** | **59%** |
| `DEPENDS_ON` | 36 | 23 | **13** | 36% |
| `USES` | 31 | 20 | **11** | 35% |
| `PRECEDES` | 6 | 6 | 0 | 0% |
| **total** | **100** | **60** | **40** | **40%** |

### Mode 1 split — by the kind of the unresolved endpoint (48 incidents over 40 edges)

| unresolved endpoint kind | incidents | bucket |
|---|---:|---|
| Equation (derived/variant forms) | 31 | **1a** |
| Condition | 11 | 1b |
| Simplification | 3 | 1b |
| Definition | 2 | 1b |
| ProcedureStep | 1 | 1b |
| **1a total (equations)** | **31** | |
| **1b total (non-equations)** | **17** | |

---

## Problem 1 (Mode 1a) — derived / variant equation forms do not resolve

**Symptom.** A student writes a rearranged, solved, or numerically-substituted
form of an in-scope equation. It fails to resolve to the governing entity, so
every edge touching it is dropped.

**Evidence (econ deflator problem, every run — attempts 20, 31, 32, 33):**
```
Node 'deflator - (nomGDP/realGDP)*100'  →  eq.gdp_deflator   (base form, resolves)
Node 'realGDP - nomGDP/(PI/100)'        →  UNRESOLVED         (rearranged form)
Edge: ProcedureStep 'substitute…' -USES-> (Equation/UNRESOLVED)   ← DROPPED, every run
```
Same fingerprint on fluids (attempt 8: `v2 = sqrt(2*g*h1)` unresolved → USES
dropped) and on growth (`growth - (10739.0/2859.5)*100`, attempts 23/24) and on
numeric substitutions (`realGDP - 543.3 * 0.19`, attempts 22/29). **Case-3 is
general, not fluid-specific.**

**Root cause.** Resolution decides node identity in isolation
(`resolver.py::_content_match`). The symbolic tier (`tiers.match_symbolic`) only
matches sign-exact equivalence under a per-problem `symbolic_mappings` table; a
*solved* form (`v2 = sqrt(2gh1)`, `realGDP = nomGDP/(PI/100)`) is **not**
symbolically equivalent to the base equation under any variable substitution — it
is the *answer*, reached via solve(). No tier resolves it; the LLM adjudicator
judged it (correctly) "not literally that equation" and left it unresolved.

**Where it dies.** `canonical.py:275` (`build_student_canonical`): an edge with
either endpoint unresolved is dropped and only counted (`dropped_edge_count`).

**The handoff's "derive-from-simplifications" fix (uncommitted) targets exactly
this** but is narrow: it only resolves a *pressure-cancelled equation node* via an
explicit `substitution` map, and the live probe showed that node is often not even
produced. It does nothing for solved/numeric forms.

---

## Problem 2 (Mode 1b) — non-equation reference nodes have no aliases

**Symptom.** Conditions, simplifications, and definitions resolve poorly, so
`SCOPES` edges (simplification/condition → equation) die at the highest rate of
any edge type (16/27 = 59%).

**Evidence.** Dropped `SCOPES` endpoints that are unresolved conditions/simps:
"open to the atmosphere", "the reservoir is wide", "P1 = P2",
"nominal and real GDP are basically the same",
"transfer payments…are included in GDP",
"consumption, investment…counted toward GDP". Also the unresolved Definition
"streamline" (attempts 1, 6).

**Root cause (code-level, confirmed).** The closed candidate set is built from the
reference solution with **`aliases=()`** for every reference node
(`candidates.py:101`, `candidates_from_reference_solution`). Equations get a
`symbolic` form for the symbolic tier; **non-equation reference nodes get nothing
but their `display_name`.** So a student condition/simplification can only resolve
by:
- `exact` (surface ≈ display, rare for free-text conditions),
- `fuzzy` ≥ 0.9 token_set_ratio against the display name (brittle for paraphrase),
- the single LLM adjudication call.

There is no alias channel for legitimate course conditions/simplifications —
**ironically, only misconceptions carry aliases** (`trigger_phrases`,
`candidates.py:133`). Non-equation resolution is therefore structurally weak, and
the handoff's equation-only symbolic fix cannot help it.

---

## Problem 3 — over-merge collapses distinct procedure steps into a `PRECEDES` self-loop

**Symptom.** In S_norm, a legitimate `ProcedureStep -PRECEDES-> ProcedureStep`
edge becomes a self-loop `proc.X -PRECEDES-> proc.X`.

**Evidence (attempt 33 S_norm reconstruction):**
```
proc.rearrange_for_real_gdp [ProcedureStep] «MERGED x2»
  evidence: 'rearrange the deflator definition to solve for real GDP'
  evidence: 'Substitute nomGDP = 543.3 and PI = 19.0 … to calculate real GDP'
edge: proc.rearrange_for_real_gdp -PRECEDES-> proc.rearrange_for_real_gdp   ← self-loop
```

**Root cause.** Two *distinct* steps (rearrange, substitute) both resolve to the
single reference proc candidate `proc.rearrange_for_real_gdp`. The resolver
**intentionally allows many student nodes → one candidate**
(`assignment.py:50-51`: "Many nodes MAY share one candidate … candidates are not
consumed") — correct for restating an *equation*, wrong for two sequential
*procedure steps*. `build_student_canonical` then merges them by `resolved_key`
into one node, and the `PRECEDES` edge between them collapses to `from_key ==
to_key`.

**Secondary defect.** This self-loop **bypasses the ontology guard** — the
self-loop check runs only at `Edge` construction (`edges.py:73`), not after the
canonical merge that manufactures it. The merge can emit a structurally-illegal
edge the parser layer would have rejected.

---

## Problem 4 — cross-type resolution (LLM type-gate violation) + a broken merge invariant

**Symptom.** A `Definition` node and a `Simplification` node resolve to the SAME
canonical key, of a single declared type.

**Evidence (attempt 33):**
```
simp.deflator_is_price_index [Definition]      evidence: 'GDP deflator'
simp.deflator_is_price_index [Simplification]  evidence: 'The price index … is the GDP deflator'
```
`simp.deflator_is_price_index` is a *simplification* candidate. The Simplification
node resolving to it is type-correct; the **Definition node resolving to it is
cross-type.**

**Root cause.** The content tiers enforce a HARD same-type gate
(`structural.py:42`, `type_compatible`: "No cross-type resolution, ever"). So the
cross-type resolution must have come through the **LLM adjudication path**
(`resolver.py:182`, `adjudicate(...)`), which does not re-apply `type_compatible`.
(Matches the "LLM adjudicator type-gate violation" noted in the macro-probe
writeup.)

**Secondary defect.** `build_student_canonical` groups merged members by key and
takes `member_nodes[0].node_type`, with the comment "All merged members share one
node type — type-compat guarantees it" (`canonical.py:248-249`). That guarantee is
**false in live data**: the merged node's type becomes whichever node-id sorts
first. A latent correctness bug independent of the LLM gate.

---

## Problem 5 (marginal) — `DEPENDS_ON` type & direction drift

`DEPENDS_ON` is the only fully-generic edge — `EDGE_ALLOWED_PAIRS` permits all 36
kind-pairs (`edges.py:43`). The LLM uses it across **9 distinct kind-pair
directions**, which lets two low-frequency defects in:

- **Type drift (2 cases):** a `DEPENDS_ON` used where a typed edge is correct —
  e.g. attempt 26 `ProcedureStep -DEPENDS_ON-> Equation` (should be `USES`).
- **Direction drift (1 pair):** the deflator eq↔def `DEPENDS_ON` appears as
  `Equation→Definition` in attempts 20/33 but `Definition→Equation` in attempt 31.
  Whichever direction disagrees with the reference DAG silently misses the match
  (exact endpoint+type match, `core.py:180`).

**These are real but together only 3 of ~43 loss events. Deprioritize.** They
matter only after Mode 1 is fixed.

---

## What is NOT a problem: the `db.schema.visualization()` "overflow"

`CALL db.schema.visualization()` returned **8 nodes, 56 relationship arcs** (36 of
them `DEPENDS_ON`) — the "messy/overflow" hairball. It is an **aggregate-schema
artifact**, not instance pollution:

- It ignores the `attempt_id` property and unions **all 33 attempts** into one
  picture.
- The universal `:_KGNode` label means every node also presents as `:_KGNode`, so
  one logical edge pattern is drawn as ~4 arcs (source∈{_KGNode,kind} ×
  target∈{_KGNode,kind}).
- The generic `DEPENDS_ON` (9 real kind-pairs) × that ~4 shadow ≈ the 36 arcs.

**Per-attempt instance graphs are clean and sparse** (1–7 edges, edge/node ratio
~0.5–1.0, textbook shapes). To see the real graph for one attempt, use
`MATCH p=(:_KGNode {attempt_id: N})-[]->(:_KGNode {attempt_id: N}) RETURN p`, NOT
the schema view.

---

## Pipeline order — where each loss happens

`apollo/handlers/done_grading.py::run_graph_simulation`, in order:

1. **KG built** (parser → Neo4j). Minor cleanse: store rejects type-invalid edges.
2. `validate_student_graph` — a 422 gate, not a transform.
3. **`resolve_attempt`** (`resolution/resolver.py`) — cleanser #1 (identity). Emits
   `unresolved` for Problems 1a/1b; emits cross-type via LLM path (Problem 4);
   permits many-to-one (Problem 3).
4. `write_resolution` — persists `resolution`/`resolved_key` onto each `:_KGNode`.
5. **`build_student_canonical`** (`graph_compare/canonical.py`) — cleanser #2
   (structure). **Drops unresolved-endpoint edges (Problems 1a/1b)**; merges by key
   (Problems 3/4 manifest here as self-loop + arbitrary-type node).
6. **`grade_attempt`** (`graph_compare/core.py`) — scores run last over S_norm vs
   R_norm; edge match is exact `(type, from_key, to_key)` (`core.py:180`).
7. audit → abstention → persist → rubric/calibration.

**The scorer (step 6) is blameless.** All loss is in steps 3 and 5.

---

## Impact on the Direction A vs B decision

- **Direction A** (resolve derived equation forms → governing entity) addresses
  **1a only (31/48 incidents)**. It does nothing for **1b (17)** or Problems 3/4.
- **Direction B** (keep unresolved nodes first-class + a tolerant per-edge matcher)
  structurally covers **1a + 1b** — the edge survives regardless of which endpoint
  failed, and a matching layer decides credit. It still does not, by itself, fix
  over-merge (Problem 3) or the LLM type gate (Problem 4); those are separate
  resolver fixes.
- **The problem is resolution end-to-end.** "A vs B" is really "make the
  resolution→matching boundary tolerant"; B is the framing that does not leave the
  1b bucket on the floor.

---

## File / function reference map

| Concern | Location |
|---|---|
| Edge taxonomy + `EDGE_ALLOWED_PAIRS` (generic `DEPENDS_ON`) | `apollo/ontology/edges.py` |
| Node taxonomy + dual-label model | `apollo/ontology/nodes.py` |
| Resolver orchestration (tiers → assignment → 1 LLM) | `apollo/resolution/resolver.py` |
| Many-to-one assignment (Problem 3) | `apollo/resolution/assignment.py:50` |
| HARD same-type gate (Problem 4 bypassed by LLM path) | `apollo/resolution/structural.py:42` |
| Closed candidate set; reference `aliases=()` (Problem 2) | `apollo/resolution/candidates.py:101` |
| Symbolic tier (equation-only) | `apollo/resolution/tiers.py` |
| S_norm build: edge drop + merge-by-key (Problems 1/3/4) | `apollo/graph_compare/canonical.py:215` |
| Grading core; exact edge match (Problem 5) | `apollo/graph_compare/core.py:174` |
| Owner doc | `docs/architecture/apollo.md` |

---

## Appendix — read-only reproduction (Cypher)

S_norm is **not persisted**; it is a pure function of the persisted graph. These
mirror `build_student_canonical` (change `33` to any attempt):

```cypher
// S_norm NODES — resolved nodes merged by key (merged_count>1 = a merge)
MATCH (n:_KGNode {attempt_id: 33})
WHERE n.resolution = 'resolved' AND n.resolved_key IS NOT NULL
RETURN n.resolved_key AS canonical_key,
       head([l IN labels(n) WHERE l <> '_KGNode']) AS node_type,
       count(*) AS merged_count,
       collect(coalesce(n.symbolic,n.applies_when,n.concept,n.term,n.action,n.label)) AS evidence
ORDER BY canonical_key;
```
```cypher
// S_norm EDGES — both endpoints resolved, normalized to canonical keys
MATCH (a:_KGNode {attempt_id: 33})-[e]->(b:_KGNode {attempt_id: 33})
WHERE a.resolved_key IS NOT NULL AND b.resolved_key IS NOT NULL
RETURN a.resolved_key AS from_key, type(e) AS edge_type, b.resolved_key AS to_key;
```
```cypher
// DROPPED edges — the loss that never reaches scoring (dropped_edge_count)
MATCH (a:_KGNode {attempt_id: 33})-[e]->(b:_KGNode {attempt_id: 33})
WHERE a.resolved_key IS NULL OR b.resolved_key IS NULL
RETURN type(e) AS dropped_edge_type,
       coalesce(a.resolved_key,'«'+a.symbolic+'»') AS from_,
       coalesce(b.resolved_key,'«'+coalesce(b.symbolic,b.applies_when,b.action)+'»') AS to_;
```
```cypher
// The REAL instance graph for one attempt (use this, NOT db.schema.visualization)
MATCH p=(:_KGNode {attempt_id: 33})-[]->(:_KGNode {attempt_id: 33}) RETURN p;
```
