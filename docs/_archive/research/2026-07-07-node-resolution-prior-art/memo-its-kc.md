# ITS expectation matching & knowledge components (AutoTutor family, KC models, Q-matrix)

Research memo, 2026-07-07. Angle: what 25+ years of AutoTutor-family "expectation coverage" and
knowledge-component modeling say about our resolver-recall problem (free student text -> canonical
reference nodes).

## Key findings

1. **EMT dialogue IS node coverage, and coverage was always CUMULATIVE across turns.** AutoTutor's
   Expectation-and-Misconception-Tailored (EMT) dialogue stores per-problem "expectations"
   (sentence-like good-answer aspects = our reference nodes) and misconceptions. An expectation is
   "covered" when the student's *cumulative set of turns* meets/exceeds an LSA cosine threshold —
   not any single utterance (Graesser et al., "AutoTutor: a tutor with dialogue in natural language",
   https://link.springer.com/article/10.3758/BF03195563; Olney chapter
   https://cpb-us-w2.wpmucdn.com/blogs.memphis.edu/dist/d/2954/files/2019/10/unreasonable-autotutor-authorversion-final.pdf).
   Relevance: our resolver matches per-candidate-statement; the field's baseline design pools all
   evidence for a node across the whole transcript before thresholding.

2. **Thresholds: .40–.75 across instantiations; .55 @ 200-dim LSA was the tuned optimum (r=.49 with
   human raters, vs human-human r=.51).** Wiemer-Hastings et al. swept 19 thresholds x 3
   dimensionalities against human ratings (https://reed.cs.depaul.edu/peterh/papers/Wiemer-Hastingsaied99.pdf,
   https://reed.cs.depaul.edu/peterh/papers/Wiemer-Hastings99approx.pdf). Later production tutors used
   ~.70 (AI Magazine, https://aaai.org/ojs/index.php/aimagazine/article/view/1591/1490). Penumatsa et
   al. 2006 ("The Right Threshold Value", https://www.worldscientific.com/doi/10.1142/S021821300600293X;
   https://digitalcommons.memphis.edu/facpubs/8806/) found the best expert agreement when **the cosine
   threshold is a function of the lengths of both the student answer and the expectation** — a fixed
   global threshold is provably suboptimal. Relevance: our NLI/fuzzy tier thresholds are fixed and
   precision-first; prior art says condition thresholds on node text length/type and tune per corpus
   against human judgments.

3. **Span-based "new-info" LSA fixed the cumulative-evidence dilution problem.** Hu, Cai et al.
   (IJCAI 2003, https://www.ijcai.org/Proceedings/03/Papers/248.pdf) showed naive vector addition
   across turns penalizes students whose early turns were weak; projecting each new contribution onto
   the subspace spanned by prior contributions splits it into new-vs-old / relevant-vs-irrelevant, and
   coverage then reaches 1.0 where the old algorithm stalled. Relevance: a principled way to
   accumulate per-node evidence over a long teaching transcript without dilution.

4. **Hybrid matchers won; syntax lost; modern embeddings did NOT beat LSA.** Graesser (2016,
   https://files.eric.ed.gov/fulltext/ED586836.pdf) reports the best results across many years were
   **LSA + frequency-weighted word overlap (rare words and negations weighted higher) + regular
   expressions**; "syntactic computations did not prove useful" because student input is telegraphic,
   elliptical, ungrammatical. Carmon et al. 2023
   (https://doi.org/10.3390/electronics12173654) benchmarked LSA, Word2Vec, SBERT, SGPT ± RegEx on
   5,202 electronics response-expectation pairs: **RegEx alone F1=0.509 vs human-human ceiling
   F1=0.532; best stand-alone corpus model 0.398; SBERT/SGPT did not eclipse LSA; RegEx+corpus
   combinations best (~0.49)**. ElectronixTutor production weighting: **LSA 0.25 / RegEx 0.75**
   (Carmon dissertation, https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=4861&context=etd).
   Relevance: don't bet recall on a bigger encoder; add a precision-anchored keyword/term-proportion
   channel (negation-aware) per node and combine channels.

5. **Absolute match accuracy was always moderate — even between humans.** Human-human agreement on
   whether a response matched an expectation: κ=.456–.699 depending on threshold regime; ACE
   (LSA+RegEx) vs humans κ=.288–.493; lenient-threshold recall 83.5% at accuracy 64.7% (Carmon et al.
   2019, https://doi.org/10.1145/3330430.3333649; thesis
   https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=3256&context=etd). LSA-expert
   coverage correlations ~.50 (Graesser et al. 2000, https://doi.org/10.1076/1049-4820(200008)8:2;1-b;ft129).
   Relevance: the field never solved resolution to high fidelity; it *designed around* a mediocre
   matcher (see 6, 7). Our 1/5-node recall is far below even this bar, though — so there is headroom.

6. **Graesser names our exact under-crediting failure as one of two problems that "continue to haunt"
   AutoTutor.** (a) *Partial coverage*: if a good answer has content words A,B,C,D, students who say
   A,B expect credit; AutoTutor "does not score it as covered unless the students express the
   remaining words" -> frustration. (b) *Semantic blur* between expectations and misconceptions ->
   false positive/negative feedback. Mitigations shipped: neutral short feedback on borderline
   matches, and more-discriminating hints/prompts (Graesser 2016, ED586836). Relevance: (a) is our
   node-recall defect in 1998 clothing; their mitigation was graded/partial evidence + dialogue, not a
   better one-shot matcher.

7. **The canonical recall mechanism is dialogue, not the matcher: hint -> prompt -> assertion.** Per
   uncovered expectation, AutoTutor asks a hint (elicits a clause), then a prompt (elicits ONE
   specific missing word), then asserts the content; coverage is re-checked each turn and the cycle
   aborts as soon as the threshold is met (Olney chapter, above). Follow-up dialogue moves
   *significantly* improved assessed knowledge vs initial answers alone (semantic match initial vs
   initial+follow-up: F(1,133)=129.88, p<.001; Carmon dissertation, above). Relevance: this is 25
   years of precedent for our clarification-loop-as-resolver-tier, and it argues follow-ups should
   target the *specific missing content word* of the unresolved node, not re-ask generically.

8. **Per-item matchability varies wildly and is predictable in advance.** Cai et al. 2016
   (https://files.eric.ed.gov/fulltext/ED617867.pdf): per-question LSA-human correlations ranged
   **-0.174 to 0.995**; prompts (single-word targets) matched well (7/11 r>.7), hints (sentence
   targets) poorly (11/38 r<.3). "Question uncertainty" = normalized entropy of DBSCAN clusters of
   real student answers (distance = 1-cosine, threshold .25) predicted matcher performance (r=-.51).
   Fix: **iterative script authoring** — mine collected student answers, cluster them, and add cluster
   exemplars as additional good answers per item. Relevance: we can compute per-node uncertainty from
   our 31-attempt corpus and know which reference nodes will under-resolve; corpus-mined paraphrase
   variants (not hand-authored aliases) are the field's standard remedy.

9. **AutoTutor Lite exposes the scoring decomposition worth copying:** per-turn scores CO (cumulative
   overall coverage 0-1), RN/RO/IN/IO (relevant/irrelevant x new/old, summing to 1), with feedback
   rules keyed to CO early and RN/IN late; expectation texts and LSA-space parameters calibrated by a
   documented successive-refinement loop (Wolfe et al. 2013, https://doi.org/10.3758/s13428-013-0352-z).

10. **KC models / Q-matrix: our reference nodes are KCs, and KC-response mappings are validated by
    data, not authored once.** The KLI framework defines KCs and links them to assessment events
    (Koedinger, Corbett & Perfetti 2012,
    https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1551-6709.2012.01245.x). A Q-matrix is the
    binary item x KC map. Learning Factors Analysis refines a KC model by combinatorial search over
    "difficulty factors" scored by AIC/BIC on learning curves (Cen, Koedinger, Junker 2006,
    http://pact.cs.cmu.edu/pubs/Cen,%20Koedinger%20&%20Junker06.pdf; DataShop tooling). Relevance:
    whether our node set is even the *right* grain is testable — nodes whose resolution never improves
    across attempts are mis-specified KCs, not just resolver misses.

11. **2023-2026 LLM turn: KC-level correctness labeling of open-ended work reaches κ=0.74 vs human
    κ=0.86 — but only with chain-of-thought + solution-strategy context.** (arXiv Feb 2026,
    https://arxiv.org/html/2602.17542v2: GPT-4o labels which of 18 KCs are correctly demonstrated in
    CodeWorkout submissions; without CoT, gains were marginal; LLM labels improved AFM learning-curve
    fit AUC .538->.631.) Related: LLM multi-agent knowledge tagging (https://arxiv.org/pdf/2409.08406),
    automated KC generation/annotation (https://arxiv.org/pdf/2405.20526,
    https://arxiv.org/pdf/2410.01727, https://arxiv.org/pdf/2502.18632, KC-Finder EDM 2023
    https://educationaldatamining.org/EDM2023/proceedings/2023.EDM-long-papers.3/2023.EDM-long-papers.3.pdf).
    Relevance: an LLM resolver tier is viable but its realistic ceiling is κ~.74 with careful CoT
    prompting — consistent with our finding that a naive transcript-audit over-credits.

12. **The deep-NLP branch (Why2-Atlas) is a cautionary tale, with one useful nuance.** Why2-Atlas
    parsed student physics essays into proofs via abductive theorem proving to find missing/wrong
    propositions (VanLehn et al. 2002, https://link.springer.com/chapter/10.1007/3-540-47987-2_20).
    It never scaled beyond the lab; BEETLE II's authors likewise document deep-parser brittleness on
    low-quality student text (Dzikovska et al. 2014, IJAIED 24:284-332). The nuance: a *hybrid*
    symbolic/statistical classifier beat either alone on Why2 essay data (Rosé et al.,
    https://www.researchgate.net/publication/2885713_A_Hybrid_Text_Classification_Approach_for_Analysis_of_Student_Essays).

13. **Optimal word-alignment beats greedy matching (slightly) and frames the task correctly.** Rus &
    Lintean modeled student-input assessment as paraphrase/entailment and used Kuhn-Munkres optimal
    assignment over word-to-word similarities (accuracy .643 vs .615 greedy on ULPC;
    https://aclanthology.org/W12-2018.pdf), packaged in SEMILAR (https://aclanthology.org/P13-4028.pdf).
    DeepTutor's DeepEval added coreference/ellipsis handling because "isolated pair of texts"
    similarity fails on dialogue turns (https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=2177&context=etd)
    — student turns are context-dependent; resolve pronouns/ellipsis against the problem statement
    before matching.

## Adoptable artifacts

- **GIFT** (https://www.gifttutoring.org/projects/gift/wiki/Overview) — US Army open-source tutoring
  framework integrating the AutoTutor Conversation Engine (ACE) + ASAT scripts. Source available
  after free registration; actively maintained. Adoption = reference implementation of EMT coverage
  scoring + hint/prompt cycles, not a drop-in library for us.
- **SEMILAR toolkit** (http://www.semanticsimilarity.org, paper https://aclanthology.org/P13-4028.pdf)
  — free Java library: optimal-alignment word-to-word similarity, subsumption scoring with negation
  handling. Stale (2013) but algorithms are small; porting the optimal-alignment + negation-aware
  subsumption scorer to our fuzzy tier is a weekend-size job.
- **DT-Grade corpus** (900 annotated DeepTutor physics answers, ~25% context-dependent;
  https://github.com/ashrefm/dt-grade-prediction, paper via deeptutor.memphis.edu/resources.htm) —
  external benchmark to sanity-check our resolver tiers against a labeled ITS dataset.
- **DataShop / LFA** (https://pslcdatashop.web.cmu.edu) — learning-curve tooling to validate our node
  (KC) model grain once we have longitudinal attempts.
- **LLM KC-labeling recipe** (https://arxiv.org/html/2602.17542v2) — CoT prompt structure +
  intended-solution-context mechanism + temperature=0; directly transplantable to a per-node
  LLM adjudicator with a measured κ ceiling.
- **ASAT / AutoTutor Lite** — binaries only; adopt the *process* (iterative answer-cluster-driven
  script refinement, CO/RN/RO/IN/IO decomposition), not the code.

## Recall lessons

- **Pool evidence per node across the whole transcript before thresholding** (cumulative coverage,
  span-projection for new-vs-old). Per-statement matching is why single mentions fall under
  threshold.
- **Tune thresholds empirically per corpus against human judgments; make them conditional** on node
  text length and node type (equation/definition/concept — cf. prompts matching far better than
  hints). Historical optimum for sentence-level expectations: cosine ~.55, never a fixed .85-class
  bar. Lenient thresholds bought recall .618->.835 at modest accuracy cost in ACE.
- **Partial credit by content-word proportion**: score each node as proportion-of-expected-key-terms
  observed (RegEx channel), not binary covered/uncovered; students legitimately convey a node with
  1-2 content words.
- **Dialogue is the recall backstop**: when a node is near- but sub-threshold, ask a prompt targeting
  the specific missing content word; credit on the follow-up answer. This measurably raises assessed
  coverage and is the single most replicated design in the family.
- **Mine your own transcripts**: cluster real student phrasings per node; add cluster exemplars as
  extra reference texts; use cluster entropy to rank which nodes need authoring attention. This is
  data-driven alias generation, distinct from hand-authoring.
- **Handle negation and rare words explicitly** (weight rare terms up, negations flip); resolve
  pronouns/ellipsis against problem context before matching (DeepEval).
- **Expect a κ~.5-.75 ceiling** (human-human is only .46-.86 depending on task grain); design the
  score to be robust to that (graded evidence, abstention -> clarification, not silent zeroes).

## Dead ends

- **Deep syntactic/semantic parsing of student utterances** — explicitly found useless for AutoTutor
  (telegraphic input); Why2-Atlas/BEETLE II document the brittleness. Our parser-level equation
  handling is different (SymPy on extracted equations is fine); parsing student *prose* is not.
- **Swapping in a bigger sentence encoder as the fix** — SBERT/SGPT failed to beat LSA on in-domain
  data; domain corpus and hybrid channels mattered more. (2023 result, pre-frontier-LLM embeddings,
  but the burden of proof is on the encoder.)
- **A single global similarity threshold** — dominated by length- and type-conditional thresholds.
- **Chasing human-level one-shot matching** — the ceiling itself is moderate; the family shipped
  around it with dialogue and partial credit.
- **Pure similarity on long, multi-proposition node texts** (the "hint"-style targets): correlations
  with humans go to ~0 or negative when the answer space is divergent; split such nodes into smaller
  KC-grain targets instead.

## Sources

- https://reed.cs.depaul.edu/peterh/papers/Wiemer-Hastingsaied99.pdf
- https://reed.cs.depaul.edu/peterh/papers/Wiemer-Hastings99approx.pdf
- https://www.worldscientific.com/doi/10.1142/S021821300600293X
- https://digitalcommons.memphis.edu/facpubs/8806/
- https://www.ijcai.org/Proceedings/03/Papers/248.pdf
- https://files.eric.ed.gov/fulltext/ED586836.pdf  (Graesser 2016, IJAIED 26:124-132)
- https://cpb-us-w2.wpmucdn.com/blogs.memphis.edu/dist/d/2954/files/2019/10/unreasonable-autotutor-authorversion-final.pdf
- https://link.springer.com/article/10.1007/s40593-014-0029-5  (Nye, Graesser & Hu 2014 review)
- https://doi.org/10.1145/3330430.3333649  (Carmon et al. 2019, L@S)
- https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=3256&context=etd  (Carmon thesis)
- https://doi.org/10.3390/electronics12173654  (Carmon et al. 2023)
- https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=4861&context=etd  (Carmon dissertation)
- https://files.eric.ed.gov/fulltext/ED617867.pdf  (Cai et al. 2016, answer clustering)
- https://doi.org/10.3758/s13428-013-0352-z  (AutoTutor Lite / BRCA Gist)
- https://doi.org/10.1076/1049-4820(200008)8:2;1-b;ft129  (Graesser et al. 2000)
- https://aaai.org/ojs/index.php/aimagazine/article/view/1591/1490
- https://aclanthology.org/W12-2018.pdf  (Rus & Lintean 2012)
- https://aclanthology.org/P13-4028.pdf  (SEMILAR)
- https://digitalcommons.memphis.edu/cgi/viewcontent.cgi?article=2177&context=etd  (DeepEval)
- https://github.com/ashrefm/dt-grade-prediction  (DT-Grade)
- https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1551-6709.2012.01245.x  (KLI)
- http://pact.cs.cmu.edu/pubs/Cen,%20Koedinger%20&%20Junker06.pdf  (LFA)
- https://educationaldatamining.org/EDM2023/proceedings/2023.EDM-long-papers.3/2023.EDM-long-papers.3.pdf  (KC-Finder)
- https://arxiv.org/html/2602.17542v2  (LLM KC-level correctness labeling, 2026)
- https://arxiv.org/pdf/2409.08406 ; https://arxiv.org/pdf/2405.20526 ; https://arxiv.org/pdf/2410.01727 ; https://arxiv.org/pdf/2502.18632
- https://link.springer.com/chapter/10.1007/3-540-47987-2_20  (Why2-Atlas)
- https://www.researchgate.net/publication/2885713_A_Hybrid_Text_Classification_Approach_for_Analysis_of_Student_Essays
- https://arxiv.org/html/2406.13919 ; https://arxiv.org/html/2501.06682  (SPL — LLM-era AutoTutor successor)
- https://www.gifttutoring.org/projects/gift/wiki/Overview
