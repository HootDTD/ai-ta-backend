# Calculus 2 concept-matching corpora (reversed-provisioning assessment)

Artifacts from the 2026-07-07 assessment of the **reversed provisioning design**:
instead of autonomously generating concepts from uploaded documents, the course
carries a premade concept list and uploaded problems are *matched* to it.
This directory preserves the corpora and results so nothing has to be
re-extracted or re-authored to continue the work.

## Contents

- `concepts.json` — the hand-authored 40-concept Calculus 2 course list
  (granularity anchor: "integration-by-parts" is one concept). This is the
  premade-list artifact of the reversed design, reviewed by Ishaan.
- `textbook/` — 79 exercises extracted from staging `aita_documents` id=5
  (OpenStax *Calculus Volume 2*), with `section_path`/page provenance
  (`problems.json`, `section_map.json`), blind gpt-5.1 match results
  (`matches.json`), scoring (`results.md`, `mismatches.json`), and the expert
  adjudication (`adjudication.md`/`.json`).
- `authored/` — 60 ORIGINAL problems + full worked solutions authored for Hoot
  (2026-07-07), 6 per concept across the 10 confusable-technique/series
  concepts, arranged as 6 mixed HW-style sets with paired problem/solution
  PDFs (`hw{1..6}_{problem,solution}.pdf`) suitable for the authored-sets
  upload path. All 60 answers SymPy-verified. `authored_corpus.json` carries
  the private per-problem `concept_slug` ground truth and `looks_like`
  discrimination-plant metadata.

## Headline results (full details in the results/adjudication files)

- Textbook corpus, blind matching (gpt-5.1, effort=low, the staging
  MAIN_MODEL config): raw 58.2% — but adjudication attributes the gap to
  upstream math-stripping in `aita_chunks` for doc 5 and single-label ground
  truth; **97.3% (71/73) on adequately-specified problems**; 2 genuine model
  errors.
- Authored corpus (clean text, incl. the confusable clusters the textbook
  couldn't test): **95.0% at effort=low, 96.7% with medium retry; 100% on
  non-trap problems**. Both remaining misses are concept-boundary taxonomy
  cases (∫ln x, ∫1/√(9−x²)), supporting equivalence-set (not single-slug)
  concept mapping.
- Side finding: the doc-5 indexing pipeline strips inline math from many
  exercise chunks (sections 1.6–1.7, 3.2–3.4, 5.5, 6.3–6.4, 7.1–7.4) — a
  Hoot RAG quality issue independent of Apollo.

## Licensing

- `textbook/problems.json` texts derive from OpenStax *Calculus Volume 2*
  (CC BY-NC-SA 4.0) — internal assessment use only; do NOT ship these into
  the product problem bank.
- `authored/` content is original work authored for Hoot on 2026-07-07 — no
  external license encumbrance.
