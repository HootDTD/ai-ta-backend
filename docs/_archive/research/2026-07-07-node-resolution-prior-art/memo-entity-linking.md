# Entity / Concept Linking to a Closed KB — prior art for Apollo node resolution

Angle: treat Apollo's "candidate statement → canonical reference node" step as **entity linking (EL)
to a tiny closed KB** (~5–20 nodes/problem). The EL field has 15 years of recall-oriented
architecture we can lift wholesale. Core reframing below: our node set is so small that the
classic candidate-generation bottleneck **inverts** — we should score every node with the
expensive matcher and spend our engineering on threshold calibration + multi-alias views, not on
retrieval recall.

## Key findings

- **Standard recall architecture = high-recall candidate generation + precision reranking (BLINK).**
  BLINK (Wu et al., EMNLP 2020) is the canonical two-stage EL system: a bi-encoder retrieves top-k
  candidates in dense space, a cross-encoder reranks by concatenating mention+entity text.
  https://aclanthology.org/2020.emnlp-main.519.pdf ,
  https://github.com/facebookresearch/BLINK (MIT, **archived 2024-03-01**).
  Relevance: our resolver is already this shape (tiers = candidate gen; NLI/clarify = rerank), but
  we hard-threshold at the *candidate* stage, which the architecture explicitly avoids — candidate
  generation is meant to be generous and recall-first.

- **Candidate generation sets the CEILING for the whole system.** On ZESHEL, BLINK bi-encoder
  recall@64 = 82.1% (test) vs BM25 69.1%; every downstream number is capped by this.
  https://arxiv.org/pdf/1911.03814 ; original ZESHEL BM25 top-64 recall = 68% (Logeswaran et al.,
  ACL 2019, https://aclanthology.org/P19-1335/). Relevance: our node_coverage 0.20 on *provably-
  taught* concepts means we are losing recall at the match/threshold step, not at reranking — the
  literature says fix the recall stage first because nothing downstream can recover it.

- **For a tiny KB the bottleneck INVERTS — score all nodes, skip retrieval.** All the heavy
  candidate-gen machinery (FAISS over 5.9M entities in BLINK) exists because KBs are huge. With
  5–20 nodes you can run the *cross-encoder / NLI / LLM against every node* — there is no recall
  loss from retrieval at all, and the problem collapses to a per-node scoring + calibration
  problem. This is the single most load-bearing insight for us.

- **Multi-view / multi-alias entity representations raise recall (MuVER).** MuVER (Ma et al.,
  EMNLP 2021, https://aclanthology.org/2021.emnlp-main.205.pdf) splits each entity description into
  multiple "views" and scores a mention by the **max over views**, lifting ZESHEL recall@64
  markedly over single-vector BLINK. Relevance: our NLI tier uses ONE affirmative label per node —
  that is single-view. Give each reference node many affirmative surface forms / paraphrases and
  take max-over-forms; this directly attacks the "strong explanation resolves 1/5" failure.

- **Self-alignment pretraining pulls synonyms together (SapBERT).** SapBERT (Liu et al., NAACL
  2021) uses metric learning over UMLS synonym pairs; *unsupervised* Acc@1 = 92.0% NCBI, 93.5%
  BC5CDR, 96.5% MedMentions — beating supervised SOTA. https://aclanthology.org/2021.naacl-main.334.pdf ,
  model cambridgeltl/SapBERT-from-PubMedBERT-fulltext (Apache-2.0/MIT),
  https://github.com/cambridgeltl/sapbert . Relevance: a synonym-aligned encoder makes paraphrased
  student statements land near the canonical node embedding without hand-authoring aliases; the
  training recipe (positive = synonyms of same concept) is portable to our physics/econ nodes.

- **Generative linking with constrained decoding over a custom trie (GENRE).** GENRE (De Cao et
  al., ICLR 2021) generates the entity's unique name token-by-token under a prefix-trie constraint
  so only valid KB names can be produced. https://openreview.net/pdf?id=5k8F6UU39V ,
  https://github.com/facebookresearch/GENRE (**CC-BY-NC 4.0**, archived 2025-07-17; supports
  user-built tries). Relevance: a trie over our 5–20 node names is trivial and guarantees outputs
  are canonical — but the "entity has a unique NAME" assumption is weak for procedure-step /
  described-concept nodes (see Dead ends).

