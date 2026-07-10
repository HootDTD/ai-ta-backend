# Recent surveys & terminology mapping (2023–2026)

Angle: name the standard terminology for "free student text → canonical concept/relation alignment,"
map which research communities own it, and list the load-bearing surveys. Bottom line: **no single
survey owns our problem — it is fragmented across ≥5 communities under ≥6 different names.** The names
matter: they are the search keys the other angles should use.

## Key findings

### The terminology map (our problem, per community)

1. **"Expectation coverage" / "semantic match" in EMT dialogue** — AIED/ITS community (AutoTutor lineage,
   Graesser et al.). AutoTutor's Expectation-and-Misconception-Tailored (EMT) dialogue is *structurally
   identical to Apollo*: a curriculum script holds 3–7 "expectations" (≈ our reference nodes) +
   misconceptions per problem; student contributions are matched by LSA cosine + RegEx against each
   expectation; an expectation is "covered" when a threshold (historically 0.40–0.85) is met; uncovered
   expectations trigger targeted hints/prompts. 25 years of published engineering on exactly our resolver.
   (AutoTutor chapter: https://blogs.memphis.edu/aolney/files/2019/10/AutoTutor-chapter-olney_publications.pdf ;
   Graesser et al. 2000: https://doi.org/10.1076/1049-4820(200008)8:2;1-b;ft129)
2. **"Student response analysis" (SRA)** — educational-NLP + textual-entailment communities.
   SemEval-2013 Task 7 (Dzikovska et al.) fixed the benchmark name, datasets (Beetle, SciEntsBank), and a
   5-way label taxonomy: correct / partially_correct_incomplete / contradictory / irrelevant / non_domain.
   9 teams; framed explicitly as an RTE (entailment) task. (https://aclanthology.org/S13-2045/)
3. **"Reference answer facets" / facet-based entailment** — Nielsen, Ward & Martin (Natural Language
   Engineering 2009) decompose each reference answer into fine-grained word-pair "facets" and label each
   facet understood / contradicted / unaddressed — i.e., node-level resolution with a 3-way outcome instead
   of our binary resolved/unresolved. (https://www.cambridge.org/core/journals/natural-language-engineering/article/abs/recognizing-entailment-in-intelligent-tutoring-systems/C4925CE6B644A53F5B3D460328126BD1)
4. **"Entity linking" / "concept normalization"** — (Bio)NLP. Mapping free-text mentions to canonical
   ontology concepts is the most mature instance of our task. Surveys: "A Survey on Multilingual Clinical
   Entity Linking in the LLM Age" (2023–2025 methods; Springer 2026,
   https://link.springer.com/chapter/10.1007/978-3-032-30153-6_3) and "Systematic Review of Named Entity
   Linking and Knowledge Organisation Systems in Biomedical and Clinical Domain" (ACM 2025,
   https://dl.acm.org/doi/full/10.1145/3796222). Consensus architecture: NER → **high-recall candidate
   generation** → precision reranking; LLMs fail at direct linking against large concept spaces but are
   effective *rerankers* and term standardizers.
5. **"Key point matching" (KPA)** — argument-mining community. KPA-2021 shared task (ArgMining@EMNLP):
   Matching Track = map each free-text statement to a small canonical key-point set, with a confidence
   score and an explicit "no matching KP" outcome; evaluated by mAP with strict/relaxed handling of
   undecided labels. 17 teams; contrastive-learning matchers won.
   (https://aclanthology.org/2021.argmining-1.16/ ; Bar-Haim et al. 2020: https://arxiv.org/abs/2010.05369)
6. **"OKB canonicalization"** — KG community's name for clustering synonymous noun/relation phrases into
   canonical identifiers post-extraction (CESI, SIST, CMVC). Overview: "Open Knowledge Base
   Canonicalization: Techniques and Challenges" (Text2KG@ESWC 2024, https://ceur-ws.org/Vol-3747/text2kg_paper5.pdf).
7. **"Knowledge component (KC) tagging" / "concept tagging" / Q-matrix** — EDM/LAK community. LLM-based KC
   tagging is hot in 2024–25 (arXiv 2403.17281, 2405.20526, 2410.01727) but tags *questions/items*, not
   live student utterances — a terminology source, not a drop-in method.

### The 10 most load-bearing surveys (what each covers)

1. **Burrows, Gurevych & Stein 2015, "The Eras and Trends of Automatic Short Answer Grading"** (IJAIED;
   ~380 cites) — canonical ASAG survey; 35 systems, 5 eras (incl. an explicit *concept-mapping era* —
   grading by mapping answers onto expected concepts). (https://www.semanticscholar.org/paper/6404b29ac83a69670f1dd4b887e026bfbd844d83)
2. **Haller et al. 2022, "Survey on Automated Short Answer Grading with Deep Learning"**
   (arXiv:2204.03503) — bridges embeddings → transformers for ASAG. (https://arxiv.org/abs/2204.03503)
3. **Frederick Eneye et al. 2025, "Advances in Auto-Grading with Large Language Models: A
   Cross-Disciplinary Survey"** (BEA@ACL 2025) — LLM grading across six sub-fields; headline finding:
   inconsistency and the need for human oversight persist. (https://aclanthology.org/2025.bea-1.35/)
4. **Paladines & Ramírez 2020, "A Systematic Literature Review of ITSs With Dialogue in Natural
   Language"** (IEEE Access) — 33 dialogue ITSs over 20 years; most implement the EMT approach; classifies
   NLU as symbolic vs statistical vs hybrid; hybrids recommended. (https://oa.upm.es/78939/)
5. **Maurya et al. 2025, "Unifying AI Tutor Evaluation"** (NAACL 2025) — 8-dimension pedagogical taxonomy
   + MRBench (192 dialogues, 1,596 responses); spawned the BEA 2025 shared task on mistake
   identification in tutor–student dialogue. (https://aclanthology.org/2025.naacl-long.57/)
6. **Maurya & Kochmar 2025, "Pedagogy-driven Evaluation of GenAI-powered ITSs"** (arXiv:2510.22581) —
   state of ITS evaluation practice; documents the absence of standardized benchmarks. (https://arxiv.org/abs/2510.22581)
7. **Zhong et al. 2023, "A Comprehensive Survey on Automatic Knowledge Graph Construction"** (ACM
   Computing Surveys 56(4); 300+ methods) — the KGC reference; fixes vocabulary: entity discovery/typing/
   linking, coreference, relation extraction, knowledge fusion. (https://arxiv.org/abs/2302.05019)
8. **"LLM-empowered knowledge graph construction: A survey"** (arXiv:2510.20345, Oct 2025) — the
   2023–25 LLM-era KGC update (schema-guided vs open extraction, canonicalization).
   (https://arxiv.org/html/2510.20345v1) Companion scoping review of 126 studies (SBBD 2025) finds
   persistent **low precision + manual curation** across all four method families.
   (https://sol.sbc.org.br/index.php/sbbd_estendido/article/download/37613/37395/)
9. **Wang et al. 2024, "Large Language Models for Education: A Survey and Outlook"** (arXiv:2403.18105) —
   technology-centric taxonomy + educational datasets/benchmarks table; and **Yan et al. 2024** systematic
   scoping review (118 papers, 53 use cases incl. grading + knowledge representation)
   (https://arxiv.org/abs/2303.13379).
10. **Clinical entity-linking surveys** (items 4 above) — the recall/precision architecture playbook.

Adjacent, thinner threads: concept-map assessment ("concept map mining"; automated CM scoring — IEEE TLT
2021 https://doi.org/10.1109/tlt.2021.3103331 , Waterloo Rubric https://doi.org/10.1109/access.2021.3124672 ,
RBIE 2019 mapping study https://doi.org/10.5753/rbie.2019.27.03.150); educational-KG-construction SLR
(Heliyon 2024, https://www.sciencedirect.com/science/article/pii/S2405844024014142); concept extraction +
prerequisite-dependency detection survey (≈ our PRECEDES edges).

**Search-term glossary for future queries:** student response analysis; reference answer facets; answer
assessment; expectation coverage / EMT dialogue; (medical) concept normalization; entity linking candidate
generation recall@k; key point matching; OKB canonicalization; knowledge component tagging; analytic
content scoring; concept map mining.

## Adoptable artifacts

- **SemEval-2013 Task 7 data (Beetle + SciEntsBank)** — labeled student answers vs reference answers with
  the 5-way taxonomy; copies circulate on HuggingFace (license per original task release). Use: calibration
  corpus + richer label scheme for our resolver outcomes. (https://aclanthology.org/S13-2045/)
- **EDC — Extract, Define, Canonicalize** (EMNLP 2024; code https://github.com/clear-nus/edc, public,
  actively cited) — LLM OIE → schema definition → canonicalization against a target schema, plus a trained
  **schema retriever** (recall@10 0.66–0.82) that feeds relevant schema slices into extraction. Use: replace
  "open-parse then hope the resolver matches" with schema-aware parsing against our per-problem node set.
- **xMEN** (https://github.com/hpi-dhc/xmen, Apache-2.0; JAMIA Open 2024/25) — modular multilingual
  concept-normalization toolkit: dictionary+embedding candidate generation, trainable reranker. Use: template
  for restructuring our tiers into candidate-gen/rerank with per-stage recall metrics.
- **ArgKP-2021 dataset + KPA matching recipe** (~24K argument–KP pairs; RoBERTa/contrastive matchers)
  (https://aclanthology.org/2021.argmining-1.16/). Use: fine-tuning data shape + mAP-with-abstention eval
  protocol for statement→node matching.
- **MRBench** (NAACL 2025) — benchmark for tutor-response quality; relevant to Apollo's dialogue side, not
  the resolver. (https://aclanthology.org/2025.naacl-long.57/)
- **LLM KC-tagging pipelines** (arXiv 2405.20526, 2403.17281, 2410.01727) — prompt/verification patterns
  for concept tagging with weak supervision.

## Recall lessons

- **Recall is achieved through dialogue, not matcher perfection.** AutoTutor's answer to sub-threshold
  expectations is hints/prompts targeting the missing content words ("pattern completion"), and coverage is
  **cumulative across all turns** (AutoTutor Lite's CO score aggregates evidence over the whole session).
  This is exactly Apollo's clarification-loop design — validated prior art, not a novelty.
- **Static thresholds are known-wrong.** Penumatsa et al. 2006: best agreement with experts when the LSA
  cosine threshold is a **function of the lengths of both student answer and expectation**
  (https://doi.org/10.1142/s021821300600293x). Our fixed, precision-first NLI thresholds repeat a mistake
  the field corrected in 2006.
- **Split recall from precision architecturally.** Clinical EL consensus: a high-recall candidate generator
  (dictionary + embedding, top-k) followed by a precision reranker (now often an LLM); measure recall@k of
  the generator separately. Our tiered resolver conflates the two — we cannot currently say whether misses
  are candidate-generation or adjudication failures.
- **Ensembles of lexical + semantic matchers are standard** (AutoTutor: RegEx + LSA). Even mature systems
  plateau below humans (machine–human κ≈.49 vs human–human κ≈.70 — Carmon,
  https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=3256&context=etd): design grading to
  tolerate imperfect recall (partial credit, abstention) rather than assume it away.
- **Richer outcome labels preserve signal.** SemEval's partially_correct_incomplete vs contradictory vs
  irrelevant, and Nielsen's per-facet understood/contradicted/unaddressed, both beat binary
  resolved/unresolved — contradiction and partial coverage should be first-class resolver outputs.
- **Abstention is part of the task definition.** KPA scores match confidence with mAP and an explicit
  no-match option; ~5% multi-label simplification; strict/relaxed scoring for undecided gold labels.

## Dead ends

- **Hunting for "the" survey of our exact problem** — none exists; transcript→reference-KG alignment for
  grading sits in the seams between ASAG, SRA, EL, KPA, and KGC. Fastest path is adopting EL/KPA machinery,
  not waiting for the education literature to name it.
- **Direct one-shot LLM linking against the whole concept inventory** — clinical EL surveys report it
  underperforms due to search-space size; LLMs belong in reranking/standardization.
- **LLM-as-judge for fine-grained coverage/mistake judgments** — UCL EVAL-LAC 2025 pilot: κ≈0.2 vs humans
  on mistake identification (https://discovery.ucl.ac.uk/id/eprint/10212920/); consistent with our
  over-crediting transcript audit. Surface qualities score well; fine-grained crediting does not.
- **BLEU/ROUGE/BERTScore-style reference similarity** for tutoring assessment — repeatedly rejected by the
  tutor-evaluation literature (NAACL 2025; EVAL-LAC 2025).
- **Classical OKB canonicalization clustering (CESI/CMVC)** — built for corpus-scale dedup of open triples;
  sparsity-sensitive; wrong shape for our small per-problem graphs. LLM-based canonicalization (EDC) supersedes it.
- **KC-tagging literature as a method source** — it labels items/questions, not student utterances mid-dialogue;
  mine it for terminology and prompt patterns only.

## Sources

- https://aclanthology.org/S13-2045/ — SemEval-2013 Task 7 (student response analysis; Beetle/SciEntsBank)
- https://www.cambridge.org/core/journals/natural-language-engineering/article/abs/recognizing-entailment-in-intelligent-tutoring-systems/C4925CE6B644A53F5B3D460328126BD1 — Nielsen et al., facet-based entailment
- https://blogs.memphis.edu/aolney/files/2019/10/AutoTutor-chapter-olney_publications.pdf — AutoTutor EMT dialogue + LSA thresholds
- https://doi.org/10.1076/1049-4820(200008)8:2;1-b;ft129 — Graesser et al. 2000, LSA evaluation of student contributions
- https://doi.org/10.1142/s021821300600293x — Penumatsa et al. 2006, length-adaptive cosine thresholds
- https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=3256&context=etd — Carmon, LSA+RegEx semantic-match agreement
- https://www.semanticscholar.org/paper/6404b29ac83a69670f1dd4b887e026bfbd844d83 — Burrows et al. 2015, ASAG eras
- https://arxiv.org/abs/2204.03503 — Haller et al. 2022, deep-learning ASAG survey
- https://aclanthology.org/2025.bea-1.35/ — BEA 2025 cross-disciplinary LLM auto-grading survey
- https://oa.upm.es/78939/ — Paladines & Ramírez, dialogue-ITS SLR (EMT prevalence)
- https://aclanthology.org/2025.naacl-long.57/ — Maurya et al., unified AI-tutor evaluation taxonomy + MRBench
- https://arxiv.org/abs/2510.22581 — Maurya & Kochmar, pedagogy-driven ITS evaluation
- https://aclanthology.org/2025.bea-1.87.pdf — BEA 2025 shared task (mistake identification) participant paper
- https://arxiv.org/abs/2302.05019 — Zhong et al., ACM CSUR KGC survey
- https://arxiv.org/html/2510.20345v1 — LLM-empowered KGC survey (2025)
- https://sol.sbc.org.br/index.php/sbbd_estendido/article/download/37613/37395/ — KGC-with-LLMs scoping review (126 studies)
- https://ceur-ws.org/Vol-3747/text2kg_paper5.pdf — OKB canonicalization overview
- https://aclanthology.org/2024.emnlp-main.548/ + https://github.com/clear-nus/edc — EDC framework
- https://link.springer.com/chapter/10.1007/978-3-032-30153-6_3 — multilingual clinical entity linking in the LLM age
- https://dl.acm.org/doi/full/10.1145/3796222 — systematic review, biomedical named entity linking + KOS
- https://doi.org/10.1093/jamiaopen/ooae147 + https://github.com/hpi-dhc/xmen — xMEN concept-normalization toolkit
- https://aclanthology.org/2021.argmining-1.16/ — KPA-2021 shared task overview (key point matching)
- https://arxiv.org/abs/2010.05369 — Bar-Haim et al., cross-domain key point analysis
- https://arxiv.org/abs/2403.18105 — Wang et al., LLMs for Education: Survey and Outlook
- https://arxiv.org/abs/2303.13379 — Yan et al., LLMs-in-education scoping review (53 use cases)
- https://arxiv.org/abs/2507.18882 — Zerkouk et al., AI-based ITS comprehensive review
- https://arxiv.org/pdf/2403.17281 / https://arxiv.org/pdf/2405.20526 / https://arxiv.org/pdf/2410.01727 — LLM KC/concept tagging
- https://www.sciencedirect.com/science/article/pii/S2405844024014142 — educational KG construction SLR
- https://doi.org/10.1109/tlt.2021.3103331 , https://doi.org/10.1109/access.2021.3124672 , https://doi.org/10.5753/rbie.2019.27.03.150 — automated concept-map assessment
