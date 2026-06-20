# Plan: Apollo KG WU-2A — context-aware one-call typed-edge extraction with explicit/inferred provenance

**Goal:** Upgrade `parse_utterance` to a single GPT-4o strict-structured-outputs call that emits typed nodes AND all four edge types (PRECEDES/USES/SCOPES/DEPENDS_ON), links across turns via an optional `graph_context`, rejects edges against `EDGE_ALLOWED_PAIRS`, and tags every parser edge `explicit` vs `inferred` — parser-only, fully backward-compatible, LLM mocked in every test.

**Architecture:** student utterance + optional prior-attempt graph context → one GPT-4o `json_schema` call → typed Nodes + typed Edges (provenance-tagged, pair-validated) → `(nodes, edges)`. No Neo4j, no network, no `chat.py`/`store.py` changes (those are WU-2B).

**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §4 (extraction), §5 (edge vocabulary), §6.3 (EDGE_ALLOWED_PAIRS endpoint rules), §11 (anti-scope), §12 phase 2.

---
provides:
  - parse_utterance(..., graph_context: GraphContext | None = None) — optional context param, default None (backward compatible)
  - GraphContext dataclass (frozen) — prior-attempt node summaries threaded in from WU-2B later
  - EdgeProvenance literal "explicit" | "inferred" on Edge
  - strict json_schema for one-call node+typed-edge+provenance extraction (apollo/parser/extraction_schema.py)
  - edge endpoint-pair validation reused from EDGE_ALLOWED_PAIRS at parse time (rejects + logs, no silent drop within the parser)
  - updated parser_prompt_template.md (edge vocabulary + EXISTING GRAPH section + provenance rubric)
consumes:
  - apollo.ontology.edges (EdgeType, Edge, EDGE_ALLOWED_PAIRS) — extended with provenance
  - apollo.ontology.nodes (Node, NodeType, build_node) — unchanged
  - apollo.subjects.ConceptDefinition (parser_prompt_template) — unchanged loader
  - OpenAI sync client (mocked in tests)
depends_on:
  - none in-unit. WU-2B (store.write_edges no-silent-drop + chat.py wiring + cross-turn de-dup) consumes graph_context but is OUT OF SCOPE here.
---

## Overview

Today (`apollo/parser/parser_llm.py`) the runtime parser:
- makes ONE GPT-4o call in `{"type": "json_object"}` mode (NOT strict
  `json_schema`), temp 0.0, system prompt = `build_system_prompt(concept)`,
  user = the raw utterance;
- extracts nodes from `payload["entries"]`, then derives edges
  **deterministically and within-turn only**: `_resolve_uses_edges`
  (USES proc→equation from `uses_equation_ordinals`) and
  `_build_precedes_chain` (PRECEDES proc→proc by emission order);
- never emits SCOPES (dead code in `EdgeType`) or DEPENDS_ON (only
  `Problem.to_kg_graph` reference graphs emit DEPENDS_ON);
- has no `graph_context`, no cross-turn linking, no edge provenance;
- short-circuits triviality via `_is_non_trivial` (length floor < 10, ACK
  set, math-char regex, then `cheap_chat` classifier at threshold 0.6) and
  raises `ParserCouldNotExtractError` only when a non-trivial utterance
  yields zero nodes.

WU-2A makes the parser emit nodes AND all four typed edges in a **single
strict-`json_schema` GPT-4o call** (the RQ3-spike shape, §4), with the edge
vocabulary and an EXISTING-GRAPH section in the prompt so edges can reference
prior-turn node ids, and with an `explicit`/`inferred` provenance tag on every
parser edge. The triviality short-circuit, the
`ParserCouldNotExtractError` contract, and node confidence pass-through are
preserved unchanged. `graph_context` is an OPTIONAL parameter (default
`None`) — when omitted the call behaves like the current within-turn-only
parser, so `apollo/handlers/chat.py` keeps working untouched until WU-2B
wires context in.

**Why one call (spike-verified, §4):** the RQ3 spike
(`scripts/spikes/rq3_edge_extraction.py` + `rq3_results.json` — REFERENCE
ONLY; never imported) showed a single GPT-4o strict-outputs call with the
attempt graph as context yields ~88% valid edges, 11/21 edges crossing turns
(incl. late conditions SCOPES-linked to earlier equations), orphan nodes
20→6, ~$0.004/turn at median 2.45s. WU-2A ports that prompt + schema shape
into the production parser; it does NOT import the spike module.

**What stays out (WU-2B, §4 / §12 phase 2 tail):** `store.write_edges`
currently silently drops invalid edges — fixing that to log-not-drop,
`chat.py` wiring of `graph_context` from the live attempt graph, and
cross-turn de-duplication are WU-2B. WU-2A only changes the parser's
*output*; the in-parser pair check (rejects + logs) is the parser's own
guard so it never emits an edge that would violate the `Edge` validator.

## Prior art in repo

Copy shapes from these; do not invent new ones.

1. **RQ3 spike `SYSTEM_PROMPT` + `RESPONSE_SCHEMA`**
   (`scripts/spikes/rq3_edge_extraction.py:45-163`) — the proven prompt
   wording (`EDGE_VOCAB`, the EXISTING-GRAPH rules, the per-entry confidence
   rubric, the `from_ref`/`to_ref` ref convention `"n<i>"` = i-th entry of
   this response 0-based OR an existing-graph id) and the strict flat schema
   (every type-specific field present-and-nullable so `additionalProperties:
   false` + `strict: true` validate). **REFERENCE ONLY — do NOT import.** The
   plan re-implements these as production code in `parser_llm.py` /
   `extraction_schema.py` / the template, adding the `provenance` field the
   spike lacked.

2. **Existing parser deterministic edge derivation**
   (`parser_llm.py:183-242`, `_resolve_uses_edges` / `_build_precedes_chain`)
   — these become the **fallback** when `graph_context is None` and the model
   returns no explicit `edges`, preserving today's behavior bit-for-bit. They
   also model the exact `Edge(...)` construction call (with
   `from_node_type` / `to_node_type` set so the pair validator runs).