- **Character n-gram TF-IDF ANN candidate gen (scispacy) — cheap, lexical, customizable.**
  scispacy links UMLS by TF-IDF over character 3-grams + approximate-NN, covering 99% of
  MedMentions mention-concepts. https://aclanthology.org/W19-5034.pdf ,
  https://github.com/allenai/scispacy (MIT, active). Custom KBs are first-class via
  `create_tfidf_ann_index` + `LinkerPaths` / pyobo. Relevance: a drop-in, dependency-light
  candidate generator for our nodes+aliases — but purely lexical, so it fails on paraphrase exactly
  where our fuzzy tier already fails.

- **Zero-shot mention detection with labels at inference (GLiNER).** GLiNER (Zaratiana et al.,
  NAACL 2024) extracts arbitrary entity types given only label strings at inference, beating
  ChatGPT/UniNER zero-shot. https://aclanthology.org/2024.naacl-long.300.pdf ,
  https://github.com/urchade/GLiNER (Apache-2.0, **active — v0.2.27 May 2026**, bi-encoder variant
  scales to many labels). Relevance: we could feed the node canonical-keys as GLiNER labels to
  detect which concepts a transcript span *asserts*, replacing brittle regex/parser candidate
  extraction — improving upstream recall before matching.

- **Zero-shot RELATION extraction (GLiREL) for edges.** GLiREL (Boylan et al., 2025) scores unseen
  relation labels over given entity pairs. https://arxiv.org/pdf/2501.03172 ,
  https://github.com/jackboyla/GLiREL (**CC-BY-NC-SA 4.0**). Relevance: our edge_coverage caps at
  0.25 because it needs BOTH endpoints resolved then an edge; a direct relation classifier over
  resolved node pairs (USES/DEPENDS_ON/PRECEDES as labels) recovers edge credit even when the
  student phrases the relation implicitly, decoupling edge recall from quadratic node recall.

- **NIL / out-of-KB detection: learned beats fixed thresholds (BLINKout), and thresholds must be
  calibrated on a dev set WITH negatives.** BLINKout (Chen et al., CIKM 2023) adds a NIL entity,
  NIL classification, and synonym enhancement; learned NIL > threshold NIL across UMLS/SNOMED/
  Wikidata. https://arxiv.org/abs/2302.07189 . Standard practice: pick the abstain threshold to
  **maximize F1 on a validation set that contains NIL/negative mentions**, not by precision-first
  intuition. Relevance: our thresholds were "tuned precision-first" and we now *over*-abstain — the
  literature prescribes calibrating on labeled positives+negatives to trade precision for recall
  explicitly.

