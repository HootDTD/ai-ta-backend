# Apollo Misconception Detector — Corroboration & Keying Redesign

**Date:** 2026-07-08
**Status:** Design approved (brainstorming) → this spec → implementation plan
**Scope:** Apollo student grading, `ai-ta-backend`
**Branch (build on):** `feat/apollo-misconception-detector` (the detector already exists there)
**Owner doc to reconcile on landing:** `docs/architecture/apollo.md` (drift contract, `owns: apollo/overseer/misconception_detector/**`)
**Predecessor spec (WHAT the detector is):** `docs/_archive/specs/2026-07-08-apollo-misconception-detector-design.md`
**Predecessor plan (contract labels A1–A8, section numbering):** `docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md`
**Validation evidence this spec responds to:** `docs/_archive/experiments/2026-07-08-misconception-detector-validation.md` (§6.6, the twice-run NO-OP result)

> **Continuity note.** This spec does NOT re-open the amendments A1 (dual-tau
> selection), A4 (band-system separation), A5 (`canonical_key` = bare
> `misc.<code>`), A6 (centrality cycle safety), or A8 (inline rounding). They
> hold verbatim. This spec adds a new amendment band **A9–A13** for the
> corroboration/keying/anti-dilution changes, and edits `judge.py`, `gate.py`,
> `merge.py`, `types.py`, and `config.py` only. (A13 — the cross-namespace keying
> reconciliation, §4.0 — is contained entirely within `gate.py`; `detector.py`,
> `bank_pattern.py`'s keying, `centrality.py`, and `done.py`'s wiring stay
> untouched.) All module section numbers below (5.4/5.5/5.6, 2, 3) refer to the
> predecessor plan's numbering so the two documents stay cross-referenceable.

---

## 1. Problem / bottleneck

The detector is built, wired end-to-end, and **inert**. On the 20-attempt
`v2-qa-2026-07-08` A/B (real `make_openai_judge`, gpt-4o, temperature 0.0,
local Docker Postgres + Neo4j), it produced **0/20 docked findings**: penalty
0.0, `ceiling_applied=False`, `misconceptions_found=[]` on every row;
`baseline_band == detector_band` on all 20; misconception detection **0/16** on
the misconception-class attempts; false-Strong **7 → 7 (no change)**. This is
the second consecutive NO-OP — the first was a judge JSON-schema collapse
(fixed via Structured Outputs, confirmed working); this second one is the
**corroboration gate itself**.

Root cause, exactly (validation §6.2–§6.6):

