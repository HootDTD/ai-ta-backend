# Plan: WU-3B2b — PURE 8-gate promotion lint (safety core)

**Goal:** A pure, LLM-free, DB-free `apollo/provisioning/` package that runs the §8B.4 eight promotion gates in order, short-circuiting on the first failure, returning a frozen `PromotionResult`, plus the gate-8 `problem_dup_hash` key — exercisable entirely on hand-written fixtures with no LLM/DB/container running.
**Architecture:** New `apollo/provisioning/` package (mirrors `apollo/resolution/`). Composes existing frozen primitives (`Problem`, `EDGE_ALLOWED_PAIRS`, `KGGraph`, `parse_zero_form`, `validate_reference_graph`); gates 4/7/8 are net-new pure logic. NO new dependency outside stdlib `hashlib`.
**Tech stack:** Python 3.12 / pytest (`asyncio_mode=auto`, `testpaths = tests apollo`) / pydantic v2 / sympy (already present). Interpreter: `.venv/Scripts/python.exe`.

---
provides:
  - apollo.provisioning.PromotionResult (frozen dc: ok, failed_gate, diagnostic)
  - apollo.provisioning.run_promotion_lint(graph, *, canonical_symbols, normalization_map, existing_problem_hashes) -> PromotionResult
  - apollo.provisioning.problem_dup_hash(problem) -> str
consumes:
  - apollo.schemas.problem.Problem (gate 1, gate 8 inputs)
  - apollo.ontology.edges.EDGE_ALLOWED_PAIRS + Edge (gate 1 edge-type)
  - apollo.ontology.graph.KGGraph.topological_order / precedes_chain (gates 3, 5)
  - apollo.solver.sympy_exec.parse_zero_form (gate 6)
  - apollo.persistence.learner_model_seed.validate_reference_graph (gate 2)
  - apollo.persistence.learner_model_seed._ENTRY_TYPE_TO_KIND_PREFIX (gate 1 mint-map sub-check, READ-ONLY)
  - caller-supplied canonical_symbols / normalization_map (gate 4), existing_problem_hashes (gate 8)
depends_on:
  - WU-3B2a (migration 030 + ORM + Tier-2 gate) — ALREADY MERGED on this branch's history (commits 7413801/6572077/b868c2d). 3B2b consumes NO DB; the dependency is logical (gate-4 symbols + gate-8 hashes are passed in by the caller, populated by 3B2a/3B2d later).
---

## Overview

WU-3B2b is the safety core of the §8B auto-provisioning pipeline: before any auto-scraped
problem is promoted Tier-1 → Tier-2 (teachable), it must pass eight gates run in order
(§8B.4, spec lines 1332-1353). Any failure short-circuits and reports EXACTLY which gate
fired; the orchestrator (3B2g, not this unit) maps a failure to an `apollo_rejected_problems`
row and a pass to promotion. This unit is the keystone PR: 100% LLM-free, DB-free, fully
fixture-testable, and reuses the largest existing surface in the build.

~60% of the lint is pre-built — gates 1/2/3/5/6 compose existing primitives; gates 4/7/8 are
net-new pure logic. The module is PURE: gate-4 canonical symbols + gate-8 dup hashes are
PASSED IN by the caller (populated later by 3B2a/3B2d), so the core never touches the DB.

**The single most load-bearing assertion:** each gate's adversarial fixture must go RED iff
that gate is reverted — discriminating, not tautological. The orchestrator applies independent
mutation testing; a fixture that fails for the wrong reason (e.g. a gate-5 fixture that also
trips gate-1) defeats the safety guarantee.

## Prior art (sibling modules)

- **`apollo/resolution/__init__.py:1-36`** — the package-seam template to mirror exactly:
  module docstring describing the standalone-by-design contract, then a flat re-export of the
  public names via `from apollo.resolution.<mod> import ...` + `__all__`. `apollo/provisioning/__init__.py`
  copies this shape.
- **`apollo/persistence/learner_model_seed.py:307-364`** — `validate_reference_graph` is GATE 2
  VERBATIM (the §6.1 closure check), NOT the whole lint. It takes the annotated-problem DICT
  (`reference_solution` steps carrying `entity_key`, plus `declared_paths`) and returns a frozen
  `ReferenceGraphValidation{ok, missing_entity_links, undeclared_paths, errors}`. Gate 2 calls it
  and maps `ok=False` → `failed_gate=2`.
- **`apollo/persistence/tests/test_reference_graph_validation.py:1-101`** — the PURE fixture-test
  pattern to mirror: a `_problem(steps, declared_paths)` builder + a `_step(node_id, entity_key)`
  helper + one focused test per failure mode, no DB/LLM/marks. The 3B2b suite follows this exact
  shape (a positive builder + one adversarial mutation per gate).
- **`apollo/schemas/tests/test_problem_schema.py:18-95`** — `_minimal_problem_dict()` shows how a
  valid `Problem` dict is hand-built (note: the module is `pytest.skip`-ed at top for a LEGACY
  reason, but the dict shapes are current and reused as fixture seeds). `Problem.model_validate`
  raises `pydantic.ValidationError` on a bad entry_type / empty reference_solution.
- **`apollo/agent/tests/test_leakage_judge.py:31-36`** — the `patch(...cheap_chat, return_value=json.dumps(...))`
  template. NOT needed in 3B2b (no LLM), referenced only to confirm the unit is deliberately
  LLM-free where every sibling provisioning unit mocks an LLM.
- **`apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/problems/problem_01.json`** — the
  real seeded bernoulli reference solution (7 steps: 2 equations, 1 condition, 1 simplification,
  3 procedure_steps) that the POSITIVE fixture is modeled on. `canonical_symbols.json` (base symbols
  `P, rho, v, A, h, g, Q`) + `normalization_map.json` (phrase→symbol) are the gate-4 inputs.

## Input-shape decision (what `graph` is)

The pinned public signature is `run_promotion_lint(graph, *, canonical_symbols,
normalization_map, existing_problem_hashes) -> PromotionResult`. The parameter name `graph`
is the spec's word for "the minted reference graph", but the EIGHT gates need three distinct
views of the same problem, and the names of those views are fixed by the existing primitives:

