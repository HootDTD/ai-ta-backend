# Plan: Apollo Graph-Grader Phase 1b — Exact-only alias channel

**Goal:** Let curated reference-solution phrasings (conditions / simplifications / definitions) resolve via an EXACT-normalized alias tier, lifting shadow-rubric coverage, WITHOUT opening a loose fuzzy channel that over-credits hand-waving.
**Architecture:** Pure resolution layer (`apollo/resolution/**`), upstream of `build_student_canonical`. No grade-math touched. Shadow-gated (`APOLLO_GRAPH_SIM_SHADOW_ENABLED`); live flag stays OFF.
**Tech stack:** Python 3 / FastAPI backend, pytest (`ai-ta-backend/.venv/Scripts/python.exe -m pytest`), ruff + mypy (BLOCKING on new/changed `.py`), diff-cover ≥95% patch vs `origin/staging`.

---
provides:
  - `Candidate.exact_aliases: tuple[str, ...]` field (new, defaulted)
  - exact-only alias resolution channel on reference-solution candidates
  - optional `content.aliases: list[str]` on reference-solution JSON steps
consumes:
  - `apollo/resolution/candidates.py` (Candidate dataclass, candidates_from_reference_solution)
  - `apollo/resolution/tiers.py` (match_alias_all, match_fuzzy_all)
  - reference-solution JSON `content` dicts (problem_01.json etc.)
depends_on:
  - Phase 1a (same branch `feat/apollo-grader-node-recovery`): ALSO edits `candidates.py`
    (adds `"derived"` method + `METHOD_CONFIDENCE_CAP["derived"]=0.95`). The merger
    sequences the two `candidates.py` edits; this plan's edits MUST compose (see Compose note).
  - PR #63 derived-eq work is already in the base (origin/staging).
---

## Overview

Phase 1b adds a precision-first alias channel for NON-equation reference nodes
(conditions / simplifications / definitions). Today reference candidates carry
`aliases=()` (`candidates.py:102`) so the alias/fuzzy tiers never fire for them;
only misconceptions carry aliases (from `trigger_phrases`, `candidates.py:133`).
A teacher can now hand-author `content.aliases: list[str]` on a reference-solution
step; those land in a NEW `exact_aliases` field that the EXACT-alias tier reads —
but the FUZZY tier deliberately does NOT read it. Net: curated phrasings resolve
via exact-normalized equality (high precision); they can never leak free coverage
credit through `token_set_ratio >= 0.9` hand-wave matching.

Four-line change surface plus tests. All edits are upstream of
`build_student_canonical`, so grade-math (`graph_compare/{core,scores,coverage,
bisimilarity,soundness}.py`) is byte-identical and its purity tests stay green.

## Prior art (sibling tiers)

- **Exact-alias tier:** `apollo/resolution/tiers.py:210-228` `match_alias_all` —
  `_normalize`-exact equality over `cand.aliases`, one `TierHitAll` per candidate
  (`tiers.py:224-227` is the loop to extend).
- **Fuzzy tier (must stay aliases-only):** `apollo/resolution/tiers.py:269-302`
  `match_fuzzy_all` — `token_set_ratio >= 0.9` over `cand.aliases` (`tiers.py:293`
  is the loop that MUST keep reading only `cand.aliases`).
- **Candidate construction pattern:** `apollo/resolution/candidates.py:95-106`
  `candidates_from_reference_solution` builds each `Candidate` with all-keyword
  args, `aliases=()` hardcoded at `:102`.
- **Frozen dataclass convention:** `candidates.py:56-73` — `Candidate` is
  `@dataclass(frozen=True)`, NO defaulted fields today. Python dataclass rule: a
  defaulted field must come AFTER all non-defaulted fields → `exact_aliases` is
  appended LAST (after `opposes_key`, `candidates.py:72`).
- **Caps-table snapshot test:** `test_candidates.py:62-71`
  `test_method_confidence_caps_match_spec` — see Compose note (Phase 1a owns it).

## Compose note (Phase 1a coupling on candidates.py)

Both phases edit `candidates.py`. They touch DISJOINT regions, so they compose,
but the executor must be aware:

