# Apollo Misconception Detector — Corroboration & Keying Redesign: TDD Implementation Plan

**Date:** 2026-07-08
**Spec (source of truth):** `docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md`
**Predecessor plan (A1/A4/A5/A6/A8 contracts, section numbering):** `docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md`
**Owner doc to reconcile on landing:** `docs/architecture/apollo.md` (`owns: apollo/overseer/misconception_detector/**`)
**Repo:** `ai-ta-backend`
**Flag:** `APOLLO_MISCONCEPTION_DETECTOR` (default OFF; flag-OFF must stay byte-identical)

---

## 0. Branch, base, and setup

**Base branch:** `feat/apollo-misconception-detector` — VERIFIED present locally and
currently checked out (`git branch --list` returns it; HEAD `6d3e3f1 docs: apollo
misconception detector handoff`). This work **builds on the unmerged detector**, so
cut the work branch off it, NOT off `staging` directly.

```bash
cd ai-ta-backend
git checkout feat/apollo-misconception-detector
git pull --ff-only            # only if a remote tracking branch exists; skip if local-only
git checkout -b feat/apollo-judge-authoritative-gate
```

**New work branch:** `feat/apollo-judge-authoritative-gate`

**Fallback (only if the base were absent):** the base IS present, so no fallback is
needed. Were it ever missing, cut off `origin/staging` and cherry-pick the three
detector commits `479a104` (feat: parallel misconception detector), `d411dfb`
(fix: structured-outputs + tolerant parse), `6d3e3f1` (docs handoff) — but this is
NOT the path here; use the real base.

**Tooling verified:** `origin/staging` exists (`origin/HEAD -> origin/staging`);
`diff_cover` importable. The coverage compare base is `origin/staging` throughout.

**Baseline regression gate (record before touching code):** `pytest apollo/`
baseline was **2562 passed, 14 skipped**. No task may reduce the passing count or
add an unexplained skip.

**Test file map (all edits land in existing files; no new test modules needed
except where noted):**
- `apollo/overseer/tests/test_misconception_detector_types.py` (140 lines)
- `apollo/overseer/tests/test_misconception_detector_config.py` (115 lines)
- `apollo/overseer/tests/test_misconception_detector_judge.py` (719 lines)
- `apollo/overseer/tests/test_misconception_detector_gate.py` (256 lines — **largest rewrite**)
- `apollo/overseer/tests/test_misconception_detector_merge.py` (308 lines)
- `apollo/overseer/tests/test_misconception_detector_bank_pattern.py` (585 lines)
- `apollo/handlers/tests/test_misconception_flag_off_golden.py` (golden — must pass unchanged)
- `apollo/handlers/tests/test_misconception_ledger_feed.py` (emergent feed)

---

## 1. Ordering rationale (dependency order — DO NOT reorder)

The spec is explicit that the type change is the foundation and the gate is the
heart. The dependency chain (types → config → judge → gate → merge → wiring →
validation) is mandatory because:

- `gate.py` and `merge.py` read the three new `ConceptFinding` fields
  (`bank_code`, `bank_match_above_floor`, `ceiling_eligible`) — those must exist
  first (Task 1).
- `gate.py`'s solo-dock path reads `TAU_SOLO_JUDGE` — the constant must exist
  first (Task 2).
- The gate's `bank_by_code` index and co-key logic consume `bank_code` set by
  BOTH `judge.py` (validated) and `bank_pattern.py` (from the matched entry) — so
  judge keying (Task 3) and bank below-floor emission (Task 5) both feed the gate
  (Task 4). Judge is ordered before the gate; bank_pattern's output-shape change
  is small and independent, placed as Task 5 (after gate) because the gate tests
  can synthesize `bank_pattern` findings directly without the real emission path.
- `merge.py`'s one-line `ceiling_eligible` predicate (Task 6) depends on the gate
  stamping that bit (Task 4).

Each task is RED (failing test first) → GREEN (minimal impl) → VERIFY (exact
command). Tasks are individually committable and individually green.

---

## Task 1 — `types.py`: three new frozen fields (decouple docked-ness from bank_corroborated)

**Files:** `apollo/overseer/misconception_detector/types.py`,
`apollo/overseer/tests/test_misconception_detector_types.py`

**Spec refs:** §4.1, §4.6. Amendments A10 (`bank_match_above_floor`), A11
(`bank_code`), A12 (`ceiling_eligible`).

This is FIRST because every downstream module constructs / reads these fields. The
core decoupling: today "keyed" is only expressible via `signature` string-parsing
and "corroborated" conflates bank-agreement with dock-eligibility. The three
fields make each concern explicit and immutable.

### RED
Add to `test_misconception_detector_types.py`:

1. `test_conceptfinding_new_fields_default` — construct a `ConceptFinding` with
   ONLY the pre-existing required args; assert `bank_code is None`,
   `bank_match_above_floor is True`, `ceiling_eligible is False`. (Guards that
   every existing tier constructor still compiles — the defaults are load-bearing.)
2. `test_bank_code_signature_invariant_keyed` — a finding with
   `bank_code="includes_transfers"` and `signature="misc.includes_transfers"`
   satisfies the invariant `bank_code is not None ⟺ signature == f"misc.{bank_code}"`.
   Assert the derived equality holds. (No runtime enforcement is added — this is a
   documentation/consistency test, per §4.1.)
3. `test_bank_code_none_unkeyed_signature` — `bank_code=None`,
   `signature="unkeyed:node.x"`; invariant holds (LHS False, so no constraint).
4. `test_new_fields_frozen_mutation_raises` — attempting to set
   `finding.ceiling_eligible = True` raises `FrozenInstanceError`.