| Gate | Needs |
|---|---|
| 1 | the RAW problem dict for `Problem.model_validate(...)` + every edge of `Problem.to_kg_graph` to satisfy `EDGE_ALLOWED_PAIRS` |
| 1 (sub-check, ADJ #5) | each step's `entry_type` ∈ `_ENTRY_TYPE_TO_KIND_PREFIX` |
| 2 | the ANNOTATED dict (`entity_key` per step + `declared_paths`) for `validate_reference_graph` |
| 3 | `Problem.to_kg_graph(...).topological_order(DEPENDS_ON)` |
| 5 | `Problem.to_kg_graph(...).precedes_chain()` + procedure_step set |
| 6 | each equation step's `content["symbolic"]` for `parse_zero_form` |
| 7 | `given_values`, `target_unknown`, and the equations' free symbols |
| 8 | `problem_text`, `given_values`, `target_unknown` (via `problem_dup_hash`) |

**DECISION: `graph` is the annotated problem DICT** (the exact shape of
`apollo/subjects/.../problems/problem_01.json` — a `Problem`-validatable dict that ALSO carries
per-step `entity_key` and top-level `declared_paths`). Rationale, grounded in code:

1. It is the ONLY shape that simultaneously feeds `Problem.model_validate` (gate 1 — which
   IGNORES the extra `entity_key`/`declared_paths` keys, confirmed: `Problem`/`ReferenceStep`
   have no `model_config = extra='forbid'`, so unknown keys are dropped, `schemas/problem.py:40-55`)
   AND `validate_reference_graph` (gate 2 — which REQUIRES `entity_key`+`declared_paths`,
   `learner_model_seed.py:319-356`).
2. It matches what the 3B2g orchestrator actually holds at promotion time: the minted, annotated
   problem produced by `annotate_reference_solution` (`learner_model_seed.py:272-299`), the same
   payload `done.py`'s shadow chain reads RAW from `ConceptProblem.payload`.
3. It is hand-buildable in a fixture with zero DB (the `problem_01.json` structure).

**Implementation note for the executor:** inside `run_promotion_lint`, gate 1 builds the
validated `Problem` ONCE (`problem = Problem.model_validate(graph)`); gates 3/5/6/7 derive from
`problem.to_kg_graph(attempt_id=_LINT_ATTEMPT_ID)` and `problem.reference_solution` (so the
validated, typed view is reused — no re-validation per gate). `_LINT_ATTEMPT_ID` is a module
constant (e.g. `0`) — the lint is attempt-agnostic; `to_kg_graph` only uses `attempt_id` to
stamp nodes/edges, never for gate logic. Gate 2 runs `validate_reference_graph(graph)` on the
RAW dict (it reads `entity_key`/`declared_paths` which the `Problem` schema drops).

**Do NOT** change the public signature. `graph` stays positional; the three keyword-only args
stay keyword-only exactly as pinned.

## Per-gate contract and implementation

Each gate is a small pure function `_gate_N(...) -> str | None` returning `None` on pass or a
diagnostic STRING on fail. `run_promotion_lint` calls them in order and returns
`PromotionResult(ok=False, failed_gate=N, diagnostic=msg)` on the first non-None, else
`PromotionResult(ok=True, failed_gate=None, diagnostic="")`. Short-circuit is structural: gate
N+1 is never called once gate N fails (so a problem failing gate 3 reports 3, never a later gate).

**Gate ordering rationale (binding):** gate 1 (schema) MUST be first because gates 3/5/6/7 call
`Problem.model_validate` / `to_kg_graph`, which RAISE on a malformed problem — running them on
an unvalidated dict would surface a pydantic error attributed to the wrong gate. Gate 1 builds
the `Problem` once and the validated object is threaded to later gates.

### Gate 1 — Schema (compose `Problem` + `EDGE_ALLOWED_PAIRS` + mint-map sub-check)
- `try: problem = Problem.model_validate(graph) except (ValidationError, ValueError): return "<diag>"`.
  `Problem`'s own `_resolve_references` validator already enforces depends_on resolution,
  uses_equations resolution, and contiguous procedure `order` (`schemas/problem.py:61-100`).
- **Edge-type sub-check:** building `problem.to_kg_graph(attempt_id=_LINT_ATTEMPT_ID)` constructs
  every `Edge`, and `Edge._check_pair` (`edges.py:71-86`) RAISES `ValueError` if any
  (from_type, to_type) pair is not in `EDGE_ALLOWED_PAIRS` or is a self-loop. Wrap the
  `to_kg_graph` call in gate 1's `try` so a forbidden edge pair (the adversarial g1 `equation
  SCOPES condition` case) fails AT gate 1. NOTE the graph is built here and REUSED by gates 3/5/6/7.
- **Mint-map membership sub-check (ADJUDICATION #5, defense-in-depth):** for every step,
  `if step.entry_type not in _ENTRY_TYPE_TO_KIND_PREFIX: return "<diag: unmapped entry_type ...>"`.
  On THIS branch the map has 5 keys and LACKS `variable_mapping` (verified live:
  `['condition','definition','equation','procedure_step','simplification']`), so a
  `variable_mapping` reference step FAILS CLOSED at gate 1 instead of KeyError-ing mid-mint at
  `_entity_key_for_step` (`learner_model_seed.py:196-199`). 3B2b does NOT edit the frozen map
  (the additive `"variable_mapping": ("variable","varmap")` extension is 3B2d's edit per ADJ #5);
  3B2b only GUARDS. Import the map READ-ONLY. Re-derive membership from the LIVE dict — do NOT
  hardcode the 5 keys (so when 3B2d extends the map, this gate auto-accepts `variable_mapping`
  with no 3B2b edit; the g1 `variable_mapping` adversarial test asserts the gate fires ON THIS
  BRANCH where the map is not yet extended).
- **Diagnostic:** include the pydantic error text or the offending edge/entry_type.

### Gate 2 — Reference closure (`validate_reference_graph` VERBATIM)
- `result = validate_reference_graph(graph)` on the RAW annotated dict.
- `if not result.ok: return <prefix + "; ".join(result.errors)>`.
- §6.1 closure ONLY (non-empty `entity_key` per step; `declared_paths` present + non-empty; paths
  reference only real node ids; every node on ≥1 path). The g2 adversarial (dangling `depends_on`
  realized as a missing `entity_key`) is caught here. **Do NOT reimplement** — call the frozen
  function. NOTE: a dangling `depends_on` is ALSO caught by `Problem`'s validator at gate 1, so the
  PURE g2 adversarial fixture must be a MISSING `entity_key` (schema-legal, closure-illegal) — see
  the test section.

### Gate 3 — DAG (acyclic DEPENDS_ON)
- `kg = <gate-1 graph>`. `try: kg.topological_order(EdgeType.DEPENDS_ON) except ValueError: return
  "<diag: cycle ...>"`. `topological_order` raises on a cycle (`graph.py:137-141`).
- **Reachability/orphan clause (spec §8B.4:1343):** for a DEPENDS_ON DAG derived from a
  schema-valid `Problem`, the realizable failure is the CYCLE; a node with no depends_on is a valid
  root, not an orphan. Document gate 3 = acyclicity in the module docstring.
- **Fixture construction (discriminating):** a `Problem`-validatable problem whose `depends_on`
  forms a cycle (e.g. step A `depends_on:[B]`, step B `depends_on:[A]`). `Problem._resolve_references`
  only checks each dep EXISTS (`schemas/problem.py:69-75`), NOT acyclicity — so A↔B passes gate 1
  and fails gate 3. This is the clean discriminating g3 case. (PRECEDES cycles are impossible —
  `order` must be 1..N contiguous.)

### Gate 4 — Symbol consistency (the SOLE foreign-symbol guard, ADJUDICATION #4)
- **NET-NEW pure logic.** For each equation step (entry_type=="equation") extract free symbols via
  `parse_zero_form(symbolic, entry_id=step.id).free_symbols` (on `MalformedEquationError`, SKIP
  that equation — gate 6 owns malformed syntax; gate 4 runs first but must not steal gate 6's
  verdict). Also include every `given_values` key and `target_unknown`. Each symbol is ACCEPTED iff
  `_normalize_symbol(name, canonical_symbols, normalization_map)` is not None; else fail.
- **`_normalize_symbol`:** (a) exact membership in `canonical_symbols`; (b) strip a trailing digit
  run and test the BASE (`P1`→`P`, `v2`→`v`) — the seeded equations use subscripted symbols and
  `canonical_symbols.json` holds the BASE set `P,rho,v,A,h,g,Q`; (c) `normalization_map` lookup
  (phrase/alias → canonical, the caller-injected map). Return the canonical base or None.
- **g4 adversarial = a foreign symbol** (`x` in an equation; `canonical_symbols` excludes `x`,
  `normalization_map` does not map it). The fixture MUST pass gates 1/2/3 (x is schema-legal, no
  cycle/closure issue) and fail gate 4.
- **Module docstring MUST state** gate 6 does NOT reject foreign symbols (`parse_expr` auto-creates
  unknown symbols, `sympy_exec.py:35-64`); gate 4 is the only foreign-symbol guard; gate 4 reads
  PASSED-IN symbols so the core stays pure/DB-free (caller/3B2d populate them).

### Gate 5 — Procedure coherence (one PRECEDES chain, terminal computes target)
- `procs = kg.by_type("procedure_step")`. `heads = [n for n in procs if not kg.incoming(n.node_id,
  EdgeType.PRECEDES)]`; `if len(heads) != 1: return "<diag: not a single chain head>"`.
  `chain = kg.precedes_chain()`; `if len(chain) != len(procs): return "<diag: PRECEDES chain does
  not cover all procedure steps>"` (`graph.py:71-99`).
- **Terminal-computes-target clause:** the terminal step (last in `chain`) must have a non-empty
  `uses_equations` AND `target_unknown` must appear as a free symbol of ≥1 used equation. Lenient
  enough that real bernoulli passes (terminal `plan_solve_bernoulli_for_p2` uses `bernoulli`, whose
  symbolic contains `P2`).
- **CONSTRUCTION CAUTION:** a schema-valid `Problem` ALWAYS yields a single linear PRECEDES chain
  (built from the validated 1..N `order`, `schemas/problem.py:169-185`), so a FORK cannot arise via
  `to_kg_graph`. Therefore the DISCRIMINATING adversarial g5 fixture targets the
  TERMINAL-COMPUTES-TARGET sub-clause: terminal step's `uses_equations` does not reach
  `target_unknown` (e.g. terminal uses only `continuity`, which lacks `P2`). Passes 1-4, fails 5.
  The pure single-chain-head / chain-coverage branches are ALSO unit-tested white-box on a
  hand-built forked `KGGraph` (so those branches stay covered). **SIGNAL: this is the one place the
  §3 fixture wording "forked PRECEDES chain" is operationally realized as the terminal-target
  sub-clause — see Risks + Deviations.**

### Gate 6 — SymPy parse (malformed syntax only)
- For every equation step with a `symbolic`, `try: parse_zero_form(symbolic, entry_id=step.id)
  except MalformedEquationError: return "<diag: malformed equation ...>"`. g6 adversarial =
  `rho*A1*v1 - = P2` (dangling operator → `parse_expr` raises → `MalformedEquationError`).
- Does NOT reject foreign symbols (documented). Skip a step whose `content.get("symbolic")` is
  absent (defensive; structure is gate-1's concern).

### Gate 7 — Equation-system closure (paper-only, NOT a solve, spec §8B.4:1347)
- **NET-NEW pure logic.** Collect all free symbols across all equation steps (all parse here, gate
  6 already passed). A symbol is CLOSED iff: (a) in `given_values`; OR (b) == `target_unknown`; OR
  (c) it is an INTERMEDIATE — a free symbol of any equation USED by a NON-terminal procedure step
  (the procedure computes it en route), minus givens/target; OR (d) it is named (whole-token match)
  in a simplification step's `transformation` string OR its `content.get("variables", [])`. An
  UNCLOSED symbol (none of a-d) → fail gate 7.
- **Real bernoulli walk-through (must PASS):** equation free symbols ⊇ {rho,A1,v1,A2,v2,P1,g,h1,P2,h2};
  givens={A1,A2,P1,v1,rho}; target=P2; v2 is an intermediate (continuity used by the first
  procedure step); g,h1,h2 are cancelled by `horizontal_simplification`
  (`transformation:"rho*g*h1 and rho*g*h2 cancel"`). So every symbol is closed.
- **g7 adversarial = an unclosed free symbol** (`w` added to an equation, with no
  given/target/intermediate/simplification reaching it). Passes 1-6, fails 7.
- **Document gate 7's honest limit** (spec §8B.4:1350-1353): equations that parse but don't produce
  the claimed answer can pass; the per-problem quarantine (3B2h) is the runtime catch. Gate 7 is a
  PAPER closure check, NOT an end-to-end solve.

### Gate 8 — Duplicate detection (`problem_dup_hash` ∉ `existing_problem_hashes`)
- `h = problem_dup_hash(problem)`; `if h in existing_problem_hashes: return "<diag: duplicate ...>"`.
- `existing_problem_hashes` is a `set[str] | frozenset[str]` PASSED IN, scoped by the caller to
  THIS course's BIGINT concept (ADJ #6/#8 — gate 8 keys on the BIGINT, caller builds the set). The
  lint NEVER queries the DB. g8 adversarial passes `existing_problem_hashes={problem_dup_hash(same)}`.

### `problem_dup_hash(problem) -> str` (gate-8 key, separate module `problem_hash.py`)
- `sha256(normalize(problem_text) + canonical(given_values) + target_unknown)` (spec §8B.4:1348).
- **`normalize(problem_text)`:** `re.sub(r"\s+", " ", text).strip().lower()` (stdlib `re`, deterministic).
- **`canonical(given_values)`:** `sorted(given_values.items())` rendered `f"{k}={v!r}"` joined by
  `,` (float `2.0`/`2.00` collapse via float equality; `repr(float)` is stable).
- **Input:** takes the validated `Problem` model (gate 8 already holds it; the 3B2g orchestrator
  also holds a `Problem`). Read `.problem_text`/`.given_values`/`.target_unknown`. One input type.
- `hashlib.sha256(payload.encode("utf-8")).hexdigest()`. Stdlib only. NO new package.
- A module constant `_DUP_HASH_VERSION = "promotion-dup-v1"` PREFIXED into the payload (so a future
  normalization change is detectable — mirrors `reference_hash.py`'s `REFERENCE_HASH_VERSION`).

## Structural prep (from neighborhood scan)

All files in the change path are NET-NEW (`apollo/provisioning/` does not exist). The one-ring-out
imports are the frozen primitives, scanned for debt:

- `apollo/schemas/problem.py` (208 lines, ~6 imports) — clean, well under thresholds. READ-ONLY.
- `apollo/ontology/edges.py` (87 lines), `graph.py` (161 lines), `nodes.py` (193 lines) — clean,
  no circular import into provisioning. READ-ONLY.
- `apollo/solver/sympy_exec.py` (137 lines) — clean. READ-ONLY.
- `apollo/persistence/learner_model_seed.py` (365 lines, DB-free by docstring) — clean. READ-ONLY
  (3B2b imports `validate_reference_graph` + `_ENTRY_TYPE_TO_KIND_PREFIX`; the map EXTENSION is
  3B2d's, NOT this unit).
- No file in the import chain imports `apollo.provisioning` (it is new) → no circular-import risk.
- Coupling fan-in: provisioning will be imported by 3B2g only (not yet present) → not a hub.

**None — neighborhood is clean. No structural prep required.** All new files are <200 lines.
- Verify: `.venv/Scripts/python.exe -c "import apollo.provisioning"` (after creation).

## TDD-ordered tasks (RED first)

Strict TDD: every test is written to FAIL first (the module/symbol does not exist → ImportError or
AssertionError), then the minimal implementation makes it GREEN. NO skip/xfail/assert-nothing. A
bare-`python` ImportError is an interpreter-selection error (use `.venv/Scripts/python.exe`), NOT a
test failure.

1. **[RED] Create the test files first** (`test_problem_hash.py`, `test_promotion_lint.py`) with
   the full assertions below. Run → they fail with `ModuleNotFoundError: apollo.provisioning`.
   - Verify: `.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/ -q` → collection/import error (expected RED).
2. **[GREEN] `apollo/provisioning/__init__.py`** — package seam (re-exports). At this step the
   sub-modules don't exist yet; create them as the next steps demand. (Practically: create the
   three source files in 3-4 then wire `__init__` exports.)
3. **[GREEN] `apollo/provisioning/problem_hash.py`** — `problem_dup_hash` + `normalize`/`canonical`
   helpers + `_DUP_HASH_VERSION`. Run `test_problem_hash.py` → GREEN.
   - Verify: `.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_problem_hash.py -q`.
4. **[GREEN] `apollo/provisioning/promotion_lint.py`** — `PromotionResult` frozen dc + the 8 gate
   functions + `run_promotion_lint` + `_normalize_symbol` + `_LINT_ATTEMPT_ID`. Implement gates in
   order; run `test_promotion_lint.py` incrementally (positive first, then each adversarial).
   - Verify: `.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_promotion_lint.py -q`.
5. **[GREEN] wire `__init__.py`** re-exports; assert `from apollo.provisioning import
   PromotionResult, run_promotion_lint, problem_dup_hash` works.
   - Verify: `.venv/Scripts/python.exe -c "from apollo.provisioning import PromotionResult, run_promotion_lint, problem_dup_hash; print('ok')"`.
6. **[GREEN] full suite + coverage gate.**
   - Verify: `.venv/Scripts/python.exe -m pytest apollo/provisioning -q` then
     `.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml apollo/provisioning -q` and
     `.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml --compare-branch=feat/apollo-kg-wu3b2a-migration-030 --fail-under=95`
     (pure module — AIM FOR 100%).
7. **[GREEN] owner-doc reconcile** — register `apollo/provisioning/` in `docs/architecture/apollo.md`,
   set `last_verified: 2026-06-19` (see Owner-doc updates).

## File: apollo/provisioning/__init__.py

Mirrors `apollo/resolution/__init__.py`. Module docstring describing the §8B promotion-lint safety
core (PURE, LLM-free, DB-free; gate-4 symbols + gate-8 hashes passed in by the caller), then:

```python
from __future__ import annotations

from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promotion_lint import PromotionResult, run_promotion_lint

__all__ = ["PromotionResult", "run_promotion_lint", "problem_dup_hash"]
```

Plus an empty `apollo/provisioning/tests/__init__.py` is NOT required (pytest rootdir collection
works without it; sibling `apollo/resolution/tests/` has none — confirm by globbing). Create
`apollo/provisioning/tests/` as a directory holding the two test modules only.

## File: apollo/provisioning/problem_hash.py

```python
from __future__ import annotations

import hashlib
import re
from typing import Mapping

from apollo.schemas.problem import Problem

_DUP_HASH_VERSION = "promotion-dup-v1"

def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()

def _canonical_givens(given_values: Mapping[str, float]) -> str:
    return ",".join(f"{k}={v!r}" for k, v in sorted(given_values.items()))

def problem_dup_hash(problem: Problem) -> str:
    """Gate-8 dedup key: sha256 over normalized text + canonical givens + target.

    Course/concept scoping is the CALLER's job (the BIGINT-concept-scoped set passed to
    run_promotion_lint); this hash is content-only and deterministic.
    """
    payload = (
        f"{_DUP_HASH_VERSION}|{_normalize_text(problem.problem_text)}"
        f"|{_canonical_givens(problem.given_values)}|{problem.target_unknown}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- ~30 lines. Stdlib only. No DB, no LLM.

## File: apollo/provisioning/promotion_lint.py

Module docstring MUST carry (binding):
- The 8-gate list + short-circuit-on-first-failure contract (§8B.4).
- "Gate 4 is the SOLE foreign-symbol guard; gate 6 (`parse_zero_form`) does NOT reject foreign
  symbols — `sympy.parse_expr` auto-creates unknown symbols (§9 FEAS-2 / ADJ #4)."
- "Gate 7 is a PAPER closure check, NOT an end-to-end solve (honest v1 limit, §8B.4:1347); the
  per-problem quarantine (3B2h) is the runtime catch."
- "PURE / DB-free / LLM-free: gate-4 `canonical_symbols`/`normalization_map` and gate-8
  `existing_problem_hashes` are PASSED IN by the caller (populated by 3B2a/3B2d)."

Public + internal shape:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pydantic import ValidationError

from apollo.ontology.edges import EdgeType
from apollo.schemas.problem import Problem
from apollo.solver.sympy_exec import parse_zero_form
from apollo.errors import MalformedEquationError
from apollo.persistence.learner_model_seed import (
    validate_reference_graph,
    _ENTRY_TYPE_TO_KIND_PREFIX,
)
from apollo.provisioning.problem_hash import problem_dup_hash

_LINT_ATTEMPT_ID = 0  # attempt-agnostic; to_kg_graph uses it only to stamp nodes/edges.

@dataclass(frozen=True)
class PromotionResult:
    ok: bool
    failed_gate: int | None   # 1..8, None on pass
    diagnostic: str           # "" on pass

def run_promotion_lint(
    graph: dict,
    *,
    canonical_symbols: set[str] | frozenset[str],
    normalization_map: Mapping[str, str],
    existing_problem_hashes: set[str] | frozenset[str],
) -> PromotionResult: ...
```

**Internal structure (executor-binding):**
- Gate 1 is special: it both validates AND produces the `Problem` + `KGGraph` reused downstream.
  Structure `run_promotion_lint` as: build/validate in a gate-1 try → if it fails return gate-1
  result; else thread `(problem, kg)` to gates 2-8. Each `_gate_N` returns `str | None`.
- Order the gate dispatch as a list of (n, callable) so the loop returns on first non-None — this
  keeps the short-circuit ORDER guarantee explicit and testable.
- `_normalize_symbol(name: str, canonical_symbols, normalization_map) -> str | None` — the gate-4
  helper (exact → strip-subscript base → normalization_map). Pure, unit-tested directly.
- Diagnostics are human-readable strings; tests assert `failed_gate` (the discriminating signal),
  NOT exact diagnostic wording (so wording stays editable — see Deviations).
- ~180-220 lines. Keep <800 (well within). If it grows past ~250, extract `_gates.py` (per
  small-file rule) — but the 8 gates as private functions in one module is fine and cohesive.

## Full test list — test_problem_hash.py

File: `apollo/provisioning/tests/test_problem_hash.py`. No DB/LLM/mocks (pure). Uses a
`_bernoulli_problem() -> Problem` helper that builds a valid `Problem.model_validate(...)` from a
minimal-but-valid dict (modeled on `problem_01.json`, trimmed to what `Problem` requires — note
`Problem` drops `entity_key`/`declared_paths`, so the helper need only pass `id`, `concept_id`,
`difficulty`, `problem_text`, `given_values`, `target_unknown`, `reference_solution`).

| Test name | Asserts | Deps mocked |
|---|---|---|
| `test_hash_is_deterministic` | two calls on the SAME problem return the identical hexdigest (string equality) | none (pure) |
| `test_hash_is_sha256_hexdigest_shape` | result is 64 lowercase hex chars (`len==64`, `int(h,16)` parses) | none |
| `test_hash_ignores_problem_text_whitespace_and_case` | a problem whose `problem_text` differs ONLY by surrounding/internal whitespace + case from another hashes IDENTICALLY (proves `normalize`) | none |
| `test_hash_ignores_given_values_key_order` | two `given_values` dicts with the same pairs in different insertion order hash IDENTICALLY (proves `sorted`) | none |
| `test_hash_treats_float_equal_values_equal` | `given_values={"P1": 2.0}` and `{"P1": 2.00}` hash IDENTICALLY (float canonicalization) | none |
| `test_hash_differs_on_problem_text_change` | changing a substantive word in `problem_text` changes the hash | none |
| `test_hash_differs_on_given_values_change` | changing a given value (`2.0`→`3.0`) changes the hash | none |
| `test_hash_differs_on_target_unknown_change` | changing `target_unknown` (`P2`→`v2`) changes the hash | none |
| `test_dup_collision_for_semantically_identical_problems` | two DISTINCT `Problem` objects with the same normalized text + givens + target (e.g. differing only by `id` and whitespace) produce the SAME hash — the gate-8 dup-collision case | none |

**Why these discriminate:** the three `_differs_*` tests + the three `_ignores/_treats_equal` tests
pin every component of the hash payload independently — reverting `normalize`, `sorted`, or any
payload field makes at least one test RED.

## Full test list — test_promotion_lint.py

File: `apollo/provisioning/tests/test_promotion_lint.py`. No DB/LLM/mocks (pure). Central fixtures:

- `_bernoulli_graph() -> dict` — the POSITIVE fixture: the FULL annotated bernoulli problem dict
  (the `problem_01.json` shape, INCLUDING per-step `entity_key` + top-level `declared_paths`), the
  same 7 steps. Hand-inline it in the test module (do NOT read the JSON file — keep the test
  hermetic and the fixture explicit so mutations are visible in-diff). This is the seeded problem
  that passes all 8 gates.
- `_canonical_symbols() -> set[str]` → `{"P","rho","v","A","h","g","Q"}` (from `canonical_symbols.json`).
- `_normalization_map() -> dict` → the `normalization_map.json` phrase map (or the subset needed).
- `_mutate(graph, **patch)` helper — returns a DEEP COPY of the bernoulli graph with one targeted
  change, so each adversarial fixture is the positive baseline + ONE mutation (guarantees the
  fixture fails for EXACTLY its target gate; see the discipline section).

### Positive (1 test)
| Test name | Asserts |
|---|---|
| `test_seeded_bernoulli_passes_all_eight_gates` | `run_promotion_lint(_bernoulli_graph(), canonical_symbols=_canonical_symbols(), normalization_map=_normalization_map(), existing_problem_hashes=set())` returns `PromotionResult(ok=True, failed_gate=None, diagnostic="")` |

### Adversarial — one per gate, each asserting EXACTLY `failed_gate==N` (9 tests; gate 1 has two)
| Test name | Mutation of the bernoulli baseline | Asserts | Discrimination note |
|---|---|---|---|
| `test_gate1_fires_on_forbidden_edge_pair` | force a forbidden edge: add a step whose typed edge violates `EDGE_ALLOWED_PAIRS` — e.g. an `equation` step with `depends_on` to a `condition` is FINE (DEPENDS_ON is generic); instead inject a SCOPES-shaped violation by making a `simplification` step the SOURCE and an entry that yields `equation SCOPES condition`. SIMPLEST realizable g1-edge case: a `simplification` step whose target via `to_kg_graph` produces a SCOPES edge to a non-equation. Since `to_kg_graph` only emits SCOPES via... (NOTE: `to_kg_graph` does NOT emit SCOPES edges — only DEPENDS_ON/USES/PRECEDES). **REVISED construction:** trigger gate-1 via the `_check_pair` self-loop OR a USES edge to a non-equation. A procedure_step `uses_equations:["<a condition id>"]` makes `Problem._resolve_references` RAISE (uses_equations must be an equation id, `schemas/problem.py:77-85`) → caught at gate 1. | `ok is False and failed_gate == 1` | fails at `Problem.model_validate` / `to_kg_graph` |
| `test_gate1_fires_on_unmapped_entry_type_variable_mapping` | change one step's `entry_type` to `"variable_mapping"` (schema-LEGAL — it's in `EntryType`, `schemas/problem.py:33-36`) with valid `VariableMappingContent`-shaped content (`{"term":..,"symbol":..}`) | `ok is False and failed_gate == 1` (the mint-map sub-check fires; `variable_mapping` ∉ `_ENTRY_TYPE_TO_KIND_PREFIX` on THIS branch) | this is the ADJ #5 fail-closed guard; asserts the gate fires BEFORE 3B2d extends the map |
| `test_gate2_fires_on_missing_entity_link` | delete one step's `entity_key` (schema-legal — `Problem` drops it; closure-illegal) | `ok is False and failed_gate == 2` | passes gate 1 (schema), fails closure |
| `test_gate3_fires_on_depends_on_cycle` | set step A `depends_on=["B"]` and step B `depends_on=["A"]` (both ids exist → passes `Problem`'s existence check; forms a DEPENDS_ON cycle) | `ok is False and failed_gate == 3` | passes 1-2; `topological_order(DEPENDS_ON)` raises |
| `test_gate4_fires_on_foreign_symbol` | swap one equation's `symbolic` to introduce `x` (e.g. `... + x`), with `x` ∉ canonical_symbols and not normalizable | `ok is False and failed_gate == 4` | passes 1-3; the SOLE foreign-symbol guard |
| `test_gate5_fires_on_terminal_not_computing_target` | change the terminal procedure step's `uses_equations` to reference ONLY `continuity` (which lacks `P2==target`) | `ok is False and failed_gate == 5` | passes 1-4; terminal-computes-target sub-clause |
| `test_gate6_fires_on_malformed_equation` | set one equation's `symbolic` to `"rho*A1*v1 - = P2"` (dangling operator) | `ok is False and failed_gate == 6` | passes 1-5 (the malformed eq still has only canonical symbols so gate 4 — which SKIPS malformed eqs — passes); `parse_zero_form` raises `MalformedEquationError` |
| `test_gate7_fires_on_unclosed_system` | add a free symbol `w` to one equation (`... + w`), with `w` in canonical_symbols (so gate 4 passes) but no given/target/intermediate/simplification reaching it | `ok is False and failed_gate == 7` | passes 1-6; paper-closure check. NOTE: `w` must be made acceptable to gate 4 — either add `w` to the passed `canonical_symbols` for THIS test, or use an existing canonical base with no closure path; pick the construction that fails ONLY gate 7 |
| `test_gate8_fires_on_duplicate` | pass `existing_problem_hashes={problem_dup_hash(Problem.model_validate(_bernoulli_graph()))}` | `ok is False and failed_gate == 8` | passes 1-7; the only gate reading `existing_problem_hashes` |

### Short-circuit ORDER tests (2 tests — prove the first-failure-wins guarantee)
| Test name | Asserts |
|---|---|
| `test_short_circuit_reports_earliest_gate` | a problem mutated to fail BOTH gate 3 (depends_on cycle) AND gate 8 (hash in existing set) reports `failed_gate == 3` (the EARLIER gate), never 8 | 
| `test_short_circuit_gate1_precedes_all` | a schema-broken problem that ALSO would fail later gates reports `failed_gate == 1` |

### White-box helper tests (keep pure branches covered — 3 tests)
| Test name | Asserts |
|---|---|
| `test_normalize_symbol_accepts_subscripted_base` | `_normalize_symbol("P2", {"P",...}, {})` returns `"P"`; `_normalize_symbol("x", {"P"}, {})` returns `None` |
| `test_normalize_symbol_uses_normalization_map` | `_normalize_symbol("static pressure", set(), {"static pressure":"P"})` returns `"P"` |
| `test_gate5_chain_helper_rejects_forked_chain` | build a hand-made forked `KGGraph` (two procedure_steps with no incoming PRECEDES = two heads) directly and assert the single-chain-head branch of gate 5 fires (covers the `len(heads) != 1` path that `to_kg_graph` can never produce) |

**Construction discipline note (executor):** each adversarial test starts from `_bernoulli_graph()`
and applies ONE `_mutate(...)`. Before asserting the target gate, the test SHOULD assert the
baseline still passes (a single `test_seeded_bernoulli_passes_all_eight_gates` covers this; do not
repeat). If a mutation accidentally trips an earlier gate, the `failed_gate` assertion will catch it
(RED) — that is the mutation-discrimination guarantee working. Where a clean single-mutation
construction is hard (gate 1 forbidden-edge, gate 7 unclosed), the table's REVISED construction is
binding; the executor MUST verify the produced fixture fails ONLY the intended gate by checking
`failed_gate` equals the target and NOT an earlier number.

## Discriminating-fixture discipline (mutation survivability)

The orchestrator applies INDEPENDENT MUTATION testing: each gate's adversarial fixture must go RED
iff THAT gate is reverted. To satisfy this:

1. **One mutation per fixture.** Every adversarial graph = `_bernoulli_graph()` + exactly one
   `_mutate`. A fixture that changes two things can pass even if its target gate is reverted (the
   other change still fails it at a different gate). This is the cardinal rule.
2. **Assert `failed_gate == N`, not just `ok is False`.** Asserting only `ok is False` is NOT
   discriminating — reverting gate 6 and a malformed-eq fixture would still report `ok False` via
   gate 4/7. The `failed_gate == N` assertion is what makes each test target-specific.
3. **Each adversarial must pass gates 1..N-1.** Verified by the `failed_gate == N` value (a fixture
   that trips gate 3 when targeting gate 6 reports 3, RED). The construction notes in the test table
   are chosen so the mutation is schema-legal and closure-legal up to gate N.
4. **Gate-revert simulation (what the orchestrator will do):** comment out `_gate_4` from the
   dispatch list → `test_gate4_fires_on_foreign_symbol` must FAIL (the foreign symbol now slips to
   gate 7 or passes). If it still passes, the fixture is not discriminating. Same for each gate.
5. **Gate 5 caveat (flagged):** because a schema-valid `Problem` cannot produce a forked PRECEDES
   chain, the gate-5 ADVERSARIAL uses the terminal-computes-target sub-clause, and the forked-chain
   branch is covered WHITE-BOX (`test_gate5_chain_helper_rejects_forked_chain`). Reverting the
   terminal-target check makes `test_gate5_fires_on_terminal_not_computing_target` RED; reverting the
   single-head check makes the white-box test RED. Both halves of gate 5 are independently pinned.

## Owner-doc updates

Owner doc: `docs/architecture/apollo.md` (its `owns: apollo/**` glob ALREADY covers
`apollo/provisioning/**` — no frontmatter glob edit needed). Two reconciliations, SAME commit:

1. **`last_verified:` → `2026-06-19`** in the frontmatter (line 13; it is already 2026-06-19 from
   3B2a — re-affirm/keep). The binding-constraints text says "set to 2026-06-16"; the spec/file
   header and the task date are 2026-06-19 (the 3B2a commits already stamped 2026-06-19). **Use
   `2026-06-19`** to match the rest of the doc and the current date; flagged as a MEDIUM signal
   (the 2026-06-16 in the task constraints is stale relative to the 2026-06-19 the doc already
   carries — do not REGRESS the doc's date backward).
2. **Add a module-map row** for `apollo/provisioning/` (insert after the `apollo/schemas/` row, or
   wherever provisioning sorts — adjacent to `apollo/resolution/` is most navigable). Row content
   (register the package + the 8-gate lint, ADDITIVE, prefixed `**WU-3B2b**`):

   > **`apollo/provisioning/`** | `__init__.py`, `promotion_lint.py`, `problem_hash.py` |
   > **WU-3B2b — the §8B.4 PURE 8-gate promotion lint (the auto-provisioning SAFETY CORE; no
   > LLM/DB/Neo4j/container/migration).** `run_promotion_lint(graph, *, canonical_symbols,
   > normalization_map, existing_problem_hashes) -> PromotionResult(ok, failed_gate, diagnostic)`
   > runs the eight gates IN ORDER, short-circuiting on first failure: (1) schema — `Problem.model_validate`
   > + every edge in `EDGE_ALLOWED_PAIRS` (via `to_kg_graph`) + a mint-map-membership sub-check
   > (any `entry_type` ∉ the frozen `_ENTRY_TYPE_TO_KIND_PREFIX` fails CLOSED — ADJ #5 defense-in-depth,
   > so a `variable_mapping` step rejects until 3B2d extends the map); (2) reference closure —
   > `validate_reference_graph` VERBATIM (§6.1, NOT the whole lint); (3) DAG — `KGGraph.topological_order(DEPENDS_ON)`
   > raises on cycle; (4) symbol consistency — the SOLE foreign-symbol guard (gate 6 does NOT reject
   > foreign symbols: `parse_expr` auto-creates unknown symbols), reading PASSED-IN
   > `canonical_symbols`/`normalization_map` (populated by 3B2d) so the core stays pure; (5) procedure
   > coherence — one PRECEDES chain + terminal computes `target_unknown`; (6) SymPy parse —
   > `parse_zero_form` catches MALFORMED syntax only; (7) equation-system closure — a PAPER check
   > (every symbol given/target/intermediate/cancelled), NOT an end-to-end solve (honest v1 limit
   > §8B.4:1347; the per-problem quarantine 3B2h is the runtime catch); (8) duplicate — `problem_dup_hash(problem)`
   > (sha256 over normalized text + canonical givens + target, version-prefixed `promotion-dup-v1`)
   > ∉ the caller-supplied concept-scoped `existing_problem_hashes` (keyed on the BIGINT concept).
   > PURE + fixture-tested (one positive bernoulli passing all 8 + one discriminating adversarial per
   > gate asserting `failed_gate==N` + short-circuit-order). 3B2b owns the gate logic + diagnostic
   > ONLY: it does NOT promote, call `project_canon`, write `rejected_problems`, or touch the DB —
   > the `PromotionResult` → promote/reject mapping is 3B2g's. Mirrors `apollo/resolution/`
   > (standalone, re-export `__init__`).

   Keep the row terse but interface-complete (matching the doc's dense-row convention). Do NOT edit
   any other row.

## Out-of-scope boundaries

This unit touches ONLY these files (binding scope):
- `apollo/provisioning/__init__.py` (NEW)
- `apollo/provisioning/promotion_lint.py` (NEW)
- `apollo/provisioning/problem_hash.py` (NEW)
- `apollo/provisioning/tests/test_promotion_lint.py` (NEW)
- `apollo/provisioning/tests/test_problem_hash.py` (NEW)
- `docs/architecture/apollo.md` (EDIT — register the package + bump last_verified)

**Explicitly NOT in this unit (block + escalate if a step proposes them):**
- NO DB / migration / ORM. The verify REAL-INFRA gate MUST NOT trigger — there are NO files under
  `database/migrations|tests/database|apollo/knowledge_graph`. If it triggers, the unit is mis-scoped.
- NO LLM / network / containers. NO `cheap_chat`/`main_chat`/OpenAI import.
- NO edit to the frozen `_ENTRY_TYPE_TO_KIND_PREFIX` map (that additive `variable_mapping` extension
  is 3B2d's edit per ADJ #5). 3B2b only READS the map to GUARD.
- NO `project_canon` call, NO `apollo_rejected_problems` write, NO promotion / tier flip, NO
  `apollo_concept_problems` read/write — all of that is 3B2g's orchestrator.
- NO new package. Stdlib `hashlib`/`re` + the already-present sympy/pydantic only. If a step wants a
  package (rapidfuzz, scipy, instructor, a pytest-LLM helper), BLOCK + escalate (ADJ #8).
- NO dedup ladder (3B2c), NO scrape/mint (3B2d), NO solution lifecycle (3B2e), NO queue/metered
  client (3B2f), NO quarantine (3B2h). Gate 8 is the DUP-HASH key only — NOT the dedup ladder.
- NO branch/PR/push (work on `feat/apollo-kg-wu3b2b-promotion-lint`, already checked out).

## Risks

- **[HIGH] Gate 5 "forked PRECEDES chain" is structurally unreachable from a schema-valid `Problem`.**
  `to_kg_graph` builds the PRECEDES chain from the validated 1..N `order`, so it is always linear.
  The plan resolves this by (a) targeting the adversarial at the TERMINAL-COMPUTES-TARGET sub-clause
  and (b) covering the single-chain-head branch white-box on a hand-built forked `KGGraph`. If the
  executor cannot make the white-box forked-graph test fail when the head-count check is reverted,
  escalate — the gate-5 fork branch may need a different exercise. Confidence: HIGH this is the right
  decomposition; MEDIUM the executor lands both halves without a round-trip.
- **[HIGH] Each adversarial must fail ONLY its target gate.** The single-mutation discipline is the
  safeguard, but gate 1 (forbidden-edge) and gate 7 (unclosed) are the two hardest clean
  single-mutations (a foreign/forbidden construct can accidentally trip gate 4 or gate 2). The plan
  pins REVISED constructions (gate 1 via `uses_equations`→non-equation; gate 7 via a closure-only
  free symbol that IS canonical so gate 4 passes). The `failed_gate==N` assertion is the RED tripwire
  if a fixture is non-discriminating.
- **[MEDIUM] Gate 4 normalization semantics (subscript stripping).** The seeded equations use
  subscripted symbols (`P1`,`v2`) while `canonical_symbols.json` holds BASE symbols (`P`,`v`). The
  plan's `_normalize_symbol` strips a trailing digit run. RISK: a multi-digit subscript (`h12`) or a
  symbol like `A2` must reduce to `A`. The strip-trailing-digits rule (`re.sub(r"\d+$","",name)`)
  handles these; the positive bernoulli fixture exercises `A1`,`A2`,`v1`,`v2`,`h1`,`h2`,`P1`,`P2`.
  Verify the positive fixture passes gate 4 before declaring done.
- **[MEDIUM] `gate 7` intermediate-symbol derivation is the fuzziest gate.** The spec calls it a
  "paper closure check"; the plan's a/b/c/d rule (given/target/intermediate/cancelled) is a
  defensible deterministic v1. RISK: the real bernoulli problem must pass — the plan walks it through
  ({v2} as intermediate, {g,h1,h2} cancelled by the horizontal simplification). If the executor's
  cancellation-token match is too strict and the positive fixture fails gate 7, loosen the
  simplification-`transformation` token match (it is a v1 paper check, lenient by design).
- **[MEDIUM] Owner-doc `last_verified` date.** Task constraints say "2026-06-16" but the doc already
  carries 2026-06-19 (stamped by 3B2a) and the current date is 2026-06-19. The plan uses 2026-06-19
  to avoid regressing the doc backward. Flagged for the orchestrator.
- **[LOW] `Problem.model_validate` extra-key tolerance.** The plan relies on `Problem`/`ReferenceStep`
  NOT setting `extra='forbid'` so the annotated dict's `entity_key`/`declared_paths` are silently
  dropped at gate 1. Verified: neither model sets `model_config` (`schemas/problem.py:40-55`). If a
  future pydantic bump changes the default, gate 1 would reject the positive fixture — the positive
  test catches it immediately.
- **[LOW] `parse_zero_form` in gate 4.** Using it for symbol extraction couples gate 4 to the sympy
  parser. A malformed equation would raise `MalformedEquationError` inside gate 4; the plan SKIPS
  malformed equations in gate 4 (leaving the verdict to gate 6). Since gate 4 runs before gate 6,
  this skip is essential — confirm the g6 malformed fixture reports `failed_gate==6`, not 4.

## Deviations I'd allow the executor

- **Diagnostic strings are free text.** Tests assert `failed_gate` (the discriminating signal) and
  `ok`, NOT exact `diagnostic` wording. The executor may phrase diagnostics however reads clearest.
  (A single test MAY assert a diagnostic CONTAINS a keyword like "duplicate"/"foreign" for
  human-debuggability, but never the full string.)
- **Gate internal helper layout.** The eight `_gate_N` functions may live in `promotion_lint.py`
  (preferred — cohesive) OR be extracted to `apollo/provisioning/_gates.py` if the module exceeds
  ~250 lines (small-file rule). Either is fine; keep the public surface (`PromotionResult`,
  `run_promotion_lint`) in `promotion_lint.py`.
- **`_normalize_symbol` subscript rule.** `re.sub(r"\d+$", "", name)` vs a `rstrip(digits)` — either,
  as long as `A1`→`A`, `v2`→`v`, `h12`→`h`, and a bare `x` stays `x` (→ None if not canonical).
- **`_LINT_ATTEMPT_ID` value.** Any fixed int (0 is fine); it is attempt-agnostic.
- **Gate 7 cancellation-token matching.** Whole-token substring match on the simplification
  `transformation` string OR reading `content.get("variables", [])` — the executor picks whichever
  makes the positive bernoulli fixture pass while the g7 adversarial (`w`) fails. The v1 closure
  check is intentionally lenient.
- **`existing_problem_hashes` accepted type.** `set[str] | frozenset[str]` — accept any container
  supporting `in`. Do not over-constrain the annotation.
- **The positive fixture's exact prose/values** may be trimmed from `problem_01.json` as long as it
  remains a real, gate-passing bernoulli problem with ≥2 equations, ≥1 condition, ≥1 simplification,
  ≥2 procedure_steps (so every gate has something to check). Keep it inline in the test module.
- **What the executor must NOT change:** the public signatures, the gate ORDER, the
  one-mutation-per-adversarial discipline, the `failed_gate==N` assertions, the PURE/no-DB/no-LLM
  boundary, the read-only treatment of `_ENTRY_TYPE_TO_KIND_PREFIX`, and the stdlib-only constraint.

## Verify commands

All commands use the venv interpreter (a bare-`python` ImportError is an interpreter-selection
error, not a blocker). Run from the repo root `ai-ta-backend/`.

```bash
# 1. Package imports + public surface resolves
.venv/Scripts/python.exe -c "from apollo.provisioning import PromotionResult, run_promotion_lint, problem_dup_hash; print('import ok')"

# 2. The two test modules pass
.venv/Scripts/python.exe -m pytest apollo/provisioning -q

# 3. Patch coverage >= 95% vs the 3B2a compare branch (pure module — AIM FOR 100%)
.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml apollo/provisioning -q
.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml \
  --compare-branch=feat/apollo-kg-wu3b2a-migration-030 --fail-under=95
#   (if the `diff-cover` console script is on PATH, `diff-cover coverage.xml
#    --compare-branch=feat/apollo-kg-wu3b2a-migration-030 --fail-under=95` is equivalent)

# 4. No real-infra trigger — confirm NO files were created under the real-infra globs
#    (must print nothing):
git diff --name-only feat/apollo-kg-wu3b2a-migration-030 -- database/migrations tests/database apollo/knowledge_graph

# 5. Full apollo suite still green (no regression from the new package collection)
.venv/Scripts/python.exe -m pytest apollo -q

# 6. Owner-doc drift reconciled: apollo.md mentions apollo/provisioning and last_verified is 2026-06-19
.venv/Scripts/python.exe -c "import pathlib,sys; t=pathlib.Path('docs/architecture/apollo.md').read_text(encoding='utf-8'); sys.exit(0 if 'apollo/provisioning' in t and 'last_verified: 2026-06-19' in t else 1)"
```

**Definition of done:** commands 1-6 all pass; `test_promotion_lint.py` has the 1 positive + 9
adversarial (g1×2, g2-g8) + 2 short-circuit + 3 white-box tests; each adversarial asserts
`failed_gate==N`; `test_problem_hash.py` has the determinism + dup-collision suite; no new package;
no file under the real-infra globs; `apollo.md` registers the package + carries `last_verified: 2026-06-19`.