- **Phase 1a** edits `RESOLUTION_METHODS` (`candidates.py:24-31`, adds `"derived"`)
  and `METHOD_CONFIDENCE_CAP` (`:35-42`, adds `"derived": 0.95`), and updates the
  snapshot test `test_candidates.py:62-71` (L1 landmine) to include `derived:0.95`.
- **Phase 1b** (this plan) edits the `Candidate` dataclass body (`:56-73`, append
  `exact_aliases`) and `candidates_from_reference_solution` (`:95-106`, populate
  it). It does NOT touch `RESOLUTION_METHODS`, `METHOD_CONFIDENCE_CAP`, or the
  caps snapshot test.
- **No conflict:** if Phase 1a lands first, this plan's diff still applies cleanly
  (different lines). If this plan lands first, Phase 1a's caps edit still applies.
  The merger sequences; neither needs the other's changes to be correct.
- **DO NOT** in this plan touch the caps table or add a method — that is Phase 1a's
  job. exact_aliases reuses the existing `alias` method + cap 0.92.

## Structural prep (from neighborhood scan)

- [ ] **None — neighborhood is clean.** Files in the change path:
  - `candidates.py`: ~7 imports, ~4 top-level functions + one dataclass — well under
    thresholds (8 imports / 20 methods).
  - `tiers.py`: ~6 imports, ~12 functions — under thresholds.
  - No circular imports introduced: `tiers.py` imports `Candidate` from `candidates.py`;
    `candidates.py` does not import `tiers.py`. Adding a field + reading it preserves
    this one-way edge.
  - Fan-in: `candidates.py` is imported by `resolver.py`, `tiers.py`, and tests — a
    legitimate hub, but this change is purely additive (new defaulted field), so no
    consumer breaks.
- Verify: `ai-ta-backend/.venv/Scripts/python.exe -m pytest apollo/resolution -q`
  (baseline 429 passed — confirmed by scout).

## Layered tasks (ORDER MATTERS — TDD: RED test first each task)

### T1 — Data model: `Candidate.exact_aliases`

**(a) File + insertion point:** `apollo/resolution/candidates.py`, inside the
`@dataclass(frozen=True) class Candidate` body, append AFTER `opposes_key: str | None`
(currently the last field, `candidates.py:72`). The new field MUST be last and
defaulted (dataclass non-default-after-default rule; L6 landmine — `Candidate` has
no defaulted fields today, so this is the first one and must be terminal):

```python
    opposes_key: str | None
    exact_aliases: tuple[str, ...] = ()
```

Update the class docstring (`candidates.py:58-63`) with one sentence: `exact_aliases`
holds curated reference-solution phrasings matched EXACT-only (never fuzzy).

**(b) RED test (write first):** `apollo/resolution/tests/test_candidates.py`, new
test `test_candidate_exact_aliases_defaults_empty_and_is_frozen`:

```python
def test_candidate_exact_aliases_defaults_empty_and_is_frozen():
    c = Candidate(
        canonical_key="cond.x", canon_key=1, node_type="condition",
        is_misconception=False, symbolic=None, aliases=(),
        display_name="x", opposes_key=None,
    )
    assert c.exact_aliases == ()            # defaulted, backward-compatible
    c2 = Candidate(
        canonical_key="cond.y", canon_key=2, node_type="condition",
        is_misconception=False, symbolic=None, aliases=(),
        display_name="y", opposes_key=None, exact_aliases=("open to the atmosphere",),
    )
    assert c2.exact_aliases == ("open to the atmosphere",)
    import dataclasses
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        c2.exact_aliases = ()  # type: ignore[misc]
```
RED reason: field does not exist → `TypeError` on the kwarg / `AttributeError`.

**(c) Minimal change:** the one defaulted field above. NOTE: every existing
`Candidate(...)` construction in code and tests omits `exact_aliases` → the default
`()` keeps them all valid (`test_candidate_is_frozen_immutable` at
`test_candidates.py:125-139` and the `_cand` helpers in `test_tiers.py:47-57` /
`test_resolver.py:39-49` stay green unchanged).

**(d) Verify:**
`ai-ta-backend/.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q`

