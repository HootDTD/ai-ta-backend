// Neo4j schema for Apollo V3 KG layer.
// Run once on a fresh Aura instance.
// All KG nodes carry the secondary label `:_KGNode` so a single index covers
// all subgraph reads + cleanup. Application code applies both labels at create.

// ---------------------------------------------------------------------------
// Uniqueness constraints: (attempt_id, node_id) is unique per node label
// ---------------------------------------------------------------------------
CREATE CONSTRAINT equation_attempt_id_unique IF NOT EXISTS
  FOR (n:Equation) REQUIRE (n.attempt_id, n.node_id) IS UNIQUE;
CREATE CONSTRAINT condition_attempt_id_unique IF NOT EXISTS
  FOR (n:Condition) REQUIRE (n.attempt_id, n.node_id) IS UNIQUE;
CREATE CONSTRAINT simplification_attempt_id_unique IF NOT EXISTS
  FOR (n:Simplification) REQUIRE (n.attempt_id, n.node_id) IS UNIQUE;
CREATE CONSTRAINT definition_attempt_id_unique IF NOT EXISTS
  FOR (n:Definition) REQUIRE (n.attempt_id, n.node_id) IS UNIQUE;
CREATE CONSTRAINT variable_mapping_attempt_id_unique IF NOT EXISTS
  FOR (n:VariableMapping) REQUIRE (n.attempt_id, n.node_id) IS UNIQUE;
CREATE CONSTRAINT procedure_step_attempt_id_unique IF NOT EXISTS
  FOR (n:ProcedureStep) REQUIRE (n.attempt_id, n.node_id) IS UNIQUE;

// ---------------------------------------------------------------------------
// Indexes for fast subgraph reads + cleanup
// ---------------------------------------------------------------------------
CREATE INDEX kgnode_attempt_id IF NOT EXISTS FOR (n:_KGNode) ON (n.attempt_id);

// Edge attempt_id index — needed because edges are also scoped per attempt
// for clean cascade delete via DETACH DELETE on nodes (edges follow).
// Aura-supported: relationship property indexes
CREATE INDEX precedes_attempt_id IF NOT EXISTS FOR ()-[e:PRECEDES]-() ON (e.attempt_id);
CREATE INDEX uses_attempt_id IF NOT EXISTS FOR ()-[e:USES]-() ON (e.attempt_id);
CREATE INDEX depends_on_attempt_id IF NOT EXISTS FOR ()-[e:DEPENDS_ON]-() ON (e.attempt_id);
CREATE INDEX scopes_attempt_id IF NOT EXISTS FOR ()-[e:SCOPES]-() ON (e.attempt_id);

// ---------------------------------------------------------------------------
// WU-3C1 — :Canon projection + Layer-2 node scoping.
//
// :Canon nodes are projected from Postgres (apollo_kg_entities) by
// apollo/knowledge_graph/canon_projection.py. The `key` is the surrogate
// entity id; this uniqueness constraint is what makes MERGE (c:Canon {key})
// an upsert (not a duplicate-create) and enforces one-node-per-entity-id at
// the DB level — the defense-in-depth behind the application MERGE.
CREATE CONSTRAINT canon_key_unique IF NOT EXISTS FOR (c:Canon) REQUIRE c.key IS UNIQUE;

// Course-scoped :Canon filters (WU-3C2 resolver filters :Canon by course).
CREATE INDEX canon_search_space_id IF NOT EXISTS FOR (c:Canon) ON (c.search_space_id);

// Layer-2 node user scoping. The existing kgnode_attempt_id index (above) stays.
CREATE INDEX kgnode_user_id IF NOT EXISTS FOR (n:_KGNode) ON (n.user_id);
