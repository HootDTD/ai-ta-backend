# Plan: Apollo KG WU-2B — cross-turn linking + write_edges validation/logging + chat wiring + inherited nit fixes

**Goal:** Make `write_edges` validate+log instead of silently dropping, thread the prior-attempt graph into `parse_utterance` for cross-turn linking with cross-turn node de-dup, finalize the graph_context node-id format, and fix three inherited WU-2A LOW nits — each with real TDD tests, no live Neo4j.
**Architecture (layers touched):** `apollo/knowledge_graph/store.py` (store boundary: edge validation/logging + node de-dup), `apollo/handlers/chat.py` (handler wiring), `apollo/parser/{parser_llm,edge_resolver,graph_context}.py` (parser-boundary nits + id format). Pure-function + mocked-Neo4j tests only.
**Tech stack:** Python 3.11 / FastAPI, SQLAlchemy async + asyncpg (Postgres), Neo4j async driver (Aura), Pydantic v2 ontology, pytest + pytest-asyncio. No NestJS/ORM-migration here — Layer-2 graph is Neo4j, no DB migration in this unit.

---
provides:
  - `KGStore.write_edges(...)` returning a structured `WriteEdgesResult` (written/dropped/invalid counts + per-edge reasons) instead of a bare int, validating endpoint existence + EDGE_ALLOWED_PAIRS before CREATE; structured `write_edges` log line
  - `KGStore.write_nodes(...)` cross-turn de-dup (prior-graph references reused by id, never re-minted)
  - `chat.py` threading `store.read_graph(attempt_id)` → `GraphContext` → `parse_utterance(graph_context=...)`
  - Finalized graph_context node-id format guaranteed to never match `^n\d+$` (a pure helper `build_graph_context(graph) -> GraphContext`)
consumes:
  - WU-2A parser contract: `parse_utterance(utterance, *, concept, attempt_id, model=None, graph_context=None)`, FLAT strict-json_schema entries, `edge_resolver.resolve_typed_edges`, `graph_context.GraphContext`/`ContextNode`, per-entry `reuse_of` hint
  - `apollo.ontology`: `EDGE_ALLOWED_PAIRS`, `Edge`, `EdgeType`, `Node`, `KGGraph`, `NODE_LABELS`, `build_node`
  - `apollo.persistence.neo4j_client.Neo4jClient` (async session wrapper — mocked in tests)
depends_on:
  - `feat/apollo-kg-wu2a-typed-edge-extraction` (the diff-cover compare branch; the WU-2A modules must exist)
  - No DB migration; no `plans/db.md`
---

## Overview

WU-2A made the parser emit typed, provenance-tagged edges and accept an optional `graph_context`, but the wiring stops at the parser boundary. WU-2B closes four gaps:

1. **Store-boundary edge validation (the headline).** `KGStore.write_edges` (`store.py:246`) groups edges by type and runs `MATCH (a) MATCH (b) CREATE (a)-[..]->(b)` (`_EDGE_CREATE_CYPHER`, `store.py:62`). Neo4j's `MATCH...CREATE` silently produces zero rows for any edge whose endpoints don't both exist — the docstring even admits "silently dropped". §4 of the spec requires this to STOP: validate endpoint existence + EDGE_ALLOWED_PAIRS *before* the CREATE, and emit a structured `write_edges` log (counts written/dropped/invalid + per-edge reason). This is the store-side mirror of the parser-side `parser_edge_rejected` (`edge_resolver.py:66`).

