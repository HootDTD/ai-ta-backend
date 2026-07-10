# NLI / embedding alignment & threshold calibration — prior art for Apollo node resolution

Researched 2026-07-07. Question: how does the literature align free text against canonical reference
claims with NLI/embeddings, and how do you tune for RECALL without collapsing precision?

## Key findings

1. **Granularity mismatch is the #1 failure mode — and the fix is hypothesis-centric max-aggregation
   (SummaC).** NLI models are trained on sentence pairs; fed document-level premises they degrade badly.
   SummaC segments the source into sentence units, scores every (source-sentence, claim-sentence) pair,
   takes **max over source sentences** per claim, then mean over claims; this alone produced SOTA
   inconsistency detection (74.4% balanced acc, +5pp) with *zero training* (SummaC-ZS)
   (https://arxiv.org/abs/2111.09525, TACL 2022). Relevance: our resolver is **candidate-centric** —
   a node is only matched if the parser extracted a candidate for it, so every parser miss is an
   unrecoverable recall loss. Standard practice inverts this: for **each reference node**, score its
   hypothesis against **all transcript windows** and take the max. The parser stops being the recall
   bottleneck.
2. **Chunk-level premises beat sentence-level premises (AlignScore).** AlignScore splits context into
   ~350-token chunks (not sentences), max-pools alignment per claim-sentence over chunks, then averages
   (https://arxiv.org/abs/2305.16739; https://github.com/yuh-zha/AlignScore). Evidence for one concept
   is often spread over several sentences/turns; sentence-only premises lose it. Later work confirms
   chunk-level scoring outperforms sentence-level for document entailment and needs fewer model calls
   (https://arxiv.org/pdf/2310.13189).
3. **Small specialized checkers now match GPT-4 (MiniCheck, EMNLP 2024).** MiniCheck-FT5 (Flan-T5-large,
   770M) reaches GPT-4 accuracy on the LLM-AggreFact benchmark at ~400x lower cost and beats AlignScore
   by 4.3% overall (https://arxiv.org/abs/2404.10774; https://github.com/Liyan06/MiniCheck). Trained on
   GPT-4-synthesized data specifically containing multi-fact sentences requiring **multi-sentence evidence
   aggregation** — exactly our "node taught across several turns" case.
4. **Hypothesis wording swings zero-shot NLI accuracy by >12pp; multiple hypotheses per class add 8-10pp
   more.** Goldzycher & Schneider tested 24 template variants: best ("That contains hate speech.") 79.4%
   vs. a near-identical variant 66.6%; combining predictions from multiple targeted hypotheses gained
   +7.9/+10.0pp on HateCheck/ETHOS (https://arxiv.org/abs/2210.00910). Yin et al. 2019 founded this
   "label verbalization" paradigm. Relevance: our single affirmative label per node is one point in a
   very sensitive design space — several paraphrase hypotheses per node, max-aggregated, is the
   literature-standard recall lever.
5. **Direction and score formula matter (MENLI, TACL 2023).** Plain entailment probability `e` was the
   best formula (beats e−c etc.); the optimal premise→hypothesis direction is task-dependent; and NLI
   metrics should be **linearly interpolated with embedding-based metrics** (w_nli 0.2–0.8) — the combo
   improved BOTH adversarial robustness (+15–30%) and standard quality (+5–30%)
   (https://arxiv.org/abs/2208.07316). NLI and embedding similarity fail on complementary phenomena
   (NLI: fluency/paraphrase; embeddings: negation/numbers/names), so score fusion beats our either/or
   tier ladder with per-tier caps.
6. **NLI-as-metric was already validated at benchmark scale (TRUE, 2022).** Across 11 datasets spanning
   summarization, dialogue, and fact verification, NLI-based metrics (T5-11B on ANLI) were the top
   performers (https://arxiv.org/pdf/2204.04991). The approach is sound; the engineering around
   granularity/thresholds is what determines recall.
7. **Calibration: in-domain temperature scaling works; out-of-domain scores drift badly (Desai & Durrett,
   EMNLP 2020).** Pretrained transformers on NLI are roughly calibrated in-domain and temperature scaling
   further reduces ECE, but out-of-domain (our case: tutoring dialogue vs. MNLI/news) calibration error
   grows up to 3.5x (https://arxiv.org/abs/2003.07892). Practical corollary observed repeatedly: some
   models (e.g., mDeBERTa) emit 0.66–0.96 entailment for *everything* under certain hypothesis styles —
   **calibration quality, not accuracy, decides whether a threshold is even usable**
   (https://arxiv.org/pdf/2506.01156). Recalibrate on our own labeled transcripts before setting floors.
8. **Thresholds are always tuned per-task on a dev set — tiny dev sets suffice.** SummaC tunes the
   decision threshold per dataset on validation. Schuster et al. tuned on just 0.2% of DocNLI dev and
   the optimal threshold jumped to 0.95 (https://ar5iv.labs.arxiv.org/html/2204.07447). Nobody ships a
   universal 0.75/0.80/0.85 constant. Conformal risk control gives distribution-free threshold selection
   with an explicit guaranteed bound on (e.g.) false-negative rate — a principled way to set a
   recall-targeted floor (https://arxiv.org/pdf/2208.02814; survey
   https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00715/125278).
9. **Confidence-gated cascade: small NLI resolves the easy 40%, LLM only sees the ambiguous rest.**
   A 2026 study: when NLI confidence > 0.99, its accuracy is 98.5% (FEVER) / 90.1% (SciFact-Open) —
   as reliable as the LLM; accuracy collapses to ~68% in the 0.90–0.95 band. The NLI-then-LLM hybrid
   slightly *beats* pure-LLM while cutting LLM calls ~40% (https://arxiv.org/pdf/2601.22984). This is
   our tier architecture done right: gate on **calibrated confidence**, not on method identity with
   hand-set per-tier score caps — and it scopes the over-crediting LLM audit down to escalated cases.
10. **Dialogue claims must be decontextualized before matching.** 42.1% of DialFact claims contain
    in-claim pronouns; unresolved references directly damage retrieval and verification
    (https://aclanthology.org/2022.acl-long.263.pdf). Choi et al. 2021 defines the rewrite operation
    (pronoun/NP swap, bridging, discourse-marker removal) (https://arxiv.org/abs/2102.05169);
    decomposition + decontextualization improves entailment verification at test time (WiCE Claim-Split,
    GPT-3.5 decomposition, https://arxiv.org/abs/2303.01432; DnDScore https://arxiv.org/pdf/2412.13175).
    Our parser candidates from tutoring dialogue ("so it stays the same because of that") are exactly
    this failure class.
11. **Bidirectional entailment = equivalence, one-directional = coverage.** Semantic-entropy work
    clusters meanings by requiring entailment ≥0.5 in BOTH directions (https://arxiv.org/pdf/2302.09664).
    For coverage grading we need only student-text → node-label; requiring the reverse (label → student
    text) would collapse recall since student utterances carry extra content.
12. **NLI is brittle around negation; affirmative hypotheses are correct.** Models trained on standard
    NLI fail systematically on negated examples and lean on negation-word heuristics
    (https://arxiv.org/pdf/2004.14623; https://arxiv.org/pdf/2306.08189). This independently confirms
    our finding that node labels must be affirmative and that polarity handling (litotes) needs its own
    guard, not a smarter template.

## Adoptable artifacts

- **MiniCheck** — https://github.com/Liyan06/MiniCheck. Apache-2.0, maintained (Ollama + Guardrails
  integrations, 2024+). Models: RoBERTa-L / DeBERTa-v3-L / Flan-T5-L (770M, best <1B) / Bespoke-MiniCheck-7B.
  Adoption: new resolver tier scoring each node hypothesis against transcript chunks; sentence-level
  claims required (fits our per-node labels).
- **AlignScore** — https://github.com/yuh-zha/AlignScore. MIT, checkpoints on HF (base 125M / large 355M).
  Adoption: drop-in `score(context=transcript_window, claim=node_label)`; its 350-token chunk + max/mean
  aggregation code is directly liftable even if we keep our own model.
- **Vectara HHEM-2.1-Open** — https://huggingface.co/vectara/hallucination_evaluation_model. Apache-2.0,
  DeBERTa-v3-base, unlimited input length, de-facto open RAG consistency scorer. Adoption: cheapest
  pairwise scorer for the cascade's first stage.
- **MoritzLaurer deberta-v3-{base,large}-zeroshot-v2.0** —
  https://huggingface.co/MoritzLaurer/deberta-v3-large-zeroshot-v2.0. MIT ("-c" variants trained only on
  commercially clean data). Binary entailment/not_entailment head purpose-built for "This text is about /
  explains {label}" hypotheses; model card explicitly recommends testing multiple hypothesis templates.
- **MENLI** — https://github.com/cyr19/MENLI (also on PyPI). Reference implementation of NLI+embedding
  score interpolation and direction/formula ablations.
- **FIZZ** — https://github.com/plm3332/FIZZ. Apache-2.0, EMNLP 2024, limited maintenance. Worth mining
  for its atomic-fact decomposition + granularity-expansion prompts rather than adopting wholesale.
- **Conformal risk control** — https://arxiv.org/pdf/2208.02814 (Angelopoulos et al.; reference code by
  the authors on GitHub). Adoption: calibrate the abstention floor to a target false-negative bound on a
  held-out labeled transcript set instead of hand-picking 0.85.

## Recall lessons

- **Invert the loop:** for each reference node, scan ALL transcript windows and max-aggregate — never let
  extraction candidates gate which nodes can resolve (SummaC/AlignScore/MiniCheck all do this).
- **Windows over sentences:** ~350-token overlapping chunks of the student's turns capture multi-turn
  evidence; sentence-only premises systematically under-recall (AlignScore, arXiv 2310.13189).
- **Decontextualize candidates** (resolve pronouns/ellipsis using dialogue context, LLM rewrite) before
  any matching tier; 40%+ of dialogue claims need it (DialFact, Choi 2021, WiCE).
- **Multiple affirmative hypotheses per node** (3-5 paraphrases incl. a definition-style and an
  application-style phrasing), max-aggregated; wording is worth ~12pp and must be tested, not intuited.
- **Calibrate, then threshold:** temperature-scale the NLI head on a labeled dev set of our transcripts;
  set per-node-type thresholds by optimizing a recall-weighted objective (F2) or a conformal FNR bound.
  Tiny dev sets (tens of examples) already move optimal thresholds dramatically.
- **Fuse scores instead of tier caps:** interpolate embedding similarity and NLI entailment (MENLI) so a
  moderately-confident NLI + high lexical overlap can clear the bar that neither clears alone.
- **Cascade by confidence:** trust the small model above a strict calibrated bar (~0.99-grade), send the
  gray zone to an LLM verifier that must quote its evidence window (evidence-scoped, unlike our global
  transcript audit that over-credits).
- **Partial credit exists in the data:** WiCE shows real claims are often *partially* supported —
  sub-claim-level entailment supports fractional node credit rather than binary resolved/unresolved.

## Dead ends

- **Whole-transcript premise / document-level NLI (DocNLI-style):** performance degrades with premise
  length and long-range attention does not rescue it (arXiv 2204.07447, 2310.13189). Don't buy a
  long-context NLI model; fix granularity instead.
- **Bidirectional entailment as the resolution criterion:** correct for equivalence clustering, fatal for
  coverage recall (student text ⊃ label content).
- **Negated or clever hypothesis templates:** NLI negation brittleness is systematic; keep hypotheses
  affirmative and simple.
- **Pure embedding similarity as a scorer or premise retriever:** neutral sentences can be highly similar
  ("uninformative but similar" — Schuster et al.); fine as a recall-stage candidate generator, never as
  the decision layer.
- **Trusting raw softmax scores out-of-domain:** fixed global thresholds on uncalibrated scores is
  exactly the trap our 0.85 floor fell into; some model/hypothesis combos make thresholds meaningless
  regardless of accuracy.
- **Scaling to T5-11B-class NLI (TRUE's SOTA):** matched/beaten by 770M MiniCheck-tier models at a
  fraction of cost; no reason to host an 11B checker in 2026.
- **Thin area (honest gap):** almost nothing published on NLI alignment against *knowledge-graph node
  labels* specifically (vs. free-text claims), and ASAG-entailment work (Basak et al. 2019 etc.) predates
  modern checkers — our node-label-as-hypothesis setup is standard zero-shot NLI practice, but per-node
  calibration for KG coverage grading appears genuinely novel/unstudied.

## Sources

- https://arxiv.org/abs/2111.09525 (SummaC, TACL 2022)
- https://arxiv.org/abs/2305.16739 + https://github.com/yuh-zha/AlignScore (AlignScore, ACL 2023)
- https://arxiv.org/abs/2404.10774 + https://github.com/Liyan06/MiniCheck (MiniCheck, EMNLP 2024)
- https://arxiv.org/abs/2208.07316 + https://github.com/cyr19/MENLI (MENLI, TACL 2023)
- https://arxiv.org/abs/2210.00910 (Hypothesis Engineering for Zero-Shot Hate Speech Detection)
- https://arxiv.org/pdf/2204.04991 (TRUE benchmark)
- https://arxiv.org/abs/2003.07892 (Calibration of Pre-trained Transformers, EMNLP 2020)
- https://ar5iv.labs.arxiv.org/html/2204.07447 (Stretching Sentence-pair NLI Models, Schuster et al.)
- https://arxiv.org/pdf/2310.13189 (Fast & Accurate Factual Inconsistency Detection Over Long Documents)
- https://arxiv.org/pdf/2601.22984 (NLI-gatekeeper + LLM cascade for claim verification, 2026)
- https://aclanthology.org/2022.acl-long.263.pdf (DialFact)
- https://aclanthology.org/2021.emnlp-main.619/ (Q², dialogue factual consistency)
- https://arxiv.org/abs/2102.05169 (Decontextualization, Choi et al. 2021)
- https://arxiv.org/abs/2303.01432 (WiCE + Claim-Split)
- https://arxiv.org/pdf/2412.13175 (DnDScore: decontextualization + decomposition)
- https://arxiv.org/html/2404.11184 (FIZZ) + https://github.com/plm3332/FIZZ
- https://arxiv.org/pdf/2302.09664 (Semantic uncertainty / bidirectional entailment clustering)
- https://arxiv.org/pdf/2004.14623 (NLI + negation, MoNLI) ; https://arxiv.org/pdf/2306.08189 (LMs on negation)
- https://arxiv.org/pdf/2208.02814 (Conformal Risk Control) ;
  https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00715/125278 (Conformal prediction for NLP survey)
- https://huggingface.co/MoritzLaurer/deberta-v3-large-zeroshot-v2.0 (zeroshot-v2.0 model card)
- https://huggingface.co/vectara/hallucination_evaluation_model + https://www.vectara.com/blog/hhem-2-1-a-better-hallucination-detection-model (HHEM-2.1-Open)
- https://arxiv.org/pdf/2506.01156 (calibration > accuracy for NLI thresholding, mDeBERTa example)
- https://machinelearningmastery.com/threshold-moving-for-imbalanced-classification/ (threshold moving / F2)
