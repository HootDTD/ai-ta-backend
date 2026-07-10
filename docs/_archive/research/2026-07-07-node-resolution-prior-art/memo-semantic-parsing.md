# Proposition extraction (OpenIE / SRL / AMR) + graph comparison metrics (SMATCH / SPICE)

Research memo for the Apollo node-resolution prior-art sweep (2026-07-07). Angle: extracting
predicate-argument propositions from explanatory text and scoring them against a reference
graph via soft graph-tuple alignment. Bottom line: the metrics half of this literature (SMATCH
family, SPICE, CaRB scorer) is directly adoptable and attacks our exact failure mode (binary,
per-candidate, precision-first matching); the parsing half (stock OpenIE/SRL/AMR parsers) is
NOT robust on dialogue and should be treated as blueprint, not drop-in.

## Key findings

1. **SMATCH is the canonical "align two semantic graphs, then score triples" metric** — it searches
   a node mapping maximizing matched (edge) triples and reports P/R/F1 over triples.
   https://github.com/snowblink14/smatch. Relevance: it scores AFTER finding one globally optimal
   alignment, whereas our resolver accepts/rejects each candidate independently. The global-alignment-
   then-score order is the core design our pipeline is missing.
2. **Smatch++ (Opitz, 2023) shows the alignment step must be solved optimally**: it provides ILP
   optimal alignment, standardized preprocessing, fine-grained subgraph ("aspect") scoring, and
   bootstrap CIs, and its README warns that hill-climbing alignment "will yield Smatch scores that
   are not verifiable and are likely false." https://github.com/flipz357/smatchpp (GPL-3.0, active,
   v1.8.0 May 2025). Relevance: greedy/first-match resolution demonstrably underestimates matches —
   an independent reason our coverage reads low.
3. **S2match adds soft (graded) concept matching inside the alignment** — instead of hard triple
   matches it maximizes embedding-similarity-weighted matches (cat~kitten), from "AMR Similarity
   Metrics from Principles" (TACL 2020). https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00329/96472
   and https://github.com/flipz357/amr-metric-suite. Relevance: prior art for replacing our binary
   tier thresholds with graded similarity credit inside a global alignment.
4. **Asymmetric Wasserstein Weisfeiler-Leman measures give exactly "is the reference graph contained
   in the student graph"** — AMR4NLI (IWCS 2023) scores entailment as hypothesis-being-a-semantic-
   substructure of premise, with precision-like (`-prs p`) and recall-like (`-prs r`) sub/super-graph
   measures shipped in code; WWLK also yields n:m node alignments in polynomial time (TACL 2021 BAMBOO
   paper). https://arxiv.org/abs/2306.00936, https://github.com/flipz357/weisfeiler-leman-amr-metrics,
   https://doi.org/10.1162/tacl_a_00435. Relevance: our node_coverage is literally the recall-like
   asymmetric measure; an off-the-shelf, embedding-soft, alignment-producing implementation exists.
5. **SPICE (ECCV 2016) is the tuple-F-score blueprint**: parse candidate + reference into scene-graph
   tuples (objects/attributes/relations), match tuples with lemma + WordNet-synonym equivalence, report
   P/R/F1. It deliberately gives NO partial credit within a tuple — a choice the authors justify only
   because caption relations (in/on) are too generic to deserve credit alone. https://arxiv.org/abs/1607.08822,
   https://github.com/peteanderson80/SPICE. Relevance: our edge_coverage inherits SPICE's harshest
   design choice (all-elements-or-nothing) in a domain where partial credit IS meaningful.
6. **Parser quality dominates tuple-metric quality, and a small fine-tuned seq2seq crushes rule
   pipelines**: FACTUAL (ACL Findings 2023) re-annotated Visual Genome and fine-tuned Flan-T5; the
   original rule-based SPICE parser scores 13.0 exact set match / 56.2 SPICE vs 80.8/93.0 for
   Flan-T5-base — a 6x jump from swapping the extractor, not the metric. Also ships Soft-SPICE
   (embedding-based tuple similarity). https://aclanthology.org/2023.findings-acl.398,
   https://pypi.org/project/FactualSceneGraph/0.4.0/. Relevance: strongest evidence that our leverage
   is in the extractor/resolver model, not in threshold tuning.