---

### T2 — Populate `exact_aliases` in `candidates_from_reference_solution`

**(a) File + insertion point:** `apollo/resolution/candidates.py:95-106`, the
`out.append(Candidate(...))` call inside `candidates_from_reference_solution`.
Read `content.get("aliases", ())` (note: `content` already extracted at
`candidates.py:92`) and pass it as `exact_aliases`, keeping `aliases=()`:

```python
        out.append(
            Candidate(
                ...
                aliases=(),                                  # unchanged (refs have no fuzzy aliases)
                display_name=display,
                opposes_key=None,
                exact_aliases=tuple(content.get("aliases", ())),
            )
        )
```

`candidates_from_misconceptions` (`candidates.py:110-138`) is UNCHANGED — it keeps
populating `aliases` from `trigger_phrases` and leaves `exact_aliases` at its `()`
default. This is the precision asymmetry: misconceptions get fuzzy recall, reference
aliases get exact-only.

**(b) RED test (write first):** `test_candidates.py`, new test
`test_reference_aliases_flow_into_exact_aliases_not_aliases`. The on-disk
`problem_01.json` has no `content.aliases` yet — assert via an in-memory problem
dict so the test is self-contained and deterministic:

```python
def test_reference_aliases_flow_into_exact_aliases_not_aliases():
    problem = {"reference_solution": [{
        "entry_type": "condition", "entity_key": "cond.open_tank",
        "content": {"label": "Open tank", "aliases": ["open to the atmosphere", "vented tank"]},
    }]}
    cands = candidates_from_reference_solution(problem, canon_key_by_canonical_key={})
    c = cands[0]
    assert c.exact_aliases == ("open to the atmosphere", "vented tank")
    assert c.aliases == ()          # reference fuzzy channel stays empty
```
Also add a no-aliases regression: a step WITHOUT `content.aliases` →
`c.exact_aliases == ()` (proves the default path). RED reason: pre-T1 the kwarg
fails; post-T1-pre-T2 `exact_aliases` is `()` not the curated tuple.

**(c) Minimal change:** the single `exact_aliases=tuple(content.get("aliases", ()))`
kwarg. `tuple(...)` guards JSON `list` → frozen-friendly tuple (mypy: field is
`tuple[str, ...]`, JSON gives `list`; the `tuple()` cast satisfies the annotation).

**(d) Verify:**
`ai-ta-backend/.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q`

---

### T3 — Matching rule: exact-alias tier reads BOTH; fuzzy tier reads ONLY `aliases`

This is the precision control — the anti-hand-waving guard.

**(a) File + insertion point:** `apollo/resolution/tiers.py:210-228`,
`match_alias_all`. Extend the per-candidate alias loop (`tiers.py:224`) to iterate
BOTH `cand.exact_aliases` and `cand.aliases`. Concretely, replace the source of the
inner loop at `:224`:

```python
    for cand in candidates:
        if cand.node_type != node.node_type:
            continue
        for alias in (*cand.exact_aliases, *cand.aliases):   # was: cand.aliases
            if surface == _normalize(alias):
                hits.append(TierHitAll(cand, "alias", 1.0, alias))
                break  # one hit per candidate (the first matching alias)
```

Order `exact_aliases` first so a curated phrasing wins the `winning_alias` slot
deterministically when both lists contain the same surface (rare; misconceptions
and references are distinct candidates so practically disjoint). Update the
`match_alias_all` docstring (`tiers.py:211-216`) to state it reads exact_aliases +
aliases.

**`match_fuzzy_all` (`tiers.py:269-302`) MUST NOT CHANGE its alias source.** The
loop at `tiers.py:293` (`for alias in cand.aliases:`) stays EXACTLY `cand.aliases`.
`exact_aliases` must NEVER enter the fuzzy path. Add a one-line comment at `:293`
pinning the invariant: `# exact_aliases is deliberately NOT read here (Phase 1b: exact-only).`
(A comment-only line is covered by the diff-cover gate via the adjacent tested
loop body; if diff-cover flags the bare comment, fold the note into the existing
docstring at `:275-283` instead so no uncovered standalone line is added.)

