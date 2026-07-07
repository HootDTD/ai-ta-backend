# Benchmarks & datasets for evaluating resolver recall

Research memo, 2026-07-07. Angle: what public datasets and annotation methodologies exist for
"does this student text express reference concept X?", and how prior work measures alignment
recall/precision — so we stop tuning our resolver against a 31-attempt corpus with no gold labels.

## Key findings

1. **The exact annotation protocol we need already exists: Nielsen's facet-level entailment corpus
   (LREC 2008).** Nielsen, Ward, Martin & Palmer annotated 15,357 student-answer pairs with
   ~145,911 *facet-level* entailment relationships: each reference answer is decomposed into facets
   (dependency-parse-derived word-pair relations ≈ our nodes), and each facet gets a per-answer label
   from a 5-category scheme (expressed / inferred / contradicted-expressed / contradicted-inferred /
   unaddressed). Inter-annotator agreement: 86.2%, kappa = 0.728.
   https://www.researchgate.net/publication/220747012_Annotating_Students'_Understanding_of_Science_Concepts
   and the companion JNLE paper https://www.researchgate.net/profile/Wayne-Ward/publication/220597280_Recognizing_entailment_in_intelligent_tutoring_systems/links/02e7e520ce5211aeab000000/Recognizing-entailment-in-intelligent-tutoring-systems.pdf
   Relevance: this is literally per-node resolution gold labeling at scale, with proof it can be done
   reliably. The **expressed vs inferred distinction** is the diagnosis of our recall failure: students
   routinely *imply* a facet without stating it, and a resolver demanding explicit statement undercounts.

2. **SemEval-2013 Task 7 (SciEntsBank + Beetle) is the standard external benchmark.** ~10k answers to
   197 science questions (SciEntsBank) + 3k answers on electricity/electronics (Beetle); labels
   {correct, partially_correct_incomplete, contradictory, irrelevant, non_domain}; splits for Unseen
   Answers / Unseen Questions / Unseen Domains. http://www.aclweb.org/anthology/S13-2045 ; ready-to-use
   HF copy https://huggingface.co/datasets/nkazi/SciEntsBank
   Relevance: `partially_correct_incomplete` is exactly our partial-coverage case, and the UQ/UD splits
   test the failure mode we fear most (resolver tuned per-problem, dies on new problems).

3. **DT-Grade: the only public dataset grading answers *inside* a tutoring dialogue.** 900 student
   answers from DeepTutor (Newtonian physics) annotated in context as correct / correct-but-incomplete /
   contradictory / incorrect; **~25% of answers require dialogue context** (ellipsis, coreference) to
   interpret at all. Banjade et al., BEA 2016. https://digitalcommons.memphis.edu/facpubs/2787/
   Relevance: our parser extracts candidate statements and the resolver matches them out of context —
   DT-Grade quantifies why that alone can cost a quarter of recall in dialogue data.

4. **Gap-annotated ASAG dataset (2025) is a near-clone of our node/edge coverage machinery.** Models
   reference and student answers as directed graphs (ClausIE SPO triples), aligns them to identify
   missing content ("gaps") at word/phrase/sentence level; releases gap-annotated gold over UNT/Mohler,
   SciEntsBank and Beetle. https://arxiv.org/abs/2504.04473 , data:
   https://github.com/sahuarchana7/gaps-answers-dataset
   Relevance: external gold for "which reference units did the student miss" — a direct recall
   benchmark for our resolver's miss detection. (Paper doesn't report IAA; treat gold as silver-ish.)

5. **Zero-shot LLMs are NOT SOTA on these benchmarks.** GPT-4 zero-shot: SciEntsBank 2-way F1 = 0.744
   (3-way w-F1 0.729), Beetle 2-way F1 = 0.611 (3-way 0.516) — *worse than fine-tuned specialized
   models*, and on Beetle providing the reference answer *lowered* GPT-4's accuracy.
   https://arxiv.org/abs/2309.09338 / https://link.springer.com/article/10.1007/s44163-024-00147-y
   Relevance: independent confirmation that our LLM transcript-audit cannot be trusted as score source;
   the known-good path is a small model fine-tuned on in-domain gold.

