---
doc: campaign/_index
description: Router for the offline Apollo grading-campaign harness — casts an AI student against the live teaching loop and scores it.
owns: []
related: []
last_verified: 2026-08-12
stub: false
---

# campaign/ — offline grading-campaign harness

A NEW durable domain: `campaign/**` was an ownership GAP (no architecture doc
owned it). Purpose: cast an AI "student" against Apollo's LIVE teaching loop and
score it with the permanent transcript grader + the S1–S5 judge pipeline.
**LOCAL Docker only, never prod** — every campaign session mints its own
`get_async_session`, so it is RLS-exempt (owner role).

| Leaf | One-liner | Owns |
|---|---|---|
| [transcript-replay](transcript-replay.md) | Deterministic transcript-grader fixture replay + the shared grading core | `transcript_replay.py`, `__init__.py` + README.md |
| [turn-replay](turn-replay.md) | Per-turn producer replay through the live questioning engine (S3 client seam) | `turn_replay.py` |
| [cast-student](cast-student.md) | AI-student session driver + JSONL ledger | `cast/student.py`, `cast/__init__.py` |
| [cast-teacher](cast-teacher.md) | Teacher provisioning driver (seeded + authored) | `cast/teacher.py` |
| [cast-subjects-materials](cast-subjects-materials.md) | Subject registry + PDF fixture generator | `cast/subjects.py`, `cast/materials/generate_fixtures.py` |
| [cast-personas](cast-personas.md) | Persona schema + validation vs reference keys | `cast/personas/{schema,validate,__init__}.py` |
| [infra](infra.md) | Local Postgres/Neo4j bootstrap + reset | `infra/{apply_migrations,reset,__init__}.py` |
| [judges-base](judges-base.md) | StageJudge framework + pass-rate gate | `judges/{base,__init__}.py` |
| [judges-s1-s2](judges-s1-s2.md) | Reference-graph + ingestion judges | `judges/s1_reference_graph.py`, `s2_ingestion.py` |
| [judges-s3-s4](judges-s3-s4.md) | Student-fidelity + Apollo-coherence judges | `judges/s3_student_fidelity.py`, `s4_apollo_coherence.py` |
| [judges-s5](judges-s5.md) | Misconception-recall gate | `judges/s5_misconceptions.py` |
| [scripts-run-s1-s2](scripts-run-s1-s2.md) | S1/S2 raw-input harness runner | `scripts/run_s1_s2.py`, `scripts/__init__.py` |
| [scripts-diff-eval](scripts-diff-eval.md) | Provisioning-quality eval (generated vs gold) | `scripts/diff_generated_vs_authored.py`, `eval_authored_calc2.py` |

## Cross-cutting invariants

- **A7 (2026-07-20) REMOVED** the abandoned graph-grader replay, paired-artifact
  comparison, config-freeze, and report adapters. The retained surface is what
  the leaves above document.
- The single attempt-ledger seam is `cast-student.append_attempt_record`
  (`attempts.jsonl`), which the S3/S4/S5 judges read.

## NOT owned as code (excluded assets)

`campaign/tests/**` (tests rule); the persona JSON corpus under
`campaign/cast/personas/<subject>/`; materials PDFs; `campaign/infra/
docker-compose.neo4j.yml` + `env.campaign.example`; and `campaign/fixtures/**` /
`campaign/tests/fixtures/**`. The coverage lint applies to `.py` source only.
