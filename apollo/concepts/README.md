# Concept Content — Apollo v1

JSON content files consumed by Apollo. See schemas under `apollo/schemas/`.

## Files

- `bernoulli_dag.json` — concept hierarchy for Bernoulli + continuity cluster (v1 pilot topic)
- `variable_normalization/fluid_mechanics.json` — natural-language → symbolic variable map

## Authoring a new DAG node

Each node:
- `id`: snake_case, globally unique
- `label`: human-readable name
- `prerequisites`: node ids that must be understood first
- `scope_boundary`: concepts explicitly *out of scope* for this node (e.g., Bernoulli excludes compressibility)
- `topic_cluster`: currently only `fluid_mechanics`

Edge types:
- `requires` — A requires B (B is a prerequisite of A)
- `extends` — A is a refinement of B
- `excludes` — A and B are mutually exclusive framings

Validate with: `python -c "from apollo.schemas.dag import load_dag; load_dag('apollo/concepts/bernoulli_dag.json')"`