3. **`response_format={"type":"json_schema",...}` call shape** — the spike
   (`scripts/spikes/rq3_edge_extraction.py:239-247`) already calls
   `client.chat.completions.create(..., response_format={"type":
   "json_schema", "json_schema": RESPONSE_SCHEMA}, temperature=0.0)`. Same
   client instantiation pattern as `parser_llm.parse_utterance:260-269`
   (`client = OpenAI(); client.chat.completions.create(...)`), so the mock
   point is unchanged: `@patch("apollo.parser.parser_llm.OpenAI")`.

4. **Edge pair validation** — `EDGE_ALLOWED_PAIRS` + the `Edge` model
   validator (`apollo/ontology/edges.py:28-71`). The parser already relies on
   the validator raising `ValueError` for bad pairs (it wraps construction in
   `try/except ValueError`, `parser_llm.py:216-217`). WU-2A pre-checks the
   pair against `EDGE_ALLOWED_PAIRS` and logs a rejection reason before
   construction, matching §4's "deterministic validation + logging is the
   rejection point."

5. **Test conventions** — `test_triviality.py` / `test_parser_confidence.py`
   are the live (non-skipped) parser tests. They use a module-scoped
   `concept = load_concept("fluid_mechanics", "bernoulli_principle")` fixture,
   `@patch("apollo.parser.parser_llm.OpenAI")` for the main call, and
   `@patch("apollo.parser.parser_llm.cheap_chat")` for the triviality
   classifier, with `_mock_openai_response(...)` helpers. WU-2A tests copy
   this exact mocking discipline. (`test_parser.py` is legacy V2,
   `skip(allow_module_level=True)` — do NOT touch or revive it; WU-2A adds a
   NEW test module instead.)

## Structural prep (from neighborhood scan)

Change-path artifacts scanned (one ring out): `parser_llm.py`,
`prompt_builder.py`, `ontology/edges.py`, `ontology/nodes.py`, the
per-concept `parser_prompt_template.md`.

- **CBO / imports:** `parser_llm.py` imports ~7 modules — under the >8
  threshold. Adding `extraction_schema` + a `GraphContext` import keeps it at
  the line; acceptable.
- **WMC / size:** `parser_llm.py` is 305 lines today. The one-call rewrite
  REMOVES net logic (the deterministic edge derivation becomes a
  no-context fallback, not the primary path) but ADDS schema-response parsing
  + edge-ref resolution + provenance handling. To stay well under 800 lines
  and keep functions <50 lines:
  - [ ] Extract the strict schema into a new `apollo/parser/extraction_schema.py`
        (pure data + a small `build_extraction_schema()` returning the dict)
        so `parser_llm.py` does not grow a ~80-line literal.
  - [ ] Extract edge-ref resolution (LLM `from_ref`/`to_ref` → node_ids,
        provenance, pair-check) into a focused helper
        `_resolve_typed_edges(...)` in `parser_llm.py` (mirrors the existing
        `_resolve_uses_edges` granularity).
  - Verify: `python -c "import ast,sys; [print(f.name, f.end_lineno-f.lineno) for f in ast.walk(ast.parse(open('apollo/parser/parser_llm.py').read())) if isinstance(f, ast.FunctionDef)]"`
    — every function < 50 lines; file < 400 lines.
- **Pipeline coupling:** the parser shares no state with `store.py` except
  the `(nodes, edges)` return tuple — the contract is clean. WU-2A does NOT
  touch `store.py`; the `graph_context` IN-param is a new, explicit interface
  (a frozen `GraphContext`), not a shared mutable.
- **Retry/error sprawl:** none introduced — the parser keeps the single
  `ParserCouldNotExtractError` path; edge rejections are logged, not raised
  (matches §4 "log, don't drop silently" at the parser boundary).

Structural prep is ~2 of ~10 implementation steps (≤30% budget) and is folded
into the feature steps below, not a separate task.

## Pipeline shape (parser stage only)

This unit owns exactly ONE box of the larger Apollo pipeline — the per-turn
parse. No queue, no Neo4j, no Postgres, no external service beyond the OpenAI
call (mocked in tests). Diagram of the parser stage WU-2A delivers:

```
[utterance + optional GraphContext]
  → [_is_non_trivial gate]  (length/ACK/math-regex/cheap_chat@0.6 — UNCHANGED)
  → [build_system_prompt(concept) + EXISTING-GRAPH context block]
  → [OpenAI gpt-4o  json_schema strict call]   (mock: parser_llm.OpenAI)
  → [parse strict payload: entries[] + edges[]]
  → [_entry_to_node per entry → typed Nodes]   (UNCHANGED node path)
  → [_resolve_typed_edges: from_ref/to_ref → node_ids, provenance,
      EDGE_ALLOWED_PAIRS pre-check, Edge(...) construct]   (NEW)
  → [if graph_context is None AND model emitted no edges:
      _resolve_uses_edges + _build_precedes_chain]   (FALLBACK = today)
  → (list[Node], list[Edge])
```

Per box — owner, behavior, failure mode:

| Box | Owner | Behavior | Failure mode |
|---|---|---|---|
| triviality gate | `_is_non_trivial` (unchanged) | short-circuits trivial input | classifier soft-fails to trivial (unchanged) |
| prompt build | `build_system_prompt` + new `_render_graph_context` | injects EXISTING-GRAPH block only when `graph_context` non-empty | empty/None context → "(empty)" block, identical to no-context |
| LLM call | `parser_llm.parse_utterance` (`OpenAI()` direct) | one `json_schema` strict call, temp 0.0 | `json.JSONDecodeError` → same `_is_non_trivial`-gated raise-or-empty as today |
| node build | `_entry_to_node` (unchanged) | typed Node + parser_confidence | malformed entry skipped (unchanged) |
| edge resolve | `_resolve_typed_edges` (NEW) | resolve refs, tag provenance, pair-check, construct `Edge` | bad pair / unresolved ref / self-loop → edge dropped + logged with reason (never raises) |
| no-context fallback | `_resolve_uses_edges` + `_build_precedes_chain` | preserves today's deterministic edges | unchanged |

