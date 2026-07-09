# Bug: authored-set tag/mint dedup fuses distinct entities, binds foreign-set entities, and persists graph cycles

**Component:** `apollo/provisioning/dedup.py` (+ `tag_mint.py`, `tag_mint_persist.py`, `learner_model_seed.py`)
**Found:** 2026-06-30, read-only audit of `apollo_authored_sets id=4` (AAE 333 HW1) on **staging** (`hjevtxdtrkxjcaaexdxt`).
**Severity:** High — silently corrupts the per-concept reference KG graph (nodes + prereq edges) that backs grading (`:Canon`) and personalization. Not student-visible, not crash-causing, and invisible to `status=done`, so it persists unnoticed.

> Scope note: students are NOT served this graph (only `payload.problem_text/given_values/target_unknown`), and the consumers tolerate the damage (concept-scoped, single-hop prereq reads; node-only `:Canon`). The harm is **grading fidelity** and any future multi-hop sequencing, plus a corrupted source-of-truth reference graph.

---

## Summary

`tag_and_mint` converts each `reference_solution` step into an `apollo_kg_entities` row keyed `(concept_id, canonical_key)`, where `canonical_key = "{prefix}.{node_id}"` (e.g. `varmap.vm_M`, `eq.eq_pressure_B`, `proc.proc_overall`). Before insert, each candidate runs a dedup ladder (`resolve_candidate`) whose candidate pool is **course-scoped** (filtered on `Subject.search_space_id` only — never `concept_id`) and whose merge tiers are **slug-exact** and **embedding cosine ≥ 0.92** over a `scope_summary` that is essentially `"Display Name | kind <kind>"`. The `llm_judge` middle band (0.82–0.92) is **hardwired to return `distinct`**.

Three failure modes result, all confirmed against `apollo_dedup_decisions` for set 4:

1. **Bug #2 — False merges of semantically distinct entities.** Generic node ids + a weak `scope_summary` make cosine ≥ 0.92 (and slug-exact) fuse unrelated quantities.
2. **Bug #4 — Foreign-set / cross-concept binding.** Course-scoped resolution reuses entities from *other concepts and earlier (even deleted) sets* in the same subject; the recent "drop unminted-key prereq edges" fix does NOT catch these because a merged key is still "resolvable."
3. **Bug #3 — Persisted graph cycles.** There is no acyclicity guard at mint, so LLM-drafted (and merge-induced) reverse edges create cycles in `apollo_entity_prereqs`.

---

## Bug #2 — Dedup false-merges distinct entities

### Evidence (`apollo_dedup_decisions`, concepts 47–53)

Semantically WRONG merges (distinct physics collapsed to one entity):

| concept | candidate_key | merged into entity | matched concept / key | method | sim | what got fused |
|---|---|---|---|---|---|---|
| 49 | `varmap.vm_m` | 751 | 49 `varmap.vm_M` | embedding | **1.000** | hanging mass *m* (0.1 kg) ≡ block mass *M* (2 kg) |
| 52 | `varmap.vm_m` | 751 | 49 `varmap.vm_M` | embedding | **1.000** | same |
| 49 | `varmap.vm_h` | 741 | 48 `varmap.vm_h0` | embedding | 0.937 | oil-film thickness *h* ≡ mug rest depth *h₀* |
| 52 | `varmap.vm_h` | 741 | 48 `varmap.vm_h0` | embedding | 0.937 | same |
| 51 | `eq.eq_pressure_B` | 771 | 51 `eq.eq_pressure_A` | embedding | 0.951 | gate fluid-B pressure ≡ fluid-A pressure |
| 53 | `varmap.vm_p2` | 797 | 53 `varmap.vm_p1` | embedding | 0.941 | box-2 (atmospheric) ≡ box-1 (vacuum) |
| 53 | `def.def_given_p2` | 799 | 53 `def.def_given_p1` | embedding | 0.928 | box-2 given ≡ box-1 given |

The `m ≡ M` fusion at cosine **1.000** is the canonical example: `scope_summary` for both is `"Vm M | kind variable"` vs `"Vm m | kind variable"`, which embeds identically.