6. **WorldTree V2 + TextGraphs explanation-regeneration shared tasks (2019/2020/2021) = "resolve free
   text to canonical explanation nodes" as a mature shared task.** Gold explanations average 6 (max 16)
   facts from a semi-structured KB; by 2021 organizers replaced binary gold with **~250k expert graded
   relevancy ratings** and evaluated with rank metrics (MAP/NDCG) because binary single-path gold
   under-credited valid alternative explanations; absolute performance stayed low (task is hard).
   https://aclanthology.org/D19-5309/ , https://github.com/cognitiveailab/tg2021task ,
   https://arxiv.org/abs/2107.13031
   Relevance: strongest precedent for scoring *graded* node relevance + multiple valid paths instead of
   hard-threshold binary resolution.

7. **Answer Equivalence (BEM), Google 2022.** 43k human judgments of whether a candidate answer is
   equivalent to a reference *given the question*; trained BERT matcher (BEM) beats token overlap
   decisively. https://github.com/google-research-datasets/answer-equivalence-dataset ,
   model https://huggingface.co/kortukov/answer-equivalence-bem , paper https://arxiv.org/abs/2202.07654
   Relevance: the "give the matcher the question/problem context" recipe is the fix for our
   context-free NLI tier; also an annotation-schema template (5 quick questions per pair).

8. **SAF / EngSAF: grading + elaborated feedback that names what's missing.** SAF (Filighera et al.,
   ACL 2022, communication networks, DE+EN) and EngSAF (~5.8k answers, 25 engineering courses, 2024)
   attach content-focused feedback explaining the score — implicit missing-concept labels.
   https://arxiv.org/html/2407.12818 , models/data: https://huggingface.co/Short-Answer-Feedback
   ASAG2024 unifies SciEntsBank+SAF+Mohler+STITA+DigiKlausur onto one scale:
   https://arxiv.org/pdf/2409.18596 , https://huggingface.co/datasets/Meyerger/ASAG2024

9. **REC-CBM (2025-26): LLM-propose + expert-verify concept annotation over existing benchmarks.**
   Ships ordinal per-concept scores (e.g., 8 concepts for Mohler: factual correctness, concept
   coverage, reasoning depth...) over ASAP-2 (17,292 essays) and Mohler (2,273 answers); inventory
   drafted by 3 domain experts, labels from GPT-4o + Gemini-2.5-pro proposals reviewed by experts with
   majority-vote conflict resolution. https://huggingface.co/datasets/scott-f-zhang/REC-CBM
   Relevance: a current, cheap, defensible pipeline for building our own per-node gold labels.

