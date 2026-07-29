---
doc: apollo/_index
description: Apollo mega-domain router — two index tiers over the teach-a-confused-AI mode (365 files); routes to 12 sub-domain indexes.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# apollo/ — the Apollo teaching mode

Apollo is Hoot's second mode: the student teaches a deliberately confused AI
learner, a Neo4j KG + SymPy solver + LLM adjudicator model the explanation, and a
grade is committed when the student clicks Done. 365 files, so this is a **two-tier
router**: this index → a sub-domain `_index.md` → leaf. `provisioning/` nests one
level further (`authored-sets/`, `problem-generation/`).

| Sub-domain | Index | Covers |
|---|---|---|
| conversation | [conversation/_index](conversation/_index.md) | the live teaching-turn path: routing, handlers, agent, parser, questioning, curriculum, session entry |
| ontology | [ontology/_index](ontology/_index.md) | the typed KG shape layer (nodes, edges, graph) every module mirrors |
| knowledge-graph | [knowledge-graph/_index](knowledge-graph/_index.md) | Neo4j-backed per-attempt KG persistence + degraded-mode invariant |
| resolution | [resolution/_index](resolution/_index.md) | the §5 reference-anchored resolver (built but unwired — dormant) |
| solver | [solver/_index](solver/_index.md) | the SymPy symbolic-math seam + a dormant planner chain |
| overseer | [overseer/_index](overseer/_index.md) | grading, scoring, narrative, XP, selection — **grading-path invariants live here** |
| grading | [grading/_index](grading/_index.md) | shared grading value objects + canonical artifact builder |
| projections | [projections/_index](projections/_index.md) | read-side projections over the committed grading artifact |
| persistence | [persistence/_index](persistence/_index.md) | ORM hub, scoped repos, Layer-1 seed converter, Neo4j seam |
| learner-model | [learner-model/_index](learner-model/_index.md) | Bayesian belief filter + the live personalization wedge |
| schemas | [schemas/_index](schemas/_index.md) | the central Problem schema + small hand-authored-JSON schemas |
| provisioning | [provisioning/_index](provisioning/_index.md) | teacher reference-content pipelines + the auto-provision stages |

## Cross-cutting orientation

- **Grading is one lane.** The transcript adjudicator is the only grader of record;
  the whole grade path + its invariants (composite retired, misconceptions-empty,
  score→letter→narrative) live in [overseer/_index](overseer/_index.md), which names
  the `conversation/handlers/done` orchestrator. Start any grading task there.
- **The teaching turn** is documented end-to-end by
  [conversation/_index](conversation/_index.md) — the Apollo teaching-turn authority
  the shared-architecture data-flow router points at.
- **Provisioning** (teacher-authored content → teachable problems) carries its own
  savepoint recipe in [provisioning/_index](provisioning/_index.md).