> Legitimate-looking sibling merges also occur (mug parts 48↔50 share `vm_R/eq_free_surface`; oil-block parts 49↔52 share `vm_l/eq_tension`). These may be desirable, but they are produced by the *same* unguarded mechanism — there is no signal distinguishing "same physical quantity in a sibling part" from "different quantity that happens to embed similarly."

### Root cause (code)

- Pool is course-scoped, not concept-scoped: `apollo/provisioning/dedup.py:101-110` (`_in_course_entities` filters `Subject.search_space_id` only; `concept_id` is recorded on the audit row but never used to restrict candidates).
- Generic, non-namespaced key: `apollo/persistence/learner_model_seed.py:206` (`canonical_key = f"{prefix}.{node_id}"` with raw scrape node ids).
- Weak discriminator: `scope_summary` is `"{display_name} | kind {kind}"` (see `tag_mint.py:163-172`), so embeddings of distinct variables/equations are near-identical.
- Merge thresholds: slug-exact tier `dedup.py:176-194`; embedding cosine ≥ 0.92 tier `dedup.py:220-247`.
- Middle band does not judge: `apollo/provisioning/tag_mint.py:108-113` (`_judge_distinct` always returns `distinct`) — confirmed by data: every `llm_judge` row in set 4 has `verdict='distinct'`.

### Impact

- The reference graph loses distinct nodes (`m`, the second pressures, the box-2 quantities) — grading via `:Canon` has no node to match a student's correct reasoning against.
- Prereq edges to a merged step are rewired onto the merge target (this is what creates the 751↔755 cycle below).

### Fix directions (pick per design review)

- Scope the dedup pool to the current set (or current concept) by default; only widen to course scope behind an explicit "shared vocabulary" decision.
- Namespace `canonical_key` with the concept/set (e.g. include `concept_id` or a problem-local prefix) so `vm_M` in problem 1 ≠ `vm_M` in problem 5.
- Enrich `scope_summary` with the step's symbolic/meaning content so embeddings actually discriminate `m` from `M`, `p1` from `p2`, `pressure_A` from `pressure_B`.
- Raise the cosine threshold and/or make the `llm_judge` band actually call the model instead of hardwiring `distinct`.

---

## Bug #4 — Foreign-set / cross-concept entity binding survives the edge-drop fix

### Evidence (`apollo_dedup_decisions`, concepts 47–53)

Merges into entities owned by **other concepts**, including concepts 40/41 from the **earlier 2026-06-26 set** in the same subject (concept 41 still alive; the whole earlier set was never deleted):

| concept | candidate_key | merged into entity | owner concept | method |
|---|---|---|---|---|
| 51 | `proc.proc_overall` | 672 | **41** (drag-force-ratio, 06-26) | slug |
| 50 | `proc.proc_1` | 659 | **40** (duct-flow, 06-26) | slug |
| 50 | `proc.proc_2` | 659 | **40** | embedding 0.927 |
| 50 | `proc.proc_3` | 660 | **40** | slug |
| 52 | `proc.proc_1` | 659 | **40** | slug |
| 52 | `proc.proc_2` | 659 | **40** | embedding 0.927 |
| 52 | `proc.proc_3` | 660 | **40** | slug |
| 52 | `proc.proc_4` | 661 | **40** | slug |