**Idempotency note (not a pipeline-of-record, but stated for the contract):**
`parse_utterance` is a pure function of `(utterance, concept, attempt_id,
graph_context, model)` plus the (mocked) LLM response — no DB write, no
mutation of inputs. `graph_context` is a frozen dataclass; node/edge
construction returns NEW objects (immutable style). Re-invocation with the
same inputs + same canned LLM response yields structurally-equal output.
There is no persistence in this unit, so there is no de-dup/ON-CONFLICT
concern here — cross-turn de-dup is WU-2B.

## Public signatures (backward-compat contract)

**`parse_utterance` — additive, backward-compatible.** Existing keyword args
keep their names, order, and defaults; the new param is keyword-only with a
`None` default so every current caller (`chat.py`, existing tests) is
unaffected:

```python
# apollo/parser/parser_llm.py
def parse_utterance(
    utterance: str,
    *,
    concept: ConceptDefinition,
    attempt_id: int,
    graph_context: "GraphContext | None" = None,   # NEW — keyword-only, default None
    model: str | None = None,
) -> tuple[list[Node], list[Edge]]:
    ...
```

**`GraphContext` — new frozen dataclass** (new module
`apollo/parser/graph_context.py`, small + focused). Carries the minimal
prior-attempt graph the LLM needs to link across turns: a stable id, node
type, and a short label per existing node. It is what WU-2B will build from
the live attempt graph and thread in; in WU-2A it is exercised only by tests.

```python
# apollo/parser/graph_context.py
from __future__ import annotations
from dataclasses import dataclass, field
from apollo.ontology import NodeType

@dataclass(frozen=True)
class ContextNode:
    node_id: str          # the existing node's stable id (e.g. "stu_ab12cd34ef56")
    node_type: NodeType   # so the parser can type-check cross-turn edge endpoints
    label: str            # short human label for the prompt's EXISTING GRAPH block

@dataclass(frozen=True)
class GraphContext:
    nodes: tuple[ContextNode, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return len(self.nodes) == 0

    def type_of(self, node_id: str) -> NodeType | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n.node_type
        return None
```

(Frozen + tuple, not list — immutable style per coding rules; `type_of` lets
`_resolve_typed_edges` enforce `EDGE_ALLOWED_PAIRS` for cross-turn endpoints
whose type isn't in the current response.)

**`Edge.provenance` — new optional field on the existing model**
(`apollo/ontology/edges.py`), default `"explicit"` so every existing
construction site (the deterministic fallback, `Problem.to_kg_graph`, WU-2B's
`store`) round-trips unchanged:

```python
EdgeProvenance = Literal["explicit", "inferred"]

class Edge(BaseModel):
    ...
    provenance: EdgeProvenance = "explicit"   # NEW — default preserves all callers
```

Default `"explicit"` (not `"inferred"`) because: the deterministic fallback
edges (USES from ordinals, PRECEDES from order) are directly supported by the
student's wording/sequence, and reference-graph edges (`Problem.to_kg_graph`)
are authored ground truth — both are explicit. Only the LLM may downgrade an
edge to `inferred`.

**No signature changes to:** `build_node`, `Node`/`NodeType`, `KGStore.*`,
`compute_coverage`, `chat.py`, `done.py`. `build_system_prompt` keeps its
signature; the EXISTING-GRAPH block is rendered by a NEW private helper in
`parser_llm.py` (`_render_graph_context`) and concatenated onto the system
prompt at the call site — `build_system_prompt` stays a pure template
substitution (its `test_prompt_builder.py` guard keeps passing).

## Strict json_schema contract

New module `apollo/parser/extraction_schema.py` exposes
`build_extraction_schema() -> dict` returning the OpenAI strict
`json_schema` payload. Ported from the spike's `RESPONSE_SCHEMA`
(`scripts/spikes/rq3_edge_extraction.py:88-163`) with TWO additions: a
`provenance` field on each edge, and the schema named/wired for the
production `response_format`.

Strict-mode rules that the schema MUST honor (OpenAI structured outputs):
`strict: true`, `additionalProperties: false` on every object, and **every
property listed in `required`** — optional-by-value fields are expressed as
nullable types (`{"type": ["string", "null"]}`), exactly as the spike does
(this is why all type-specific node fields are present-and-nullable rather
than omitted).

Schema shape (abridged — full literal lives in `extraction_schema.py`):

```jsonc
{
  "name": "kg_extraction",
  "strict": true,
  "schema": {
    "type": "object", "additionalProperties": false,
    "required": ["entries", "edges"],
    "properties": {
      "entries": { "type": "array", "items": {
        "type": "object", "additionalProperties": false,
        "required": ["type","confidence","reuse_of","symbolic","label",
                     "variables","applies_when","transformation","concept",
                     "meaning","term","symbol","action","purpose"],
        "properties": {
          "type": {"enum": ["equation","condition","simplification",
                            "definition","variable_mapping","procedure_step"]},
          "confidence": {"type": "number"},
          "reuse_of": {"type": ["string","null"]},        // existing-graph id this entry refers to
          // ...all type-specific fields, each present-and-nullable (spike shape)...
        }
      }},
      "edges": { "type": "array", "items": {
        "type": "object", "additionalProperties": false,
        "required": ["edge_type","from_ref","to_ref","provenance"],   // provenance NEW
        "properties": {
          "edge_type": {"enum": ["PRECEDES","USES","DEPENDS_ON","SCOPES"]},
          "from_ref": {"type": "string"},   // "n<i>" (0-based, THIS response) OR existing-graph id
          "to_ref":   {"type": "string"},
          "provenance": {"enum": ["explicit","inferred"]}   // NEW
        }
      }}
    }
  }
}
```

**Ref convention (from the spike, kept verbatim):** `from_ref`/`to_ref` are
either `"n<i>"` = the i-th entry of THIS response (0-based) or an
existing-graph node id from the EXISTING GRAPH block. `_resolve_typed_edges`
maps `"n<i>"` → the node built from `entries[i]` and looks up bare ids in
`graph_context`. Refs that resolve to neither are dropped + logged.

**Response parsing:** the strict call returns a single JSON object; the
parser reads `payload["entries"]` (node path, unchanged) and `payload["edges"]`
(new edge path). If `json.loads` fails, the existing
`_is_non_trivial`-gated raise/empty behavior is preserved unchanged. If
`edges` is missing or not a list, treat as `[]` (then fallback applies when
`graph_context is None`).

## Provenance semantics (explicit vs inferred)

Per §4: "Parser edges carry a provenance tag: `explicit` (directly supported
by wording) vs `inferred` — consumed by the §6 edge weighting." WU-2A is the
PRODUCER of that tag; the §6 consumer is out of scope.