1. **The judge is the only tier that fires on real data, and it is structurally
   a single source.** Across the probed sample every finding was
   `source="judge"`. `sympy_veto` fired **0** (no sign-mutant reference/student
   pairs exist in this corpus). `bank_pattern` fired **0** (see #3).
2. **`gate.py` never docks a lone judge finding.** The current contract:
   deterministic `sympy_veto` self-docks, OR ≥2 independent sources agree with a
   judge clearing tau; a lone judge → `needs_clarification`, never a dock. With
   the judge as the only live source, nothing corroborates and nothing docks —
   *even when the judge is right and confident* (attempt 110's judge fired at
   0.635 with correct `clear` on the covered nodes; attempt 88/95 at 0.952/0.973
   — all downgraded to `needs_clarification`, none docked).
3. **`bank_pattern` cannot corroborate because its 0.80 cosine floor guarantees
   zero recall on this bank.** Measured (production `text-embedding-3-large`,
   correctly concept-scoped): the *correct* top code was retrieved on 88/95/110
   at similarities **0.582 / 0.614 / 0.675** — all below 0.80. And sibling codes
   on the same concept cross-match at ~0.74, so *any* fixed absolute floor that
   admits the true code also admits its siblings. The floor is a
   representation/threshold mismatch, not a scoping bug: the ranking is right,
   the absolute number is uncalibratable.
4. **Every judge signature is `unkeyed:*`, never `misc.<code>`.** The judge tier
   emits `signature=f"unkeyed:{concept_key}"` unconditionally (`judge.py`
   `_finding_from_row`/`_all_clear`). So even if a judge finding could dock, it
   would never emit a keyed `misconceptions[]` ledger row (merge only keys
   `misc.<code>` signatures, A5) and could never promote through the emergent
   store. Judge signatures being unkeyed is *itself downstream of* `bank_pattern`
   never firing to seed a keyed signature.

**Net:** the binding constraint is the corroboration design (a lone judge never
docks) compounded by `bank_pattern`'s uncalibratable floor and the judge's
un-keyed signatures. Fixing the judge parsing (already done) moved nothing. The
hard constraint held — **zero strong-control false positives** — but
non-vacuously only for attempt 77 (judge independently cleared it at ~0.996).

## 2. Goal

Make the detector **produce correct docks on real misconception attempts
without introducing a single strong-control false positive**, by:

- **G1.** Letting a **bank-keyed, high-confidence judge finding dock on its
  own** — a deliberate, calibrated corroboration decision (validation §6.6 item
  3b), NOT a silent gate loosening. A lone judge that is *not* bank-keyed still
  routes to `needs_clarification` (unchanged).
- **G2.** **Keying judge findings to `misc.<code>`** by having the judge name the
  matched bank code and **validating that code against the concept's own bank
  entries** (never trusting a hallucinated code). A validated code upgrades the
  finding's signature from `unkeyed:*` to `misc.<code>`; an unvalidated one stays
  `unkeyed:*`.
- **G3.** **Floor-free bank-ranking corroboration** (proposal 3): let
  `bank_pattern`'s *best-ranked* match corroborate a co-keyed judge finding
  regardless of the 0.80 absolute cosine floor, so the two independent tiers can
  agree on the same `misc.<code>` even when neither clears an absolute number.
- **G4.** **Severity gradient / anti-dilution ceiling** (proposal 4): a **lone
  judge** finding may *subtract* (graduated penalty) but must **NEVER trip the
  hard band ceiling**. Only a **deterministic `sympy_veto`** or a
  **bank-corroborated dock on a maximally-central concept** may set
  `ceiling_applied=True`. This defeats "one confident LLM opinion nukes the band"
  while keeping "a real, corroborated, load-bearing misconception caps below
  Strong."
- **G5.** Keep the whole change **behind `APOLLO_MISCONCEPTION_DETECTOR` (default
  OFF)**, byte-identical to today when OFF, and re-validate on the same
  20-attempt set before any environment flip.

**Non-goals (unchanged from predecessor §2):** N1 D5 clarification-refuted
wiring; N2 conceptual-omission; N3 resolver nondeterminism; N4 promoting the
shadow graph grader. See §10 for the additional OUT items specific to this spec.

## 3. Locked decisions

These are settled in brainstorming and are NOT to be re-litigated in the plan:

- **L1.** A **lone judge finding can dock ONLY IF it is bank-keyed** (its named
  code validated against the concept's bank) **AND** clears its routed tau
  (`TAU_FIRE` token-prob path / `TAU_FIRE_VERBALIZED` verbalized path, A1). A
  lone judge that is bank-keyed but sub-tau → `needs_clarification`. A lone judge
  that is NOT bank-keyed → `needs_clarification` regardless of confidence
  (unchanged from today). **(A9)**
- **L2.** A **lone judge dock is penalty-only**: it contributes to
  `misconception_penalty` but MUST NOT set `ceiling_applied=True`, even on a
  maximally-central concept. **(A12)**
- **L3.** **Only two things trip the hard band ceiling:** (a) a deterministic
  `sympy_veto` dock, or (b) a **bank-corroborated** dock (judge + bank_pattern
  agreeing on the same `misc.<code>`) **on a maximally-central concept**. **(A12)**
- **L4.** **Bank corroboration is floor-free and code-scoped** (proposal 3): a
  `bank_pattern` finding corroborates a judge finding iff they share the same
  concept AND the same bank code (`misc.<code>`), using the bank's *best-ranked*
  match — the `BANK_SIM_FLOOR` no longer gates whether a match may
  **corroborate**. `BANK_SIM_FLOOR` is retained ONLY as the gate for
  `bank_pattern` firing as a *standalone/self-standing* signal (kept for API
  compatibility; not the corroboration path). **(A10)**
- **L5.** **The judge names the code; the code is validated, never trusted.** The
  judge output row gains an optional `"misconception_code"` string. On parse, the
  code is accepted ONLY if it exactly matches one of the `code` values in the
  concept's supplied `bank_entries`. A non-matching / absent / hallucinated code
  → the finding keeps `signature="unkeyed:<concept_id>"`. **(A11)**
- **L6.** All new thresholds are **env-overridable constants in `config.py`**
  with conservative pre-calibration defaults (favor false-negative over
  false-positive; predecessor R3). No inline literals.
- **L7.** **`bank_pattern` still returns its best-ranked match even below the
  floor**, tagged so the gate can tell a floor-clearing standalone hit from a
  below-floor corroboration-only hit. This is the one behavior change to
  `bank_pattern`'s *output shape*; its firing contract (`>= BANK_SIM_FLOOR`
  yields a standalone finding) is preserved. **(A10)**

## 4. Per-module architecture change

Only five modules change. `centrality.py`, `sympy_veto.py`, `apply.py`,
`detector.py`, `bank_pattern.py`'s Postgres/SQLite dispatch, and every wiring
edit in `done.py`/`artifact_build.py`/`artifact_writer.py`/`coverage.py` are
**untouched**. (`bank_pattern.py` gains one below-floor emission branch — see
§4.5 — but its dispatch and soft-fail structure are unchanged.)

### 4.0 The concept-key namespace mismatch — the load-bearing precondition (A13)

**This subsection is a hard blocker for everything that follows.** Verified in
code, the two corroborating tiers key their findings in **two different
`concept_key` namespaces**, and the gate's per-concept grouping silently keeps
them apart, so the floor-free co-key path (rows 3/3b/4, §7) **cannot fire in
production** as originally written:

- **Judge tier** keys each finding by the reference-graph node's **semantic
  `node_id`** — `detector.py::_judge_concept_inputs` builds
  `JudgeConceptInput(concept_key=node.node_id, …)` (line 129), and
  `judge.py::_finding_from_row` carries that through
  (`concept_key=concept_key`). `node_id` is a semantic string
  (`Field(min_length=1)`), one per reference-graph node.
- **`bank_pattern` tier** keys every finding by the **integer concept FK cast to
  string** — `bank_pattern.py::_finding_for_match` sets
  `concept_key=str(entry.concept_id)` (line 141). Worse than a mere encoding
  difference: `concept_id` is the **session-level** scalar (`sess.concept_id`,
  a single integer scoping the whole attempt's bank), so **every** bank_pattern
  finding in an attempt shares the **same** `str(concept_id)` key, whereas the
  judge emits **one finding per node** under distinct `node_id`s. The two
  namespaces differ in both **encoding and cardinality/granularity**.
- **`gate.py`** groups strictly by `concept_key`
  (`by_concept.setdefault(finding.concept_key, …)`, line 62) and `done.py:515`
  passes `detection.per_concept` straight to `gate_findings` with **no
  re-keying**. Because `node_id != str(concept_id)` by construction, a judge
  finding and a bank_pattern finding on the same underlying misconception land
  in **different groups**, so within any group `B = best bank_pattern finding on
  this concept` is absent from the judge's group and `co_key(J,B)` is never
  evaluable.
- **`centrality.py`** keys its `{concept: weight}` map by `node_id`
  (`compute_centrality`, `n.node_id`; the invariant is stated in
  `sympy_veto.py:53-59`: "centrality.py keys its `{node_id: centrality}` map the
  same way"). A bank finding's `str(concept_id)` is therefore **absent from the
  centrality map** and falls to the `CENTRALITY_W_MIN` floor in both
  `merge.py::_severity_for` (line 119) and `merge.py::_any_central` (line 142) —
  so a bank-keyed representative can never be "maximally central" and can never
  trip the ceiling.

The existing gate unit tests
(`test_misconception_detector_gate.py`) **hide** this: every corroboration test
gives BOTH synthetic findings the SAME shared string key (e.g.
`_finding(concept_key=key, source="bank_pattern", …)` and
`_finding(concept_key=key, source="judge", …)`), so the group forms and the
truth-table asserts pass while the real cross-namespace production path is dead.
Every §13 acceptance claim depends on this path and, unfixed, reproduces the
same 0-detection NO-OP the spec exists to fix.

**Fix (A13): the gate matches corroboration on the validated `bank_code`, NOT on
`concept_key`, and always docks under the JUDGE finding's (node_id) key so
centrality resolves.** Two coordinated changes, both inside `gate.py` (no change
to `detector.py`, `bank_pattern.py`'s keying, or `centrality.py`):

1. **Co-key on `bank_code`, gate-globally.** `gate_findings` still groups the
   *decision unit* per judge/deterministic concept, but the co-key predicate
   `co_key(J,B)` is evaluated by scanning **all** `bank_pattern` findings in the
   detection result for one whose `bank_code` equals the judge finding's
   validated `bank_code` (both non-None and equal) — it does **not** require
   `J.concept_key == B.concept_key`. Concretely, `_gate_one_concept` receives (in
   addition to its own group) the set of bank_pattern findings available for
   co-keying; the corroborating `B` is `max` over bank findings with
   `B.bank_code == J.bank_code` (ties broken by similarity/confidence). This is
   what makes a session-scoped bank finding (`str(concept_id)`) corroborate a
   node-scoped judge finding (`node_id`) on the same misconception `misc.<code>`.
2. **Dock under the judge's key.** For the bank-corroborated dock (rows 3/4) and
   every judge-originated dock, the **docked representative is the JUDGE
   finding** — it carries the node_id `concept_key` that `centrality` can
   resolve, the validated `bank_code`, and the evidence span. The bank finding is
   consumed only as the corroborating witness; it is never the docked
   representative (its `str(concept_id)` key would silently floor-weight in
   `merge` and could never be central). This requirement is **load-bearing for
   both grouping and centrality**, and §4.4 row 3 already says "prefer the judge
   finding as the docked representative" — A13 makes that mandatory, not a
   preference, for any bank-corroborated dock.

Restructuring the gate signature (change 1) is a mechanical, testable edit:
`gate_findings` splits the incoming findings into (deterministic ∪ judge groups)
and a flat `bank_by_code: dict[str, list[ConceptFinding]]` index keyed by
`bank_code`, and threads the matching bank list into `_gate_one_concept`. The
grouping loop's *shape* (one representative per judge concept) is unchanged; only
the corroboration lookup crosses the namespace. **`done.py:515` stays a single
`gate_findings(detection.per_concept)` call — the reconciliation is entirely
inside `gate.py`, so no wiring edit is needed.**

**Test contract (A13):** the gate tests MUST exercise the **real cross-namespace
keys** — the judge finding keyed by a `node_id`-shaped string (e.g.
`"node.demand_curve"`) and the bank finding keyed by `str(concept_id)` (e.g.
`"42"`), with the co-key carried by a shared `bank_code` (e.g.
`"includes_transfers"`) — and assert the dock fires, is represented by the
**judge** finding (so `docked.concept_key == "node.demand_curve"`), and resolves
against a centrality map keyed on that same `node_id`. The old shared-synthetic-
key tests are replaced, not merely kept, so a regression to same-namespace-only
grouping fails.

### 4.1 `types.py` — `ConceptFinding` gains a corroboration-eligibility bit

**Before:** `ConceptFinding` has `verdict_token_prob_present: bool = True` as its
only origin bit. A judge finding's `signature` is always `unkeyed:<concept_id>`.

**After:** add two frozen fields (plus `ceiling_eligible`, declared in §4.6),
all defaulted so every existing *tier* constructor (sympy_veto, bank_pattern,
judge) keeps compiling. **Caution (A13):** the gate's
`_docked`/`_needs_clarification` builders are NOT plain copies — they
**enumerate** fields explicitly (`gate.py:145-170`), so a defaulted new field
does NOT automatically survive a dock. See §4.6 for the mandatory builder change
(switch to `dataclasses.replace`) that keeps `bank_code` /
`bank_match_above_floor` / `ceiling_eligible` from being silently dropped on
every docked finding:

```python
@dataclass(frozen=True)
class ConceptFinding:
    concept_key: str
    verdict: Verdict
    confidence: float
    severity: float
    evidence_span: str
    signature: str
    source: DetectorSource
    corroborated: bool
    verdict_token_prob_present: bool = True
    # A11: the validated bank code this finding names, or None. Set ONLY after
    # the code is validated against the concept's bank_entries (judge tier) or
    # taken from the matched entry (bank_pattern/sympy_veto). Drives whether a
    # lone judge finding is dock-eligible (A9) and how merge keys the row (A5).
    bank_code: str | None = None
    # A10: for a bank_pattern finding, True iff its best-ranked match cleared
    # BANK_SIM_FLOOR (a self-standing standalone hit) vs a below-floor
    # corroboration-only hit. Meaningless for non-bank sources; defaults True so
    # sympy_veto/judge constructors are unaffected.
    bank_match_above_floor: bool = True
```

- `bank_code` is redundant-but-explicit with `signature` (`misc.<code>` implies
  the code); it is kept as a separate field so the gate can test
  code-equality/keying cheaply without string-splitting `signature`, and so a
  future non-`misc` keying scheme does not overload `signature` parsing. **The
  invariant: `bank_code is not None` ⟺ `signature == f"misc.{bank_code}"`.**
- No field is ever mutated; `merge.py`/`gate.py` continue to build NEW instances.

`MergeOutcome`, `JudgeRaw`, `DetectionResult`, the Protocols, and
`JudgeConceptInput` are unchanged.

### 4.2 `config.py` — one new constant

**Before:** `TAU_FIRE`, `TAU_FIRE_VERBALIZED`, `SEVERITY_CLAMP`,
`CENTRALITY_W_MIN`, `CEILING_COMPOSITE`, `BANK_SIM_FLOOR`.

**After:** add

```python
# A9: minimum routed-tau confidence for a LONE bank-keyed judge finding to dock
# on its own (no second tier). Deliberately >= TAU_FIRE so a solo dock is
# strictly harder than a corroborated one. Pre-calibration default 0.90; MUST be
# tuned on the labeled 20-set before any env flip (R2). Applies on TOP of the
# A1 routed-tau check (the finding must clear BOTH its routed tau AND this).
TAU_SOLO_JUDGE: float = _float_env("APOLLO_MISC_TAU_SOLO_JUDGE", 0.90)
```

`BANK_SIM_FLOOR` semantics narrow (L4): it now gates only whether a
`bank_pattern` match is a *standalone* finding, not whether it may corroborate.
The constant, its name, and its default (0.80) are unchanged — only the
docstring is updated to say so. No other constant changes value.

### 4.3 `judge.py` — name a code, keep it optional, validate downstream

**Before:** the `_JSON_SCHEMA` row is `{concept_key, verdict, confidence,
evidence_span}` (all required, `strict:true`). `_finding_from_row` always sets
`signature=f"unkeyed:{concept_key}"`.

**After:**

1. **Schema (`_JSON_SCHEMA`)** gains one required field
   `"misconception_code"` of `{"type": "string"}` (under `strict:true` every
   field must be `required`; the model emits `""` when no code applies — an
   empty string is treated as "no code named"). The row becomes
   `{concept_key, verdict, confidence, evidence_span, misconception_code}`.
   `additionalProperties:false` and the `required` list both grow by one.
2. **`_SYSTEM_PROMPT`** gains one instruction: *"If — and only if — the student's
   belief matches one of the `known_misconceptions` you were given for this
   concept, put that misconception's `code` verbatim in `misconception_code`.
   Otherwise put an empty string. Never invent a code that was not in the
   `known_misconceptions` list."*
3. **`judge_concepts`** signature is UNCHANGED — no new parameter is needed
   because `concepts[i].bank_entries` already carries the allowed `code` set per
   concept:

   ```python
   def judge_concepts(
       *,
       problem_text: str,
       concepts: tuple[JudgeConceptInput, ...],
       judge_fn: JudgeFn,
   ) -> tuple[ConceptFinding, ...]:
   ```

   The internal change is that the per-concept loop now passes the matching
   `JudgeConceptInput` (specifically its `bank_entries`) into `_finding_from_row`
   so that function can validate the named code. Today `judge_concepts` already
   iterates `for c in concepts` and calls `_finding_from_row(c.concept_key,
   rows_by_key.get(c.concept_key), ...)`; the edit adds `c.bank_entries` (or `c`)
   to that call.
4. **`_finding_from_row`** validates and keys:
   - read `code = (row.get("misconception_code") or "").strip()`.
   - `allowed = {e.code for e in concept_input.bank_entries}`.
   - if `code and code in allowed`: `bank_code = code`,
     `signature = f"misc.{code}"`, `bank_code`-field set.
   - else: `bank_code = None`, `signature = f"unkeyed:{concept_key}"` (today's
     behavior).
   - `verdict`, `confidence`, `evidence_span`, `verdict_token_prob_present` logic
     is **unchanged** (A1 dual-tau origin bit intact).
   - `_all_clear` and the `row is None` branch keep `signature="unkeyed:..."`,
     `bank_code=None` (a soft-fail names no code).
5. **`_normalize_rows`** and the soft-fail structure are unchanged. A row missing
   `misconception_code` under the tolerant fallback path (a future model that
   drops the field) is treated as `code=""` → unkeyed (never a crash).

The single live `client.chat.completions.create` call in `make_openai_judge`
remains the sole coverage exemption; its logprob walk is unchanged.

### 4.4 `gate.py` — the new truth-table (the heart of this spec)

**Before:** `_gate_one_concept` docks on deterministic OR ≥2-source agreement;
a lone judge ≥tau → `needs_clarification`; everything else drops. Grouping and
corroboration both key off `concept_key`.

**After** (`_gate_one_concept` rewrite + a cross-namespace bank index in
`gate_findings`, A13). Three things are threaded through: the A1 routed tau (per
finding), the new `TAU_SOLO_JUDGE` (A9), and the **`bank_by_code` index** (§4.0)
so co-key can cross the judge/​bank namespace gap. `gate_findings` builds
`bank_by_code: dict[str, list[ConceptFinding]]` (every `bank_pattern` finding
with a non-None `bank_code`, grouped by that code) ONCE, then passes into each
`_gate_one_concept` call the bank findings whose code the concept's judge finding
names. The per-concept decision order:

1. **Deterministic present** (`source="sympy_veto"`): dock, `corroborated=True`,
   `ceiling_eligible=True` (this is the sympy path — see §4.6 for how "ceiling
   eligible" is carried). Unchanged from today except it now also asserts ceiling
   eligibility explicitly.
2. **Bank-corroborated dock (A10 + A13):** if this concept's `judge` finding `J`
   has `J.bank_code is not None` AND there exists a `bank_pattern` finding `B` in
   the detection result with `B.bank_code == J.bank_code` (looked up via
   `bank_by_code[J.bank_code]`, **NOT** by `concept_key` equality — the two live
   in different namespaces, §4.0), AND `J` clears its routed tau (A1), dock with
   `corroborated=True`, `ceiling_eligible=True`. `B` is used **regardless of
   `B.bank_match_above_floor`** (floor-free corroboration, L4). **The docked
   representative MUST be the JUDGE finding `J`** — it carries the node_id
   `concept_key` that `centrality` (also node_id-keyed) can resolve, plus the
   validated `bank_code` and evidence span. `B` (keyed by `str(concept_id)`) is
   the corroborating witness only; it is NEVER the docked representative, because
   its key is absent from the centrality map and would floor-weight to
   `CENTRALITY_W_MIN`, defeating the ceiling. This is the *strong* dock, and A13
   makes "prefer the judge finding" **mandatory**, not optional, here.
3. **Any other ≥2 independent-source agreement** (e.g. two non-judge sources, or
   a judge + bank_pattern that do NOT share a code but are both present): dock,
   `corroborated=True`, `ceiling_eligible=True` **iff** at least one of the
   agreeing sources is deterministic/bank-keyed; otherwise `ceiling_eligible`
   follows the same "bank-corroborated on central" rule in merge. **When a judge
   is present it remains the docked representative** (A13: only a node_id-keyed
   representative resolves centrality). (In practice on this corpus this branch is
   dormant; it preserves the predecessor's non-judge-pair dock.) The judge, if
   present, must clear its routed tau.
4. **Lone bank-keyed judge (A9) — the new solo-dock path:** exactly one
   independent source, it is `judge`, `bank_code is not None`, and its confidence
   clears BOTH its routed tau (A1) AND `TAU_SOLO_JUDGE`: dock,
   `corroborated=True`, **`ceiling_eligible=False`** (penalty-only, A12/L2). The
   judge finding (node_id-keyed) is the representative — resolves centrality for
   the penalty subtract even though it never trips the ceiling.
5. **Lone judge, sub-solo-tau or unkeyed:** `needs_clarification`
   (`corroborated=False`) if it clears its *routed* tau (so the clarification
   loop still gets the strongest signal), else drop. Unchanged routing target;
   the difference from #4 is only whether it becomes a dock.
6. **Lone bank_pattern** (judge absent for this code), even above floor:
   `needs_clarification` is NOT appropriate (bank_pattern has no clarification
   semantics) — **drop**. A standalone bank hit without a judge to corroborate
   does not dock (preserves the predecessor invariant that `bank_pattern` alone
   never docks) and does not route. This is the honest "one embedding match, no
   second opinion" case. (Note: because bank findings are indexed by `bank_code`
   and consumed only as witnesses for a same-code judge, a bank finding whose
   code no judge named simply never anchors a group — it is dropped here.)
7. **Lone judge sub-routed-tau (no keying):** drop (today's behavior).

### 4.5 `bank_pattern.py` — emit the best match even below floor

**Before:** a match `>= BANK_SIM_FLOOR` yields a finding; below floor →
abstain (no finding for that utterance).

**After:** compute the best-ranked match as today. Emit a `ConceptFinding` when
there is *any* best match with the entry's `bank_code` set, tagging
`bank_match_above_floor = similarity >= BANK_SIM_FLOOR`. So:
- `similarity >= BANK_SIM_FLOOR` → finding with `bank_match_above_floor=True`
  (standalone-eligible; today's behavior, unchanged).
- `0 < similarity < BANK_SIM_FLOOR` → finding with `bank_match_above_floor=False`
  (corroboration-only; the gate uses it ONLY to co-key a judge finding, never as
  a standalone dock).
- no bank / no utterances / embedding failure → abstain (unchanged soft-fail).

`_finding_for_match` sets `bank_code=entry.code` and
`signature=f"misc.{entry.code}"` (it already builds the `misc.<code>` signature;
this adds the parallel `bank_code` field). **A below-floor corroboration-only
finding must never reach `merge` as a standalone dock** — the gate's branch #6
guarantees this (a lone bank_pattern finding drops).

> **Anti-regression guard:** to keep the flag-OFF path and any caller that reads
> raw `bank_pattern` output unchanged, the below-floor emission is what changes
> the *pre-gate* finding count. This is internal to the detector chain (only
> `gate.py` consumes `bank_pattern` output) and is fully covered by the gate
> truth-table tests. No wiring or artifact shape changes.

### 4.6 Carrying "ceiling eligibility" from gate to merge

`ceiling_applied` is computed in `merge.py` today purely from centrality (`any`
docked finding on a max-central concept). Under A12 a **lone-judge dock on a
central concept must NOT trip the ceiling**, so merge needs to know *how* each
dock was reached.

The dock class **cannot** be re-derived from the finding's own fields: a keyed
lone-judge dock (row 5) and a keyed bank-corroborated dock (row 3) are
byte-identical afterward — same `source="judge"`, same non-None `bank_code`, same
`corroborated=True`. The only stage that knows which corroboration branch fired
is the gate. **Decision (A12): the gate stamps the eligibility explicitly.** Add
one frozen bit to `ConceptFinding`, set by the gate's `_docked` builder:

```python
# A12: True only when this dock is allowed to trip the anti-dilution band
# ceiling on a maximally-central concept. sympy_veto docks and bank-corroborated
# docks set it True; a lone-judge (penalty-only) dock sets it False. merge.py
# reads THIS, not centrality-plus-source-inference, to decide ceiling_applied.
ceiling_eligible: bool = False
```

added to `ConceptFinding` (defaulted False so non-dock findings and all existing
tier constructors are unaffected). `merge.py::_any_central` becomes: *a docked
finding trips the ceiling iff `ceiling_eligible AND centrality[concept] >=
max_centrality`.* This is the minimal, explicit, immutable way to separate the
two dock classes without merge re-deriving gate logic.

#### 4.6.1 The gate builders must propagate the full field set (A13 — implementation gap)

`gate.py`'s `_docked` and `_needs_clarification` builders (`gate.py:145-170`)
construct a **new** `ConceptFinding` by **enumerating fields explicitly** — they
are NOT `dataclasses.replace` and do NOT copy any field they don't name. Today
they thread only the pre-existing eight fields plus `verdict_token_prob_present`.
If this spec merely *adds* `ceiling_eligible` stamping to `_docked` while leaving
the builders enumerated, **every docked finding silently loses `bank_code` and
`bank_match_above_floor`** (they were never enumerated), which:

- re-breaks A5/A11 keying — `merge.py::_is_bank_keyed` reads `signature`, but the
  emergent feed and any `misc.<code>`-keyed consumer read `bank_code`; a docked
  representative that arrives with `bank_code=None` cannot key its ledger row and
  cannot promote (the exact NO-OP this spec fixes); and
- would leave `signature` and `bank_code` **inconsistent** on the docked row
  (signature `misc.<code>` but `bank_code=None`), violating the §4.1 invariant.

**Mandatory fix (A13): rewrite both builders to use `dataclasses.replace` so no
field is ever silently dropped.** Each builder changes only the verdict/​
corroboration/​eligibility fields and inherits everything else (`concept_key`,
`confidence`, `severity`, `evidence_span`, `signature`, `source`,
`verdict_token_prob_present`, `bank_code`, `bank_match_above_floor`) verbatim
from the incoming finding:

```python
import dataclasses

def _docked(finding: ConceptFinding, *, ceiling_eligible: bool) -> ConceptFinding:
    # ceiling_eligible passed by the caller per which truth-table branch fired
    # (row 1/2/3/4 = True; row 5 solo-judge = False). bank_code /
    # bank_match_above_floor / signature / concept_key all inherited so the
    # docked representative stays bank-keyed and centrality-resolvable (A13).
    return dataclasses.replace(
        finding,
        verdict="misconception",
        corroborated=True,
        ceiling_eligible=ceiling_eligible,
    )

def _needs_clarification(finding: ConceptFinding) -> ConceptFinding:
    return dataclasses.replace(
        finding,
        verdict="needs_clarification",
        corroborated=False,
        # ceiling_eligible NOT overridden -> inherits the incoming finding's value,
        # which is always False for a pre-gate tier finding (tiers never set it), so
        # a clarification row never becomes ceiling-eligible. If defensiveness is
        # wanted, pass ceiling_eligible=False explicitly.
    )
```

Consequences to encode in the plan:

- `_docked` gains a **required** `ceiling_eligible` keyword (no default) so every
  call site is forced to state the branch's eligibility explicitly — a caller that
  forgets it is a compile-time error, not a silent False. The §4.4 decision order
  passes `ceiling_eligible=True` for rows 1/2/3 (and row 4 when a bank-keyed
  source agrees) and `ceiling_eligible=False` for row 5 (solo judge, penalty-only).
- Because the docked representative for any bank-corroborated dock is the **judge**
  finding (A13/§4.4 row 3), and the judge finding already carries the validated
  `bank_code` + `misc.<code>` signature (A11), `replace` preserves both — the row
  reaches `merge` correctly keyed **and** node_id-keyed (centrality-resolvable).
- **Test contract (A13):** a gate test must assert that a docked finding
  round-trips `bank_code`, `signature`, and `bank_match_above_floor` **unchanged**
  from the input judge finding (guarding against a future re-enumeration
  regression), in addition to the per-row `ceiling_eligible` assertions in §12.

### 4.7 `merge.py` — one-line ceiling rule change; penalty math unchanged

**Before:** `ceiling_applied = any(centrality[f] >= max_centrality for f in
docked)`. **After:** `ceiling_applied = any(f.ceiling_eligible and
centrality[f] >= max_centrality for f in docked)`.

Everything else in `merge.py` is unchanged: `severity = centrality * confidence`,
`penalty = min(clamp, Σ severity)` over ALL docked findings (lone-judge docks
included — they still subtract, L2), `misconceptions[]` keyed rows built from
bank-keyed docks (A5, now correctly populated because judge docks can be keyed),
`ledger_findings` = all docked findings.

> **Load-bearing dependency on A13 (§4.0/§4.4/§4.6).** Both centrality reads in
> `merge.py` — `_severity_for`'s `centrality.get(finding.concept_key,
> CENTRALITY_W_MIN)` (line 119) and `_any_central`'s
> `centrality.get(f.concept_key, …) >= max_centrality` (line 142) — resolve
> against a map that `compute_centrality(reference_graph)` keys by **`node_id`**.
> A `bank_pattern` finding's `concept_key` is `str(concept_id)`, which is
> **absent** from that map and floors to `CENTRALITY_W_MIN`, so a bank finding can
> never be maximally central and (even with co-key fixed at the gate) could never
> trip the ceiling if it were the docked representative. This one-line change is
> therefore correct **only because A13 mandates the JUDGE finding (node_id-keyed)
> as the docked representative of every bank-corroborated dock** (§4.4 rows 2/3).
> `merge.py` itself is not re-keyed and needs no change beyond this predicate; the
> guarantee comes from what the gate hands it. If a future change let a bank
> finding be the docked representative, this predicate would silently
> under-report the ceiling — a merge test must assert a bank-corroborated dock
> arrives node_id-keyed and trips the ceiling on the central concept.

## 5. Full gate truth-table

The decision unit is a **judge/deterministic concept** (grouped by the judge or
sympy finding's own `concept_key` = `node_id`); the corroborating bank witness is
found across the whole detection result by **`bank_code`, not `concept_key`**
(A13/§4.0 — the two tiers live in different `concept_key` namespaces). Let: `D` =
a `sympy_veto` finding present; `J` = the best `judge` finding on this concept
(node_id-keyed); `B` = the best `bank_pattern` finding **anywhere in the
detection result with `B.bank_code == J.bank_code`** (looked up via
`bank_by_code[J.bank_code]`, session-scoped `str(concept_id)`-keyed — NOT
required to share `J.concept_key`); `routed_ok(J)` = `J.confidence >= (TAU_FIRE if
J.verdict_token_prob_present else TAU_FIRE_VERBALIZED)` (A1); `solo_ok(J)` =
`routed_ok(J) and J.confidence >= TAU_SOLO_JUDGE` (A9); `co_key(J,B)` =
`J.bank_code is not None and J.bank_code == B.bank_code` (A10, ignores
`B.bank_match_above_floor`). **In every DOCK row the docked representative is `J`
(or `D` for sympy) — never `B` — so the docked `concept_key` is a `node_id` that
`centrality` resolves (A13).**

| # | Case (sources present) | Condition | Outcome | `corroborated` | `ceiling_eligible` | Docked signature |
|---|---|---|---|---|---|---|
| 1 | **sympy solo** (D, no J/B) | always | **DOCK** | True | **True** | `misc.<code>` (from D) |
| 2 | sympy + anything | always | **DOCK** (prefer D) | True | **True** | `misc.<code>` (from D) |
| 3 | **judge + bank agree** (J, B, `co_key`) | `routed_ok(J)` | **DOCK** (prefer J) | True | **True** | `misc.<code>` (validated) |
| 3b | judge + bank agree, `co_key`, judge sub-routed-tau | `not routed_ok(J)` | J → **needs_clarification**; B dropped | False | — | (n/a) |
| 4 | judge + bank, present but NOT co-keyed | `routed_ok(J)` | **DOCK** (≥2 sources; **representative = J** for centrality, A13) | True | True iff a bank-keyed source agrees, else False | J's `misc.<code>` if keyed else `unkeyed:<node_id>` |
| 5 | **lone judge ≥ solo-tau, bank-keyed** (J only, `bank_code`≠None) | `solo_ok(J)` | **DOCK** (penalty-only) | True | **False** | `misc.<code>` (validated) |
| 6 | lone judge ≥ routed-tau, **bank-keyed but sub-solo-tau** | `routed_ok(J) and not solo_ok(J)` | **needs_clarification** | False | — | (n/a) |
| 7 | **lone judge, ≥ routed-tau, UNKEYED** (`bank_code`=None) | `routed_ok(J)` | **needs_clarification** | False | — | (n/a) |
| 8 | lone judge, **sub-routed-tau** (keyed or not) | `not routed_ok(J)` | **DROP** | — | — | (n/a) |
| 9 | **lone bank** (B only, no J), any floor | always | **DROP** (no second opinion; bank alone never docks) | — | — | (n/a) |

Notes:
- Rows 5 vs 3: a **corroborated** dock (row 3) trips the ceiling; a **lone-judge**
  dock (row 5) does not — this is the A12/proposal-4 severity gradient. Both
  subtract from the composite; only the corroborated one caps the band.
- Row 9 is the deliberate "one embedding match is not evidence" rule — a
  below-floor bank match exists only to *co-key* a judge (rows 3/4), never to
  stand alone.
- `solo_ok` requires clearing BOTH the routed tau AND `TAU_SOLO_JUDGE`, so a solo
  dock is strictly harder than a corroborated one (which needs only the routed
  tau). This is intentional: solo docks carry the most FP risk, so they face the
  strictest bar (predecessor R3: favor FN over FP).

## 6. Keying + code-validation against the concept bank

The judge is told each concept's `known_misconceptions` (code + description) in
the user prompt (already built by `_user_prompt`). The judge echoes a
`misconception_code` per row when the student's belief matches one. Validation
(A11, in `_finding_from_row`):

```
code = (row.get("misconception_code") or "").strip()
allowed = {e.code for e in concept_input.bank_entries}
if code and code in allowed:
    bank_code, signature = code, f"misc.{code}"
else:
    bank_code, signature = None, f"unkeyed:{concept_key}"
```

- The code is **validated against the SPECIFIC concept's bank entries**, not a
  global code set — a code valid for a different concept is rejected on this one.
- A hallucinated / empty / cross-concept code degrades gracefully to `unkeyed:*`
  (today's behavior). No crash, no trust of unverified LLM output.
- This closes validation §6.6-item-3's "judge signatures all `unkeyed:*`"
  observation and is what makes rows 3/4/5 able to emit a keyed
  `misconceptions[]` ledger row (A5) and thus feed the emergent store.

Consistency mirror: `apollo/clarification/candidate_assembly.py:56` already forms
`f"misc.{e.code}"` as the canonical key — the judge-validated signature uses the
identical scheme, so a judge-detected and a bank/clarification-detected
occurrence of the same misconception aggregate under one signature downstream.

## 7. Floor-free bank-ranking corroboration (proposal 3)

`bank_pattern` now emits its **best-ranked** match even below `BANK_SIM_FLOOR`
(§4.5), tagged `bank_match_above_floor`. The gate's `co_key(J,B)` (rows 3/4) uses
that match **regardless of the tag** — the floor no longer decides whether a bank
match may corroborate a judge that named the same code. Rationale (validation
§6.6 item 1): the bank *ranking* is correct (right top code on 88/95/110) even
though the absolute similarity (0.58–0.68) is below any usable floor and sibling
codes sit at ~0.74. So:

- **Corroboration** trusts the *agreement of two independent tiers on the same
  code* (judge-named + bank-ranked), not an absolute similarity number.
  Crucially, "same code" means **same validated `bank_code`**, matched across the
  judge and bank namespaces (A13/§4.0) — NOT same `concept_key`; the judge keys by
  `node_id`, the bank by session-scoped `str(concept_id)`, so a `concept_key`-based
  match would never fire.
- **Standalone** bank firing still requires `>= BANK_SIM_FLOOR` (row 9 drops a
  below-floor lone bank anyway, so the floor only matters if a future tier wants
  a standalone bank signal — kept for compatibility).
- This makes the common real case (judge confidently names `includes_transfers`,
  bank ranks `includes_transfers` top at 0.675) a **corroborated dock (row 3)**
  that trips the ceiling on a central concept — exactly the false-Strong that
  the campaign showed the detector missing.

No ROC/threshold re-calibration of `BANK_SIM_FLOOR` is required by this spec
(that remains a documented OUT item, §10) because corroboration is now
floor-independent.

## 8. Severity gradient / anti-dilution ceiling policy (proposal 4)

The composite is affected two ways (predecessor A4): a graduated **subtract**
(`misconception_penalty`, clamped at `SEVERITY_CLAMP`) and a hard **band ceiling**
(`ceiling_applied` → `apply.py` caps the composite at `CEILING_COMPOSITE=0.84`,
below the named Strong band 0.85). This spec makes the ceiling **strictly harder
to trip than the subtract**:

- **Every dock subtracts** — lone-judge docks included (`severity = centrality *
  confidence`, summed and clamped). A confident lone judge on a central concept
  can pull the composite down by up to `SEVERITY_CLAMP`, moving the band by
  graduation.
- **Only these trip the hard ceiling** (`ceiling_eligible=True` AND concept is
  maximally central):
  1. a **deterministic `sympy_veto`** dock (row 1/2), or
  2. a **bank-corroborated** dock — judge + bank agreeing on the same code (rows
     3, and row 4 when a bank-keyed source agrees).
- **A lone judge NEVER trips the ceiling** (row 5, `ceiling_eligible=False`),
  even at confidence 1.0 on the single most central concept. One LLM opinion,
  however confident, cannot by itself cap a student below Strong — it can only
  graduate the score down. This is the anti-dilution *and* anti-overreaction
  guard: corroborated/deterministic evidence caps the band; a solo opinion only
  nudges the number.

The `ceiling_eligible` bit is stamped by the gate (§4.6) and read by
`merge.py::_any_central` (§4.7). `apply.py` is untouched — it already caps at
`CEILING_COMPOSITE` when `outcome.ceiling_applied` (A4/A8 intact).

## 9. Data flow

```
done.py::handle_done  (flag-gated on detector_enabled(); OFF → byte-identical)
└─ detect_misconceptions            (UNCHANGED orchestrator; loads bank, runs 3 tiers)
     ├─ sympy_veto                   (UNCHANGED — deterministic, sets bank_code from mutant code)
     ├─ bank_pattern                 (§4.5 — now emits best match even below floor, tagged)
     └─ judge_concepts               (§4.3 — names misconception_code; §6 validates → bank_code)
   ↓ gate_findings                   (§4.4/§5 — NEW truth-table: solo-keyed-judge dock + floor-free co-key)
     · A13: indexes bank findings by bank_code (crosses the node_id vs str(concept_id) namespace gap),
       co-keys on bank_code not concept_key, docks under the JUDGE finding (node_id) so centrality resolves
     · stamps corroborated + ceiling_eligible on each dock (via dataclasses.replace — no field dropped)
   ↓ merge_detections                (§4.7 — ceiling_applied reads ceiling_eligible; penalty math unchanged)
   ↓ apply_penalty / rubric_overall_after_penalty   (UNCHANGED, A4/A8)
   ↓ build_llm_artifact              (UNCHANGED wiring; now receives keyed misconceptions[] rows)
   ↓ emergent store feed             (UNCHANGED; gated on APOLLO_EMERGENT_MISCONCEPTIONS; now gets keyed rows)
```

The only stages that change are `bank_pattern` (output shape), `judge` (schema +
validation), `gate` (truth-table + stamping), `merge` (one predicate),
`types`/`config` (new fields/constant). Every wiring edit from the predecessor
plan (T10–T15) stands as-is.

## 10. Error handling / soft-fail

All predecessor soft-fail contracts hold verbatim and are extended to the new
code:

- **Judge code-validation never raises.** A missing/empty/non-string
  `misconception_code`, a code not in `allowed`, or absent `bank_entries` →
  `bank_code=None`, `signature="unkeyed:..."`. Wrapped by the same
  `_finding_from_row` that already tolerates malformed rows.
- **`bank_pattern` below-floor emission never raises.** The best-match
  computation already soft-fails to "no match" on any embedding error; adding a
  finding for a below-floor best-match is pure tuple construction.
- **Gate is pure** (no IO/LLM/DB) and total: every case in the §5 table has a
  defined outcome; the `_gate_one_concept` default remains DROP. A malformed
  finding (e.g. `bank_code` set but `signature` inconsistent) is handled
  conservatively — the gate keys corroboration off `bank_code` only, and merge
  keys the ledger row off `signature` (A5), so an inconsistent pair simply fails
  to co-key (safe: it drops or clarifies rather than docks).
- **`detector.py` orchestrator** already wraps every tier in try/except → zero
  findings from a raising tier. Unchanged. A defect in the new judge validation
  or bank emission surfaces as "that tier contributed nothing," never a crash.
- **The whole detect→gate→merge chain in `done.py`** is inside one
  `try/except Exception` → `detection_outcome=None` → unpenalized grade, HTTP 200
  (predecessor §6.1). Unchanged.

## 11. Scope boundaries

**IN scope (this spec):**
- `types.py`: `bank_code`, `bank_match_above_floor`, `ceiling_eligible` fields
  (all defaulted, immutable).
- `config.py`: `TAU_SOLO_JUDGE` constant; `BANK_SIM_FLOOR` docstring narrowing.
- `judge.py`: `misconception_code` in schema + prompt; code-validation keying in
  `_finding_from_row`; `judge_concepts` passing per-concept `bank_entries` to
  validation.
- `gate.py`: the §5 truth-table (solo-keyed-judge dock, floor-free co-key,
  ceiling stamping); the A13 cross-namespace reconciliation (co-key on
  `bank_code` via a `bank_by_code` index, judge finding as the docked
  representative, `_docked`/`_needs_clarification` switched to
  `dataclasses.replace` so `bank_code`/`bank_match_above_floor`/`ceiling_eligible`
  survive a dock).
- `bank_pattern.py`: below-floor best-match emission with the floor tag.
- `merge.py`: one-line `ceiling_applied` predicate change.
- Tests for all of the above; re-run of `campaign/validate_misconception_detector.py`.
- Owner-doc reconciliation of `docs/architecture/apollo.md`.

**OUT of scope (explicitly deferred — documented, not silent):**
- **O1. Seeding `sympy_veto` `eq:`-mutants.** No sign-mutant reference/student
  pairs exist in the corpus; sympy_veto stays inert on this data. Seeding mutants
  into `MisconceptionEntry.trigger_phrases` (`eq:` prefix) is an **offline data
  authoring task**, not code, and is a prerequisite for any sympy_veto claim
  (validation §6.6 item 2). This spec does NOT author mutants.
- **O2. Fixing the bank's description→trigger-phrase representation.** The bank
  embeds `description` only; utterance-vs-`trigger_phrase` scores ~0.06–0.13
  higher (validation §6.6 item 1). Indexing `trigger_phrase` embeddings (a
  migration + re-embed) is a **separate data/schema task**. This spec sidesteps
  it entirely by making corroboration floor-free (§7) — it does NOT change what
  the bank embeds.
- **O3. Re-calibrating `BANK_SIM_FLOOR` off a labeled ROC/PR pass.** Not needed
  here (corroboration is floor-independent); remains a future tuning task.
- **O4. Wiring `detector`'s `needs_clarification` into a live student
  follow-up** (D5). The gate still routes lone/keyed-sub-solo-tau and
  unkeyed-judge findings to `needs_clarification`, but consuming that route as a
  live clarification question is bespoke, uncalibrated D5 work (predecessor N1).
  This spec produces the signal; it does NOT consume it.
- **O5. Promoting the shadow graph grader** (`GRAPH_GRADER_LIVE` stays OFF, N4).
- **O6. Tuning `TAU_SOLO_JUDGE`/`TAU_FIRE`/`SEVERITY_CLAMP`/centrality curve** to
  final values. Defaults ship conservative; final calibration is a labeled-set
  tuning pass (R2), gated before the env flip.

## 12. Testing strategy (95% patch-coverage gate)

Contract: `pytest --cov --cov-report=xml` then
`diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95` on all
changed lines. Every changed module is pure or DI-seamed, so 100% of new lines
are reachable offline.

- **`types.py`** — the three new fields default correctly; a keyed finding
  satisfies the `bank_code is not None ⟺ signature == misc.<bank_code>`
  invariant; frozen-mutation still raises.
- **`config.py`** — `TAU_SOLO_JUDGE` parses / falls back on malformed env; default
  0.90; `BANK_SIM_FLOOR` still 0.80.
- **`judge.py`** — stub `JudgeFn` returning a row with `misconception_code`
  matching a concept's bank code → finding is keyed (`bank_code`, `misc.<code>`
  signature); a hallucinated/empty/cross-concept code → `unkeyed:*`, `bank_code
  None`; malformed JSON still soft-fails all-`clear` unkeyed; the schema/prompt
  changes covered by the parse tests (no network). `make_openai_judge`'s live
  line stays the sole documented exemption; its logprob walk covered by a
  fabricated `resp`.
- **`bank_pattern.py`** — above-floor match → finding `bank_match_above_floor
  True`; below-floor best match → finding `bank_match_above_floor False`,
  `bank_code` set; no bank / embed failure → abstain. SQLite in-memory cosine
  path with a stub `embed_fn` (no pgvector).
- **`gate.py`** — one test per §5 row (1,2,3,3b,4,5,6,7,8,9): assert dock vs
  clarify vs drop, `corroborated`, `ceiling_eligible`, and the docked signature.
  **A13 test contract (MANDATORY, replaces the shared-synthetic-key tests):**
  every co-key test MUST use the **real cross-namespace keys** — the judge finding
  keyed by a `node_id`-shaped string (e.g. `"node.demand_curve"`), the bank
  finding keyed by `str(concept_id)` (e.g. `"42"`), with agreement carried ONLY by
  a shared `bank_code` (e.g. `"includes_transfers"`). Assert (a) the dock fires,
  (b) the docked representative is the **judge** finding
  (`docked.concept_key == "node.demand_curve"`, NOT `"42"`), (c) the docked row
  round-trips `bank_code`/`signature`/`bank_match_above_floor` unchanged (the
  `dataclasses.replace` guarantee, §4.6.1), and (d) it resolves against a
  centrality map keyed on that same `node_id` (feeds the merge ceiling test). A
  same-namespace-only test (both findings sharing one `concept_key`) is
  explicitly a REGRESSION and must NOT be the co-key coverage. Key adversarial
  cases: **row 5** (lone keyed judge at 0.95 docks penalty-only,
  `ceiling_eligible=False`); **row 3** (judge `node.X`@0.86 + bank `42`@0.60
  below-floor sharing `bank_code` still docks under `node.X`,
  `ceiling_eligible=True` — the floor-free cross-namespace path); **row 9** (lone
  bank at 0.99 whose code no judge named does NOT dock); **row 7** (lone unkeyed
  judge at 0.99 → clarify, never dock).
- **`merge.py`** — a `ceiling_eligible=False` docked finding on the
  max-central concept → `ceiling_applied=False` but nonzero penalty (the
  severity gradient); a `ceiling_eligible=True` dock on the same concept →
  `ceiling_applied=True`; penalty still sums all docks and clamps; keyed rows
  emitted for `misc.<code>` docks incl. the now-keyable judge docks. **A13
  regression guard:** the `ceiling_eligible=True` dock under test carries a
  **`node_id` `concept_key`** present in the centrality map (mirroring what the
  gate now hands merge for a bank-corroborated dock), so `_severity_for` and
  `_any_central` resolve it as central; add a contrasting case where the same dock
  carries a `str(concept_id)` key **absent** from the map and confirm it floors to
  `CENTRALITY_W_MIN` and does NOT trip the ceiling — documenting exactly why the
  docked representative must be node_id-keyed.
- **Flag-OFF byte-identical guarantee.** The predecessor's T15 golden suite
  (`test_misconception_flag_off_golden.py`) must still pass **unchanged**: with
  `APOLLO_MISCONCEPTION_DETECTOR` OFF, `handle_done` + `build_llm_artifact` are
  byte-identical to pre-detector output. Because all changes are inside the
  detector chain (only reached when the flag is ON) and every new
  `ConceptFinding` field is defaulted, the OFF path constructs and compares
  identically. Add an explicit assertion that the new `types.py` fields do not
  appear in any flag-OFF artifact/rubric dict (they are internal to the chain,
  never serialized).
- **Emergent-feed test** (predecessor T14): with both flags ON, a co-keyed
  judge+bank dock (row 3) now writes a **non-zero** keyed row to
  `apollo_misconception_observations` — the assertion the predecessor run could
  never satisfy because judge signatures were unkeyed.

## 13. Validation plan

Re-run the **exact same A/B** as `docs/_archive/experiments/2026-07-08-misconception-detector-validation.md`:

```
APOLLO_MISCONCEPTION_DETECTOR=1 python -m campaign.validate_misconception_detector
```

`full_judge` mode (real `make_openai_judge`, gpt-4o, temp 0.0), local Docker
Postgres + Neo4j, the same 20 attempt_ids. Acceptance (predecessor spec §7,
sharpened by the validation baseline):

- **Detection rate rises from 0/16** on the misconception-class attempts (target:
  the co-key path fires on at least 88/95/110, whose bank rankings are known
  correct — floor-free corroboration should now dock them).
- **False-Strong on misconception-class drops materially from 7** (target ≥ half
  reduction, predecessor §7) — driven by the ceiling on corroborated central
  docks and the subtract on lone-judge docks.
- **ZERO strong-control false positives (hard constraint).** Controls 77/89/106
  (strong) and 97 (partial) must keep `penalty=0.0`,
  `baseline_band==detector_band`, `control_credit_ok=True`. Attempt 77's judge
  already independently clears at ~0.996, so the solo-dock path must not fire on
  it (its judge verdicts are `clear`, not `misconception`, so no dock — verify).
- **Keyed ledger rows appear** (`misconceptions_found != []`) on the docked
  attempts — the observable that the two prior runs both showed empty.
- Record the result as a new dated section appended to the SAME validation
  experiment file (or a sibling `-v2` writeup), with the full 20-row table, so
  the before/after is one artifact. Honesty contract preserved: report the delta
  as measured, do not claim a fix worked without the observable rows.

## 14. Drift-doc reconciliation

On landing, in the SAME commit, update `docs/architecture/apollo.md` (owner,
`owns: apollo/overseer/misconception_detector/**`):

- In the `apollo/overseer/` row's `misconception_detector` paragraph and in
  `## Main data flows (b)` step 5a, revise the `gate.py` description from
  *"sympy_veto self-corroborates, else ≥2 independent sources agreeing, else a
  lone/sub-τ judge routes to needs_clarification instead of docking"* to the new
  truth-table: **a bank-keyed judge finding may dock on its own above
  `TAU_SOLO_JUDGE` (penalty-only, never trips the ceiling); a judge + bank_pattern
  agreeing on the same validated `misc.<code>` corroborate floor-free and may trip
  the ceiling on a central concept; sympy_veto self-docks; a lone bank match or a
  lone unkeyed/sub-tau judge does not dock.**
- Note `judge.py` now emits a validated `misconception_code` → `misc.<code>`
  signature (keying), and `bank_pattern.py` now emits its best match even below
  `BANK_SIM_FLOOR` (corroboration-only).
- Add `TAU_SOLO_JUDGE` to the flag/constants list next to `TAU_FIRE`.
- Bump `last_verified` to `2026-07-08`.
- No `owns:` glob change (all files already covered). No new flag (reuses
  `APOLLO_MISCONCEPTION_DETECTOR`).

## 15. Amendment ledger (this spec)

| ID | Amendment | Module |
|---|---|---|
| A9 | Lone bank-keyed judge ≥ `TAU_SOLO_JUDGE` may dock (penalty-only) | gate, config |
| A10 | Floor-free bank-ranking corroboration; `bank_pattern` emits below-floor best match tagged `bank_match_above_floor` | gate, bank_pattern, types |
| A11 | Judge names `misconception_code`; validated against the concept's own bank entries → keys `bank_code`/`signature` | judge, types |
| A12 | `ceiling_eligible` bit: only deterministic or bank-corroborated docks trip the band ceiling; a lone-judge dock is penalty-only | gate, merge, types |
| A13 | Cross-namespace keying reconciliation (§4.0): co-key on validated `bank_code` (not `concept_key` — judge keys `node_id`, bank keys `str(concept_id)`) via a `bank_by_code` index; the JUDGE finding is the docked representative of every bank-corroborated dock so `centrality` (node_id-keyed) resolves it; `_docked`/`_needs_clarification` switch to `dataclasses.replace` so `bank_code`/`bank_match_above_floor`/`ceiling_eligible` are never dropped; gate tests exercise real cross-namespace keys | gate |

A1/A4/A5/A6/A8 (predecessor) hold verbatim and are unchanged by this spec.