2. **Cross-turn linking, wired.** `chat.py:253` calls `parse_utterance(message, concept=..., attempt_id=...)` with NO `graph_context`, so cross-turn edges (the §4 spike's 11/21 valid edges that spanned turns; SCOPES coming alive) never happen in production. WU-2B reads the prior attempt graph via `store.read_graph(attempt_id)`, builds a `GraphContext`, and threads it in.

3. **Cross-turn node de-dup.** `write_nodes` (`store.py:211`) unconditionally `CREATE`s every node. When a turn re-asserts a prior-turn node (the parser flags it via the per-entry `reuse_of` hint, `extraction_schema.py:51`), writing it again would mint a duplicate Neo4j node and unfairly inflate the graph (double coverage). WU-2B drops parser nodes whose `reuse_of` names an existing prior-graph node id, so the edge endpoint resolves to the existing node, not a clone.

4. **graph_context node-id format finalized + 3 inherited LOW nits.** Pin the rule that context node ids never match `^n\d+$` (the `edge_resolver._resolve_ref` ordinal collision, `edge_resolver.py:49`); fix the `_resolve_uses_edges` fallback ordinal-skew (`parser_llm.py:208`); remove the unused `from typing import Any` (`parser_llm.py:28`).

The §6.3 EDGE_ALLOWED_PAIRS endpoint rules are already encoded in `apollo/ontology/edges.py:39` and reused verbatim — this unit does NOT redefine them. SCOPES (emitted by WU-2A) already round-trips through `write_nodes`/`read_graph` for nodes; this unit adds the missing *edge* validation so SCOPES edges persist correctly.

## Prior art (sibling modules)

- **Parser-boundary edge rejection (the exact pattern to mirror at the store boundary):** `apollo/parser/edge_resolver.py:66` `_reject(reason, raw, **extra)` logs `parser_edge_rejected reason=... edge=...` via `_LOG.info` and returns None; the caller drops it (`edge_resolver.py:128`). WU-2B's store log uses the SAME structured, key=value, `_LOG.info` style under the namespace `write_edges`.
- **EDGE_ALLOWED_PAIRS reuse:** `apollo/ontology/edges.py:39` (dict keyed by `EdgeType`) — both the `Edge` Pydantic validator (`edges.py:71`) and `edge_resolver._build_typed_edge` (`edge_resolver.py:104`) already consult it. The store check uses `(from_type, to_type) not in EDGE_ALLOWED_PAIRS[edge_type]`, identical idiom.
- **Offline Neo4j mocking convention (the exact harness for store tests):** `apollo/knowledge_graph/tests/test_store_negotiation.py:155-236` — `_FakeRecord`/`_FakeResult`/`_FakeNeo4jSession`/`_FakeNeo4jClient` pattern-match on cypher strings, hold a `(attempt_id, node_id) -> props` dict, and expose `async with client.session()`. WU-2B extends this fake to (a) answer the new endpoint-existence MATCH and (b) record which CREATE cyphers ran, so a test can assert validation happened BEFORE CREATE. Live tests stay in `test_store_neo4j.py` (skip if `NEO4J_URI` unset).
- **Parser-only LLM mocking convention:** `apollo/parser/tests/test_typed_edge_extraction.py:43-58` — `@patch("apollo.parser.parser_llm.OpenAI")` + `_mock_client(entries, edges)` returning a strict-json `choices[0].message.content`. WU-2B reuses `_flat_entry`/`_edge`/`_mock_client` verbatim for the nit-1 regression test.
- **Chat handler test (legacy-skipped; WU-2B writes the live replacement):** `apollo/handlers/tests/test_chat.py` is module-skipped (V2). WU-2B's chat wiring test mocks `apollo.handlers.chat.parse_utterance`, `apollo.handlers.chat.draft_reply`, and the store's `read_graph`/`write_nodes`/`write_edges` (sibling pattern from the skipped test's `@patch("apollo.handlers.chat.parse_utterance")`), asserting the threaded `graph_context` argument — no Neo4j, no LLM.
- **`apollo.conftest`:** `TEST_USER_ID`/`TEST_SPACE_ID`/`neo4j_client` fixture (`apollo/conftest.py:19,41`). Negative `attempt_id` convention for any live test.

## Structural prep (from neighborhood scan)

Neighborhood is **clean** — no prerequisite refactor.

| File | Lines | Imports (CBO proxy, >8 flags) | def/async-def (WMC proxy, >20 flags) | Fan-in | Verdict |
|---|---|---|---|---|---|
| `apollo/knowledge_graph/store.py` | 571 | 11 | 21 | 6 importers (handlers + models) | At thresholds, NOT over. <800 lines. The 21 methods are the established `KGStore` surface; WU-2B adds ~2 helpers + edits 2 methods — net stays <800. The store is a legitimate coupling hub (it IS the Neo4j boundary); splitting it is out of scope and would balloon the diff. No prep. |
| `apollo/handlers/chat.py` | 304 | 15 | 7 | 1 importer (`api.py`) | Imports 15 but each is a distinct collaborator (intent, parser, store, history, models); not a god-module. No prep. |
| `apollo/parser/parser_llm.py` | 384 | 16 | 11 | imported by `chat.py` | Within budget. nit-2 *removes* an import (16→15). No prep. |
| `apollo/parser/edge_resolver.py` | 142 | 4 | 5 | imported by `parser_llm.py` | Small, focused. No prep. |
| `apollo/parser/graph_context.py` | 38 | 3 | 2 | imported by parser + (new) store/chat | Tiny. WU-2B adds one builder helper. No prep. |

No circular imports introduced: `store.py` already imports `apollo.parser`? — **No.** Verified: `store.py` imports only `apollo.ontology`, `apollo.persistence`, `apollo.solver`, `apollo.errors`. WU-2B adds `from apollo.ontology import EDGE_ALLOWED_PAIRS` (already an ontology import) to `store.py` — no new package edge. The graph_context builder lives in `apollo/parser/graph_context.py` (pure, no store import), and `chat.py` (which already imports both `KGStore` and `parse_utterance`) calls it — so the new dependency edge is `chat → graph_context`, which already exists transitively via `parser_llm → graph_context`. No cycle.

Budget check: structural prep = 0 steps of ~30 plan steps (0%). Well under the 30% cap.

## Layered tasks (ORDER MATTERS — TDD: tests RED first, then implementation GREEN)

For every task below: **write the listed tests first, watch them fail, then implement.** No skip-marks, no xfail. The full test catalogue (names + asserts + mocking) is in §8.

### 1. DB migration

**None — justified.** Layer-2 evidence graphs live in Neo4j, which is schemaless; `write_edges`/`write_nodes` operate on existing labels/edge-types (`apollo/ontology/edges.py`, `nodes.py`). No Postgres table or column changes in this unit. The `:Canon`/`RESOLVES_TO`/`apollo_kg_entities` schema work is WU-3C (anti-scope, §11). This satisfies the layered-order rule by explicit absence.

### 2. graph_context node-id format (pure) — finalize + add builder

**File (edit):** `apollo/parser/graph_context.py`

The format is *already* collision-safe: WU-2A node ids are `stu_<uuid12>` (`parser_llm.py:330`) and the reference/test ids are `eq_prev`/`t0_n0`/`def_prev` — none match `^n\d+$`. WU-2B makes the invariant **explicit and enforced** rather than incidental, and adds the builder that `chat.py` needs.

- [ ] Add a module-level guard regex + validator:
  ```python
  import re
  _ORDINAL_REF = re.compile(r"^n\d+$")   # the parser's this-response ordinal shape

  def is_safe_context_id(node_id: str) -> bool:
      """A context node id is safe iff it can never be mistaken for a
      `^n\d+$` this-response ordinal in edge_resolver._resolve_ref."""
      return bool(node_id) and _ORDINAL_REF.match(node_id) is None
  ```
- [ ] Add the builder (pure; lives here, NOT in store, so it is unit-testable with no Neo4j and importable by `chat.py` without a store→parser dependency surprise):
  ```python
  from apollo.ontology import KGGraph

  def build_graph_context(graph: KGGraph) -> GraphContext:
      """Project a read KGGraph into the minimal prior-attempt context the
      parser needs. Skips any node whose id is NOT context-safe (would
      collide with a `^n\d+$` ordinal) — logged, never silently kept.
      Label is the node's display label where present, else its node_type."""
  ```
  Label derivation (deterministic, no LLM): `equation` → `content.label or content.symbolic`; `condition`/`simplification` → `content.applies_when`; `definition` → `content.concept`; `variable_mapping` → `content.term`; `procedure_step` → `content.action`. Truncate to 60 chars to mirror `_render_graph_context` (`parser_llm.py:286`). Construct `ContextNode(node_id=n.node_id, node_type=n.node_type, label=...)`.
- [ ] Immutability: returns a NEW `GraphContext(nodes=tuple(...))`; never mutates `graph`.
- Verify: `pytest apollo/parser/tests/test_graph_context_builder.py -q` (new file).

### 3. edge_resolver precedence pin (inherited nit 3)

**File (edit):** `apollo/parser/edge_resolver.py` — `_resolve_ref` (line 35).

The current precedence is CORRECT but UNPINNED: `_resolve_ref` checks `ref.startswith("n") and ref[1:].isdigit()` FIRST (this-response ordinal), then falls to graph_context. Because §2 guarantees context ids never match `^n\d+$`, the two namespaces are disjoint and the ordinal-first rule is safe. WU-2B pins this with a test and a clarifying comment; **no logic change** unless the test surfaces one.

- [ ] Tighten the comment at `edge_resolver.py:49` to state the invariant: "`^n\d+$` is the reserved this-response ordinal namespace; `build_graph_context` guarantees context ids never collide (see graph_context.is_safe_context_id), so ordinal-first resolution is unambiguous."
- [ ] (No behavioral edit expected.) If the pin test reveals a real collision path, the minimal fix is to gate the ordinal branch on `int(ref[1:]) in index_to_node` before claiming it — but only if RED. Document the decision in the test.
- Verify: `pytest apollo/parser/tests/test_edge_resolver_precedence.py -q` (new file).

### 4. parser_llm inherited nits (nit 1 fallback skew, nit 2 unused import)

**File (edit):** `apollo/parser/parser_llm.py`

**nit 2 (trivial, do first):** remove `from typing import Any` (line 28) — grep-confirm `Any` is unused in the module after WU-2A. (If `Any` *is* still referenced, this nit is void; note it in the test/PR. Recon says it is unused.)

**nit 1 (the real fix):** `_resolve_uses_edges` (line 208) zips `raw_entries` with `nodes` and indexes `nodes[o]` by the LLM ordinal `o`:
```python
for raw, node in zip(raw_entries, nodes):   # <- skew source
    ...
    target = nodes[o]                        # <- o is numbered against ORIGINAL entries
```
But `nodes` is the COMPACTED list (malformed entries dropped by `_build_nodes`), while `o` (`uses_equation_ordinals`) is numbered against the LLM's ORIGINAL entry list. A malformed entry wedged *before* an equation shifts every subsequent compacted index, so `nodes[o]` points at the wrong target. The typed path already solved this with `index_to_node` (ORIGINAL-index → node, `parser_llm.py:325`). Apply the same map here.

- [ ] Change the deterministic fallback to use `index_to_node`. Concretely, thread `index_to_node` (already built by `_build_nodes`, `parser_llm.py:368`) into `_resolve_uses_edges` and resolve both the step and the target through it:
  - New signature: `_resolve_uses_edges(raw_entries: list[dict], index_to_node: dict[int, Node], *, attempt_id: int) -> list[Edge]`.
  - Iterate `for i, raw in enumerate(raw_entries)`; `node = index_to_node.get(i)`; skip if `node is None` or `node.node_type != "procedure_step"`.
  - For each ordinal `o`: `target = index_to_node.get(o)`; skip if `target is None or target.node_type != "equation"`.
  - This makes the fallback robust to skipped entries exactly like the typed path.
- [ ] Update the single caller (`parser_llm.py:381`): `_resolve_uses_edges(kept_raw, index_to_node, attempt_id=...)` — note it must now receive `index_to_node`, and `raw_entries` should be the ORIGINAL `_as_list(payload.get("entries"))` (not the compacted `kept_raw`) so the `enumerate(i)` matches the original index. **Binding detail:** pass the ORIGINAL entries list and `index_to_node`; do NOT pass `kept_raw` (compacted) — that would re-introduce skew. `_build_nodes` must therefore return (or the caller must retain) the original entries; it already has them as its input argument, so the caller passes `_as_list(payload.get("entries"))`.
- [ ] `_build_precedes_chain` (line 246) is NOT affected — it operates on the already-built `nodes` list in order, and PRECEDES is purely positional among procedure steps; ordinal skew does not apply.
- Verify: `pytest apollo/parser/tests/test_typed_edge_extraction.py -q` (existing suite stays green) + the new skew regression test.

### 5. store.write_edges — validation + structured logging (NO silent drop)

**File (edit):** `apollo/knowledge_graph/store.py`

Introduce a small frozen result type and rewrite `write_edges` to validate before CREATE.

- [ ] Add a frozen result dataclass (top of module, after imports):
  ```python
  from dataclasses import dataclass, field

  @dataclass(frozen=True)
  class WriteEdgesResult:
      written: int = 0
      dropped: int = 0          # endpoint(s) absent in the subgraph
      invalid: int = 0          # EDGE_ALLOWED_PAIRS / type violation
      reasons: tuple[tuple[str, str], ...] = ()  # (edge_repr, reason) per rejected edge
      def __int__(self) -> int:  # back-compat: callers reading the old int still work
          return self.written
  ```
  `__int__` keeps the public contract loose for any caller that did `int(result)`; the sole non-test caller (`chat.py`) does NOT use the return value today, so this is belt-and-braces.
- [ ] New private helper `async def _existing_node_ids(self, s, attempt_id, ids) -> set[str]` issuing ONE scoped read:
  ```cypher
  MATCH (n:_KGNode {attempt_id: $aid}) WHERE n.node_id IN $ids RETURN n.node_id AS id
  ```
  returning the subset that exists. This is the endpoint-existence check that replaces relying on `MATCH...CREATE` silent-drop.
- [ ] Rewrite `write_edges` body (keep the freeze pre-check at lines 259-262 unchanged):
  1. Collect all endpoint ids across `edges`; one `_existing_node_ids` call → `present: set[str]`.
  2. For each edge, in order, classify (pure, before any CREATE):
     - endpoint missing (`from_node_id not in present or to_node_id not in present`) → `dropped += 1`, append reason `"endpoint_absent"`, log, skip.
     - `(from_node_type, to_node_type) not in EDGE_ALLOWED_PAIRS[edge_type]` → `invalid += 1`, reason `"disallowed_pair"`, log, skip. (Endpoint types come off the typed `Edge` — `from_node_type`/`to_node_type`; if either is None, treat as `invalid`/`"unknown_endpoint_type"` rather than trusting the CREATE.)
  3. Group only the SURVIVING edges by type; run the existing `_EDGE_CREATE_CYPHER[et]` CREATE; sum `written` from the `count(e)` return.
  4. Emit ONE structured summary log line BEFORE returning:
     ```python
     _LOG.info(
         "write_edges attempt_id=%s written=%s dropped=%s invalid=%s reasons=%r",
         attempt_id, written, dropped, invalid, reasons,
     )
     ```
     Per-rejected-edge lines use the same `parser_edge_rejected`-style key=value shape but namespaced `write_edge_rejected reason=... edge=...` so log greps stay symmetric with the parser boundary.
  5. Return `WriteEdgesResult(written, dropped, invalid, tuple(reasons))`.
- [ ] Update the `write_edges` docstring: remove the "silently dropped by Neo4j's MATCH...CREATE" sentence; state the new validate-then-create + structured-log contract and that endpoints are checked in one scoped read.
- [ ] Immutability: build `reasons` as a local list, freeze into a tuple on return; never mutate the input `edges`.
- Verify: `pytest apollo/knowledge_graph/tests/test_store_write_edges.py -q` (new file, mocked Neo4j).

### 6. store.write_nodes — cross-turn de-dup

**File (edit):** `apollo/knowledge_graph/store.py` — `write_nodes` (line 211).

The parser flags a re-asserted prior node via the per-entry `reuse_of` hint, but that hint is consumed at the PARSER boundary, not the store. By the time nodes reach `write_nodes` they are typed `Node`s with fresh `stu_<uuid>` ids. The clean, store-local de-dup signal is: **does a node with this id already exist in the subgraph?** Cross-turn references arriving via `graph_context` keep the PRIOR id (the parser emits an edge to `eq_prev`, it does not re-mint the node), so the duplication risk is specifically *new* parser nodes that restate an existing one.

Decision (binding): de-dup is **id-based against the existing subgraph**, applied in `write_nodes`:
- [ ] Before the CREATE loop, fetch existing ids once: `present = await self._existing_node_ids(s, attempt_id, [n.node_id for n in nodes])` (reuse the helper from §5).
- [ ] Partition `nodes` into `to_create = [n for n in nodes if n.node_id not in present]` and `skipped = [... in present]`. CREATE only `to_create`.
- [ ] Log `write_nodes attempt_id=%s created=%s reused=%s` (counts), so the de-dup is observable and matches the §5 logging style.
- [ ] Return value stays `int` = number of nodes CREATED (the `kg_entries_added` contract `chat.py:258` surfaces). Reused nodes are NOT counted as added (they already existed) — this is the "fair coverage" requirement: a re-assertion must not inflate the added count.

**Name-based de-dup (the prep doc's "by id/name"):** id-based covers the cross-turn-via-context case (same id reused). For the *name* case — a NEW node id whose content duplicates a prior node — the authoritative signal is the parser's `reuse_of` hint, which belongs to resolution (§5 of the spec, WU-3C territory) not the store. **Binding scope call:** WU-2B does id-based store de-dup only; content/name canonicalization is explicitly deferred to the §5 resolver (out-of-scope, see §"Out-of-scope"). This is flagged as a risk so the orchestrator can confirm.

- Verify: `pytest apollo/knowledge_graph/tests/test_store_write_nodes_dedup.py -q` (new file, mocked Neo4j).

### 7. chat.py — thread graph_context from prior attempt graph

**File (edit):** `apollo/handlers/chat.py` — normal teaching path (lines 252-265).

- [ ] Read the prior attempt graph and build the context BEFORE parsing:
  ```python
  from apollo.parser.graph_context import build_graph_context
  ...
  prior_graph = await store.read_graph(attempt_id=current_attempt.id)
  graph_context = build_graph_context(prior_graph)
  nodes, edges = parse_utterance(
      message,
      concept=concept,
      attempt_id=current_attempt.id,
      graph_context=graph_context,
  )
  ```
  Note: `read_graph` returns the CURRENT subgraph (everything taught so far this attempt) — that IS the prior-turns graph at parse time, since the new turn's nodes are not written until after parsing. No extra "freeze" needed.
- [ ] `write_nodes`/`write_edges` calls stay in the same order (nodes then edges, `chat.py:258-265`); §5/§6 make them validate+log internally. Capture `write_edges`' result is optional — `chat.py` may keep ignoring it (the structured log is the observable). Do NOT change the response envelope (`test_chat_no_signals.py` guards `"sufficiency"`/`"misconception"`/`"olm_invite"` absence and `problem_text=problem.problem_text`).
- [ ] The later `student_graph = await store.read_graph(...)` at line 275 (for the response `kg` dump) is now a SECOND read after the write; keep it — it reflects the post-write graph the FE renders. (Two reads per turn is acceptable; the first is pre-parse context, the second is post-write render. Noted as a minor latency risk.)
- [ ] Cross-turn de-dup correctness: because `write_nodes` (§6) skips ids already present, and the parser emits edges to prior ids via `graph_context`, a re-assertion links to the existing node instead of cloning. Verified by the chat wiring test asserting `write_nodes` was NOT asked to create a node with a prior id.
- Verify: `pytest apollo/handlers/tests/test_chat_crossturn.py -q` (new file; mocks `parse_utterance`, `draft_reply`, and store methods — no Neo4j, no LLM) + `pytest apollo/handlers/tests/test_chat_no_signals.py -q` (unchanged guard stays green).

### 8. Tests (full list, consolidated)

See the **per-test catalogue** in the dedicated "Tests" section below (name + assert + mocking for each). All tests are pure-function or mocked-Neo4j/LLM; the >=95% patch coverage comes entirely from them, never from skipped live-Neo4j tests.

## Tests (full catalogue — write these FIRST, RED before GREEN)

All deterministic. Mocking: pure functions need none; Neo4j is the `test_store_negotiation.py` fake (extended); the LLM is `@patch("apollo.parser.parser_llm.OpenAI")` / `@patch("apollo.handlers.chat.parse_utterance")`. Any live-Neo4j test SKIPS without `NEO4J_URI` and uses NEGATIVE `attempt_id` — those do NOT count toward the 95% patch gate.

### A. `apollo/parser/tests/test_graph_context_builder.py` (pure, no mocks)

| Test | Asserts | Mocking |
|---|---|---|
| `test_is_safe_context_id_rejects_ordinal_shape` | `is_safe_context_id("n0") is False`, `is_safe_context_id("n12") is False`, `is_safe_context_id("stu_ab12cd34ef56") is True`, `is_safe_context_id("eq_prev") is True`, `is_safe_context_id("") is False` | none |
| `test_build_graph_context_projects_all_node_types` | Build a `KGGraph` with one node of each of the 6 types (via `build_node`); resulting `GraphContext` has 6 `ContextNode`s with matching `node_id`/`node_type` and a non-empty `label` derived per type (e.g. equation label/symbolic, condition applies_when, definition concept) | none |
| `test_build_graph_context_truncates_label_to_60` | A node with a 200-char `applies_when` yields a `label` of length ≤60 | none |
| `test_build_graph_context_skips_unsafe_ids` | A `KGGraph` node whose id is `"n5"` (synthetic, would collide) is EXCLUDED from the context; a sibling `stu_...` node is kept; `caplog` shows it was logged-skipped | none (caplog) |
| `test_build_graph_context_empty_graph_is_empty` | `build_graph_context(KGGraph()).is_empty() is True` | none |
| `test_build_graph_context_does_not_mutate_input` | input `KGGraph.nodes` list identity/length unchanged after call (immutability) | none |

### B. `apollo/parser/tests/test_edge_resolver_precedence.py` (pure)

| Test | Asserts | Mocking |
|---|---|---|
| `test_ordinal_ref_resolves_to_this_response_node` | `_resolve_ref("n1", index_to_node={1: eq_node}, graph_context=ctx_with_other_ids)` returns `(eq_node.node_id, "equation")` — ordinal wins, context not consulted for `^n\d+$` | none |
| `test_context_id_resolves_when_not_ordinal_shaped` | `_resolve_ref("eq_prev", index_to_node={}, graph_context=ctx{eq_prev:equation})` returns `("eq_prev", "equation")` | none |
| `test_safe_context_id_never_collides_with_ordinal` | For every `ContextNode` produced by `build_graph_context` over a real graph, `is_safe_context_id(n.node_id)` holds — pins the cross-module invariant | none |
| `test_ordinal_miss_returns_none_not_context` | `_resolve_ref("n9", index_to_node={0: n}, graph_context=ctx)` returns `(None, None)` (an out-of-range ordinal does NOT fall through to context) — locks current precedence | none |

### C. `apollo/parser/tests/test_typed_edge_extraction.py` (EXTEND existing — nit 1)

| Test | Asserts | Mocking |
|---|---|---|
| `test_uses_fallback_survives_malformed_entry_before_equation` | Entries = `[step(uses_equation_ordinals=[2]), malformed_equation(missing symbolic → skipped), good_equation]`, `edges=[]`, no graph_context → the deterministic USES fallback links the step to the GOOD equation (original index 2), NOT to a skewed compacted target. Assert exactly one USES edge whose `to_node_id == ` the equation node's id and `from_node_type=="procedure_step"`, `to_node_type=="equation"`. This is the nit-1 regression. | `@patch(...OpenAI)` + `_mock_client` |
| `test_uses_fallback_no_malformed_entry_unchanged` | Control: entries `[step(uses_equation_ordinals=[1]), equation]` still produce the correct single USES edge (no regression for the happy path) | `@patch(...OpenAI)` |
| `test_typing_any_import_removed` (in a small `apollo/parser/tests/test_parser_imports.py`) | `import apollo.parser.parser_llm as m; assert "Any" not in m.__dict__` OR parse the source and assert `from typing import Any` absent — nit-2 regression | none (introspection) |

(The existing 50+ tests in `test_typed_edge_extraction.py` MUST stay green — the nit-1 signature change to `_resolve_uses_edges` is internal; `test_parse_no_model_edges_falls_back_to_deterministic` already exercises the fallback and must still pass.)

### D. `apollo/knowledge_graph/tests/test_store_write_edges.py` (mocked Neo4j — the headline)

Extend the `test_store_negotiation.py` fake so the session also answers the endpoint-existence read (`MATCH (n:_KGNode {attempt_id})... WHERE n.node_id IN $ids`) from its node store, and RECORDS every cypher run (in order) so a test can assert validation preceded CREATE.

| Test | Asserts | Mocking |
|---|---|---|
| `test_write_edges_validates_before_create` | Seed nodes `step(s0)`, `equation(e0)`. Write one valid USES `s0→e0`. The fake records the existence-MATCH cypher BEFORE any `CREATE`; result `.written==1`, `.dropped==0`, `.invalid==0` | fake Neo4j |
| `test_write_edges_drops_edge_with_absent_endpoint_and_logs` | Write USES `s0→MISSING`; result `.written==0`, `.dropped==1`, reason contains `endpoint_absent`; NO CREATE cypher ran; `caplog` has `write_edge_rejected` + `endpoint_absent` + the summary `write_edges ... dropped=1` | fake Neo4j + caplog |
| `test_write_edges_rejects_disallowed_pair_and_logs` | Seed `equation(e0)`, `condition(c0)`; write SCOPES `e0→c0` (reversed, disallowed). result `.invalid==1`, reason `disallowed_pair`; no CREATE for that edge; caplog shows it | fake + caplog |
| `test_write_edges_unknown_endpoint_type_is_invalid` | Build an `Edge` with `from_node_type=None`; write it → `.invalid==1`, reason `unknown_endpoint_type`, no CREATE, no `KeyError` | fake + caplog |
| `test_write_edges_mixed_batch_writes_valid_drops_rest` | Batch: one valid USES + one absent-endpoint + one disallowed-pair → `.written==1`, `.dropped==1`, `.invalid==1`; the valid edge's CREATE ran, the others didn't; summary log counts match | fake + caplog |
| `test_write_edges_persists_scopes_edge` | Seed `condition(c0)`, `equation(e0)`; write SCOPES `c0→e0` (allowed) → `.written==1`, CREATE used `_EDGE_CREATE_CYPHER["SCOPES"]` (SCOPES round-trips through the store, per prep item 4) | fake |
| `test_write_edges_empty_returns_zero_result` | `write_edges(edges=[])` returns a `WriteEdgesResult` with all-zero counts and runs NO Neo4j call | fake |
| `test_write_edges_result_int_coercion` | `int(WriteEdgesResult(written=3, dropped=1)) == 3` (back-compat) | none |
| `test_write_edges_respects_freeze` | Freeze the session phase → `write_edges` raises `SessionFrozenError` (existing pre-check preserved) | fake + SQLite session |
| `test_existing_node_ids_empty_input_no_neo4j_call` | `_existing_node_ids(s, aid, [])` returns `set()` and never calls `s.run` | fake |

### E. `apollo/knowledge_graph/tests/test_store_write_nodes_dedup.py` (mocked Neo4j)

| Test | Asserts | Mocking |
|---|---|---|
| `test_write_nodes_creates_new_nodes` | Empty subgraph; write 2 new nodes → return `2`; both CREATEd; log `created=2 reused=0` | fake + caplog |
| `test_write_nodes_skips_existing_id` | Seed node `eq1`; write a batch `[eq1 (same id), eq2 (new)]` → return `1` (only eq2 created); eq1's CREATE did NOT run; log `created=1 reused=1` | fake + caplog |
| `test_write_nodes_reused_not_counted_as_added` | Write only an already-present node → return `0`; fair-coverage contract (no inflation) | fake |
| `test_write_nodes_all_new_unchanged_behavior` | No prior nodes → behavior identical to pre-WU-2B (count == len(nodes)) — back-compat | fake |
| `test_write_nodes_respects_freeze` | Frozen phase → `SessionFrozenError` (pre-check preserved) | fake + SQLite |

### F. `apollo/handlers/tests/test_chat_crossturn.py` (mocked store + parser + LLM — NO Neo4j, NO live API)

Mocks: `@patch("apollo.handlers.chat.parse_utterance")`, `@patch("apollo.handlers.chat.draft_reply")`, and a fake/mock `KGStore` (or `@patch.object` on `read_graph`/`write_nodes`/`write_edges`/`summarize_for_apollo`). SQLite for the Postgres session (sibling fixture from `test_store_negotiation.py` / the skipped `test_chat.py`).

| Test | Asserts | Mocking |
|---|---|---|
| `test_chat_threads_graph_context_into_parser` | `read_graph` returns a graph with a prior `equation(eq_prev)`. `handle_chat` calls `parse_utterance` with a `graph_context` kwarg whose nodes include a `ContextNode(node_id="eq_prev", node_type="equation")`. Assert on `parse_utterance.call_args.kwargs["graph_context"]`. | mock parse + draft + store |
| `test_chat_passes_built_context_not_none` | The threaded `graph_context` is a real `GraphContext` (not `None`) even when the prior graph is non-empty — proves wiring, distinguishing it from WU-2A's None default | mock parse + draft + store |
| `test_chat_empty_prior_graph_passes_empty_context` | Empty prior graph → `graph_context.is_empty()` true is threaded (still not None) | mock parse + draft + store |
| `test_chat_does_not_recreate_prior_node` | `parse_utterance` returns a node whose id equals a prior id (`eq_prev`) AND a new node; assert `write_nodes` was called and (with the §6 de-dup) the prior id is not double-created — assert the `kg_entries_added` returned excludes the reused node (== count of genuinely new nodes) | mock parse + draft + store (real `write_nodes` against fake Neo4j seeded with `eq_prev`, OR assert the de-dup at the store layer via a spy) |
| `test_chat_response_envelope_unchanged` | Response keys are exactly `{apollo_reply, kg_entries_added, kg}`; no `sufficiency`/`misconception`/`olm_invite` keys (re-affirms `test_chat_no_signals.py` at the handler level) | mock parse + draft + store |
| `test_chat_writes_edges_after_nodes` | Order assertion: `write_nodes` is called before `write_edges` (the MATCH...CREATE ordering invariant survives the rewrite) | mock store (track call order) |

### G. Existing guards that MUST stay green (no edit, run as regression)

- `apollo/handlers/tests/test_chat_no_signals.py` — envelope/source guards (the `problem_text=problem.problem_text` and absent-signal-keys checks): the chat edit must not trip these.
- `apollo/parser/tests/test_typed_edge_extraction.py` (all existing) — parser contract intact.
- `apollo/knowledge_graph/tests/test_store_negotiation.py` — store negotiation contract intact (the fake-Neo4j extension must not break these).
- `apollo/knowledge_graph/tests/test_store_neo4j.py` — live round-trip (skips without `NEO4J_URI`; not part of the gate).

### Coverage note (95% patch gate)

Changed lines live in: `graph_context.py` (builder + guard), `edge_resolver.py` (comment/optional guard), `parser_llm.py` (`_resolve_uses_edges` rewrite + import removal), `store.py` (`WriteEdgesResult`, `_existing_node_ids`, `write_edges` rewrite, `write_nodes` de-dup), `chat.py` (context threading). Every branch above is hit by a pure or mocked-Neo4j/LLM test in A–F — no changed line depends on a live Neo4j connection. Run: `pytest --cov=apollo --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu2a-typed-edge-extraction --fail-under=95`.

## Per-endpoint contracts

WU-2B modifies the BEHAVIOR behind one existing HTTP route; it adds NO new route and changes NO request/response schema. The contract below documents the one touched endpoint for completeness.

### Endpoint: POST /apollo/sessions/{id}/chat

| Field | Value |
|-------|-------|
| Method | POST |
| Path | `/apollo/sessions/{id}/chat` |
| Auth | `require_session_owner` (token-derived identity; owner-gated — unchanged, `apollo/api.py`) |
| Request DTO | `{ "message": str }` — **unchanged** |
| Response DTO | `{ "apollo_reply": str, "kg_entries_added": int, "kg": KGGraph(json) }` (+ optional `intent_pending`/`intent_executed`) — **unchanged shape**; `kg_entries_added` now excludes cross-turn-reused nodes (semantic refinement, not schema change) |
| Success status | 200 |
| Error statuses | 401 unauth, 403 not owner, 404 session not found, 422 `parser_could_not_extract` (existing `ParserCouldNotExtractError`), 409 `session_frozen` (existing `SessionFrozenError`) |
| Side effects | Neo4j: `read_graph` (pre-parse context) → `write_nodes` (de-duped CREATE) → `write_edges` (validated CREATE) → `read_graph` (post-write render). Postgres: append (student, apollo) message pair in one commit. No external API beyond the existing `parse_utterance`/`draft_reply` LLM calls. |
| Idempotent | No (each turn appends messages + graph nodes/edges) — unchanged |

No new endpoint ships. The store methods (`write_edges`/`write_nodes`/`read_graph`) are internal Python APIs, not HTTP; their contracts are in §5/§6.

## DI / wiring discovery findings

NestJS-style DI does not apply (FastAPI + hand-constructed objects). The relevant wiring facts, grep-verified across the 3 store callers (`done.py`, `lifecycle.py`, `negotiate.py`):

1. **Where `KGStore` is constructed:** per-request, inline: `store = KGStore(db, neo)` (`chat.py:209`, `done.py`, `restart_problem.py`). It is NOT a singleton, NOT injected via FastAPI `Depends`. `db` is the request `AsyncSession`; `neo` is the process-singleton `Neo4jClient` (`apollo/api.py` `get_neo4j_client`). WU-2B does not change construction.
2. **Injection token / scope:** `Neo4jClient` is a process-singleton wrapper yielding fresh `async with client.session()` per call (`neo4j_client.py:35`); `AsyncSession` is request-scoped. The new `_existing_node_ids` helper runs inside the SAME `async with self.neo.session() as s` block as the CREATE, so it shares one session/transaction with the write — no extra connection.
3. **`build_graph_context` placement:** a free function in `apollo/parser/graph_context.py` (pure), imported by `chat.py`. It is NOT a method on `KGStore` — that would couple the store to the parser package and bloat the coupling hub; keeping it in the parser package matches where `GraphContext`/`ContextNode` already live and keeps the store free of parser imports (verified: `store.py` has no `apollo.parser` import today, and WU-2B adds none).

Sibling conventions agree; no conflict to flag.

## Transaction scope decisions

- **`write_edges` validation + CREATE:** runs inside ONE `async with self.neo.session() as s` block (the existing one). The endpoint-existence read and the per-type CREATEs share that session. Neo4j auto-commits per statement here (the store uses implicit transactions via `session.run`, same as today) — WU-2B does NOT introduce explicit Neo4j transactions; it only reorders (read-validate-then-create) within the existing single-session scope. Multiple CREATE statements are not atomic across types today and remain so; a crash mid-write leaves a partial subgraph, which is acceptable under the spec's idempotent-MERGE-on-retry story (§6.4) and is unchanged by this unit (still CREATE, still per-attempt, still wiped by `restart_problem`/janitor).
- **`write_nodes` de-dup:** same single-session scope; existence read + CREATE of the non-duplicate subset. No multi-table Postgres write in either method, so no `$transaction`-style boundary is needed.
- **No cross-store transaction touched.** `chat.py` still commits the Postgres message pair in its own `db.commit()` (`chat.py:298`), separate from the Neo4j writes — unchanged. WU-2B adds no new Postgres write and no compensation logic, because no new multi-store invariant is created.

## Error contract decisions

- **No new exception class.** The store-boundary rejection is **NOT an error** — it is logged data (exactly the parser-boundary precedent at `edge_resolver.py:66`, which logs and drops, never raises). `write_edges` returns counts; it does not raise on a dropped/invalid edge. This matches the §4 "NO-FALLBACK: dropped edges are LOGGED with reason, not silently dropped and not raised."
- **Existing exceptions preserved:** `SessionFrozenError` (freeze pre-check, `store.py:259`) and `ParserCouldNotExtractError` (parser) propagate unchanged; their handlers in `apollo/api.py`/`errors.py` (one named handler per error, the NO-FALLBACK policy from `apollo.md`) are untouched. The final HTTP response shape to the client is unchanged (422/409 envelopes).
- **Logging namespace (binding):** summary line `write_edges attempt_id=... written=... dropped=... invalid=... reasons=...`; per-edge line `write_edge_rejected reason=... edge=...`; node de-dup `write_nodes attempt_id=... created=... reused=...`. All via `_LOG.info` (the module logger `apollo.knowledge_graph.store`), consistent with `parser_edge_rejected`. Tests assert these substrings via `caplog`.

## Downstream consumers

- **Frontend (`ai-ta-student-ui`):** consumes `POST /apollo/sessions/{id}/chat` → renders `result.kg` (the understanding panel) and `kg_entries_added`. Grep target in the student UI: the `/chat` fetch and the `kg`/`kg_entries_added` reads. **No FE change needed** — the response schema is byte-identical; `kg_entries_added` semantics narrow slightly (reused nodes excluded) but the field stays an int the panel already tolerates. The FE `ApolloKG` shape mirrors `KGGraph` (nodes+edges) — cross-turn edges now actually appear in `kg.edges`, which the panel already renders. Out of scope to modify the FE here; note for the FE owner.
- **`done.py` grading:** reads the persisted graph (`read_graph`, `done.py:217`). Cross-turn edges + de-duped nodes make that graph more correct (fewer orphans, real SCOPES edges) — this is the §4 "raises the grading ceiling" benefit. `done.py` is NOT edited (anti-scope).

## Owner-doc updates

**File:** `docs/architecture/apollo.md` (the owner of `apollo/**`; `last_verified: 2026-06-15` already — re-affirm to 2026-06-15 with these edits in the same change).

- [ ] **Module map row for `apollo/knowledge_graph/`** (line 33): update the `write_edges` description — it now "validates endpoint existence + EDGE_ALLOWED_PAIRS before CREATE and emits a structured `write_edges` log (written/dropped/invalid + reasons); no silent drop" and `write_nodes` "de-dups cross-turn references by id (reused prior-graph ids are not re-minted)".
- [ ] **Public interfaces** (line 65): change the `KGStore.write_edges(...)` signature note to return `WriteEdgesResult` (written/dropped/invalid/reasons; `int()`-coercible for back-compat) instead of `int`.
- [ ] **Parser package row** (line 31) + **service entry points** (line 64): note that `graph_context` is now BUILT and THREADED by `chat.py` via `build_graph_context(read_graph(...))` (WU-2B), removing the WU-2A "default None — chat.py unchanged until WU-2B" caveat.
- [ ] **Data flow (a) step 3-4** (lines 85-86): rewrite to state chat now passes a `graph_context` built from the prior attempt graph (cross-turn linking live), and the store's `write_edges` no longer relies on Neo4j silent-drop — it validates+logs. Remove the "(WU-2B will ... fix store.write_edges's silent-drop)" forward-reference now that it's done.
- [ ] **Related plans** (line 147): add a pointer to this plan (`docs/superpowers/plans/2026-06-15-apollo-kg-wu2b-crossturn-writeedges.md`).
- [ ] Set `last_verified: 2026-06-15` (already that date; confirm unchanged or bump if edited later in the day).

## Risks

- **[MEDIUM] `write_edges` return-type change (`int` → `WriteEdgesResult`).** Only one non-test caller (`chat.py`, which ignores the return) — verified by grep. `__int__` provides belt-and-braces back-compat. Risk is low but real if a future caller assumed `int`. Mitigation: `__int__` + an explicit test that `int(result) == result.written`.
- **[MEDIUM] Name-based de-dup deferred to WU-3C.** The prep doc says "by id/name". WU-2B does id-based store de-dup only; content/name canonicalization is the §5 resolver's job (anti-scope here). If the orchestrator expected name-based de-dup in this unit, this is a scope decision to confirm. The id-based path fully covers the cross-turn-via-`graph_context` duplication the unit targets.
- **[LOW] Two `read_graph` calls per chat turn** (pre-parse context + post-write render). Adds one Neo4j round-trip to the chat loop (~tens of ms on Aura). Acceptable for v1; if latency bites, the post-write render can be reconstructed from `prior_graph` + the just-written nodes/edges in-memory instead of re-reading. Noted; not optimized now.
- **[LOW] nit-2 (`from typing import Any`) may already be needed.** Recon says unused; if a later WU-2A edit reintroduced an `Any` annotation, skip the removal and note it. The test for nit-2 is a static import-presence assertion, so it self-documents.
- **[LOW] `_existing_node_ids` on an empty id list.** Guard: return `set()` without a Neo4j call when `ids` is empty (both `write_nodes` and `write_edges` already early-return on empty input, but the helper must be safe if called with `[]`).
- **[LOW] Endpoint-type `None` on a typed `Edge`.** A parser-built `Edge` always carries `from_node_type`/`to_node_type` (resolved in `edge_resolver`), but reference-graph edges may not. The store treats `None` endpoint type as `invalid`/`unknown_endpoint_type` rather than trusting EDGE_ALLOWED_PAIRS with a None key (which would `KeyError`/`in` mismatch). Tested explicitly.

## Out-of-scope boundaries (this unit)

- **Do NOT** build the §5 reference-anchored resolver, `RESOLVES_TO` edges, `:Canon` projection, or `apollo_kg_entities` — that is WU-3C. Specifically: NO content/name canonicalization in `write_nodes` (id-based de-dup only).
- **Do NOT** touch `apollo/handlers/done.py`, grading (`coverage.py`/`rubric.py`/`diagnostic.py`), or the §6 graph-simulation core.
- **Do NOT** change `EDGE_ALLOWED_PAIRS` (`ontology/edges.py`) — reuse it.
- **Do NOT** add a DB migration, change Postgres schema, or apply any migration to any remote DB.
- **Do NOT** change the `/chat` request/response schema, the intent state machine, or the v1 "nodify + dumb reply" turn (no output filter, no sufficiency/misconception/OLM-invite) — `test_chat_no_signals.py` guards this.
- **Do NOT** introduce a new global exception/handler; rejected edges are logged, not raised.
- **Do NOT** edit the student/teacher UIs (cross-repo). Note the FE consumer for its owner; do not modify it.
- **Branch discipline:** work only on `feat/apollo-kg-wu2b-crossturn-writeedges`; no branch/switch/push/PR.
- **`.gitignore` chore (in-scope, low priority):** add `.feller/` to `ai-ta-backend/.gitignore` so the verifier's transient `.feller/tasks/<unit>/verification-report.md` is not committed. One-line edit; no test needed.

## Deviations I'd allow the executor

- The exact field names of `WriteEdgesResult` may shift (`dropped`/`invalid`/`reasons`) provided the structured log still distinguishes endpoint-absent from disallowed-pair and the counts are testable.
- The log MESSAGE wording may vary, but the grep-able tokens `write_edges`, `write_edge_rejected`, `endpoint_absent`/`disallowed_pair`, and `write_nodes ... reused=` must appear (tests assert these).
- The `build_graph_context` label-derivation per type may use a different short field as long as it is deterministic, ≤60 chars, and never an LLM call.
- If pinning nit-3 reveals a genuine collision path, the executor may apply the minimal `int(ref[1:]) in index_to_node` guard in `_resolve_ref` (documented in the test) — but only if RED first.
- The executor may keep or drop `chat.py`'s capture of the `write_edges` result (logging is the observable); either is acceptable as long as the response envelope is unchanged.