7. **CaRB (EMNLP 2019) is the partial-credit matching scorer for tuples**: all-pair matching table,
   token-level P/R per (gold, system) tuple pair, recall = mean over gold tuples of best-matching
   system tuple (multi-match: one system extraction may cover several golds; several extractions may
   serve one gold), precision matched 1:1 best-first; field-aware matching (rel with rel, args with
   args) with >=1 shared relation word to avoid spurious matches. https://aclanthology.org/D19-1651.pdf,
   https://github.com/dair-iitd/CaRB. Relevance: a ~300-line, MIT-style portable recipe to turn our
   binary node/edge coverage into continuous coverage that stops compounding quadratically.
8. **Stock OpenIE collapses on conversational text**: on a dedicated conversational triple-extraction
   test set, Stanford OpenIE got 0% precision on complete triples for statements and WH-questions;
   best conversation-trained BERT models reach only ~51% complete-triple precision on single
   utterances, worse multi-turn (coreference, ellipsis, negation/confirmation are the killers).
   https://arxiv.org/abs/2412.18364. Separately, OpenIE models degrade sharply under syntactic
   distribution shift (F1 ~0.47 on low-similarity subsets). https://arxiv.org/pdf/2301.06841.
   Relevance: do not bolt CoreNLP/ClausIE/OpenIE6 onto tutoring transcripts.
9. **Atomic-proposition decomposition before extraction raises relation recall for weak extractors**:
   an LLM "propositioner" (Qwen3-32B distilled to 0.6B) that recursively splits text into minimal
   self-contained propositions lifted CoreNLP's CaRB recall 24.3%→31.3% and FewRel relation recall
   48.7→53.1; entity recall dips, recovered by a raw-text+propositions fallback combination.
   https://ar5iv.labs.arxiv.org/html/2604.02866. Relevance: cheap pre-resolver stage that directly
   attacks candidate-generation recall.
10. **EDC (Extract-Define-Canonicalize, EMNLP 2024) is our architecture, published**: open extraction →
    schema definition → post-hoc canonicalization to a target schema, plus a trained "schema retriever"
    that pulls relevant schema elements INTO the extraction prompt (RAG-style), which improves
    extraction on KGC benchmarks with large schemas. https://arxiv.org/abs/2404.03868,
    https://github.com/clear-nus/edc. Relevance: says canonicalization should be schema-aware at
    extraction time — feed candidate node labels/descriptions to the parser, don't only match post-hoc.
