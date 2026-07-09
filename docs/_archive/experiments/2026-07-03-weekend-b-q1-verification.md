# Q1 Re-verification — Misconception detection (lane B4/Q1)

**Stack:** staging @ 2883ff7 (G3 shadow isolation, D1 empty-bank fix #85, G4 fixes,
resolution parser #87). Backend :8000 (`campaign_launch:app`, shadow mode
`APOLLO_GRAPH_GRADER_LIVE=0`, NLI prewarmed). Supabase 57322 (fluid = ss=1,
concept bernoulli_principle = concept_id 1, bank n=2). Neo4j 57687.

**Verdict: STILL SILENT — new finding.** Misconception detection is broken by a
key-prefix mismatch in the candidate-assembly seam, independent of resolution
recall. S5 precision is undefined (0 assertions / 3 attempts), same as the F1b/F1c
baseline — but the cause is now pinned with file:line evidence.

---

## 1. Personas run (live, real session flow)

Driver: `campaign/out/q1verify/run_q1.py` (untracked; reuses the exact seams from
the committed f1c driver — `HttpxApolloClient`, `default_chat_fn`,
`SqlArtifactReader`, `mint_student_token`, `run_attempt`). One fresh student per
persona. Real OpenAI (gpt-4o), real HTTP, real DB read-back.

All three fluid `misconception`-archetype personas expect `misc.density_ignored`
and assert the bank trigger phrase "ignore density" verbatim:

| persona (problem_id) | expected misconception | trigger asserted |
|---|---|---|
| bernoulli_full_find_p2 | misc.density_ignored | "ignore density" (verbatim) |
| bernoulli_horizontal_pipe_find_p2 | misc.density_ignored | "ignore density" (verbatim) |
| continuity_area_change_find_v2 | misc.density_ignored | "ignore density" (verbatim) |

## 2. Bank rows verified present (local DB)

`apollo_misconceptions`, concept_id=1:
- id=1 code=`pressure_velocity_same_direction` triggers `["pressure goes up when it speeds up", ...]`
- id=2 code=`density_ignored` triggers `["ignore density", "density doesn't matter", "leave out rho"]`

Bank IS seeded. `watch_out_status=checked` on every served scorecard confirms the
D1 empty-bank path is NOT taken (soundness_applicable=True).

## 3. Per-persona results

| persona | status | matched | served watch_out | watch_out_status | canonical.misc | pair.misc |
|---|---|---|---|---|---|---|
| bernoulli_full_find_p2 (attempt 34) | ok | True | `[]` | checked | `[]` | `[]` |
| bernoulli_horizontal_pipe_find_p2 (attempt 36) | ok | True | `[]` | checked | `[]` | `[]` |
| continuity_area_change_find_v2 (attempt 39) | ok | True | `[]` | checked | `[]` | `[]` |

Baseline (pre-fix, `campaign/out/b0smoke/attempts.jsonl`): identical — empty
watch_out AND empty misconceptions on both artifacts. No change.

**Key observation:** the students DID assert the misconception, and it reached the
graph as a student node — but landed **unresolved**, not as a contradiction:
- attempt 34 node `stu_4f9a67235b19` span: `"density remains the same for an incompressible fluid ignore …"` → status **unresolved**
- attempt 36 node `stu_56c4f1d901a7` span: `"density of the fluid is negligible ignore the density of the…"` → status **unresolved**