**(b) RED tests (write first):** `apollo/resolution/tests/test_tiers.py`:

  - `test_exact_alias_tier_resolves_curated_phrasing_via_exact_aliases`:
    ```python
    def test_exact_alias_tier_resolves_curated_phrasing_via_exact_aliases():
        node = _condition_node("s1", "open to the atmosphere")
        cands = (_cand("cond.open", node_type="condition", exact_aliases=("open to the atmosphere",)),)
        hits = match_alias_all(node, cands)
        assert len(hits) == 1
        assert hits[0].candidate.canonical_key == "cond.open"
        assert hits[0].method == "alias" and hits[0].score == 1.0
    ```
    (Requires extending the `_cand` helper at `test_tiers.py:47-57` to accept an
    `exact_aliases=()` kwarg and pass it through — a backward-compatible helper
    edit; do this in the SAME test file.)

  - **THE CRITICAL GUARD** —
    `test_fuzzy_tier_ignores_exact_aliases_near_miss_does_not_resolve`:
    ```python
    def test_fuzzy_tier_ignores_exact_aliases_near_miss_does_not_resolve():
        # near-miss paraphrase of a curated exact alias: would clear token_set_ratio>=0.9
        node = _condition_node("s1", "the tank is open to the atmosphere outside")
        cands = (_cand("cond.open", node_type="condition", exact_aliases=("open to the atmosphere",)),)
        assert match_fuzzy_all(node, cands, threshold=0.9) == []   # exact_aliases never fuzzed
        assert match_alias_all(node, cands) == []                  # not exact-equal either -> missing
    ```
    This is the load-bearing precision assertion: a hand-wave variant of a curated
    alias stays UNRESOLVED.

  - `test_exact_alias_tier_still_reads_misconception_aliases`: a misconception with
    `aliases=("faster flow means higher pressure",)` and `exact_aliases=()` still
    exact-matches that phrase via `match_alias_all` (proves we did not break the
    existing `aliases` read).

**(c) Minimal change:** one loop-source edit in `match_alias_all`; zero behavioral
change to `match_fuzzy_all`; helper `exact_aliases` passthrough in test `_cand`.

**(d) Verify:**
`ai-ta-backend/.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_tiers.py -q`

---

### T4 — Reference-solution JSON optional `content.aliases`

**(a) File + insertion point:** This is DATA, not code. v1 ships hand-authored
aliases on reference-solution steps. The harvest→approval→promote queue is DEFERRED
(needs a table + teacher-UI; `AliasCandidate` stays capped 0.75, persisted nowhere —
spec §7). For the negative-corpus tests (T5) author aliases IN-MEMORY in the test
fixtures (do NOT edit shipped `problem_01.json` unless a real authored alias is
intended). If a real authored alias IS added to a shipped problem JSON, add it under
`reference_solution[i].content.aliases: ["…", "…"]` and add a matching positive
assertion in `test_candidates.py`.

  - **Decision for this plan:** do NOT modify any shipped `subjects/**/problem_*.json`.
    All alias data lives in test fixtures. This keeps the change pure-code +
    pure-test and avoids touching curated domain data without a teacher in the loop.
    The JSON schema is OPENED (the code now reads `content.aliases`) but no shipped
    file uses it yet.

**(b) RED test:** covered by T2's `test_reference_aliases_flow_into_exact_aliases_not_aliases`
(in-memory problem dict with `content.aliases`) — that IS the schema-acceptance test.

**(c) Minimal change:** none beyond T2 (the reader). No file authored.

**(d) Verify:** same as T2.

---

### T5 — Tests: resolver end-to-end + negative corpus

**(a) Files:**
  - `apollo/resolution/tests/test_resolver.py` — end-to-end exact-alias resolution.
  - `apollo/resolution/tests/test_tiers.py` — parametrized negative corpus (§14).