Confirmed downstream effect in `apollo_entity_prereqs`: every concept-51 prereq edge naming `proc_overall` points at entity **672 (concept 41)**. Personalization filters these out at read time (`personalization_read.py:148-150` ANDs both endpoints against this concept's entity ids), so they are dead weight in the source-of-truth table; and the merged-target entities (concept 40/41) are **not** in this search-space's `:Canon`, so concept 50/52's procedure steps have no reference node for grading at all.

### Root cause (code)

- Edge endpoints resolve through `key_to_id`, which for a merged step holds the (possibly foreign) `matched_entity_id`: `tag_mint.py:281`, inserted by `tag_mint_persist.py:216-218` with no concept-scope check.
- The recent fix "drop prereq edges naming unminted keys" (`tag_mint.py:333-347`, logs `tag_mint_dropped_unresolvable_prereqs`; deployed — confirmed the running commit includes `0490cca`) only drops edges whose endpoint key is **absent** from `key_to_id`. A key that **merged to a foreign id is present**, so the foreign edge is inserted, not dropped. The drop is also silent (info-log only) with no floor asserting any edges survived.

### Fix directions

- Validate edge endpoints belong to the current set's concepts before insert (the reader already enforces concept scope — the writer should too). Fail-closed or drop with a surfaced warning, not silently.
- Same root namespace/scoping fix as Bug #2 removes the foreign collisions at the source.

---

## Bug #3 — No acyclicity guard at mint; cycle persisted

### Evidence

`apollo_entity_prereqs` contains a 2-cycle **751 ↔ 755** (`varmap.vm_M` ↔ `eq.eq_tension`, concept 49). It is a direct consequence of Bug #2: `eq_tension` depends on the hanging mass *m*, but *m* merged into *M* (751), while *M*-as-given points at `eq_tension` (755) — yielding both directed edges.

### Root cause (code)

- `insert_prereqs` only de-dups identical *directed* edges; the composite PK `(from_entity_id, to_entity_id)` permits both (A,B) and (B,A): `tag_mint_persist.py:205-232`, `apollo/persistence/models.py:451-466`.
- Edges come straight from the LLM tag draft, filtered only for endpoint resolvability: `tag_mint.py:321,333-337`.
- The only topological check in the subsystem (promotion gate 3, `promotion_lint.py:350-355`) runs over the per-problem reference-solution `KGGraph` (`promotion_lint.py:556-560`), **not** over `apollo_entity_prereqs`. And `insert_prereqs` runs *before* `promote` anyway (`orchestrator.py:308` then `:329`).

### Fix directions

- Add a cycle check (or a topological-sort assertion) over the candidate `apollo_entity_prereqs` set inside `tag_and_mint`, before insert; drop/repair reverse edges and surface them.
- Fixing Bug #2 (no `m≡M` fusion) removes this particular cycle, but the guard should exist independently.

---

## How to reproduce / verify (staging, read-only)

Supabase (staging `hjevtxdtrkxjcaaexdxt`):

```sql
-- Bug #2/#4: every merge decision for the set, with what it merged into
select d.concept_id, d.candidate_key, d.method, round(d.similarity::numeric,3) sim,
       d.verdict, d.matched_entity_id, me.concept_id as matched_concept, me.canonical_key as matched_key
from apollo_dedup_decisions d
left join apollo_kg_entities me on me.id = d.matched_entity_id
where d.concept_id between 47 and 53 and d.verdict = 'merged'
order by d.concept_id, d.candidate_key;

-- Bug #4: prereq edges from this set that point into a foreign concept
select ef.concept_id from_concept, et.concept_id to_concept, et.id to_entity, et.canonical_key
from apollo_entity_prereqs p
join apollo_kg_entities ef on ef.id=p.from_entity_id
join apollo_kg_entities et on et.id=p.to_entity_id
where ef.concept_id between 47 and 53 and (et.concept_id < 47 or et.concept_id > 53);

-- Bug #3: any 2-cycle
select a.from_entity_id, a.to_entity_id
from apollo_entity_prereqs a
join apollo_entity_prereqs b on a.from_entity_id=b.to_entity_id and a.to_entity_id=b.from_entity_id
where a.from_entity_id < a.to_entity_id;
```

Neo4j Aura (staging `791f9ced`) — needs `SSL_CERT_FILE=$(python3 -m certifi)`; creds in Railway `ai-ta-backend` staging vars:

```cypher
// node-only projection; live set is clean, but stale orphans from deleted concept 45 remain
MATCH (c:Canon) RETURN c.concept_id AS concept_id, count(*) AS n ORDER BY concept_id;
```

---

## Related (not in this writeup, but found in the same audit)

- `apollo_concept_problems.solution_source` is hardcoded `"generated"` for all 7 (should be `"extracted"`): `promote.py:55,205-206`, `scrape.py:428`, `orchestrator.py:329`.
- `delete_authored_set` leaves `:Canon` nodes orphaned (16 stale nodes for deleted concept 45): `apollo/provisioning/authored_sets/api.py:274-276`.
- Student-visible `problem_text` OCR garbles and a dropped solution page (doc 13 page 8); OCR-confidence gate (0.6) too lax to catch at 0.95.
- One pipeline-introduced math error (cp242 dropped minus sign in the slope→acceleration relation).