Rules the parser applies:
- The LLM assigns `provenance` per edge in the strict response (`explicit`
  when the student's wording directly states the relation — "use Bernoulli to
  find P2" → USES is explicit; `inferred` when the relation is implied by
  arrangement but not stated — a late condition the model SCOPES-links to an
  earlier equation the student never explicitly tied together).
- `_resolve_typed_edges` reads `edge["provenance"]`, coerces an
  absent/invalid value to `"explicit"` (safe default; strict schema makes
  this rare but the parser is defensive), and passes it to `Edge(...,
  provenance=...)`.
- **Deterministic fallback edges are always `explicit`** — USES from the
  student's own `uses_equation_ordinals` and PRECEDES from the order the
  student stated steps are both directly wording-supported. The fallback uses
  the `Edge` default, so no code change needed there beyond confirming the
  default is `"explicit"`.

The prompt's provenance rubric (added to the template, §"Files") mirrors the
spike's confidence rubric tone: explicit = the student said it; inferred =
the model is connecting two things the student mentioned separately but did
not explicitly relate. The parser does not second-guess the tag (no
heuristic re-classification) — that would duplicate the §6 edge weighting it
feeds.

## Edge validation against EDGE_ALLOWED_PAIRS

§6.3 endpoint rules (already encoded in `EDGE_ALLOWED_PAIRS`,
`apollo/ontology/edges.py:28-41`) — the parser enforces these BEFORE
constructing an `Edge`, so it never emits a structurally-invalid edge:

- `PRECEDES`: `procedure_step → procedure_step`
- `USES`: `procedure_step → equation`
- `SCOPES`: `condition → equation` OR `simplification → equation`
- `DEPENDS_ON`: any → any (no self-loops)

`_resolve_typed_edges` algorithm per LLM edge:
1. Resolve `from_ref` and `to_ref` to node_ids: `"n<i>"` → this-response
   node (must exist in the built `nodes` list); bare id → must exist in
   `graph_context`. Unresolvable → **drop + log** (`{event:
   "parser_edge_rejected", reason: "unresolvable_ref", ...}`).
2. Determine endpoint node types: this-response node → `node.node_type`;
   context node → `graph_context.type_of(id)`. Either missing type →
   **drop + log** (`reason: "unknown_endpoint_type"`).
3. Self-loop (`from == to`) → **drop + log** (`reason: "self_loop"`).
4. Map `edge_type` string → `EdgeType`; bad value → **drop + log**
   (`reason: "bad_edge_type"`).
5. Pair check: `(from_type, to_type) in EDGE_ALLOWED_PAIRS[edge_type]`?
   No → **drop + log** (`reason: "disallowed_pair"`, include the pair).
