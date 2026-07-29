---
doc: apollo/resolution/content-tiers
description: The content-first matching ladder — exact, SymPy-symbolic, derived-equation-alignment, alias, and fuzzy tiers.
owns:
  - apollo/resolution/tiers.py
  - apollo/resolution/equation_alignment.py
related:
  - apollo/solver/sympy-exec
  - apollo/resolution/resolver
  - apollo/overseer/coverage
last_verified: 2026-07-25
stub: false
---

# Resolution — content tiers

> Part of the §5 resolver, currently **unwired** (see [_index](_index.md)); the
> `tiers` helpers are the only symbols imported outside the package — by the
> dormant `overseer/coverage.py`.

The strongest resolver signal (§5 step 1): a content-first matching ladder over
one student node and the type-compatible candidates. Each matcher is a pure
`(student_node, candidates, …) → (Candidate, method, raw_score) | None`.

## Interface

`tiers.py`:
- `match_exact` — display label or normalized surface text equals a candidate's
  `canonical_key`/`symbolic` (raw 1.0).
- `match_symbolic(node, candidates, *, mappings=None)` — equation nodes only;
  sign-exact SymPy equivalence under declared `mappings`.
- `match_alias` / `match_alias_all` — normalized exact-alias hits (reads
  `exact_aliases` then `aliases`).
- `match_fuzzy` / `match_fuzzy_all` — RapidFuzz `token_set_ratio ≥ threshold`
  (default 0.9), reading `cand.aliases` ONLY.
- `student_surface_text(node)` — the per-type comparable string.
- `TierHit` / `TierHitAll` types.
- Internal helpers `_extended_locals`, `_symbolic_equiv`, `_zero_form` — imported
  cross-module by `overseer/coverage.py` (along with `student_surface_text`).

`equation_alignment.py`:
- `match_equation_alignment(node, candidates, *, mappings)` — the Phase-1a
  `derived` tier (cap 0.95): accepts a student equation stated as a
  solved/rearranged form of a reference equation.

## Data flow

The resolver runs the tiers in precedence order — exact → symbolic → **derived**
→ alias → fuzzy. `_symbolic_equiv` parses both sides to a zero-form via
`solver.sympy_exec.parse_zero_form`, substitutes declared mappings, and tests
`simplify(a - b) == 0` (sign-exact). `match_equation_alignment` instead solves
`Eq(reference_zero, 0)` for each free symbol and tests whether any branch
reproduces the student zero-form (declared mappings only; floats Rational-ized).

## Invariants & gotchas

- **Reuses, never edits, the solver:** `_extended_locals` extends
  `sympy_exec._local_dict()` with the equation's free symbols LOCALLY — SymPy
  reserved names (`pi`, `Rational`, …) are never shadowed and `sympy_exec.py` is
  never mutated. `_zero_form` delegates to the single mint-time parser so a
  minted subject and the runtime grader parse identical notations (`^`, chained
  equalities).
- **Precision asymmetry:** `exact_aliases` (curated reference phrasings) resolve
  only through the alias tier — the fuzzy tier never reads them, so they get no
  free `token_set_ratio` credit.
- **The derived tier reads NO numeric givens** and applies only DECLARED
  mappings — a purely-numeric or undeclared-simplification form stays unresolved.
- **`_all` variants return every above-threshold candidate** so a competing
  misconception reaches competition; every SymPy call is `try/except → non-match`
  (a pathological parse/solve degrades, never raises).

## Related

- [solver/sympy-exec](../solver/sympy-exec.md) — the reused `parse_zero_form` /
  `_local_dict`.
- [resolution/resolver](resolver.md) — orders and consumes the tiers.
- [overseer/coverage](../overseer/coverage.md) — the last (dormant) importer of
  the `tiers` helpers.