- **LLM-as-linker, no fine-tuning, coarse-to-fine (LELA / OneNet / ChatEL).** LELA (Haffoudhi et
  al., 2026, https://arxiv.org/pdf/2601.05192) = BM25+alias+embedding candidate gen → LLM ranks,
  zero-shot across KBs/domains, competitive with fine-tuned. OneNet (Liu et al., EMNLP 2024,
  https://aclanthology.org/2024.emnlp-main.756.pdf) = entity-reduction processor → dual-perspective
  linker (context vs prior) → consensus judger to curb hallucination, few-shot, no fine-tuning.
  ChatEL (Ding et al., LREC-COLING 2024, https://arxiv.org/pdf/2402.14858) prompts an LLM over a
  shortlist. Relevance: with ≤20 candidates we can hand the LLM the *entire* node list + rubric and
  ask "which nodes does this transcript actually teach?" — but OneNet's consensus/verification step
  is the guardrail our LLM-audit safety net lacks (that audit over-credits precisely because it has
  no consistency check).

- **Educational-domain EL exists and looks like us.** Granata et al. 2026 build EL for an
  educational RAG platform: spaCy NER → FAISS retrieval → SBERT cross-encoder rerank → GPT-4o over
  Wikidata concepts. https://arxiv.org/pdf/2512.05967 , code
  https://github.com/Granataaa/educational-rag-el . Job-market EL (Zhang et al., EACL 2024 Findings,
  https://arxiv.org/pdf/2401.17979) links *implicit* skill mentions to ESCO using **synthetic
  mention–entity pairs** for domain adaptation with no human labels — directly the "few labels"
  problem, and their implicit-mention framing matches our "concept taught but never named" case.

## Adoptable artifacts

- **GLiNER** — https://github.com/urchade/GLiNER (Apache-2.0, actively maintained). Adopt as the
  candidate-*extraction* front end: pass node keys as labels, get spans that assert each concept.
  Low integration cost (`pip install gliner`, labels at inference, no training).
- **GLiREL** — https://github.com/jackboyla/GLiREL (**CC-BY-NC-SA — check commercial fit**). Adopt
  for edge resolution: relation labels = our edge types over resolved node pairs. License is a
  blocker for a commercial product; may need retrain-from-scratch or an alternative.
- **SapBERT** — https://huggingface.co/cambridgeltl/SapBERT-from-PubMedBERT-fulltext (Apache-2.0).
  Adopt the *recipe* not the biomedical weights: fine-tune a small encoder with self-alignment on
  (canonical node, paraphrase) positive pairs to build a synonym-robust node retriever/scorer.
- **scispacy candidate-gen** — https://github.com/allenai/scispacy (MIT, active). Adopt
  `create_tfidf_ann_index` + custom `LinkerPaths` as a cheap lexical recall floor feeding the LLM/NLI
  reranker. Purely lexical — use only as one view among several.
- **BLINK** — https://github.com/facebookresearch/BLINK (MIT, **archived**). Reference architecture
  only; do not adopt the Wikipedia checkpoints (fixed KB, no custom-KB path) — the bi-encoder needs
  labeled mention–entity pairs we lack, and retrieval recall is a non-problem at 20 nodes.
- **GENRE** — https://github.com/facebookresearch/GENRE (**CC-BY-NC, archived**). Trie idea is
  reusable; weights are non-commercial + Wikipedia-named-entity shaped. Skip the code, keep the
  constrained-decoding concept if we ever generate node keys.
- **VerbalizED** — https://github.com/flairNLP/VerbalizED (Rücker & Akbik, ACL 2025). Dual-encoder
  design-decision study (label verbalization, hard negatives) — a recipe reference if we train our
  own node encoder.
- **OneNet / LELA** — no clean released code found (honest gap); adopt their *pipeline shapes*
  (reduction → dual-perspective → consensus; coarse-to-fine LLM ranking).

## Recall lessons (for maximizing reference-node recall in noisy student text)

1. **Score every node — with 5–20 nodes there is no retrieval stage.** Run the strongest matcher
   (NLI/cross-encoder/LLM) against all nodes. This removes the candidate-recall ceiling that caps
   BLINK-style systems and is the biggest available win for us.
2. **Many affirmative views per node, score = max over views (MuVER + SapBERT synonyms).** Replace
   the single per-node NLI label with a set of paraphrases/aliases; a strong explanation matches
   *some* view even if not the canonical one. This is the mechanism most likely to move node_coverage
   0.20 → high.
3. **Calibrate thresholds on a labeled dev set that contains negatives; maximize F1 (recall-aware),
   not precision.** BLINKout/NIL literature: precision-first thresholds cause exactly our
   over-abstention. Sweep per-tier thresholds against the scripted "strong" corpus (known positives)
   plus known-negatives.
4. **Decouple edge recall from node recall via a direct relation classifier (GLiREL-style).** Don't
   require independent resolution of both endpoints then an edge lookup (quadratic); classify the
   relation over candidate pairs so implicit relations still earn edge credit.
5. **Partial credit / graded matches, not binary.** Keep the max-view similarity as a soft coverage
   score; a node "mostly taught" contributes proportionally rather than being dropped at a hard cut.
6. **Improve upstream mention/assertion extraction (GLiNER).** Fair-eval work (Hulsebos/Bast et al.,
   https://arxiv.org/pdf/2305.14937) shows detection, not disambiguation, is often the true
   bottleneck — our parser dropping candidate statements before matching may be a bigger leak than
   the matcher itself; verify where recall is actually lost before tuning matchers.
7. **Add an LLM consensus/verification gate (OneNet) so the recall-boosting LLM audit stops
   over-crediting.** The safety net over-credits because it has no self-consistency check; OneNet's
   consensus judger is the missing piece.

## Dead ends

- **Adopting BLINK/GENRE pretrained checkpoints wholesale.** Wikipedia-entity-scoped, GENRE is
  non-commercial, both archived; and the candidate-generation recall they optimize is a non-problem
  at 20 nodes. Their *architecture* is the value, not the weights.
- **Purely lexical candidate gen (scispacy char-ngram TF-IDF, BM25) as the primary matcher.** It is
  the same lexical-overlap failure mode as our existing fuzzy tier — it will not catch paraphrased or
  implicit teaching, which is our core recall loss. Keep only as one cheap view.
- **Autoregressive name generation (GENRE) for non-named nodes.** Procedure-step and described-
  concept nodes have no natural "unique name" to generate; the assumption breaks for much of our KB.
- **Learned NIL classifier (BLINKout) as a first move.** It needs labeled in-KB/out-of-KB training
  data we don't have at scale, and our problem is the *opposite* (over-abstention) — start with dev-
  set threshold calibration + max-over-views, which need no new training data.
- **Training a bi-encoder retriever from scratch.** Requires many labeled mention–node pairs and
  buys retrieval recall we don't need at this KB size; the SapBERT self-alignment recipe on synthetic
  paraphrase pairs is the cheaper, higher-leverage encoder path if we want a learned scorer at all.

## Sources

- https://aclanthology.org/2020.emnlp-main.519.pdf (BLINK, EMNLP 2020)
- https://arxiv.org/pdf/1911.03814 (BLINK preprint, recall@64 tables)
- https://github.com/facebookresearch/BLINK (BLINK code, MIT, archived)
- https://openreview.net/pdf?id=5k8F6UU39V (GENRE, ICLR 2021)
- https://github.com/facebookresearch/GENRE (GENRE code, CC-BY-NC, archived)
- https://aclanthology.org/P19-1335/ (Logeswaran et al. ZESHEL + DAP, ACL 2019)
- https://aclanthology.org/2021.emnlp-main.205.pdf (MuVER multi-view retrieval, EMNLP 2021)
- https://aclanthology.org/2021.naacl-main.334.pdf (SapBERT, NAACL 2021)
- https://github.com/cambridgeltl/sapbert ; https://huggingface.co/cambridgeltl/SapBERT-from-PubMedBERT-fulltext
- https://aclanthology.org/W19-5034.pdf (scispacy, char-ngram TF-IDF linking)
- https://github.com/allenai/scispacy (scispacy code, MIT, custom-KB path)
- https://aclanthology.org/2024.naacl-long.300.pdf (GLiNER, NAACL 2024) ; https://github.com/urchade/GLiNER
- https://arxiv.org/pdf/2501.03172 (GLiREL) ; https://github.com/jackboyla/GLiREL
- https://arxiv.org/abs/2302.07189 (BLINKout / out-of-KB mention discovery, CIKM 2023)
- https://arxiv.org/pdf/2601.05192 (LELA, 2026)
- https://aclanthology.org/2024.emnlp-main.756.pdf (OneNet, EMNLP 2024)
- https://arxiv.org/pdf/2402.14858 (ChatEL, LREC-COLING 2024)
- https://arxiv.org/pdf/2512.05967 (Educational RAG EL, 2026) ; https://github.com/Granataaa/educational-rag-el
- https://arxiv.org/pdf/2401.17979 (Entity linking in the job market domain / ESCO, EACL 2024)
- https://github.com/flairNLP/VerbalizED (Rücker & Akbik, ACL 2025)
- https://arxiv.org/pdf/2305.14937 (Fair evaluation of end-to-end EL systems, 2023)