Both pair (graph) artifacts abstained with reason `unresolved_rate_above_threshold`
(broad resolution-recall weakness on this stack — the known "iter-2 equivalence
tier not on staging" gap), but that is a *symptom*, not the misconception break.

## 4. The break — file:line

Even with perfect resolution recall, a misconception can NEVER be recorded as a
contradiction on this stack. Two coupled defects at ONE line:

**`apollo/clarification/candidate_assembly.py:39`**
```python
def _misconceptions_dict(entries: list) -> dict:
    return {"misconceptions": [
        {"key": e.code, ...}          # <-- e.code = "density_ignored" (NO "misc." prefix)
        ...
    ]}
```

- The bank seed strips the prefix on the way IN
  (`apollo/persistence/misconception_bank_seed.py:79`: `code = entry["key"].removeprefix("misc.")`),
  so the DB `code` column is `density_ignored`.
- `_misconceptions_dict` reads `e.code` back **without re-prefixing**, so
  `candidates_from_misconceptions` (`apollo/resolution/candidates.py:189`) builds a
  `Candidate(canonical_key="density_ignored", is_misconception=True)`.
- Contradiction detection keys on the **key prefix**, not the `is_misconception`
  flag: `apollo/graph_compare/core.py:41-49`
  ```python
  MISCONCEPTION_KEY_PREFIX = "misc."
  def is_misconception_key(key): return key.startswith(MISCONCEPTION_KEY_PREFIX)
  def contradiction_nodes(student): return tuple(n for n in student.nodes if is_misconception_key(n.canonical_key))
  ```
  `is_misconception_key("density_ignored")` = **False**. So a resolved node with
  key `density_ignored` is routed to `unsupported_extra` (core.py:167-171), never
  to `contradiction_finding` (core.py:161-164). No CONTRADICTION finding →
  `build_misconceptions` (`apollo/grading/artifact_build.py:227-251`) returns `[]`
  → scorecard `watch_out` empty.

**Decisive repro** (assembled from the live DB bank, no resolver involved):
```
_misconceptions_dict keys: ['pressure_velocity_same_direction', 'density_ignored']
candidate canonical_key='density_ignored' is_misconception_flag=True canon_key=-1
    aliases=('ignore density', ...) -> is_misconception_key()=False
```

**Secondary coupling:** the KG holds `misc.density_ignored`
(`apollo_kg_entities.canonical_key`), so the unprefixed candidate also misses the
canon lookup (`canon_key_by_canonical_key.get("density_ignored", -1)` → **-1**),
disconnecting the misconception candidate from its KG node (degrades
opposes/competition/canon projection too).

**Blast radius:** both consumers of this seam are affected —
`done_grading.py:363` (`load_problem_candidates_with_soundness`, grading path) and
`chat.py:365` (`load_problem_candidates`, clarification/probe path). Every
misconception in the course is undetectable end-to-end.

## 5. Independent structural note (shadow mode)

Even after the prefix fix, the SERVED scorecard would still show empty
`watch_out` while `APOLLO_GRAPH_GRADER_LIVE=0`: `served_grade=llm_fallback`, so the
**canonical** artifact is the LLM payload, which hard-codes `"misconceptions": []`
(`apollo/grading/artifact_build.py:471`, comment "the LLM path detects none"), and
`render_scorecard` templates over the canonical payload
(`apollo/handlers/done.py:664`). Misconception detection lives only on the graph
grader (the **pair** artifact). So the campaign's S5 metric must read the **pair**
artifact's `misconceptions[]`, not the served scorecard, while shadow mode is on —
and the prefix fix is what makes that pair list non-empty in the first place.

## Fix direction (NOT implemented — diagnosis only)

Re-prefix the code back to the minted key in `_misconceptions_dict`:
`"key": f"{MISCONCEPTION_KEY_PREFIX}{e.code}"` (mirror-inverse of the seed's
`removeprefix`). One line; restores both the contradiction-prefix match and the
KG canon_key lookup. No resolver change needed for detection to start firing on
verbatim-trigger personas (resolution-recall abstention is a separate, known gap).

## Artifacts
- Driver: `/Users/ishaanbatra/Documents/GitHub/ai-ta-backend/campaign/out/q1verify/run_q1.py`
- Raw attempts: `/Users/ishaanbatra/Documents/GitHub/ai-ta-backend/campaign/out/q1verify/attempts.jsonl`