6. Construct `Edge(edge_type=..., from_node_id=..., to_node_id=...,
   attempt_id=attempt_id, source="parser", from_node_type=..., to_node_type=...,
   provenance=...)`. The `Edge` model validator re-checks the pair as a
   belt-and-braces guard; wrap in `try/except ValueError` and drop + log if
   it ever disagrees (it shouldn't, given step 5).

**No silent drop, no raise:** every rejected edge is logged with a reason
(§4 "dropped edges are logged with reason"); a bad edge never aborts parsing
or loses the valid nodes/edges. This is the PARSER boundary's version of the
no-silent-drop rule — `store.write_edges`'s own fix is WU-2B.

This duplicates the validator only at the **parser output** boundary (cheap,
deterministic, prevents emitting garbage). It is NOT the §6.3
`graph_compare/validator.py` (that grades a whole graph at Done — out of
scope). Reusing `EDGE_ALLOWED_PAIRS` (not re-listing pairs) keeps one source
of truth.

## Files to create / edit

All inside `ai-ta-backend/`, on branch `feat/apollo-kg-wu2a-typed-edge-extraction`
(already checked out — do NOT create/switch branches).

**Create:**
- `apollo/parser/graph_context.py` — `ContextNode` + `GraphContext` frozen
  dataclasses (signatures above). ~30 lines.
- `apollo/parser/extraction_schema.py` — `build_extraction_schema() -> dict`
  (strict json_schema, ported from spike + `provenance`). ~90 lines (data).
- `apollo/parser/tests/test_typed_edge_extraction.py` — the new parser-only
  test module (full list below). NOT skip-marked.

**Edit:**
- `apollo/ontology/edges.py` — add `EdgeProvenance` literal +
  `provenance: EdgeProvenance = "explicit"` field on `Edge`. Export
  `EdgeProvenance` from `apollo/ontology/__init__.py`.
- `apollo/parser/parser_llm.py` — switch the main call to
  `response_format={"type":"json_schema","json_schema":
  build_extraction_schema()}`; add `graph_context` param; add
  `_render_graph_context` (EXISTING-GRAPH block) and `_resolve_typed_edges`;
  keep `_resolve_uses_edges`/`_build_precedes_chain` as the no-context
  fallback; concatenate context block onto the system prompt at the call site.
- `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/parser_prompt_template.md`
  — add the edge-vocabulary section, the EXISTING-GRAPH usage rules, the
  `edges` output-shape description, and the provenance rubric (ported from
  the spike prompt). Keep `{{concept_name}}` and the existing node/confidence
  sections so `test_prompt_builder.py` still passes.
- `apollo/parser/prompt_builder.py` — **only if** the executor chooses to
  render the context block here instead of in `parser_llm.py`. Preferred: keep
  `build_system_prompt` pure (context block built in `parser_llm.py`), so
  this file may stay untouched. Listed as in-scope per the unit's allowed
  files; edit only if needed.
- `docs/architecture/apollo.md` — owner-doc reconciliation (see below);
  bump `last_verified` to 2026-06-15.

**Do NOT touch (WU-2B / out of scope):** `apollo/handlers/chat.py`,
`apollo/knowledge_graph/store.py`, `apollo/handlers/done.py`,
`scripts/spikes/*` (reference only), any Neo4j/Postgres code, any migration.

## TDD-ordered implementation steps

RED first, then GREEN, then refactor. Every step writes/extends REAL tests
before the implementation that satisfies them. The LLM is mocked
deterministically (`@patch("apollo.parser.parser_llm.OpenAI")` for the strict
call; `@patch("apollo.parser.parser_llm.cheap_chat")` for triviality). No
Neo4j, no network, no live API. No skip/xfail/assert-nothing.

- [ ] **Step 1 — RED: provenance on Edge.**
  - File: `apollo/parser/tests/test_typed_edge_extraction.py` (new).
  - Write `test_edge_provenance_defaults_explicit` and
    `test_edge_provenance_accepts_inferred` against `Edge(...)` directly.
  - Run: tests fail (`Edge` has no `provenance`).
- [ ] **Step 2 — GREEN: add provenance field.**
  - File: `apollo/ontology/edges.py` (+ `ontology/__init__.py` export).
  - Add `EdgeProvenance = Literal["explicit","inferred"]` and
    `provenance: EdgeProvenance = "explicit"`. Verify Step 1 passes and the
    existing `_resolve_uses_edges`/`_build_precedes_chain` + `Problem.to_kg_graph`
    still construct edges (default applies).
  - Verify: `pytest apollo/parser/tests/test_typed_edge_extraction.py -q` green for steps 1-2.
- [ ] **Step 3 — RED: GraphContext.**
  - Tests: `test_graph_context_is_empty`, `test_graph_context_type_of`,
    `test_graph_context_is_frozen` (assert `FrozenInstanceError` on mutation).
  - Run: fail (no module).
- [ ] **Step 4 — GREEN: GraphContext module.**
  - File: `apollo/parser/graph_context.py`. Implement frozen dataclasses.
  - Verify: step-3 tests pass.
- [ ] **Step 5 — RED: strict schema builder.**
  - Tests: `test_extraction_schema_is_strict` (asserts `strict: true`,
    `additionalProperties: false` on objects, every property in `required`,
    `provenance` enum present on edges, four edge_type enum values).
  - Run: fail (no module).
- [ ] **Step 6 — GREEN: extraction_schema.**
  - File: `apollo/parser/extraction_schema.py`. Port spike schema + provenance.
  - Verify: step-5 tests pass.
- [ ] **Step 7 — RED: one-call node+edge extraction (no context).**
  - Tests (mock `OpenAI` to return a canned strict payload with `entries` +
    `edges`): `test_parse_emits_typed_edges_from_strict_payload`,
    `test_parse_backward_compat_no_graph_context` (call with NO
    `graph_context`; assert nodes+edges returned and the call used
    `response_format` type `json_schema`).
  - Run: fail (parser still json_object, ignores `edges`).
- [ ] **Step 8 — GREEN: switch parser to strict call + edge path.**
  - File: `apollo/parser/parser_llm.py`. Add `graph_context` param; build the
    schema via `build_extraction_schema()`; parse `payload["edges"]`; add
    `_resolve_typed_edges`. Keep fallback for `graph_context is None` AND no
    model edges. Refactor schema literal into the new module (structural prep).
  - Verify: steps 7 + all earlier pass; existing `test_parser_confidence.py`
    + `test_triviality.py` STILL pass (run the whole `apollo/parser/tests/`).
- [ ] **Step 9 — RED: all four edge types + cross-turn + provenance + rejection.**
  - Tests for SCOPES condition→equation, DEPENDS_ON, cross-turn linking via
    injected `GraphContext`, explicit-vs-inferred tagging, invalid-edge
    rejection against `EDGE_ALLOWED_PAIRS`, unresolvable-ref drop, self-loop
    drop (full list below).
  - Run: the rejection/cross-turn ones fail until `_resolve_typed_edges` is complete.
- [ ] **Step 10 — GREEN: complete `_resolve_typed_edges` (ref resolution,
      type lookup via `graph_context.type_of`, pair pre-check, logging).**
  - Verify: step-9 tests pass; assert rejection log lines via `caplog`.
- [ ] **Step 11 — RED+GREEN: prompt template + context rendering.**
  - File: template + `_render_graph_context`. Test
    `test_prompt_includes_edge_vocabulary_and_existing_graph` (build prompt,
    assert edge types + "EXISTING GRAPH" rules present) and
    `test_render_graph_context_empty_vs_populated`. Ensure
    `test_prompt_builder.py` guard still passes (no unresolved slots, typing
    instructions remain).
- [ ] **Step 12 — Triviality + ParserCouldNotExtractError unchanged.**
  - Tests `test_triviality_short_circuit_unchanged`,
    `test_raises_on_nontrivial_zero_nodes_with_edges_payload`. Confirm the
    short-circuit and the no-fallback raise still behave exactly as before
    (run `test_triviality.py` unchanged — must stay green).
- [ ] **Step 13 — Refactor + coverage gate.**
  - Ensure functions <50 lines, file <400 lines (structural-prep verify cmd).
  - Run: `pytest apollo/ -q` (parser tests green; skip-marked modules stay
    skipped) then the patch-coverage gate (see Verification).
- [ ] **Step 14 — Owner-doc reconciliation.**
  - Edit `docs/architecture/apollo.md`; bump `last_verified: 2026-06-15`.

## Full test list

All in `apollo/parser/tests/test_typed_edge_extraction.py` unless noted. Mock
discipline (copied from `test_triviality.py` / `test_parser_confidence.py`):
- module-scoped `concept = load_concept("fluid_mechanics", "bernoulli_principle")`;
- `@patch("apollo.parser.parser_llm.OpenAI")` returns a `MagicMock` whose
  `.chat.completions.create.return_value` is a fake response whose
  `choices[0].message.content` is a canned JSON string with `entries` +
  `edges`;
- `@patch("apollo.parser.parser_llm.cheap_chat")` only where triviality is
  exercised on math-free prose;
- a local `_mock_strict_response(entries, edges)` helper (extends the existing
  `_mock_openai_response` to include the `edges` key);
- `attempt_id` positive (parser-only — no Neo4j; the negative-id convention is
  a Neo4j-cleanup contract that does not apply here);
- NO live API, NO Neo4j, NO `scripts/spikes` import.

**A. Edge model — provenance**
- `test_edge_provenance_defaults_explicit` — `Edge(USES, proc→eq, ...)` with
  no `provenance` arg → `.provenance == "explicit"`. Mock: none (pure model).
- `test_edge_provenance_accepts_inferred` — constructing with
  `provenance="inferred"` round-trips. Mock: none.

**B. GraphContext**
- `test_graph_context_is_empty` — empty `GraphContext().is_empty()` True;
  with one `ContextNode` → False. Mock: none.
- `test_graph_context_type_of` — `type_of(known_id)` returns the node_type;
  `type_of(unknown)` returns None. Mock: none.
- `test_graph_context_is_frozen` — assigning to `.nodes` raises
  `dataclasses.FrozenInstanceError` (immutability guard). Mock: none.

**C. Strict schema**
- `test_extraction_schema_is_strict` — `build_extraction_schema()` has
  `["strict"] is True`; entries+edges item objects have
  `additionalProperties False`; every property key is in that object's
  `required`; edge `provenance` is an enum `["explicit","inferred"]`;
  `edge_type` enum is exactly the four `EdgeType` values. Mock: none.

**D. One-call extraction, backward-compatible (no graph_context)**
- `test_parse_backward_compat_no_graph_context` — call `parse_utterance(...,
  concept=concept, attempt_id=1)` with NO `graph_context`; canned payload has
  one equation + one procedure_step + a USES edge. Asserts: returns
  `(nodes, edges)`; the create call was made with
  `response_format["type"] == "json_schema"`; node path identical to today.
  Mock: `OpenAI`.
- `test_parse_emits_typed_edges_from_strict_payload` — payload has explicit
  `edges`; asserts the returned edges match (type, endpoints by resolved
  node_id, provenance). Mock: `OpenAI`.
- `test_parse_no_model_edges_falls_back_to_deterministic` — payload has two
  procedure_steps + one equation but EMPTY `edges`, `graph_context=None`;
  asserts the deterministic fallback still produces the within-turn
  USES+PRECEDES edges (today's behavior preserved), all `provenance ==
  "explicit"`. Mock: `OpenAI`.

**E. All four edge types**
- `test_parse_emits_precedes_step_to_step` — two procedure_steps + PRECEDES
  edge in payload; valid. Mock: `OpenAI`.
- `test_parse_emits_uses_step_to_equation` — proc_step + equation + USES;
  valid. Mock: `OpenAI`.
- `test_parse_emits_scopes_condition_to_equation` — condition + equation +
  SCOPES; asserts a SCOPES edge is returned (proves SCOPES comes alive, §4 —
  dead code today). Mock: `OpenAI`.
- `test_parse_emits_scopes_simplification_to_equation` — simplification +
  equation + SCOPES (the other allowed SCOPES source). Mock: `OpenAI`.
- `test_parse_emits_depends_on_any_to_any` — e.g. equation DEPENDS_ON
  definition; asserts DEPENDS_ON edge returned. Mock: `OpenAI`.

**F. Cross-turn linking via injected graph_context**
- `test_parse_links_to_existing_graph_node` — `graph_context` carries an
  equation node `eq_prev` (type `equation`); current payload has a condition
  entry + a SCOPES edge `from_ref="n0"` (the condition) `to_ref="eq_prev"`;
  asserts the returned SCOPES edge's `to_node_id == "eq_prev"` (cross-turn
  link works). Mock: `OpenAI`.
- `test_parse_cross_turn_late_condition_scopes_earlier_equation` — the §4
  spike scenario: a late condition SCOPES-links to an equation from an
  earlier turn supplied via `graph_context`; asserts the edge resolves and
  passes the pair check. Mock: `OpenAI`.
- `test_parse_cross_turn_endpoint_type_from_context` — endpoint type for the
  context node is taken from `graph_context.type_of`, NOT re-derived; assert a
  DEPENDS_ON edge to a context `definition` node validates. Mock: `OpenAI`.

**G. Provenance tagging**
- `test_parse_tags_explicit_edge` — payload edge has
  `provenance="explicit"`; returned `Edge.provenance == "explicit"`. Mock: `OpenAI`.
- `test_parse_tags_inferred_edge` — payload edge has
  `provenance="inferred"`; returned `Edge.provenance == "inferred"`. Mock: `OpenAI`.
- `test_parse_missing_provenance_defaults_explicit` — payload edge omits
  `provenance` (defensive path); returned edge `"explicit"`. Mock: `OpenAI`.

**H. Invalid-edge rejection against EDGE_ALLOWED_PAIRS (no silent drop)**
- `test_parse_rejects_disallowed_pair` — payload has SCOPES with an
  `equation → condition` (reversed; not allowed); asserts the edge is NOT in
  the result AND a `parser_edge_rejected` log line with
  `reason="disallowed_pair"` is emitted (via `caplog`). Mock: `OpenAI`.
- `test_parse_rejects_self_loop` — edge `from_ref == to_ref`; dropped + logged
  `reason="self_loop"`; valid nodes still returned. Mock: `OpenAI`.
- `test_parse_rejects_unresolvable_ref` — edge `to_ref="n99"` (no such entry,
  no context); dropped + logged `reason="unresolvable_ref"`. Mock: `OpenAI`.
- `test_parse_rejects_unknown_endpoint_type` — `to_ref` points at a context id
  absent from `graph_context` (type unknown); dropped + logged
  `reason="unknown_endpoint_type"`. Mock: `OpenAI`.
- `test_parse_rejects_bad_edge_type` — `edge_type="RESOLVES_TO"` (not a parser
  EdgeType); dropped + logged `reason="bad_edge_type"`. Mock: `OpenAI`.
- `test_parse_one_bad_edge_does_not_drop_valid_edges` — payload has one valid
  USES + one disallowed SCOPES; asserts the valid USES survives and only the
  bad one is dropped (no all-or-nothing). Mock: `OpenAI`.

**I. Triviality + no-fallback contract UNCHANGED**
- `test_triviality_short_circuit_unchanged` — short ACK ("ok") returns
  `([], [])` and never calls `OpenAI` (assert the create mock not called).
  Mock: `OpenAI` (asserted unused).
- `test_raises_on_nontrivial_zero_nodes` — non-trivial utterance (math chars),
  payload `entries: []` → `ParserCouldNotExtractError` (existing contract,
  now under the strict schema). Mock: `OpenAI`.
- `test_returns_empty_on_trivial_zero_nodes` — trivial utterance + empty
  payload → `([], [])`, no raise. Mock: `OpenAI` + `cheap_chat` (says not
  teaching).
- `test_node_parser_confidence_still_propagates` — re-assert a node's
  `parser_confidence` flows through under the strict-schema path (guards
  against the schema change breaking the P1 confidence contract). Mock: `OpenAI`.

**J. Prompt template (in `test_typed_edge_extraction.py` or extend
`test_prompt_builder.py`)**
- `test_prompt_includes_edge_vocabulary` — `build_system_prompt(concept)`
  lowercased contains `precedes`, `uses`, `scopes`, `depends_on` and the
  endpoint-pair phrasing. Mock: none (pure template).
- `test_render_graph_context_empty_vs_populated` — `_render_graph_context(None
  or empty)` yields the "(empty)" block; a populated `GraphContext` yields one
  line per node with id + type + label. Mock: none.
- `test_prompt_builder_guard_still_passes` — re-run/keep the existing
  `test_prompt_builder.py` assertions (no unresolved `{{...}}` slots, typing
  instructions remain) — confirms the template edit didn't regress. Mock: none.

**Total: ~30 real tests.** All deterministic, all mocked, none skipped. They
cover every changed line in `edges.py`, `graph_context.py`,
`extraction_schema.py`, the new/edited functions in `parser_llm.py`, and the
template render helper → clears the 95% patch gate.

## Owner-doc updates

Edit `docs/architecture/apollo.md` (owner of `apollo/**`) in the same work;
bump `last_verified` to **2026-06-15**. Concrete changes:

1. **`apollo/parser/` row in the module map** (line ~31): update
   `parse_utterance()` description from deterministic within-turn USES/PRECEDES
   to "one strict-`json_schema` GPT-4o call emitting typed nodes AND all four
   typed edges (PRECEDES/USES/SCOPES/DEPENDS_ON) with explicit/inferred
   provenance; optional `graph_context` for cross-turn linking (default None —
   chat.py unchanged until WU-2B wires it)." Note the new
   `extraction_schema.py` + `graph_context.py` files.
2. **Key service entry points** (line ~64): update the `parse_utterance`
   signature to include `graph_context: GraphContext | None = None`.
3. **Core types → Edge types** (line ~75): add the `provenance`
   (`explicit|inferred`) field to the `Edge` description; note SCOPES and
   DEPENDS_ON are now emitted by the parser (previously "SCOPES dead /
   DEPENDS_ON reference-only").
4. **Data flow (a) step 3** (line ~85): update the parse step to "one strict
   call → typed nodes + all four typed edges (provenance-tagged), cross-turn
   when `graph_context` supplied; deterministic USES/PRECEDES remain the
   no-context fallback."
5. **Non-obvious conventions / Tests** (line ~124): note that
   `test_typed_edge_extraction.py` is a LIVE (non-skipped) parser test module
   added in WU-2A; the parser rejects invalid edges by logging
   (`parser_edge_rejected`), not silently dropping — and that
   `store.write_edges`'s own silent-drop fix + `chat.py` graph_context wiring
   remain WU-2B.
6. Add a one-line pointer to this plan + the spec §4 under the redesign
   working-docs list.

No new HTTP routes, no new tables, no migration → no
`shared-architecture/README.md` registration needed.

## Verification

- [ ] **Unit suite green:** `pytest apollo/parser/tests/ -q` — all WU-2A
      tests pass; `test_triviality.py` + `test_parser_confidence.py` +
      `test_prompt_builder.py` STILL pass unchanged; `test_parser.py` stays
      module-skipped (untouched).
- [ ] **Full apollo suite no regressions:** `pytest apollo/ -q` — no new
      failures; skip-marked modules stay skipped.
- [ ] **Backward-compat smoke:** a test calls `parse_utterance(utterance,
      concept=concept, attempt_id=1)` with NO `graph_context` and asserts the
      same `(nodes, edges)` shape `chat.py` consumes today — proves chat.py
      keeps working untouched.
- [ ] **No live calls / no Neo4j:** grep the new test module — no
      `OpenAI()` un-mocked, no `Neo4jClient`, no `scripts.spikes` import, no
      `NEO4J_URI`. (`rg -n "Neo4j|spikes|requests\.|httpx" apollo/parser/tests/test_typed_edge_extraction.py` returns nothing.)
- [ ] **Strict-schema shape check:** `test_extraction_schema_is_strict`
      asserts OpenAI strict-mode invariants so a real call would not 400.
- [ ] **Patch-coverage gate (CLAUDE.md 95%):**
      `pytest --cov --cov-report=xml` then
      `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`.
      (Compare branch per CLAUDE.md is `origin/staging`; the task's
      "diff-cover vs docs/apollo-kg-master-spec" is the same gate — use the
      repo's CI base `origin/staging`.)
- [ ] **Function/file size:** structural-prep AST command — every function
      <50 lines, `parser_llm.py` <400 lines.
- [ ] **Edge-rejection observability:** `caplog`-based tests confirm each
      rejection reason is logged (no silent drop at the parser boundary).
- [ ] **Replay/idempotency (pure-function form):** call `parse_utterance`
      twice with the same canned LLM response and assert structurally-equal
      output (no hidden state; node ids differ only by the `uuid4` fallback —
      assert edge topology + provenance equal, not the random ids).

## Risks

Confidence-rated.

- **[MEDIUM] `uuid4` node ids make `"n<i>"`→node_id resolution order-sensitive.**
  `_resolve_typed_edges` must map `"n<i>"` to the node built from
  `entries[i]` — but `_entry_to_node` SKIPS malformed entries, so the index
  of a kept node can diverge from the LLM's 0-based `"n<i>"`. Mitigation
  (binding): build a `ref→node` map keyed on the ORIGINAL entry index (the
  position in `payload["entries"]`), not the filtered `nodes` list — track
  `{original_index: node}` as entries are kept, exactly as the spike's
  `turn_ids[f"n{i}"]` does. A test (`test_parse_ref_index_survives_skipped_entry`
  — add to group H) injects a malformed entry between two good ones and
  asserts the edge ref still resolves to the right node.

- **[MEDIUM] Strict-schema 400s from a real OpenAI call** if the schema
  violates structured-outputs rules (a property missing from `required`, an
  un-nullable optional). Mitigation: `test_extraction_schema_is_strict`
  enforces the invariants offline; the schema is a verbatim port of the
  spike's already-working `RESPONSE_SCHEMA` plus the `provenance` enum. The
  executor should keep the all-fields-present-and-nullable pattern. (No live
  call in this unit, so this surfaces only at WU-2B/runtime — flagged so the
  schema is correct now.)

- **[LOW] Cost/latency drift.** The strict call replaces today's json_object
  call 1:1 (one call per turn, temp 0.0, gpt-4o); spike measured ~$0.004/turn,
  median 2.45s — within the chat loop. No new per-turn calls. Risk is only
  the slightly larger prompt (edge vocab + context). No budget regression vs
  the existing parser call.

- **[LOW] Template edit regresses `test_prompt_builder.py`.** The guard
  asserts no unresolved `{{...}}` slots and that typing instructions remain.
  Mitigation: the only slot is `{{concept_name}}` (kept); the edge/context
  additions are plain text. `test_prompt_builder_guard_still_passes` re-runs
  the guard.

- **[LOW] Provenance default direction.** Choosing `"explicit"` as the
  default could mis-tag a future caller that means `"inferred"`. Mitigation:
  only the LLM produces `inferred`; all current/known construction sites
  (fallback, reference graph) are genuinely explicit, so the default is
  correct for every existing caller and WU-2B sets it from the LLM payload.

- **[LOW] Scope creep into WU-2B.** The temptation to also fix
  `store.write_edges` or wire `chat.py` is real and explicitly forbidden.
  Mitigation: the out-of-scope section + the files-not-to-touch list; the
  parser's own pair-check/logging is the WU-2A-correct way to honor §4's
  no-silent-drop at THIS boundary.

## Out-of-scope boundaries (WU-2A)

Explicitly NOT in this unit (each is a named later work unit or phase):

- **`store.write_edges` silent-drop fix** — it still silently drops invalid
  edges; making it log-not-drop is **WU-2B** (§4). WU-2A only guarantees the
  PARSER never emits an invalid edge and logs its own rejections.
- **`chat.py` wiring of `graph_context`** — building a `GraphContext` from the
  live attempt graph and passing it into `parse_utterance` is **WU-2B**.
  WU-2A ships the optional param defaulted to `None` so chat.py is unchanged.
- **Cross-turn edge de-duplication** — detecting that a new edge duplicates an
  existing persisted edge is **WU-2B** (needs the store). WU-2A's
  `reuse_of`/context handling only avoids creating duplicate NODES within the
  prompt; it does not dedup against Neo4j.
- **The §6.3 `graph_compare/validator.py`** and the whole `apollo/graph_compare/`
  grading core — **phase 4**. WU-2A reuses `EDGE_ALLOWED_PAIRS` for a parser
  output-boundary check only.
- **Resolution / `:Canon` / RESOLVES_TO / reference-anchored matching** —
  **phase 3** (§5). No resolver here.
- **Layer-1 entities, Layer-3 learner model, the decision table, abstention
  gates, transcript audit** — phases 3/4/5.
- **§8A course→Apollo DB cutover** (`_AVAILABLE_CLUSTERS` /
  `_CLUSTER_TO_CONCEPT` / DB `load_concept`) and **§8B auto-provisioning** —
  phase 3 / 3B. The parser keeps reading the filesystem `ConceptDefinition`
  via `load_concept` exactly as today.
- **Any migration, any Neo4j write, any Postgres write, any live OpenAI call.**
- **Importing or depending on `scripts/spikes/rq3_edge_extraction.py`** —
  reference only; its prompt/schema are re-implemented as production code.
- **No new LLM provider, no LangChain, no Assistants API, no model change** —
  stays on `MAIN_MODEL` (gpt-4o) via the existing direct `OpenAI()` client.

## Deviations I'd allow the executor

- **Where the EXISTING-GRAPH block is rendered.** Preferred: a private
  `_render_graph_context` in `parser_llm.py`, concatenated onto
  `build_system_prompt(concept)` at the call site (keeps `build_system_prompt`
  pure). Acceptable alternative: render it inside `prompt_builder.py` as a
  second pure function — but do NOT change `build_system_prompt`'s signature
  or break `test_prompt_builder.py`.
- **`_resolve_typed_edges` ref-map keying.** Must key on the ORIGINAL entry
  index (risk #1), but the exact data structure (dict vs parallel list) is the
  executor's choice as long as the skipped-entry test passes.
- **Schema field ordering / exact prompt wording.** Port the spike's wording
  and rubric; minor rewording for the production template is fine provided the
  four edge types, endpoint-pair rules, EXISTING-GRAPH ref convention, and
  provenance rubric are all present and the prompt-guard tests pass.
- **Whether to add `reuse_of` handling now.** The spike uses `reuse_of` to let
  the model flag a node as a reference to an existing one. WU-2A MAY parse it
  to avoid duplicate nodes when `graph_context` is supplied, OR defer all
  node-dedup to WU-2B. If deferred, drop `reuse_of` from the node path (still
  keep it in the schema as nullable so the prompt is honest) and rely on edges
  referencing context ids directly. Either is acceptable; document the choice.
- **Test file split.** ~30 tests in one module is fine; the executor may split
  the prompt-template tests into `test_prompt_builder.py` instead. Keep all
  edge-extraction tests together.

What I would NOT allow: changing `parse_utterance`'s existing arg
names/order/defaults; making `graph_context` required; touching WU-2B files;
any un-mocked LLM/Neo4j; reviving `test_parser.py`; a model other than
`MAIN_MODEL`/gpt-4o; emitting an edge that fails the `Edge` validator.