**(b) RED tests (write first):**

  - **End-to-end (test_resolver.py):**
    `test_exact_alias_resolves_end_to_end_via_resolve_attempt`: build a one-node
    student `KGGraph` whose condition surface == a candidate's `exact_aliases`
    entry; assert `resolve_attempt(...).resolved[0]` is `resolution=="resolved"`,
    `method=="alias"`, `confidence==METHOD_CONFIDENCE_CAP["alias"]` (0.92),
    `resolved_key==` the candidate key. (Extend the `_cand` helper at
    `test_resolver.py:39-49` with an `exact_aliases=()` passthrough kwarg.)

  - **Empty-aliases byte-identical regression (test_resolver.py):**
    `test_empty_exact_aliases_resolution_unchanged`: run the existing §6.9 worked
    example (`_worked_example_candidates`, `test_resolver.py:58+`) — every candidate
    has `exact_aliases=()` by default → the `ResolutionResult` (`resolved` tuple +
    `tier_counts` histogram + `llm_calls`) is IDENTICAL to a pre-change snapshot.
    Assert the full result equals the existing expected (reuse whatever the current
    worked-example test asserts; this proves no regression when no exact alias is set).

  - **NEGATIVE CORPUS (test_tiers.py, parametrized, §14):** the spec's dropped
    surfaces. For each, the CURATED phrasing (as an `exact_aliases` entry) resolves
    via `match_alias_all`, and a HAND-WAVE variant stays missing in BOTH
    `match_alias_all` and `match_fuzzy_all`:

    | Curated alias (resolves) | Hand-wave variant (must stay missing) | node_type |
    |---|---|---|
    | `open to the atmosphere` | `it's basically open air out there` | condition |
    | `the reservoir is wide` | `the tank is kind of big I guess` | condition |
    | `P1 = P2` | `the pressures are roughly equal` | condition |
    | `nominal and real GDP are basically the same` | `gdp is gdp more or less` | definition |
    | the "streamline" definition (e.g. `a streamline is a path a fluid particle follows`) | `it's just a line in the flow` | definition |

    ```python
    @pytest.mark.parametrize("curated,handwave,ntype", _NEGATIVE_CORPUS)
    def test_negative_corpus_curated_resolves_handwave_missing(curated, handwave, ntype):
        cand = _cand("ref.x", node_type=ntype, exact_aliases=(curated,))
        good = _surface_node("g", ntype, curated)
        bad = _surface_node("b", ntype, handwave)
        assert len(match_alias_all(good, (cand,))) == 1          # curated resolves
        assert match_alias_all(bad, (cand,)) == []               # hand-wave: not exact
        assert match_fuzzy_all(bad, (cand,), threshold=0.9) == []  # and never fuzzed (exact_aliases not read)
    ```
    NOTE on `P1 = P2`: it is authored as a CONDITION alias here (a stated relation),
    matched by `student_surface_text` `applies_when` (`tiers.py:72-73`) — NOT routed
    to the symbolic tier. The negative-corpus assertion is purely about the alias
    channel (lexical exact vs fuzzy), so the condition surface is the correct vehicle.
    Provide a `_surface_node(node_id, node_type, text)` helper that builds a node
    whose `student_surface_text` equals `text` for each `node_type` (condition →
    `applies_when`; definition → `concept` only, leaving `meaning=""`, so the surface
    equals the curated string exactly).

**(c) Minimal change:** test-only. No production code beyond T1–T3.

**(d) Verify:**
`ai-ta-backend/.venv/Scripts/python.exe -m pytest apollo/resolution -q`
(full resolution suite; expect baseline 429 + new tests, all green).

## Per-task patch-coverage notes

Patch coverage measured by `diff-cover coverage.xml --compare-branch=origin/staging
--fail-under=95` over CHANGED lines only.

- **T1:** the one new field line is a dataclass declaration — covered by
  `test_candidate_exact_aliases_defaults_empty_and_is_frozen` (default + explicit +
  frozen). 100% of the changed line.
- **T2:** the new `exact_aliases=tuple(content.get("aliases", ()))` kwarg — covered
  by `test_reference_aliases_flow_into_exact_aliases_not_aliases` (curated path) AND
  the no-aliases regression (default path → `()`). Both branches of `.get` exercised.