11. **AMR parsing is strong on newswire, unproven on dialogue**: best parsers ~0.84-0.85 Smatch (IBM
    MBSE ensemble distillation https://arxiv.org/pdf/2112.07790; amrlib bart-large 83.7
    https://github.com/bjascob/amrlib), but GrAPES (EMNLP 2023) shows parsers at/above IAA Smatch still
    make frequent meaning-distorting node/structure errors across 36 phenomena
    https://aclanthology.org/2023.emnlp-main.662/. Dialogue-AMR needed a new annotation layer and its
    corpus is tiny (569 utterances) https://aclanthology.org/2020.lrec-1.86.pdf; adding question-genre
    training data moved QALD Smatch 80→86, quantifying genre shift
    https://medium.com/@sroukos/semantic-parsing-using-abstract-meaning-representation-95242518a380.
12. **SRL tooling is in transition**: AllenNLP (the standard SRL stack) has been in maintenance mode
    since Dec 2022; modern replacements exist (transformer-srl; a 2026 encoder framework with ~10x
    faster inference at ~86-87.5 F1 https://arxiv.org/html/2605.02505v1; LLM-SRL, ACL Findings 2025,
    needs retrieval-augmentation + self-correction scaffolding to beat encoders
    https://github.com/fangfang123gh/LLM-SRL). SRL outputs PropBank verb-frame roles, not domain
    concepts — it structures candidates but does not resolve them.
13. **Education-specific graph alignment exists but is shallow**: directed graph alignment of student
    vs model answers via ClausIE triples + word2vec predicate clustering + Similarity Flooding for gap
    identification (2025) https://arxiv.org/abs/2504.04473; concept-map scoring literature (e.g.
    https://www.cambridge.org/core/journals/natural-language-engineering/article/automated-evaluation-of-the-quality-of-ideas-in-compositions-based-on-concept-maps/A3E7E2D9A5F49D444736D7035A087039).
    FactGraph shows meaning-representation graphs improve semantic-relation verification vs raw text
    (+15% factuality) https://arxiv.org/abs/2204.06508.

## Adoptable artifacts

- **smatchpp** — https://github.com/flipz357/smatchpp — GPL-3.0, active (v1.8.0 2025-05). Adopt as the
  scoring engine: express reference + parsed-student graphs as triples, get ILP-optimal alignment,
  aspect-level subscores (per node type), bootstrap CIs for our 31-attempt corpora. GPL is fine for
  server-side use; vendor as a service if license hygiene matters.
- **weisfeiler-leman-amr-metrics** — https://github.com/flipz357/weisfeiler-leman-amr-metrics —
  active; asymmetric precision/recall subgraph measures (`-prs p|r`) + n:m alignments + GloVe-soft node
  match. Adopt as a second, alignment-free-ish coverage signal and as the "is reference ⊆ student"
  primitive. (License not confirmed — check before vendoring.)
- **CaRB scorer** — https://github.com/dair-iitd/CaRB — small Python; port `Matcher` + all-pair table
  logic into our grader to replace binary resolved/unresolved with token/element-level partial credit
  and multi-match recall. Note issue #4: code takes per-gold max, i.e. one gold ↔ one best extraction
  for recall.
- **FactualSceneGraph / FACTUAL-MR models** — https://pypi.org/project/FactualSceneGraph/,
  HF `lizhuang144/flan-t5-base-VG-factual-sg` — the recipe to copy: re-annotate a few thousand domain
  utterances into canonical tuples, fine-tune Flan-T5-base-class model, get faithful+consistent tuples;
  includes Soft-SPICE (SentenceTransformer tuple similarity) implementation.
- **amrlib** — https://github.com/bjascob/amrlib — MIT, actively maintained (0.8.1, 2026-03), 83.7
  Smatch parse model. Cheapest way to experiment with AMR-structured candidates; pairs with smatchpp.
- **IBM transition-amr-parser** — https://github.com/IBM/transition-amr-parser — SoTA-family parser
  with word-node alignments (useful for span-level evidence back into the transcript).
- **EDC framework** — https://github.com/clear-nus/edc — LLM extract/define/canonicalize with trained
  schema retriever; adopt the schema-retrieval-into-prompt trick for our parser stage.
- **Propositioner (MPropositionneur-V2)** — https://ar5iv.labs.arxiv.org/html/2604.02866 — 0.6B
  multilingual atomizer distilled from Qwen3-32B; adopt the pattern (LLM decompose → extract → resolve),
  even if we use our own model.
- **LLM-SRL** — https://github.com/fangfang123gh/LLM-SRL — ACL Findings 2025; reference design for
  retrieval-augmented + self-corrected structured extraction with LLMs.

## Recall lessons

- **Score via one global soft alignment, not per-candidate gates.** Every mature graph metric (SMATCH,
  S2match, WWLK, CaRB) first computes a best global alignment between candidate and reference sets and
  only then scores. Per-candidate accept/reject with precision-first thresholds (our tiers) throws away
  jointly-consistent matches; smatchpp shows even greedy alignment materially understates scores.
- **Partial credit at the element/token level.** CaRB/Wire57 score each (gold, system) tuple pair with
  token-level P/R; SPICE's binary tuple match is the outlier and was justified only for generic caption
  relations. For us: an edge should earn credit proportional to endpoint-resolution confidence, not
  require both endpoints binary-resolved (this is what makes edge recall ~quadratic in node recall).
- **Recall-shaped asymmetric measures exist**: AMR4NLI's recall-like supergraph measure is literally
  "fraction of reference structure covered by student graph," embedding-soft, with alignments as
  evidence. Coverage does not have to be built from hard resolution events.
- **Fix the extractor, not the threshold.** FACTUAL: swapping a rule parser for a fine-tuned Flan-T5
  moved exact set match 13→81. GrAPES: even 0.84-Smatch parsers make meaning-distorting errors. A few
  thousand annotated tutoring utterances → fine-tuned tuple parser likely beats any amount of NLI
  threshold tuning.
- **Over-generate candidates via atomic decomposition.** Propositioner results show splitting dense
  student sentences into minimal propositions before extraction raises relation recall for weak
  extractors (CaRB recall +7pts for CoreNLP); combine raw + decomposed channels to protect entity recall.
- **Consolidate dialogue before parsing.** Conversational phenomena (coreference, ellipsis, agreement
  turns like "right, exactly") destroy single-utterance extractors (Stanford OpenIE 0% complete-triple
  precision on statements). Rewrite the transcript into declarative student-attributed claims (LLM
  rewrite / decontextualization) and parse THAT.
- **Make the resolver schema-aware at extraction time** (EDC): retrieve the reference node labels,
  definitions, and aliases into the extraction prompt so candidates are generated in the reference
  vocabulary, instead of extracting blind and matching post-hoc.
- **Calibrate with reference-side recall in mind**: SPICE with many references is recall-dominated and
  scores drop to 0.03-0.07 — i.e., tuple metrics behave sanely only when the reference granularity
  matches extraction granularity; our 5-node references need per-node multi-alias/manifestation sets.

## Dead ends

- **Stock rule/neural OpenIE (CoreNLP, ClausIE, OpenIE5/6, IMoJIE) run directly on transcript turns.**
  0% complete-triple precision on conversational statements; trained on Wikipedia-register text; sharp
  degradation under syntactic shift. Also OpenIE canonicalizes nothing — output still needs our resolver.
- **Full AMR parsing as the resolver backbone.** Parsers are newswire-trained; dialogue AMR corpora are
  tiny (569 utterances) and required schema extensions; GrAPES shows meaning-distorting errors persist;
  and AMR nodes are PropBank frames, so we'd STILL need a second alignment from AMR concepts to our
  canonical physics/econ keys — it relocates the resolution problem without solving it. Use the AMR
  *metrics* machinery on our own graphs instead.
- **Hill-climbing/greedy alignment** (classic smatch default, and by analogy our first-tier-wins
  resolution order) — explicitly flagged as producing "likely false" scores; use ILP/optimal matching.
- **SRL as a standalone resolver.** Role inventories are verb-frame-specific, AllenNLP is unmaintained,
  and LLM SRL needs heavy scaffolding to merely tie encoders. Useful only as candidate structuring.
- **SemBLEU-style BFS n-gram graph matching** — traversal-biased; dominated by WLK/WWLK on BAMBOO.
- **Original SPICE dependency-rule parser** — 13% set match on FACTUAL; superseded by fine-tuned seq2seq.

## Sources

- https://github.com/snowblink14/smatch
- https://github.com/flipz357/smatchpp
- https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00329/96472 (AMR Similarity Metrics from Principles / S2match)
- https://github.com/flipz357/amr-metric-suite
- https://doi.org/10.1162/tacl_a_00435 (Weisfeiler-Leman in the BAMBOO, TACL 2021)
- https://github.com/flipz357/weisfeiler-leman-amr-metrics
- https://github.com/flipz357/bamboo-amr-benchmark
- https://arxiv.org/abs/2306.00936 (AMR4NLI, IWCS 2023) / https://aclanthology.org/2023.iwcs-1.29/
- https://arxiv.org/abs/1607.08822 (SPICE) / https://github.com/peteanderson80/SPICE
- https://aclanthology.org/2023.findings-acl.398 (FACTUAL) / https://pypi.org/project/FactualSceneGraph/0.4.0/
- https://aclanthology.org/D19-1651.pdf (CaRB) / https://github.com/dair-iitd/CaRB
- https://aclanthology.org/2020.emnlp-main.306.pdf (OpenIE6)
- https://arxiv.org/abs/2412.18364 (Extracting triples from dialogues for conversational social agents)
- https://arxiv.org/pdf/2301.06841 (Syntactically robust training for OpenIE)
- https://ar5iv.labs.arxiv.org/html/2604.02866 (LLM atomic propositions help weak extractors)
- https://arxiv.org/abs/2404.03868 (EDC: Extract, Define, Canonicalize) / https://github.com/clear-nus/edc
- https://arxiv.org/pdf/2112.07790 (Maximum Bayes Smatch Ensemble Distillation)
- https://github.com/bjascob/amrlib
- https://github.com/IBM/transition-amr-parser
- https://aclanthology.org/2023.emnlp-main.662/ (GrAPES) / https://github.com/jgroschwitz/GrAPES
- https://aclanthology.org/2020.lrec-1.86.pdf (Dialogue-AMR)
- https://arxiv.org/html/2508.12819v1 (spontaneous French dialogue AMR corpus)
- https://www.isca-archive.org/interspeech_2023/addlesee23_interspeech.pdf (underspecified AMR for disrupted speech)
- https://medium.com/@sroukos/semantic-parsing-using-abstract-meaning-representation-95242518a380 (QALD genre-shift numbers)
- https://arxiv.org/html/2605.02505v1 (modernized SRL framework; AllenNLP maintenance-mode statement)
- https://github.com/fangfang123gh/LLM-SRL (ACL Findings 2025)
- https://arxiv.org/abs/2204.06508 (FactGraph) / https://arxiv.org/pdf/2311.09521 (AMRFact)
- https://arxiv.org/abs/2504.04473 (Directed graph-alignment for gaps in short answers)
- https://www.cambridge.org/core/journals/natural-language-engineering/article/automated-evaluation-of-the-quality-of-ideas-in-compositions-based-on-concept-maps/A3E7E2D9A5F49D444736D7035A087039
- https://arxiv.org/html/2208.08690v7 (OpenIE survey: rules → LLMs)
