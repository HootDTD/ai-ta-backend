---
doc: campaign/scripts-diff-eval
description: Two provisioning-quality eval scripts comparing GENERATED problems against AUTHORED/gold.
owns:
  - campaign/scripts/diff_generated_vs_authored.py
  - campaign/scripts/eval_authored_calc2.py
related:
  - campaign/cast-teacher
  - apollo/provisioning/authored-sets/api
  - apollo/provisioning/problem-generation/generator
last_verified: 2026-07-25
stub: false
---

# campaign/scripts-diff-eval — provisioning-quality eval

Compare generated authored-set problems against the authored/gold corpus.

## Interface

- **`diff_generated_vs_authored.py`** (pure helpers): `norm_slug`, `_tokens`,
  `text_jaccard(a, b)`, `align_problems(generated, corpus)` (greedy best-match by
  problem-text Jaccard ≥ 0.6, each corpus entry used once), `score_concept_match`
  (accuracy vs the corpus's private `concept_slug`, tallying NO_MATCH holds and
  unaligned), the graph-diff helpers `_steps`/`_edge_pairs`/`_opaque_ids`/
  `_entry_type_histogram`, and `diff_graph(generated_payload, committed_problem)`
  (structural + text diff of a generated graph vs its authored twin).
- **`eval_authored_calc2.py`** (live end-to-end driver, pragma no-cover live I/O):
  `_mint_teacher`, `_bootstrap`, `_upload_and_poll`, `_dump_generated`,
  `_classify_generated(rows)`, `_load_gold_index` / `_match_gold(generated_payload,
  gold)`, `_run(args)`, `main()` CLI. Two subcommands (`bootstrap`, `run`) drive
  the 6 calc-2 HW PDF pairs through the REAL `/apollo/authored-sets` path, then
  score concept-match + structural diff vs the committed gold graphs.

## Invariants & gotchas

- LOCAL campaign stack only (`127.0.0.1`) — never staging/prod.
- Held problems are deliberately NOT auto-approved: the acceptance bar measures
  the pipeline's OWN automatic outcomes.
- The gold index + generated dumps live under campaign/corpus data (not owned).

## Related

- [cast-teacher](cast-teacher.md), [apollo/provisioning/authored-sets/api](../apollo/provisioning/authored-sets/api.md),
  [apollo/provisioning/problem-generation/generator](../apollo/provisioning/problem-generation/generator.md).