- **T3:** the changed loop source `(*cand.exact_aliases, *cand.aliases)` — covered by
  the exact-alias positive test (exact_aliases hit), the misconception-aliases test
  (aliases hit), and the negative-corpus tests (no hit). The fuzzy invariant comment
  carries no executable line; if a standalone comment trips diff-cover, fold it into
  the docstring (see T3(a)).
- **T4:** no production line changed (data/test only) → no patch-coverage obligation.
- **T5:** test files — counted as covered (executed by pytest). The byte-identical
  regression and the negative corpus exercise the un-hit branches of `match_alias_all`
  / `match_fuzzy_all` keeping their existing coverage intact.

Net new production lines: ~3 (T1 field, T2 kwarg, T3 loop source) + ~1 comment.
All ≥95% covered. No exemptions needed.

## Test contract / verification commands

Run from `ai-ta-backend/`:

```bash
# per-task (fast):
.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_candidates.py -q
.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_tiers.py -q
.venv/Scripts/python.exe -m pytest apollo/resolution/tests/test_resolver.py -q

# full resolution + grading + graph_compare (must stay green, baseline 429 passed):
.venv/Scripts/python.exe -m pytest apollo/resolution apollo/grading apollo/graph_compare -q

# new-file / changed-file gates (BLOCKING):
.venv/Scripts/python.exe -m ruff check apollo/resolution/candidates.py apollo/resolution/tiers.py
.venv/Scripts/python.exe -m ruff format --check apollo/resolution/candidates.py apollo/resolution/tiers.py
.venv/Scripts/python.exe -m mypy apollo/resolution/candidates.py apollo/resolution/tiers.py

# patch coverage (>=95% vs origin/staging):
.venv/Scripts/python.exe -m pytest apollo/resolution --cov=apollo.resolution --cov-report=xml
.venv/Scripts/python.exe -m diff_cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

**Purity tests that MUST stay green (do not modify, do not break):**
`test_grade_attempt_is_pure` (graph_compare/tests/test_core.py:245),
`test_sub_scores_pure_same_input_same_output` (test_scores.py:235),
`test_builder_is_pure_same_input_same_output` (test_student_canonical.py:195).
This plan touches none of their inputs in a value-changing way (exact_aliases
defaults to `()`, so an attempt with no curated aliases produces byte-identical
resolution → byte-identical canonical graph → byte-identical grade).

## Drift reconciliation (owner doc)

In the SAME commit as the code change, update
`ai-ta-backend/docs/architecture/apollo.md` (owns `apollo/**`):

- In the `apollo/resolution/` row / resolver narrative (around the
  `candidates.py`/`tiers.py` description, ~line 38 and the resolver tier prose):
  document the new `Candidate.exact_aliases: tuple[str, ...] = ()` field and the
  rule: **the exact-alias tier (`match_alias_all`) reads `exact_aliases` + `aliases`
  (exact-normalized equality); the fuzzy tier (`match_fuzzy_all`) reads ONLY
  `aliases`** — curated reference phrasings resolve exact-only, misconceptions keep
  fuzzy recall. Note `exact_aliases` is populated from reference-solution
  `content.aliases` (hand-authored v1; harvest→approval queue DEFERRED, AliasCandidate
  stays capped 0.75).
- Bump `last_verified:` (apollo.md:13) from `2026-06-23` to the implementation date.
- No new `owns:` glob needed — `candidates.py` and `tiers.py` are already owned;
  no new module in Phase 1b (equation_alignment.py is Phase 1a's).

## Files touched + tests added

**Production code (3 small edits, 2 files):**
- `apollo/resolution/candidates.py` — T1 (`exact_aliases` field on `Candidate`,
  `:72`), T2 (populate in `candidates_from_reference_solution`, `:95-106`).
- `apollo/resolution/tiers.py` — T3 (`match_alias_all` loop reads
  `exact_aliases + aliases`, `:224`; `match_fuzzy_all` unchanged, invariant comment
  at `:293`).

**Tests added/extended (3 files):**
- `apollo/resolution/tests/test_candidates.py` —
  `test_candidate_exact_aliases_defaults_empty_and_is_frozen`,
  `test_reference_aliases_flow_into_exact_aliases_not_aliases` (+ no-aliases
  regression).
- `apollo/resolution/tests/test_tiers.py` — extend `_cand` helper with
  `exact_aliases` passthrough;
  `test_exact_alias_tier_resolves_curated_phrasing_via_exact_aliases`,
  `test_fuzzy_tier_ignores_exact_aliases_near_miss_does_not_resolve` (CRITICAL guard),
  `test_exact_alias_tier_still_reads_misconception_aliases`,
  `test_negative_corpus_curated_resolves_handwave_missing` (parametrized, 5 cases)
  + `_NEGATIVE_CORPUS` table + `_surface_node` helper.
- `apollo/resolution/tests/test_resolver.py` — extend `_cand` helper with
  `exact_aliases` passthrough;
  `test_exact_alias_resolves_end_to_end_via_resolve_attempt`,
  `test_empty_exact_aliases_resolution_unchanged`.

**Owner doc:** `ai-ta-backend/docs/architecture/apollo.md` (same commit).

**NOT touched:** any shipped `subjects/**/problem_*.json`; grade-math files;
`candidates.py` caps table / `RESOLUTION_METHODS` (Phase 1a); `match_fuzzy_all`
behavior.

## Risks / residual / open questions

- **[LOW] Phase 1a candidates.py merge.** Disjoint regions; composes. The only
  shared file is `candidates.py` and the edits do not overlap. Merger sequences.
  Residual: if Phase 1a renames/reorders `candidates_from_reference_solution`'s
  Candidate construction, T2's insertion point shifts — re-locate by the
  `aliases=()` kwarg, not the line number.
- **[LOW] diff-cover on the bare invariant comment** (T3, `:293`). Mitigated by the
  fold-into-docstring fallback. No standalone uncovered executable line is added.
- **[MEDIUM] Negative-corpus surface construction.** `student_surface_text` for a
  definition concatenates `concept + " " + meaning` (`tiers.py:78-81`); to make the
  surface equal a curated string exactly, the test helper must set `meaning=""` (so
  surface == `concept`, no trailing space after `_normalize`). For `condition`,
  surface == `applies_when` directly (`tiers.py:72-73`) — clean. The plan's
  `_surface_node` helper must encode this per-type; flagged so the executor does not
  get a spurious leading/trailing-space mismatch.
- **[LOW] APOLLO-EDGE-LOSS-FINDINGS.md not on disk** in this checkout — the negative
  corpus surfaces are taken verbatim from spec §14 (self-contained list), so the
  missing doc is not a blocker. The five surfaces are authoritative as listed.
- **[LOW] `tuple(content.get("aliases", ()))`** — if a JSON author supplies a
  non-list (e.g. a string) the `tuple()` would iterate chars. Acceptable for v1
  (hand-authored, schema-trusted); a validation guard is out of scope (would be the
  harvest/approval pipeline's job). Note it; do not add validation here.
- **Open question (defer, do not block):** should `match_exact` (`tiers.py:100-115`)
  also consider `exact_aliases`? No — `match_exact` matches `canonical_key`/`symbolic`,
  a different surface; the exact-ALIAS channel is `match_alias_all`. Keep them
  separate (spec §7 scopes 1b to the alias tier only).

## Deviations I'd allow the executor

- Reordering `(*cand.exact_aliases, *cand.aliases)` vs `(*cand.aliases,
  *cand.exact_aliases)` IF a test proves a deterministic `winning_alias` either way —
  but exact_aliases-first is preferred (curated phrasing wins provenance).
- Folding the T3 fuzzy-invariant note into the `match_fuzzy_all` docstring instead of
  a standalone comment (preferred if diff-cover is strict).
- Choosing the exact test method names / `_surface_node` helper shape freely, as long
  as each task's RED assertion (existence + behavior) is present.
- Authoring ONE real `content.aliases` entry on a shipped problem JSON IF the executor
  judges a domain-accurate curated alias is safe and adds the matching positive
  assertion — but the default (test-fixture-only) is the lower-risk path.
- NOT allowed: touching the caps table / adding a resolution method (Phase 1a);
  letting `exact_aliases` reach `match_fuzzy_all`; modifying grade-math or the live
  grade path; raising/altering any abstention threshold (Phase 1c).
