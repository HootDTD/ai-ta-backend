# Adoptable open-source implementations for free-text → canonical-node resolution

Angle: concrete, maintained OSS we could plug into (or steal patterns from for) the Apollo tiered resolver. Researched 2026-07-07.

## Key findings

1. **MiniCheck — small grounding fact-checkers at GPT-4 level (EMNLP 2024).** `MiniCheck(document, claim) -> {0,1} + prob`. MiniCheck-Flan-T5-Large (770M) matches GPT-4 on the LLM-AggreFact benchmark at ~400x lower cost; repo Apache-2.0, models on HF (https://github.com/Liyan06/MiniCheck, https://arxiv.org/abs/2404.10774). Crucially it was trained on synthetic data that forces "synthesis of information across sentences" in the grounding document. Relevance: our NLI tier asks "does candidate statement entail node label" per parsed candidate; MiniCheck inverts this into "is the node's affirmative label supported by the transcript (chunk)" — a primitive purpose-built for exactly our check, robust to the concept being taught across several turns rather than in one clean sentence.
2. **MiniCheck's negative result: atomic claim decomposition is unnecessary.** Their ablation (Table 5, §7.1) shows decomposing claims into atomic facts gives near-zero or mixed gains while multiplying cost 2–4x. Relevance: we should not build a decomposition stage; check each reference-node label directly against transcript windows.
3. **SummaC — the granularity/aggregation lesson (TACL 2022).** NLI models fail when applied at document granularity; segmenting into sentence units, scoring all pairs, and max-aggregating per claim makes off-the-shelf NLI work for consistency detection (https://arxiv.org/pdf/2111.09525, https://github.com/tingofurro/summac). Relevance: this is the most likely diagnosis of our recall floor — if our NLI tier scores (single candidate sentence → node label) and the teaching evidence is spread across turns, entailment probability is structurally depressed. Score node-vs-every-window and take the max.
4. **AlignScore — unified alignment function, chunk+max aggregation (ACL 2023).** RoBERTa trained on 4.7M examples across 7 alignment-flavored tasks; splits context into ~chunks, takes max alignment per claim sentence (https://github.com/yuh-zha/AlignScore). Superseded by MiniCheck on LLM-AggreFact and repo is stale (2023) — useful as pattern, not as dependency.
5. **MoritzLaurer zeroshot-v2.0 family — 2-class entailment built for "hypothesis template" matching (MIT).** deberta-v3-{base,large}-zeroshot-v2.0 predict entailment vs not-entailment (not 3-class MNLI), trained on 27 tasks / 310 classes reformatted into universal NLI + synthetic data; `-c` variants are commercially-clean; explicit guidance that hypothesis-template phrasing materially changes scores (https://huggingface.co/MoritzLaurer/deberta-v3-large-zeroshot-v2.0). Relevance: our per-node "affirmative label" IS a hypothesis template; this family was trained for exactly that usage, and template A/B testing is a sanctioned, cheap recall lever. The classic `cross-encoder/nli-deberta-v3-base` (90.04 MNLI-mm) remains the 3-class baseline (https://sbert.net/docs/cross_encoder/pretrained_models.html).
6. **GLiNER / GLiNER2 — zero-shot span extraction with arbitrary, described labels (Apache-2.0, actively maintained).** GLiNER v0.2.27 (May 2026), 3.4k stars, bi-encoder variants for big label sets, `threshold=` is a direct recall dial (https://github.com/urchade/GLiNER). GLiNER2 (fastino-ai, 1.7k stars, Apache-2.0) adds schema-based multi-task extraction — entities *with natural-language descriptions*, classification, hierarchical structured extraction, relations — in one 205M/340M CPU-friendly model (https://github.com/fastino-ai/GLiNER2). Relevance: a recall-first candidate spotter — run over the transcript with node display-names + descriptions as label schema, low threshold, feed spans into the verifier tier.
7. **GLiREL — zero-shot relation extraction (NAACL 2025, Apache-2.0, PyPI v1.2.1).** Classifies arbitrary relation labels between entity pairs in one pass (https://github.com/jackboyla/GLiREL, https://arxiv.org/abs/2501.03172). Relevance: edge_coverage currently compounds node recall quadratically because it needs both endpoints independently resolved from parser edges. GLiREL lets us instead ask "does USES/DEPENDS_ON/PRECEDES hold between these two resolved mentions" directly over transcript text.
8. **GLiClass — single-forward-pass zero-shot multi-label classification (Knowledgator, Apache-2.0).** Scores a text against a whole label set at once, ~10x faster than cross-encoders; V3 large ≈ DeBERTa-v3-large cross-encoder quality (avg F1 0.70 vs 0.68) at 4x speed; docs show using it for NLI (premise=text, hypothesis=label) (https://github.com/Knowledgator/GLiClass, https://arxiv.org/abs/2508.07662). Relevance: score every utterance × every node label as one dense matrix, then per-node max over utterances — recall-friendly aggregation that's too expensive with per-pair cross-encoders.
9. **scispacy CandidateGenerator — the canonical recall-first alias matcher (AllenAI, Apache-2.0).** Entity linking via char-3gram TF-IDF vectors + approximate-nearest-neighbor search over ALL aliases of every KB concept, returning top-k candidates (no hard threshold gate); precision is left to a downstream disambiguation step. Custom `KnowledgeBase` is supported (jsonl of concepts+aliases; see issues #337/#361 and the KB refactor) (https://github.com/allenai/scispacy). spaCy's own `EntityLinker` API takes a custom KB + candidate-generation function (https://spacy.io/api/entitylinker/). Relevance: our fuzzy-lexical tier should become alias-set × char-ngram ANN candidate generation that ALWAYS emits top-k, with the NLI/clarification tiers doing rejection — matching the linking literature's division of labor.
10. **ASAG systems validate the two-stage "extract rubric evidence → score" shape but are not adoptable libraries.** AutoSCORE (EAAI 2026) uses a rubric-component-extraction agent producing a structured representation before a scoring agent, beating single-agent LLM grading on ASAP (https://github.com/AI4STEM-Education-Center/AutoSCORE). A 2025 multi-task DeBERTa ASAG repo adds an explicit per-key-concept multi-label coverage head + temperature-scaling calibration (https://github.com/huynhphtloi/AutomaticGrading). Both are research code, small datasets, unclear/no license review — patterns to copy, not deps.
11. **Open rubric-judge models exist if we want to replace the over-crediting GPT-4o audit with reproducible weights.** Prometheus 2 (7B/8x7B evaluator LMs, ICLR 2024 lineage) does fine-grained scoring against a custom rubric (https://github.com/prometheus-eval/prometheus-eval). Caveat: same over-credit risk as any LLM judge; value is reproducibility + fine-tunability on our adjudicated corpus, not inherent calibration.
12. **Constrained extraction: Outlines vs Instructor.** Outlines (dottxt-ai, Apache-2.0, 14.3k stars) constrains token generation to a grammar/JSON-schema — including enums — so a local model can only emit canonical node keys; Instructor (MIT, 13.2k stars) is Pydantic validation + retry over API models (https://github.com/dottxt-ai/outlines, https://github.com/567-labs/instructor). Relevance: make the LLM tier emit `node_key ∈ {enum of canonical keys} | "none"` per utterance instead of free-text that then needs re-resolution — closes a whole error class in the parser→resolver seam.

## Adoptable artifacts

| Artifact | License | Maintenance | Adoption shape for Apollo |
|---|---|---|---|
| `lytang/MiniCheck-Flan-T5-Large` (+DeBERTa/RoBERTa variants) via https://github.com/Liyan06/MiniCheck | Apache-2.0 (repo + <1B models); Bespoke-MiniCheck-7B needs commercial license | Active-ish (Ollama + Guardrails integrations; 214★) | New resolver tier: for each unresolved node, score(doc=transcript window(s), claim=node affirmative label); accept ≥ tuned threshold; sits between fuzzy and clarification tiers |
| `MoritzLaurer/deberta-v3-{base,large}-zeroshot-v2.0[-c]` | MIT | Model cards current; author at HF | Swap for current 3-class NLI checkpoint; re-tune threshold on our 31-attempt adjudicated corpus; A/B hypothesis templates per node type (concept vs procedure-step) |
| GLiNER https://github.com/urchade/GLiNER + GLiNER2 https://github.com/fastino-ai/GLiNER2 | Apache-2.0 both | Very active (GLiNER v0.2.27 May 2026; GLiNER2 11 releases) | Candidate spotter over transcript using node display-names + one-line descriptions as schema; threshold ~0.3 for recall; spans → verifier tier |
| GLiREL https://github.com/jackboyla/GLiREL (`glirel` on PyPI v1.2.1) | Apache-2.0 | Maintained (NAACL 2025 paper, 263★) | Edge tier: for each reference edge with both endpoint mentions located, classify relation labels over the containing window; replaces "parser must emit the edge" requirement |
| GLiClass https://github.com/Knowledgator/GLiClass | Apache-2.0 | Active (V3 models 2025; docs site) | Utterance × node-label dense scoring in single passes; per-node max-aggregation for coverage; CPU-viable |
| scispacy candidate generation https://github.com/allenai/scispacy (`create_tfidf_ann_index`, `CandidateGenerator`, custom `KnowledgeBase`) | Apache-2.0 | Maintained (AllenAI, v2.5.x) | Rebuild fuzzy tier: index all node aliases + display names as char-3gram TF-IDF ANN; always return top-k candidates per mention; no threshold gate at this tier |
| Outlines https://github.com/dottxt-ai/outlines / Instructor https://github.com/567-labs/instructor | Apache-2.0 / MIT | Both very active (14.3k★ / 13.2k★) | Constrain parser + LLM-resolver outputs to canonical-key enums; with GPT-4o, native structured outputs + enum may suffice (Instructor pattern), Outlines if we ever run local |
| SAF dataset + models https://huggingface.co/datasets/Short-Answer-Feedback/saf_communication_networks_english (Filighera et al., ACL 2022) | see card | Static | Fine-tuning/eval data: grades + rubric-linked feedback per response; closest public analog to "which rubric elements did the student hit" |
| ASAG2024 unified benchmark https://huggingface.co/datasets/Meyerger/ASAG2024 (7 datasets, common scale) | see card | Static (2024) | Off-the-shelf calibration/eval corpus for any resolver-scoring model before touching our own data |
| Prometheus 2 https://github.com/prometheus-eval/prometheus-eval | open weights (7B/8x7B) | Slowing (last big release 2024) | Optional: reproducible open judge to replace GPT-4o transcript audit; only worth it if we fine-tune on adjudicated transcripts |

## Recall lessons

- **Granularity is the first suspect, not the model.** SummaC: sentence-granularity scoring + max aggregation turns "broken" NLI into SOTA consistency detection. For us: score each node label against sliding windows of the transcript (1–3 turns), take max — never against one parsed candidate in isolation.
- **Candidate generation must be thresholdless top-k; precision belongs downstream.** scispacy/spaCy linking architecture: char-ngram ANN over aliases always emits k candidates; a separate disambiguator rejects. Our tier caps (fuzzy 0.80, llm 0.75) acting as hard gates *below* an 0.85 floor is the inverse of this design and is exactly the abstention bug we already hit once.
- **2-class entailment + template engineering.** Verification-style matching works better with entailment/not-entailment models than 3-class MNLI (MoritzLaurer v2.0 rationale); the phrasing of the per-node affirmative label is a tunable — the NLI-tier experiment already found labels must be affirmative; the literature says also test multiple templates per node and take max.
- **Thresholds are dataset-tuned, never universal.** MiniCheck defaults to t=0.5 but frames it as a knob; SummaC tunes per-benchmark on validation. We have 31 adjudicated attempts + ASAG2024/SAF as pretraining signal — tune per-tier thresholds (and per node-type) on held-out attempts, optimizing recall at fixed false-credit rate.
- **Aggregate per node over the whole transcript (max/any), not per candidate.** GLiClass-style dense utterance×label scoring makes "was node X taught anywhere" the primitive, which is intrinsically higher-recall than "did some parsed candidate resolve to X".
- **Don't build claim decomposition.** MiniCheck's ablation shows it adds cost, not accuracy, when the checker is trained for multi-fact synthesis.
- **Edges: classify relations directly, don't compound node recall.** Zero-shot RE (GLiREL/GLiNER2) conditioned on located mentions removes the quadratic dependence of edge_coverage on node resolution.

## Dead ends

- **Turnkey ASAG GitHub apps** (Auto-Answer-Grader, AutoGradeAI, answer-sheet evaluators): student/demo projects computing one holistic cosine similarity vs a reference answer — no per-concept coverage, no license/maintenance story. Pattern-free for us.
- **Large-KB neural entity linkers** (facebookresearch/BLINK, amazon-science/ReFinED, GENRE): engineered for millions of Wikipedia entities with learned candidate retrieval; our per-problem KB is ~5–20 nodes — the hard part they solve (candidate retrieval at scale) is trivial for us, and their training pipelines are heavy and stale.
- **microsoft/spacy-ann-linker**: same char-ngram ANN idea as scispacy but effectively unmaintained (docs/releases from ~2020–21); use scispacy's implementation instead.
- **AlignScore as a dependency**: strictly dominated by MiniCheck on the aggregate benchmark; repo frozen since 2023. Keep only its chunk+max aggregation idea.
- **Fine-tuning a DeBERTa grader on ASAP/SAS for holistic scores**: measures the wrong construct (single overall grade), and our failure mode is per-node recall, not final-score regression.
- **Bespoke-MiniCheck-7B in prod**: best accuracy in family but non-permissive commercial licensing (contact-us) — evaluate offline only; ship the Flan-T5-Large.

## Sources

- https://github.com/Liyan06/MiniCheck
- https://arxiv.org/abs/2404.10774
- https://huggingface.co/lytang/MiniCheck-Flan-T5-Large
- https://arxiv.org/pdf/2111.09525 (SummaC)
- https://github.com/yuh-zha/AlignScore
- https://huggingface.co/MoritzLaurer/deberta-v3-large-zeroshot-v2.0
- https://sbert.net/docs/cross_encoder/pretrained_models.html
- https://github.com/urchade/GLiNER
- https://github.com/fastino-ai/GLiNER2
- https://github.com/jackboyla/GLiREL / https://arxiv.org/abs/2501.03172
- https://github.com/Knowledgator/GLiClass / https://arxiv.org/abs/2508.07662
- https://github.com/allenai/scispacy
- https://spacy.io/api/entitylinker/
- https://microsoft.github.io/spacy-ann-linker/
- https://github.com/dottxt-ai/outlines
- https://github.com/567-labs/instructor
- https://github.com/AI4STEM-Education-Center/AutoSCORE
- https://github.com/huynhphtloi/AutomaticGrading
- https://huggingface.co/datasets/Short-Answer-Feedback/saf_communication_networks_english
- https://huggingface.co/datasets/Meyerger/ASAG2024
- https://github.com/prometheus-eval/prometheus-eval