10. **Tutoring-dialogue corpora exist but carry dialogue-act labels, not concept-coverage labels.**
    MathDial: 2.9k teacher–LLM-student dialogues grounded in math word problems + confusions, teacher-move
    taxonomy (https://arxiv.org/abs/2305.14536 , https://github.com/eth-nlped/mathdial). CIMA: exercises
    grounded in explicit concept sets, per-utterance action labels (https://aclanthology.org/2020.bea-1.5/ ,
    https://github.com/kstats/CIMA). MRBench/BEA-2025 shared task: 192 dialogues, 1.5k tutor responses,
    8 pedagogical dimensions; best mistake-identification macro-F1 only 71.81 (3-class)
    (https://arxiv.org/abs/2507.10579). MathTutorBench similar (https://github.com/eth-lre/mathtutorbench).
    Relevance: transcript *sources* for stress-testing our parser on dialogue phenomena; and a warning —
    even SOTA systems judging dialogue content sit at ~0.7 F1.

11. **NLI-for-science fine-tuning data: SciTail + EntailmentBank.** SciTail: 27k premise–hypothesis
    pairs from real science-QA sentences, deliberately high lexical overlap between entailing and
    non-entailing pairs (https://ojs.aaai.org/index.php/AAAI/article/view/12022). EntailmentBank: 1,840
    multi-step entailment trees over WorldTree sentences (https://arxiv.org/abs/2104.08661).
    Relevance: domain-matched data to fine-tune/calibrate our NLI tier instead of tuning thresholds on
    generic MNLI-trained models.

12. **Learning-by-teaching precedent (Betty's Brain) sidesteps free-text resolution entirely**: the
    student teaches by *constructing the concept map artifact*, so grading = compare maps, no resolver.
    Datasets via CMU DataShop (https://www.cmu.edu/datalab/tools/datashop.html ,
    https://en.wikipedia.org/wiki/Betty%27s_Brain). Relevance: a design lever (elicit structure), not a
    benchmark; no public corpus of free-text teaching + concept-coverage gold was found in this angle —
    honest gap: for "student-teaches-agent transcripts with per-concept gold," nothing public exists;
    we would be the first, which is why adopting Nielsen's protocol on our own transcripts matters.

## Adoptable artifacts

- `nkazi/SciEntsBank` (HF, splits UA/UQ/UD prebuilt) — regression benchmark for every resolver tier
  change; treat partially_correct_incomplete as the partial-coverage class. License: research-use
  (original corpus CC BY-SA-ish; check card). Maintained copy, loadable in one line.
- Nielsen facet corpus (announced public in LREC 2008 paper) — the annotation *protocol* is the
  adoptable thing: per-node labels {expressed, inferred, contradicted, unaddressed} on our own 31-attempt
  corpus. Caveat: hosting link has rotted; may need to email UNT (Rodney Nielsen) for the data itself.
- `sahuarchana7/gaps-answers-dataset` (GitHub, 2025) — gap-level gold over UNT/SciEntsBank/Beetle;
  evaluate our "missed node" detection directly. Recent, small, no IAA reported.
- DT-Grade (900 physics dialogue answers; via Memphis / mirrored in
  https://github.com/ashrefm/dt-grade-prediction) — context-dependence stress test for the parser.
- `google-research-datasets/answer-equivalence-dataset` (Apache-2.0) + `kortukov/answer-equivalence-bem`
  (HF port of BEM) — template for a context-conditioned trained matcher tier; retrain on our domain
  (SQuAD-style short answers ≠ physics explanations; use recipe, not weights).
- `Meyerger/ASAG2024` (HF) + `Short-Answer-Feedback` HF org (SAF datasets + fine-tuned graders) —
  unified multi-corpus training pool for fine-tuning an NLI/grading tier.
- `cognitiveailab/tg2021task` (WorldTree V2 + 250k relevancy ratings; open) — pattern for graded
  node-relevance gold + MAP-style eval of the resolver as a *ranker*.
- SciTail (AI2) + EntailmentBank (AI2, CC BY 4.0) — NLI fine-tuning corpora matched to science text.
- `scott-f-zhang/REC-CBM` (HF) — copyable LLM-propose/expert-verify annotation pipeline + concept
  inventory design principles (coverage of rubric axes, non-redundancy, anchored ordinal descriptors).
- `eth-nlped/mathdial` (CC BY 4.0), `kstats/CIMA` — dialogue transcripts to replay through our parser.

## Recall lessons

- **Label "inferred" separately from "expressed."** Nielsen's scheme exists because students imply
  facets constantly; a resolver crediting only explicit statements has a structural recall ceiling.
  Our gold set must distinguish these so we can measure how much recall lives in inference.
- **Resolve in dialogue context.** DT-Grade: 25% of tutoring answers are uninterpretable out of
  context. Run coreference/ellipsis resolution (or context-windowed matching) *before* the resolver;
  candidates extracted as isolated sentences are lossy at the parser stage, upstream of any tier.
- **Condition the matcher on the problem/question.** BEM's entire gain over token-F1 comes from
  giving the equivalence model the question + reference + candidate jointly. Our NLI tier matches
  (candidate, node-label) pairs without the problem statement — known-bad configuration.
- **Grade relevance, don't binarize early.** WorldTree's 2021 pivot to expert relevancy ratings +
  MAP: evaluate the resolver as a ranker (recall@k, MAP per node), then pick thresholds per operating
  point. Precision-first NLI threshold tuning without a recall curve is exactly the anti-pattern.
- **Fine-tune small, don't prompt big.** GPT-4 zero-shot underperforms fine-tuned encoders on
  SciEntsBank/Beetle; SAF/ASAG2024 give enough labeled data to fine-tune a T5/DeBERTa-class judge.
- **Partial credit is a label, not a hack**: partially_correct_incomplete (SemEval),
  correct-but-incomplete (DT-Grade), per-facet coverage (Nielsen) all treat partial coverage as
  first-class — per-node graded credit is the standard, matching our node_coverage design.
- **Evaluate on unseen problems.** SemEval's UA/UQ/UD splits: systems drop sharply from unseen-answers
  to unseen-questions/domains. Our resolver eval must hold out whole problems, not just attempts.
- **Annotation budget**: ~3 experts + LLM pre-annotation + majority vote (REC-CBM) or 2 trained
  annotators reaching kappa ≈ 0.73 (Nielsen) is the realistic bar; for 31 attempts × ~5 nodes this is
  days, not weeks.

## Dead ends

- **ASAP-SAS as a resolver benchmark** — holistic 0–2/0–3 scores only, no concept-level gold; fine for
  grading models, useless for measuring per-node recall (unless paired with REC-CBM annotations).
- **CIMA / TSCC / TalkMoves as gold sources** — dialogue-act taxonomies (hint, question, correction),
  not concept-coverage labels; wrong label type for our metric.
- **MRBench / MathTutorBench / BEA-2025 tracks** — evaluate *tutor response quality*, not student
  concept coverage; adjacent literature, not our benchmark.
- **Using BEM weights off the shelf** — trained on SQuAD entity-style answers; domain/style mismatch
  with multi-sentence physics/econ teaching text. Retrain the recipe on ASAG data instead.
- **Betty's Brain data for resolver eval** — the teaching artifact is already a structured map; there
  is no free-text-to-node alignment problem in that data.
- **2013-era SemEval feature-engineered systems** — superseded; use the dataset, ignore the systems.

## Sources

- https://www.researchgate.net/publication/220747012_Annotating_Students'_Understanding_of_Science_Concepts
- https://www.researchgate.net/profile/Wayne-Ward/publication/220597280_Recognizing_entailment_in_intelligent_tutoring_systems/links/02e7e520ce5211aeab000000/Recognizing-entailment-in-intelligent-tutoring-systems.pdf
- http://www.aclweb.org/anthology/S13-2045
- https://huggingface.co/datasets/nkazi/SciEntsBank
- https://digitalcommons.memphis.edu/facpubs/2787/
- https://arxiv.org/abs/2504.04473 / https://github.com/sahuarchana7/gaps-answers-dataset
- https://arxiv.org/abs/2309.09338 / https://link.springer.com/article/10.1007/s44163-024-00147-y
- https://aclanthology.org/D19-5309/ / https://github.com/cognitiveailab/tg2021task / https://arxiv.org/abs/2107.13031
- https://arxiv.org/abs/2202.07654 / https://github.com/google-research-datasets/answer-equivalence-dataset / https://huggingface.co/kortukov/answer-equivalence-bem
- https://arxiv.org/html/2407.12818 / https://huggingface.co/Short-Answer-Feedback
- https://arxiv.org/pdf/2409.18596 / https://huggingface.co/datasets/Meyerger/ASAG2024
- https://huggingface.co/datasets/scott-f-zhang/REC-CBM
- https://arxiv.org/abs/2305.14536 / https://github.com/eth-nlped/mathdial
- https://aclanthology.org/2020.bea-1.5/ / https://github.com/kstats/CIMA
- https://arxiv.org/abs/2507.10579 (BEA 2025 findings / MRBench)
- https://github.com/eth-lre/mathtutorbench
- https://ojs.aaai.org/index.php/AAAI/article/view/12022 (SciTail)
- https://arxiv.org/abs/2104.08661 (EntailmentBank)
- https://arxiv.org/abs/1704.04452 / https://github.com/UKPLab/emnlp2017-cmapsum-corpus (concept-map corpus)
- https://arxiv.org/abs/2504.03877 (concept-based rubrics for LLM assessment)
- https://www.cmu.edu/datalab/tools/datashop.html / https://en.wikipedia.org/wiki/Betty%27s_Brain
- https://github.com/benhamner/ASAP-SAS (ASAP-SAS)