Run — MUST fail (fields don't exist yet):
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_types.py -x -q
```

### GREEN
In `types.py`, add three defaulted fields to `ConceptFinding` AFTER
`verdict_token_prob_present` (keep it last-of-old so positional constructors are
unaffected):

```python
    bank_code: str | None = None            # A11: validated bank code or None
    bank_match_above_floor: bool = True     # A10: bank_pattern best-match >= floor
    ceiling_eligible: bool = False          # A12: may this dock trip the band ceiling
```

Add the docstrings verbatim from spec §4.1 / §4.6 (the invariant line and the
"drives lone-judge dock-eligibility" / "merge reads THIS not centrality-plus-source"
rationale). Do NOT add any validation logic — defaults + frozen only.

### VERIFY
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_types.py -q
```
Also run the whole detector suite to prove no existing constructor broke:
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/ -q -k misconception
```

**Commit:** `feat(apollo): ConceptFinding gains bank_code/bank_match_above_floor/ceiling_eligible (A10-A12)`

---

## Task 2 — `config.py`: `TAU_SOLO_JUDGE` constant

**Files:** `apollo/overseer/misconception_detector/config.py`,
`apollo/overseer/tests/test_misconception_detector_config.py`

**Spec refs:** §4.2, A9. `BANK_SIM_FLOOR` docstring narrowing only (value unchanged).

### RED
Add to `test_misconception_detector_config.py`:

1. `test_tau_solo_judge_default` — import `TAU_SOLO_JUDGE`; assert `== 0.90`.
2. `test_tau_solo_judge_env_override` — `monkeypatch.setenv("APOLLO_MISC_TAU_SOLO_JUDGE", "0.95")`,
   reimport the module (`importlib.reload`), assert `0.95`. (Mirror the existing
   env-override test pattern already in this file for `TAU_FIRE`.)
3. `test_tau_solo_judge_malformed_env_falls_back` — set the env var to `"notanum"`,
   reload, assert falls back to `0.90`.
4. `test_tau_solo_judge_at_least_tau_fire` — assert `TAU_SOLO_JUDGE >= TAU_FIRE`
   (the "solo dock is strictly harder" invariant, spec §5 note).
5. `test_bank_sim_floor_unchanged` — assert `BANK_SIM_FLOOR == 0.80` (regression
   guard that the docstring narrowing did not touch the value).

Run — MUST fail (import error on `TAU_SOLO_JUDGE`):
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_config.py -x -q
```

### GREEN
In `config.py`, after `BANK_SIM_FLOOR`:
```python
# A9: minimum routed-tau confidence for a LONE bank-keyed judge finding to dock
# on its own (no second tier). Deliberately >= TAU_FIRE so a solo dock is strictly
# harder than a corroborated one. Pre-calibration default 0.90; MUST be tuned on
# the labeled 20-set before any env flip (R2). Applies ON TOP of the A1 routed-tau
# check (the finding must clear BOTH its routed tau AND this).
TAU_SOLO_JUDGE: float = _float_env("APOLLO_MISC_TAU_SOLO_JUDGE", 0.90)
```
Update the `BANK_SIM_FLOOR` docstring in the module header to say it now gates
ONLY whether a `bank_pattern` match is a *standalone* finding, not whether it may
corroborate (L4). Value stays 0.80.

### VERIFY
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_config.py -q
```

**Commit:** `feat(apollo): add TAU_SOLO_JUDGE; narrow BANK_SIM_FLOOR to standalone-only (A9)`

---

## Task 3 — `judge.py`: name a `misconception_code`, validate against the concept bank, key the signature

**Files:** `apollo/overseer/misconception_detector/judge.py`,
`apollo/overseer/tests/test_misconception_detector_judge.py`

**Spec refs:** §4.3, §6, A11. The judge is the only tier firing on real data;
keying it to `misc.<code>` is what lets rows 3/4/5 emit keyed ledger rows.

### RED
Add to `test_misconception_detector_judge.py`. The stub `_RecordingJudgeFn` and
`_concept` helper already exist — extend `_concept` to accept `bank_entries` (build
one or two `MisconceptionEntry` rows with real `code` values). New tests:

1. `test_judge_names_valid_code_keys_signature` — concept with
   `bank_entries=(MisconceptionEntry(code="includes_transfers", ...),)`; stub row
   returns `"misconception_code": "includes_transfers"`,
   `"verdict": "misconception"`. Assert the resulting finding has
   `bank_code == "includes_transfers"` AND `signature == "misc.includes_transfers"`.
2. `test_judge_empty_code_stays_unkeyed` — stub row `"misconception_code": ""`.
   Assert `bank_code is None`, `signature == "unkeyed:<concept_key>"`.
3. `test_judge_hallucinated_code_rejected_unkeyed` — bank has only
   `includes_transfers`; stub row names `"misconception_code": "totally_made_up"`.
   Assert `bank_code is None`, `signature == "unkeyed:<concept_key>"` (never trust
   an unvalidated code, A11).
4. `test_judge_cross_concept_code_rejected` — two concepts A and B; A's bank has
   `code_a`, B's bank has `code_b`. Stub returns concept A's row naming `code_b`.
   Assert A's finding is unkeyed (code valid for a DIFFERENT concept is rejected on
   this one, §6).
5. `test_judge_missing_code_field_soft_fails_unkeyed` — stub row omits
   `misconception_code` entirely (simulates a future model dropping it under the
   tolerant fallback path). Assert no crash, `bank_code is None`, unkeyed.
6. `test_judge_all_clear_soft_fail_names_no_code` — malformed JSON → `_all_clear`;
   assert every finding has `bank_code is None`, unkeyed signature (a soft-fail
   names no code).
7. `test_judge_schema_includes_misconception_code` — assert
   `_JSON_SCHEMA["schema"]["properties"]["concepts"]["items"]["properties"]` has a
   `"misconception_code"` of `{"type": "string"}` AND it is in the row's `required`
   list (strict-mode requires every field required).

Run — MUST fail:
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_judge.py -x -q
```

### GREEN
Four edits in `judge.py`:

1. `_JSON_SCHEMA`: add `"misconception_code": {"type": "string"}` to the item
   `properties`; append `"misconception_code"` to the item `required` list. (The
   spec confirms Structured Outputs `strict:true` requires every field in
   `required`; the model emits `""` when no code applies.)
2. `_SYSTEM_PROMPT`: append the spec §4.3 instruction verbatim ("If — and only if
   — the student's belief matches one of the `known_misconceptions` you were given
   for this concept, put that misconception's `code` verbatim in
   `misconception_code`. Otherwise put an empty string. Never invent a code that
   was not in the `known_misconceptions` list.").
3. `judge_concepts`: signature UNCHANGED. In the final comprehension, pass the
   per-concept input into `_finding_from_row` so it can read `c.bank_entries`.
   Change the call from `_finding_from_row(c.concept_key, rows_by_key.get(...), verdict_token_prob=...)`
   to also pass `concept_input=c` (add a keyword param below).
4. `_finding_from_row`: add a `concept_input: JudgeConceptInput | None = None`
   keyword param (defaulted None so `_all_clear` / any existing caller is
   unaffected). In the `row is not None` branch, after computing verdict/confidence:
   ```python
   code = (row.get("misconception_code") or "").strip()
   allowed = {e.code for e in concept_input.bank_entries} if concept_input else set()
   if code and code in allowed:
       bank_code, signature = code, f"misc.{code}"
   else:
       bank_code, signature = None, f"unkeyed:{concept_key}"
   ```
   Set `bank_code=bank_code`, `signature=signature` on the returned `ConceptFinding`.
   The `row is None` branch and `_all_clear` keep `signature=f"unkeyed:..."`,
   `bank_code=None` (unchanged; a soft-fail names no code).

`_normalize_rows`, the logprob walk, and `make_openai_judge`'s live call are
UNCHANGED (the live `client.chat.completions.create` stays the sole coverage
exemption via its existing `# pragma: no cover`).

### VERIFY
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_judge.py -q
```

**Commit:** `feat(apollo): judge names+validates misconception_code -> misc.<code> keying (A11)`

---

## Task 4 — `gate.py`: lone-judge dock, three bands, cross-namespace co-key, drop lone bank (THE HEART)

**Files:** `apollo/overseer/misconception_detector/gate.py`,
`apollo/overseer/tests/test_misconception_detector_gate.py`

**Spec refs:** §4.0 (A13 cross-namespace), §4.4 (truth-table), §4.6 / §4.6.1
(`dataclasses.replace` builders + `ceiling_eligible` stamping), §5 (full
truth-table), §7 (floor-free co-key). Amendments A9, A10, A12, A13.

This is the largest task. It rewrites `_gate_one_concept`, adds a `bank_by_code`
index to `gate_findings`, threads `TAU_SOLO_JUDGE`, converts both builders to
`dataclasses.replace`, and stamps `ceiling_eligible`. The existing gate tests use
a SHARED synthetic `concept_key` for both findings, which HIDES the real
cross-namespace bug (spec §4.0) — those co-key tests are **replaced**, not kept.

### RED
Rewrite `test_misconception_detector_gate.py`. Extend `_finding` to accept
`bank_code=None`, `bank_match_above_floor=True`. Author **one test per REACHABLE §5
truth-table row (1, 2, 3, 3b, 5, 6, 7, 8, 9)** — **row 4 is UNREACHABLE under A13
and gets NO test** (see the "Row 4 is unreachable" note immediately below). Every
co-key test MUST use REAL cross-namespace keys (A13 test contract, §4.0 / §12):

- Judge finding keyed `concept_key="node.demand_curve"` (a `node_id`-shaped string).
- Bank finding keyed `concept_key="42"` (`str(concept_id)`).
- Agreement carried ONLY by a shared `bank_code="includes_transfers"`.
- A same-namespace shared-key co-key test is explicitly a REGRESSION and must NOT
  be the co-key coverage.

**Row 4 is unreachable under A13 (Blocking-issue resolution — this row is REMOVED,
not tested).** §5 row 4 ("judge + bank present in the same group but NOT co-keyed")
cannot be constructed with real keys. Under the grouping redesign (GREEN step 2
below): a real bank finding keys by `str(concept_id)`, is excluded from every
judge/deterministic group, and lives ONLY in the `bank_by_code` witness index — so a
bank finding can NEVER be co-present in a judge's `concept_key` group. The only tiers
that key by `node_id` (and could therefore share a judge's group) are `judge` itself
and `sympy_veto`, and any `sympy_veto` in the group short-circuits to row 1/2
(deterministic) BEFORE any "≥2 non-co-keyed sources" branch. Therefore the only way
to author the row-4 setup the old table asked for ("both findings under the SAME
judge concept group") is to give a bank finding a synthetic same-namespace
`concept_key` — the exact same-namespace anti-pattern the A13 test contract (§12,
below) forbids. **Resolution:** (a) delete the mandated `_gate_one_concept` code path
for a distinct "row 4" — the §4.4 row-3 co-key branch and the §4.4 row-2 sympy branch
together cover every reachable ≥2-source dock; and (b) fold row 4's `ceiling_eligible`
sub-rule into row 3: a bank-corroborated judge dock (row 3) already sets
`ceiling_eligible=True`, which is exactly "True iff a bank-keyed source agrees." No
production behavior is lost because row 4 was, per spec §4.4 note ("in practice on
this corpus this branch is dormant"), never live. GREEN step 3 below is amended to
drop the separate row-4 branch accordingly.

Truth-table tests (assert dock/clarify/drop, `corroborated`, `ceiling_eligible`,
docked `signature`, and — for docks — docked `concept_key`):

| Test | Row | Setup | Assert |
|---|---|---|---|
| `test_row1_sympy_solo_docks_ceiling_eligible` | 1 | lone `sympy_veto`, `bank_code="c"` | dock, `corroborated=True`, `ceiling_eligible=True`, sig `misc.c` |
| `test_row2_sympy_plus_anything_docks` | 2 | sympy + a judge on same node | dock (represented by sympy/D), `ceiling_eligible=True` |
| `test_row3_judge_bank_cokey_below_floor_docks_under_judge` | 3 | judge `node.X`@0.86 keyed `misc.it`; bank `"42"`@0.60 `bank_match_above_floor=False` same `bank_code="it"` | dock, `docked.concept_key=="node.X"` (NOT `"42"`), `corroborated=True`, `ceiling_eligible=True`, sig `misc.it`, and `docked.bank_match_above_floor` round-trips the JUDGE finding's value (True) |
| `test_row3b_cokey_judge_sub_routed_tau_clarifies` | 3b | same as row 3 but judge@`TAU_FIRE-0.05` | judge → `needs_clarification`, `corroborated=False`; bank dropped (no separate output row for `"42"`) |
| ~~`test_row4_two_sources_not_cokeyed_docks_under_judge`~~ **(REMOVED — row 4 is unreachable under A13; see the "Row 4 is unreachable" note below)** | 4 | — | *no test authored; the branch cannot be constructed with real keys without violating the A13 namespace contract (§12). Its `ceiling_eligible` sub-rule is folded into row 3.* |
| `test_row5_lone_keyed_judge_solo_docks_penalty_only` | 5 | lone judge `node.X` keyed `misc.it` @0.95 (`>= TAU_SOLO_JUDGE`) | dock, `corroborated=True`, **`ceiling_eligible=False`**, sig `misc.it`, `docked.concept_key=="node.X"` |
| `test_row6_keyed_judge_sub_solo_tau_clarifies` | 6 | lone judge keyed @0.86 (clears routed `TAU_FIRE` but < `TAU_SOLO_JUDGE`) | `needs_clarification`, `corroborated=False` |
| `test_row7_lone_unkeyed_judge_clarifies_never_docks` | 7 | lone judge UNKEYED @0.99 (`bank_code=None`) | `needs_clarification` (never dock even at 0.99), `corroborated=False` |
| `test_row8_lone_judge_sub_routed_tau_drops` | 8 | lone judge @`TAU_FIRE-0.10` (keyed or not) | dropped (`()` for that concept) |
| `test_row9_lone_bank_no_judge_drops` | 9 | lone bank `"42"`@0.99 keyed `misc.it`, no judge names that code | dropped — bank alone never docks/routes |

Additional MANDATORY guards (spec §4.6.1 / §12):
- `test_docked_row_roundtrips_bank_fields_via_replace` — a row-3/row-5 docked
  finding round-trips `bank_code`, `signature`, `bank_match_above_floor` UNCHANGED
  from the input judge finding (guards against a re-enumeration regression if a
  future edit reverts `dataclasses.replace`).
- `test_needs_clarification_never_ceiling_eligible` — a clarification row inherits
  `ceiling_eligible=False` (tiers never set it True pre-gate).
- Keep `test_empty_input_returns_empty_tuple`, `test_result_is_new_tuple_not_mutated_input`,
  `test_multiple_concepts_are_gated_independently`.

**MANDATORY reconciliation of EVERY existing bank+judge docking test (Blocking-issue
resolution — do NOT leave any of these unmodified).** The `_finding` helper defaults
`bank_code=None`, so any pre-existing test that builds a bank+judge pair on ONE shared
synthetic `concept_key` and asserts a DOCK becomes, under the new grouping, a LONE
UNKEYED judge in its group → §5 row 7 → `needs_clarification` (NOT a dock). Left as-is,
each such test turns RED and blocks the ≥95% patch gate + the "no regression from 2562
passed" guard (Task 8). The redesign therefore rewrites the whole file; when doing so,
each of the tests below MUST be handled EXACTLY as specified — none may be silently kept:

| Existing test (current lines) | Old assertion | REQUIRED disposition under the new gate |
|---|---|---|
| `test_bank_and_judge_agreeing_docks_corroborated` (58-70) | dock via same-key bank+judge, no `bank_code` | **REWRITE as the row-3 co-key path.** Give BOTH findings a matching `bank_code="includes_transfers"`, key the judge `"node.elasticity"` and the bank `"42"` (real cross-namespace keys), judge ≥ routed tau. Assert dock represented by the judge (`docked.concept_key=="node.elasticity"`), `corroborated=True`, `ceiling_eligible=True`. (This IS the row-3 test; either fold it into `test_row3_judge_bank_cokey_below_floor_docks_under_judge` and DELETE this name, or rename it to the row-3 test — do not keep the old same-key/no-`bank_code` body.) |
| `test_token_prob_judge_between_taus_still_corroborates_dock` (211-228) | token-prob judge@0.87 + same-key bank docks (proves origin bit routes at looser `TAU_FIRE`) | **REWRITE, do NOT keep unchanged** (this supersedes the earlier "keep it" instruction, which was a plan defect — with `bank_code=None` on both findings it would go row-7 clarify and fail). Preserve its INTENT — the token-prob path stays gated at the looser `TAU_FIRE` so a 0.87 token-prob judge still corroborates — by making it a real row-3 co-key test: judge `"node.gdp_identity"`@0.87 `verdict_token_prob_present=True` keyed `bank_code="c"`; bank `"42"` same `bank_code="c"`. Assert dock + `corroborated=True`. Keep the paired `test_verbalized_judge_between_taus_does_not_corroborate_dock` (verbalized@0.87 → no dock) but likewise give both findings the matching `bank_code` so the ONLY reason it fails to dock is the stricter verbalized tau, not an accidental row-7 fall-through. |
| `test_a1_dual_source_verbalized_path_requires_stricter_threshold` (149-169) | default-tau: same-key bank+judge@`TAU_FIRE+0.01` docks; strict-tau override: does NOT dock | **REWRITE both arms as row-3 co-key.** Give both findings the matching `bank_code`; key judge `"node.marginal_utility"`, bank `"42"`. Default-tau arm asserts dock (`corroborated=True`); strict-tau arm (`tau_fire=tau_verbalized=TAU_FIRE_VERBALIZED`) asserts NO dock (row 3b → clarify) — proving the routed tau still gates the co-key dock. |
| `test_bank_and_judge_agree_but_judge_below_tau_does_not_dock` (109-122) | same-key bank+judge@`TAU_FIRE-0.05` → nothing docks | **REWRITE as row 3b** with matching `bank_code` and real cross-namespace keys; the sub-routed-tau judge → `needs_clarification`, bank dropped, no docked/`corroborated` row. (Its no-dock assertion already survives, but it must exercise the real co-key path, not the dead same-namespace group, per the §12 A13 contract.) |
| `test_lone_judge_at_or_above_tau_is_needs_clarification_not_docked` (73-83) | lone judge@0.99, no `bank_code` → clarify | **KEEP as-is** (it IS row 7: lone UNKEYED judge never docks). Optionally rename to `test_row7_lone_unkeyed_judge_clarifies_never_docks` and merge with the new row-7 test to avoid duplication. |
| `test_judge_below_tau_is_dropped` (85-91) | lone judge@`TAU_FIRE-0.10` → `()` | **KEEP** (row 8). |
| `test_two_agreeing_non_judge_sources_dock` (94-107) | sympy + bank same key → dock | **REWRITE/KEEP as row 2:** the `sympy_veto` short-circuits (deterministic present → row 1/2), so the dock still fires and the docked representative is the sympy finding. Give it `bank_code` on the sympy finding and assert `ceiling_eligible=True`, sig `misc.<code>`. Because deterministic-present wins before any co-key logic, this test does NOT depend on the bank sharing the group. |
| `test_sympy_veto_alone_docks_deterministically` (45-55) | lone sympy → dock | **KEEP/extend as row 1** (add `ceiling_eligible=True`, `bank_code` assertions). |
| `test_a1_judge_verbalized_confidence_uses_verbalized_tau` (125-146) | lone verbalized judge between the taus → clarify | **KEEP** (still a lone UNKEYED judge → row 7 clarify when it clears the routed tau; at the mid-confidence it may instead land row 8 drop — re-derive the assertion against the routed verbalized tau and assert whichever of clarify/drop the new routing yields). |
| `test_verbalized_judge_between_taus_alone_does_not_dock` (195-208) | lone verbalized judge@0.87 → `()` | **KEEP** (row 8: sub-routed-verbalized-tau lone judge drops). |

Net rule: **no bank+judge pair may assert a DOCK on a shared synthetic `concept_key`
with `bank_code` unset.** Every surviving DOCK-asserting bank+judge test must (a) set a
matching `bank_code` on BOTH findings and use real cross-namespace keys (row-3 path),
or (b) be re-expressed as a sympy-deterministic dock (row 1/2, where grouping is
irrelevant). Any bank+judge test that should NOT dock is re-expressed as row 3b (clarify)
or row 9 (lone bank drops), never left as a dead same-namespace group.

Run — MUST fail (new fields/args/branches don't exist):
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_gate.py -x -q
```

### GREEN
Rewrite `gate.py`:

1. Import `dataclasses` and `TAU_SOLO_JUDGE`.
2. `gate_findings(findings, *, tau_fire=..., tau_verbalized=..., tau_solo=TAU_SOLO_JUDGE)`:
   - Build `bank_by_code: dict[str, list[ConceptFinding]]` = every `bank_pattern`
     finding with `bank_code is not None`, grouped by `bank_code` (built ONCE).
   - Group deterministic ∪ judge findings per `concept_key` as today for the
     *decision unit* (one representative per judge/sympy concept). Bank findings
     are NOT anchors — they are witnesses only; a `bank_pattern` finding whose code
     no judge/sympy concept names never forms its own group (row 9 → drop).
   - For each judge/deterministic group, call `_gate_one_concept(group, bank_by_code=..., tau_fire=..., tau_verbalized=..., tau_solo=...)`.
3. `_gate_one_concept` implements the §5 decision order EXACTLY:
   - **Deterministic present** → `_docked(D, ceiling_eligible=True)` (row 1/2).
   - Else compute `J = best judge (max confidence)`, `routed_ok(J)`,
     `solo_ok(J) = routed_ok(J) and J.confidence >= tau_solo`.
   - `B = max over bank_by_code.get(J.bank_code, []) by (similarity/confidence)` when
     `J.bank_code is not None` else None. `co_key = J.bank_code is not None and B is not None`
     (B already shares the code by construction of the index; ignore
     `B.bank_match_above_floor` — floor-free, L4).
   - **Row 3 — co-key + routed_ok** → `_docked(J, ceiling_eligible=True)` (J is the
     representative; B is the witness, never docked). Row 3 absorbs row 4's
     `ceiling_eligible` sub-rule: a bank-corroborated judge dock is exactly the
     "a bank-keyed source agrees" case, so `ceiling_eligible=True` here.
   - **Row 3b — co-key + not routed_ok** → `_needs_clarification(J)`.
   - **Row 4 — REMOVED (unreachable under A13).** Do NOT add a separate "≥2
     independent sources in the group NOT co-keyed" branch. Under the grouping
     redesign a real bank finding is never in a judge's group (it lives only in
     `bank_by_code`), and the only same-namespace second source (`sympy_veto`) is
     already consumed by the deterministic row-1/2 short-circuit above. The
     reachable ≥2-source docks are therefore fully covered by rows 1/2 (sympy) and
     row 3 (co-keyed judge+bank). No `_gate_one_concept` code path exists for row 4;
     no test is authored for it (see the RED "Row 4 is unreachable" note).
   - **Row 5 — lone judge, `bank_code` not None, `solo_ok`** →
     `_docked(J, ceiling_eligible=False)` (penalty-only).
   - **Row 6/7 — lone judge, routed_ok, (keyed sub-solo-tau OR unkeyed)** →
     `_needs_clarification(J)`.
   - **Row 8 — lone judge sub-routed-tau** → return None (drop).
   - **Row 9** — a lone bank concept never anchors a group (handled at the
     `gate_findings` grouping level), so no code path needs it explicitly; assert
     via test.
4. `_docked(finding, *, ceiling_eligible)` — **required** keyword (no default, so a
   forgotten call site is a compile error), body:
   ```python
   return dataclasses.replace(finding, verdict="misconception", corroborated=True, ceiling_eligible=ceiling_eligible)
   ```
5. `_needs_clarification(finding)` →
   `dataclasses.replace(finding, verdict="needs_clarification", corroborated=False)`
   (do NOT override `ceiling_eligible` — inherits the tier's False).

**Critical (spec §4.6.1):** both builders MUST use `dataclasses.replace` so
`bank_code` / `bank_match_above_floor` / `signature` / `concept_key` are never
silently dropped. The old enumerated-field builders are the exact bug the spec
calls out.

### VERIFY
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_gate.py -q
```

**Commit:** `feat(apollo): gate solo-keyed-judge dock + floor-free cross-namespace co-key + ceiling stamping (A9/A12/A13)`

---

## Task 5 — `bank_pattern.py`: emit best match even below floor, tagged

**Files:** `apollo/overseer/misconception_detector/bank_pattern.py`,
`apollo/overseer/tests/test_misconception_detector_bank_pattern.py`

**Spec refs:** §4.5, A10, L7. The one output-shape change to `bank_pattern`: a
below-floor best match becomes a corroboration-only finding tagged
`bank_match_above_floor=False`.

### RED
Add to `test_misconception_detector_bank_pattern.py` (SQLite in-memory path with a
stub `embed_fn`, no pgvector — this file already uses that harness):

1. `test_above_floor_match_tagged_true` — an utterance whose best cosine to a bank
   entry `>= BANK_SIM_FLOOR` → finding with `bank_match_above_floor is True`,
   `bank_code == entry.code`, `signature == f"misc.{entry.code}"`. (Preserves
   today's standalone behavior.)
2. `test_below_floor_best_match_emitted_tagged_false` — an utterance whose best
   cosine is `0 < sim < BANK_SIM_FLOOR` → a finding IS emitted with
   `bank_match_above_floor is False`, `bank_code` set, `confidence == sim`.
   (Today this ABSTAINS; the RED assertion that a finding exists fails first.)
3. `test_no_bank_still_abstains` — empty `bank_entries` → `()` (unchanged
   soft-fail).
4. `test_embed_failure_abstains` — `embed_fn` raising → `()` (unchanged).
5. `test_zero_similarity_abstains` — a best match with `sim <= 0` → no finding
   (guard the `> 0` predicate so a degenerate/zero-norm embedding doesn't emit a
   junk corroboration-only row).

Run — MUST fail (below-floor branch missing; `bank_code` field unset):
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_bank_pattern.py -x -q
```

### GREEN
In `bank_pattern.py`:
- `_finding_for_match(utterance, entry, similarity, *, above_floor: bool)` — add
  `bank_code=entry.code` and `bank_match_above_floor=above_floor` to the
  constructed `ConceptFinding` (`signature=f"misc.{entry.code}"` already present).
- In `detect_bank_pattern`'s per-utterance loop, replace the `if similarity >=
  BANK_SIM_FLOOR` gate with:
  ```python
  if similarity > 0.0:
      findings.append(_finding_for_match(utterance, entry, similarity, above_floor=similarity >= BANK_SIM_FLOOR))
  ```
  So above-floor → `above_floor=True` (unchanged standalone behavior), below-floor
  (but positive) → `above_floor=False` (corroboration-only), non-positive →
  abstain. Docstring updated to say it now emits below-floor best matches tagged.

### VERIFY
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_bank_pattern.py -q
```

**Commit:** `feat(apollo): bank_pattern emits below-floor best match tagged for co-key (A10)`

---

## Task 6 — `merge.py`: base vs boosted severity; ceiling gated on `ceiling_eligible`

**Files:** `apollo/overseer/misconception_detector/merge.py`,
`apollo/overseer/tests/test_misconception_detector_merge.py`

**Spec refs:** §4.7, §8. One-line predicate change: the ceiling trips ONLY on
`ceiling_eligible` docks on a maximally-central concept. Penalty math (sum over ALL
docks, clamped) is UNCHANGED — lone-judge docks still subtract (L2), they just
never ceiling.

### RED
Extend `_docked` helper in `test_misconception_detector_merge.py` to accept
`ceiling_eligible=False` and `concept_key`. New tests:

1. `test_lone_judge_dock_subtracts_but_no_ceiling` — a `ceiling_eligible=False`
   dock on the MAX-central concept (present in the centrality map at the max
   value) → `ceiling_applied is False` but `misconception_penalty > 0.0` (the
   severity gradient; the subtract still fires — L2).
2. `test_bank_corroborated_dock_trips_ceiling` — a `ceiling_eligible=True` dock on
   the same max-central `node_id` concept → `ceiling_applied is True`.
3. `test_penalty_sums_all_docks_including_lone_judge` — mix of a
   `ceiling_eligible=True` and a `ceiling_eligible=False` dock; assert penalty ==
   clamped sum over BOTH.
4. `test_keyed_rows_emitted_for_now_keyable_judge_docks` — a judge dock with
   `signature="misc.includes_transfers"` (now possible after Task 3) → appears in
   `outcome.misconceptions` with `canonical_key == "misc.includes_transfers"`.
5. `test_a13_regression_node_keyed_dock_is_central` — a `ceiling_eligible=True`
   dock carrying a `node_id` `concept_key` PRESENT in the centrality map at max →
   central → ceiling trips (mirrors what the gate hands merge for a
   bank-corroborated dock).
6. `test_a13_regression_str_concept_id_key_floors_not_central` — the SAME
   `ceiling_eligible=True` dock but carrying a `str(concept_id)` key ABSENT from
   the centrality map → floors to `CENTRALITY_W_MIN`, is NOT maximally central →
   `ceiling_applied is False`. (Documents exactly why the docked representative
   must be node_id-keyed — spec §4.7 load-bearing dependency.)

**MANDATORY reconciliation of the three EXISTING `ceiling_applied is True` tests
(merge-test regression — do NOT leave any of these unmodified).** The local
`_docked` helper in `test_misconception_detector_merge.py` (lines 43-62)
constructs its `ConceptFinding` WITHOUT passing `ceiling_eligible`, so after
Task 1 it defaults to `ceiling_eligible=False`. Under the new Task-6 `_any_central`
predicate (`any(f.ceiling_eligible and centrality.get(...) >= max_centrality ...)`),
`ceiling_eligible=False` makes the predicate return `False`, so **every existing
test that builds a docked finding via `_docked` and asserts `ceiling_applied is
True` FLIPS to `ceiling_applied is False` and FAILS** — a regression below the
Task-8 no-regression gate (2562 passed) and the ≥95% patch gate. There are
EXACTLY three such tests (verified: three `ceiling_applied is True` assertions in
the file, at lines 156, 182, 232). Each MUST be reconciled EXACTLY as specified —
none may be silently kept. These findings all model a **bank-corroborated /
deterministic CENTRAL dock** (the only dock class that trips the ceiling under
A12/L3), so the correct disposition is to make the docked finding
`ceiling_eligible=True` (NOT to weaken the predicate). Because the RED step already
extends `_docked` to accept `ceiling_eligible=False`, the mechanical fix is to
pass `ceiling_eligible=True` at each of these call sites:

| Existing test (current def line / assert line) | Old assertion | REQUIRED disposition under the new ceiling predicate |
|---|---|---|
| `test_central_docked_finding_sets_ceiling_applied` (def 148 / assert 156) | central dock on max node trips ceiling | **Set `ceiling_eligible=True` on the `central` finding** (the max-central `_docked(concept_key="concept.central", …)`). It models a corroborated/deterministic central dock, so it IS ceiling-eligible; the `peripheral` finding may stay default (it is not max-central, so its eligibility is irrelevant). Assertion `ceiling_applied is True` then holds. |
| `test_ceiling_uses_max_centrality_present_in_map` (def 171 / assert 182) | the sole docked finding is the most-central present → trips ceiling | **Set `ceiling_eligible=True` on the single `_docked(concept_key="concept.a", …)` finding.** It is the max-central docked node and models a corroborated central dock; assertion `ceiling_applied is True` holds. |
| `test_unkeyed_finding_is_penalized_but_not_emitted_as_keyed_row` (def 218 / assert 232) | an `unkeyed:*` central dock still trips the ceiling (penalized, just not keyed) | **Set `ceiling_eligible=True` on the `unkeyed` `_docked(…, signature="unkeyed:42")` finding.** A13/§4.6 note: `ceiling_eligible` is orthogonal to keying — an unkeyed dock (e.g. a sympy_veto/deterministic dock whose signature happens to be `unkeyed:*`) is still ceiling-eligible; only the `misconceptions[]` KEYED-ROW emission is gated on the `misc.<code>` signature, not the ceiling. Preserve the test's dual intent (penalty + ceiling reflect the finding; no keyed row emitted). Assertion `ceiling_applied is True` holds. |

Net rule: **no `_docked`-built finding may assert `ceiling_applied is True` while
leaving `ceiling_eligible` at its default `False`.** This mirrors the exhaustive
Task-4 RED reconciliation table (every existing test that would flip under the new
gate is dispositioned explicitly, never silently kept). The three
`ceiling_applied is False` tests in this file
(`test_peripheral_only_docked_finding_does_not_set_ceiling`,
`test_empty_input_yields_empty_outcome`, and the new
`test_lone_judge_dock_subtracts_but_no_ceiling`) require NO change — a False
assertion is unaffected by (indeed strengthened by) the eligibility gate.

Run — MUST fail (`_any_central` doesn't read `ceiling_eligible` yet):
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_merge.py -x -q
```

### GREEN
In `merge.py::_any_central`, change the predicate to require eligibility:
```python
return any(
    f.ceiling_eligible and centrality.get(f.concept_key, CENTRALITY_W_MIN) >= max_centrality
    for f in findings
)
```
Everything else UNCHANGED: `severity = centrality * confidence`,
`penalty = min(clamp, Σ severity)` over ALL docked findings, keyed rows via
`_is_bank_keyed(signature)`, `ledger_findings = all docked`.

### VERIFY
```bash
cd ai-ta-backend && pytest apollo/overseer/tests/test_misconception_detector_merge.py -q
```

**Commit:** `feat(apollo): ceiling_applied reads ceiling_eligible (severity gradient, A12)`

---

## Task 7 — Wiring-unchanged verification (done.py + emergent feed)

**Files:** NONE modified. `apollo/handlers/done.py:504-516` and the emergent-feed
wiring stay a single `gate_findings(detection.per_concept)` call (spec §4.0 last
paragraph, §9: the A13 reconciliation is entirely inside `gate.py`).

**Spec refs:** §9 data flow, §12 flag-OFF + emergent-feed tests.

### RED / VERIFY
No new production code. Prove the wiring is untouched and behavior holds:

1. **Flag-OFF golden byte-identical** (spec §12, T15). Run the golden suite; it
   MUST pass unchanged:
   ```bash
   cd ai-ta-backend && pytest apollo/handlers/tests/test_misconception_flag_off_golden.py -q
   ```
   ADD one assertion to that file: with the flag OFF, none of the three new
   `types.py` fields (`bank_code`, `bank_match_above_floor`, `ceiling_eligible`)
   appear in any serialized flag-OFF artifact/rubric dict (they are internal to the
   detector chain, never serialized). This is a cheap dict-key scan over the
   produced artifact.
2. **Emergent-feed test** (spec §12, T14). In
   `apollo/handlers/tests/test_misconception_ledger_feed.py`, add
   `test_cokeyed_judge_bank_dock_writes_keyed_row`: with both
   `APOLLO_MISCONCEPTION_DETECTOR` and `APOLLO_EMERGENT_MISCONCEPTIONS` ON, drive a
   row-3 co-keyed judge+bank dock through the chain and assert a NON-ZERO keyed row
   reaches `apollo_misconception_observations` (the observable the two prior runs
   could never satisfy because judge signatures were unkeyed).
3. **done.py integration** — run the done handler tests to prove the single
   `gate_findings(detection.per_concept)` call still composes with the new gate
   signature (new params are all defaulted, so the call site is unchanged):
   ```bash
   cd ai-ta-backend && pytest apollo/handlers/tests/ -q -k "done or misconception"
   ```

**Commit:** `test(apollo): flag-OFF golden + emergent keyed-row feed under new gate`

---

## Task 8 — Coverage gate + full-suite regression

**Spec refs:** §12. Patch coverage ≥95% on changed lines vs `origin/staging`; the
full apollo suite must not regress from **2562 passed, 14 skipped**.

### Commands (run after Tasks 1–7 are all green)
```bash
cd ai-ta-backend
# Patch coverage on changed lines:
pytest apollo/ --cov --cov-report=xml
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
# Full-suite regression:
pytest apollo/
```

- `diff-cover` MUST report `>= 95%` patch coverage. Every new line is in a pure /
  DI-seamed module (types/config/judge-parse/gate/bank_pattern/merge), so 100% is
  reachable offline. The single documented exemption is `judge.py`'s live
  `client.chat.completions.create` (already `# pragma: no cover`).
- `pytest apollo/` MUST show **≥ 2562 passed** (the new tests raise the count) and
  **14 skipped** (no new unexplained skips). Any regression is a blocker — fix
  before proceeding.
- If any changed line is genuinely untestable without a prod refactor (none
  expected here), document the exemption in the PR description per the workspace
  test-coverage contract. "Hard to test" is not an exemption.

---

## Task 9 — Drift-doc reconciliation (`docs/architecture/apollo.md`)

**Files:** `docs/architecture/apollo.md` (owner, `owns:
apollo/overseer/misconception_detector/**`).

**Spec refs:** §14. Same commit as the code (drift contract).

### Steps (no test; a doc edit)
1. In the `apollo/overseer/` row's `misconception_detector` paragraph and in
   `## Main data flows (b)` step 5a, revise the `gate.py` description from the old
   "sympy_veto self-corroborates, else ≥2 independent sources agreeing, else a
   lone/sub-τ judge routes to needs_clarification instead of docking" to the new
   truth-table: *a bank-keyed judge finding may dock on its own above
   `TAU_SOLO_JUDGE` (penalty-only, never trips the ceiling); a judge + bank_pattern
   agreeing on the same validated `misc.<code>` corroborate floor-free and may trip
   the ceiling on a central concept; sympy_veto self-docks; a lone bank match or a
   lone unkeyed/sub-tau judge does not dock.*
2. Note `judge.py` now emits a validated `misconception_code` → `misc.<code>`
   signature (keying), and `bank_pattern.py` now emits its best match even below
   `BANK_SIM_FLOOR` (corroboration-only, tagged `bank_match_above_floor`).
3. Add `TAU_SOLO_JUDGE` to the flag/constants list next to `TAU_FIRE`.
4. Note the A13 cross-namespace reconciliation: the gate co-keys on validated
   `bank_code` (not `concept_key`) and docks under the judge finding so centrality
   (node_id-keyed) resolves.
5. Bump `last_verified` to `2026-07-08`.
6. No `owns:` glob change (all files already covered). No new flag.

### VERIFY
```bash
cd ai-ta-backend && grep -n "last_verified\|TAU_SOLO_JUDGE\|bank_match_above_floor" docs/architecture/apollo.md
```

**Commit:** fold into the final code commit (drift contract requires same commit)
or a dedicated `docs(apollo): reconcile apollo.md for corroboration/keying redesign`
landed atomically with Task 6/7.

---

## Task 10 — Final validation (the acceptance gate)

**Files:** NONE modified. Run the existing harness
`campaign/validate_misconception_detector.py` over the SAME 20 attempt ids.

**Spec refs:** §13. This is the observable that both prior runs showed empty.

### Command
```bash
cd ai-ta-backend
APOLLO_MISCONCEPTION_DETECTOR=1 python -m campaign.validate_misconception_detector
```
`full_judge` mode (real `make_openai_judge`, gpt-4o, temp 0.0), local Docker
Postgres + Neo4j, attempt_ids:
`75, 77, 81, 88, 89, 95, 97, 100, 102, 105, 106, 108, 109, 110, 111, 112, 113, 114,
115, 116`.

### Success criteria (hard gate — the whole point of the change)
1. **Docks move from 0/20 to nonzero.** At least one attempt (target: 88/95/110,
   whose bank rankings are known correct) now shows
   `misconceptions_found != []` and `penalty > 0.0`. The prior twice-run NO-OP was
   0/20 — a nonzero count is the pass signal.
2. **The 4 Strong/partial controls keep 0 false positives (hard constraint).**
   Attempts 77, 89, 106 (strong) and 97 (partial) must keep `penalty == 0.0`,
   `baseline_band == detector_band`, `control_credit_ok == True`. Attempt 77's
   judge already clears at ~0.996 with `clear` verdicts (no dock) — the solo-dock
   path must NOT fire on it. ANY control false positive is a FAIL: stop and
   diagnose (likely `TAU_SOLO_JUDGE` too low or a keying leak) before the env flip.
3. **False-Strong on misconception-class drops materially from 7** (target: ≥ half
   reduction) — driven by the ceiling on corroborated central docks and the
   subtract on lone-judge docks. (Secondary; #1 and #2 are the pass/fail gates.)
4. **Keyed ledger rows appear** on docked attempts (`misconceptions_found != []`).

### Record
Append a new dated section to
`docs/_archive/experiments/2026-07-08-misconception-detector-validation.md` (or a
sibling `-v2` writeup) with the full 20-row table (before/after in one artifact).
Honesty contract: report the delta AS MEASURED; do not claim the fix worked without
the observable keyed rows. If docks are still 0/20, that is a NO-OP result to
report honestly, not to paper over — the spec's whole reason for existing is that
the prior two runs were NO-OPs.

**No env flip in this plan.** The Railway env flip (`APOLLO_MISCONCEPTION_DETECTOR=1`
on staging) is a separate human step, gated on this validation passing + a
`TAU_SOLO_JUDGE` calibration pass (spec O6). This plan ships the code flag-OFF.

---

## 2. Test enumeration for ≥95% patch coverage (consolidated)

| Module | New/changed tests | Coverage target |
|---|---|---|
| `types.py` | defaults, keyed-invariant, unkeyed-invariant, frozen-mutation-raises | new fields + defaults |
| `config.py` | default 0.90, env-override, malformed-fallback, `>= TAU_FIRE`, `BANK_SIM_FLOOR` unchanged | new constant + fallback branch |
| `judge.py` | valid-code→keyed, empty→unkeyed, hallucinated→unkeyed, cross-concept→unkeyed, missing-field→unkeyed, all-clear names-no-code, schema-has-field | validation branch (both arms), schema/prompt |
| `gate.py` | truth-table rows 1,2,3,3b,5,6,7,8,9 (**row 4 REMOVED — unreachable under A13**); `bank_by_code` index; `replace` round-trip guard; clarification-not-eligible; the A1 dual-tau tests **rewritten as row-3 co-key** (matching `bank_code` on both findings + real cross-namespace keys — see the Task-4 RED reconciliation table); empty/immutability/multi-concept | every reachable branch + both builders |
| `bank_pattern.py` | above-floor→True, below-floor→False emitted, no-bank abstain, embed-fail abstain, zero-sim abstain | new below-floor branch + tag |
| `merge.py` | lone-judge subtracts-no-ceiling, corroborated trips ceiling, penalty sums all docks, keyed rows now emit, A13 node_id-keyed central, A13 str(concept_id) floors; **the three existing `ceiling_applied is True` tests (central-docked, max-centrality-present, unkeyed-central) reconciled to pass `ceiling_eligible=True` — see the Task-6 RED reconciliation table** | new `_any_central` predicate |
| flag-OFF golden | passes unchanged + new-fields-not-serialized assertion | OFF path byte-identical |
| emergent feed | co-keyed dock writes keyed row | keyed-row promotion |

Adversarial cases explicitly required by spec §12: row 5 (lone keyed judge @0.95
docks penalty-only, `ceiling_eligible=False`); row 3 (judge `node.X`@0.86 + bank
`42`@0.60 below-floor sharing `bank_code` docks under `node.X`,
`ceiling_eligible=True`); row 9 (lone bank @0.99 whose code no judge named does NOT
dock); row 7 (lone unkeyed judge @0.99 → clarify, never dock).

---

## 3. Commit / PR sequence

1. Task 1 — types
2. Task 2 — config
3. Task 3 — judge
4. Task 4 — gate (heart)
5. Task 5 — bank_pattern
6. Task 6 — merge
7. Task 7 — wiring verification (tests only)
8. Task 9 — apollo.md drift (fold into the code commits so the drift contract's
   "same commit" holds; at minimum land atomically with Task 6/7)
9. Task 8 — coverage gate + full-suite regression (must be green before PR)
10. Task 10 — validation run (evidence appended to the experiment file)

**PR:** `feat/apollo-judge-authoritative-gate` → **`staging`** is NOT the base for
the PR of THIS branch, because this branch builds on the unmerged
`feat/apollo-misconception-detector`. Open the PR with base
`feat/apollo-misconception-detector` (so the diff is only this redesign), OR, if the
detector branch is merged to `staging` first, rebase this branch onto `staging` and
retarget. State the untested-vs-tested split and the validation result in the PR
body per the workspace test-coverage contract. Flag stays OFF; note the env flip is
a separate gated human step.

---

## 4. Guardrails / invariants to hold throughout

- **Flag-OFF byte-identical.** All changes are inside the detector chain (only
  reached when `APOLLO_MISCONCEPTION_DETECTOR` is ON) and every new
  `ConceptFinding` field is defaulted, so the OFF path constructs and compares
  identically. The golden suite is the enforcement.
- **Immutability.** No field is ever mutated; `gate.py` / `merge.py` build NEW
  instances via `dataclasses.replace`. `ConceptFinding` stays frozen.
- **Soft-fail preserved.** Judge code-validation never raises (missing/empty/
  cross-concept code → unkeyed); bank below-floor emission is pure tuple
  construction; the gate is pure and total (default = DROP); `detector.py` wraps
  every tier; `done.py` wraps the whole chain → unpenalized HTTP 200 on any defect.
- **The docked representative of any bank-corroborated dock is the JUDGE finding**
  (node_id-keyed) so `centrality` resolves it — a bank finding
  (`str(concept_id)`-keyed) is NEVER the docked representative. This is
  load-bearing for both grouping (A13) and the merge ceiling (§4.7).
- **`_docked` takes a required `ceiling_eligible` keyword** (no default) so a
  forgotten call site is a compile error, not a silent False.
