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
// Textbook problem index (concepts + problems + aliases + observability).
// Namespace marker labels: :_ConceptNode, :_ProblemNode, :_IngestEvent.
// ---------------------------------------------------------------------------
CREATE CONSTRAINT concept_id_unique IF NOT EXISTS
  FOR (c:Concept) REQUIRE (c.subject_id, c.concept_id) IS UNIQUE;

CREATE CONSTRAINT problem_id_unique IF NOT EXISTS
  FOR (p:Problem) REQUIRE p.problem_id IS UNIQUE;

CREATE CONSTRAINT cluster_alias_unique IF NOT EXISTS
  FOR (a:ClusterAlias) REQUIRE a.cluster_id IS UNIQUE;

CREATE INDEX problem_concept_difficulty IF NOT EXISTS
  FOR (p:Problem) ON (p.subject_id, p.concept_id, p.difficulty);

CREATE INDEX conceptnode_scope IF NOT EXISTS
  FOR (n:_ConceptNode) ON (n.concept_id);

CREATE INDEX problemnode_root IF NOT EXISTS
  FOR (p:Problem) ON (p.problem_id);

CREATE VECTOR INDEX concept_scope_embedding_idx IF NOT EXISTS
  FOR (c:Concept) ON c.scope_embedding
  OPTIONS { indexConfig: { `vector.dimensions`: 3072, `vector.similarity_function`: 'cosine' }};
