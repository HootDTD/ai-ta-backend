# Automated Short Answer Grading (ASAG) & reference-answer alignment

Research memo for the Apollo resolver-recall problem (free student text -> canonical concept/relation
alignment). Angle: SemEval-2013 Task 7, c-rater concept detection, facet-based entailment grading,
AutoTutor expectation matching, transformer/LLM-era ASAG. Date: 2026-07-07.

## Key findings

1. **c-rater (ETS) detects concepts by expanding the REFERENCE side, not by a smarter matcher.**
   Each rubric concept C is represented by a *set* of "model sentences" (paraphrase variants written in
   English), each with hand-picked Required Lexicon and per-word Similar Lexicon (Dekang Lin thesaurus +
   WordNet); the Goldmap matcher then checks whether the student answer entails *any* model sentence,
   after spell-correction, parsing, predicate-argument extraction, pronoun resolution, and lemmatization
   (Leacock & Chodorow 2003, https://link.springer.com/article/10.1023/A:1025779619903; Sukkarieh &
   Stoyanchev 2009, https://aclanthology.org/W09-2509.pdf). Reported ~84% agreement with human raters in
   NAEP and Indiana deployments. Manual model building cost 12h/item; Sukkarieh & Stoyanchev automated it
   to ~0h by having raters do "concept-based scoring" — mark each concept Present/Absent/Negated AND
   highlight the *evidence span* — then using those student-authored spans directly as model sentences.
   Automated models came within 0.1 unweighted kappa of hand-engineered ones (per-item kappa 0.42–0.97).
   *Relevance:* our single affirmative `content.label` per node is the analog of ONE c-rater model
   sentence; the field's answer to low recall was many student-derived paraphrase variants per concept.

2. **Facet-based grading is the exact prior form of our node-resolution task, and it plateaued around
   69–79% per-facet accuracy.** Nielsen et al. decomposed reference answers into dependency-triple facets
   (word pair + relation, ~5 facets/answer) and classified each facet 5-ways (Expressed / Inferred /
   Contradicted / Diff-Arg / Unaddressed) against the student answer: 75.5% accuracy within domain,
   65.9–68.8% out of domain, with human inter-annotator agreement 86.2% (kappa 0.728)
   (https://doi.org/10.1017/s135132490999012x; corpus paper
   http://www.lrec-conf.org/proceedings/lrec2008/pdf/873_paper.pdf). Their "Diff-Arg" label (right
   relation, wrong argument) is an explicit misconception trigger. *Relevance:* calibrates expectations —
   feature-era per-facet detection never exceeded ~80%; systems were designed to tolerate resolver error,
   not eliminate it.

3. **SemEval-2013 Task 7 ("Joint Student Response Analysis") benchmarked this field; the facet-level
   subtask went almost untried.** 9 teams classified whole responses (5/3/2-way) on Beetle + SciEntsBank;
   best 2-way macro-F1 ~0.77 (unseen answers), 0.74 (unseen questions, SoftCardinality), 0.71 (unseen
   domains); 5-way collapsed on unseen domains (macro-F1 0.12–0.38) (https://aclanthology.org/S13-2045/).
   The Partial Entailment pilot — decide *which reference facets* a response expresses, literally our
   node-resolution problem — attracted exactly ONE participant (UKP-BIU, hybrid STS + BIUTEE entailment),
   which beat only the majority baseline. *Relevance:* there is no solved, adoptable "facet detector";
   response-level classification is well-studied, facet-level detection is not.

4. **Alignment-based grading (Sultan et al., NAACL 2016) beat all SemEval systems with an asymmetric
   'coverage' feature.** Monolingual word alignment (PPDB lexical + contextual similarity) between
   reference and student answer; the key grading feature is *coverage* = proportion of the REFERENCE
   answer's content words aligned in the student response (asymmetric, so verbose answers aren't
   penalized), plus *question demoting* (delete words that appear in the question before matching).
   Ablation: removing alignment features dropped Pearson r .592->.519 — largest of any feature; question
   demoting second (.592->.571); tf-idf term weighting was nearly useless (.592->.590)
   (https://aclanthology.org/N16-1123/). 5-way SemEval weighted F1 .582/.554/.545 (UA/UQ/UD).
   *Relevance:* our fuzzy-lexical tier should be an asymmetric aligner scoring node-content coverage
   with prompt-text demoted, not symmetric string similarity.

5. **AutoTutor (Graesser et al.) is the closest production analog — dialogue tutoring with expectation
   coverage — and it assumes a mediocre matcher.** Expectations (= our nodes) are matched by LSA cosine +
   hand RegEx; crucially, evidence is pooled across *all combinations* of the student's speech acts over
   all turns so far (X, Y, Z, XY, XZ, YZ, XYZ — max cosine wins), thresholds varied 0.40–0.85 (~0.70
   typical) (https://aaai.org/ojs/index.php/aimagazine/article/view/1591/1490;
   https://link.springer.com/content/pdf/10.3758/BF03195563.pdf). Penumatsa et al. 2006 showed the optimal
   cosine threshold is a *function of the lengths* of both student text and expectation
   (https://doi.org/10.1142/s021821300600293x). LSA coverage correlated only ~0.50 with expert judgments;
   ACE-vs-human kappa .493 vs human-human .699. When an expectation is sub-threshold, AutoTutor emits
   hints/prompts targeting the specific missing content words until threshold is met. *Relevance:* the
   canonical architecture treats resolver misses as a *dialogue* problem (our clarification loop), pools
   evidence over turn combinations, and adapts thresholds to length.

6. **Transformer era: NLI pretraining is the single biggest transfer lever.** Sung et al. 2019 (BERT) was
   first past feature-engineered SOTA; Camus & Filighera 2020 showed RoBERTa-large fine-tuned on MNLI
   *then* SciEntsBank hits macro-F1 78.3/65.7/70.8 (UA/UQ/UD, 3-way) — up to +13 points absolute over
   prior SOTA, with the gain concentrated on unseen questions/domains
   (https://link.springer.com/chapter/10.1007/978-3-030-52240-7_8). Kazi & Kahanda 2023 replicate: MNLI
   transfer chiefly rescues the rare *contradictory* class (10% of data)
   (https://doi.org/10.1109/icmla58977.2023.00255). *Relevance:* our NLI tier is zero-shot +
   precision-first thresholds — the weakest configuration in this literature; MNLI-base + a few hundred
   in-domain node/statement pairs is the documented upgrade.

7. **LLM-era ASAG (2023–2026): zero-shot GPT-4 under-performs fine-tuned small models and over-credits;
   the fix is separating extraction from scoring.** Kortemeyer 2024: GPT-4 SciEntsBank 2-way F1 0.744 —
   would have won SemEval-2013 but loses to fine-tuned BERT-family
   (https://doi.org/10.1007/s44163-024-00147-y). Rubric-guided prompting (decompose into key-element
   identification -> aggregate) helps large models but *rationales hallucinate rubric elements* and token
   attribution shows scores driven by instruction/formatting tokens, not student text
   (https://dl.acm.org/doi/10.1145/3774398.3811581). AutoSCORE (2025) makes this concrete: agent 1
   extracts rubric-relevant components into a structured representation (boolean/count/span per rubric
   rule), agent 2 scores from that structure — beats single-pass LLM scoring on QWK/MAE across ASAP,
   biggest gains on multi-component rubrics and small models (https://arxiv.org/pdf/2509.21910). Medical
   ASAG: GPT-4 precision 0.91–0.98 on fully-correct answers but systematically lenient elsewhere
   (https://link.springer.com/article/10.1186/s12909-024-06026-5). *Relevance:* directly explains our
   LLM transcript-audit over-crediting and endorses our parser->resolver decomposition; the audit should
   emit quoted evidence spans per node (c-rater's "Evidence", AutoSCORE's components), never scores.

## Adoptable artifacts

- **SciEntsBank/Beetle (SRA corpus)** — HF mirror `nkazi/SciEntsBank`
  (https://huggingface.co/datasets/nkazi/SciEntsBank); original Nielsen corpus has 145,911 *facet-level*
  entailment annotations (public research resource). Adopt as an off-domain calibration/benchmark set for
  our resolver tiers (per-facet present/absent labels are exactly node-resolution ground truth).
- **`ma-sultan/short-answer-grader`** (https://github.com/ma-sultan/short-answer-grader) — open-source,
  unmaintained (2016), Python. Don't adopt the code; port the *features*: asymmetric reference-coverage
  alignment + question demoting into our fuzzy tier.
- **MNLI-fine-tuned cross-encoders** (RoBERTa-large-MNLI, DeBERTa-v3 NLI on HF) — swap our NLI tier's
  base to an MNLI checkpoint and fine-tune on a few hundred (node label, transcript statement, y/n)
  pairs; Camus & Filighera quantify the payoff.
- **SAF dataset + models** (HF org `Short-Answer-Feedback`,
  https://huggingface.co/datasets/Short-Answer-Feedback/saf_communication_networks_english; Filighera et
  al. ACL 2022 https://aclanthology.org/2022.acl-long.587/) — 4,519 answers with partial-credit scores +
  elaborated feedback; the annotation schema (score + verification label + span-grounded feedback) is a
  good template for our per-node feedback artifact.
- **AutoSCORE prompt architecture** (https://arxiv.org/pdf/2509.21910) — no code dependency; adopt the
  two-agent extract-then-score pattern for our transcript audit (structured per-node evidence, separate
  scorer).

## Recall lessons

- **Multiply reference variants, don't tune one matcher.** Every high-agreement system (c-rater, Automark,
  Ramachandran 2015) represented each concept as many paraphrase patterns *mined from real student
  answers*. Concretely for us: log every resolver miss, have a human (or LLM + human gate) highlight the
  evidence span, attach it as an alternate label/alias on the node — c-rater automated exactly this loop.
- **Asymmetric coverage of the node's content, question/prompt words demoted** (Sultan): symmetric
  similarity and prompt parroting are both recall *and* precision bugs.
- **Pool evidence across turns and turn-combinations** (AutoTutor): a facet taught across three utterances
  never matches per-statement candidates; match node labels against concatenations/windows of the
  student's accumulated turns, take the max.
- **Length-adaptive thresholds** (Penumatsa 2006): a fixed 0.75/0.80/0.85 tier cap is provably wrong;
  optimal thresholds vary with statement and label length. Calibrate per (node type x length band).
- **NLI works when fine-tuned, not zero-shot**; MNLI transfer gives the largest out-of-domain gains and
  fixes contradiction detection. Precision-first tuning of a zero-shot model was never the SOTA recipe.
- **Plan for ~70–85% per-node detection, not 95%.** No system in 20+ years achieved near-perfect facet
  recall with usable precision. The field's answer is (a) partial credit, and (b) dialogue repair —
  AutoTutor's hint/prompt escalation for the specific missing content words is our clarification loop,
  already validated in production ITS.
- **LLMs as extractors, not judges**: require verbatim quoted spans per credited node; hallucinated
  rubric elements are a documented LLM failure mode (Rubric-guided Prompting 2026).

## Dead ends

- **Hand-crafted regex/IE patterns per problem** (Automark, Oxford-UCLES, Tandalla/ASAP winner): 12h+ of
  authoring per item, brittle; the c-rater automation line exists because this failed to scale.
- **Pure embedding/LSA cosine as the score source**: r≈0.5 vs experts, kappa .49 vs human .70 — fine as a
  candidate-generation tier, disqualified as ground truth.
- **Zero-shot end-to-end LLM grading** as the score source: loses to fine-tuned small models on
  SciEntsBank, lenient, hallucinating rationales. (Fine as extraction with span evidence.)
- **tf-idf/domain-keyword term weighting**: near-zero gain (Sultan ablation) — key answer words are often
  not domain keywords ("last", "added", "first").
- **Fine-grained (5-way) per-node label schemes with little data**: SemEval 5-way macro-F1 collapsed to
  0.12–0.38 on unseen domains; keep the node decision space small (expressed / contradicted / unaddressed).
- **Waiting for an off-the-shelf facet detector**: the one facet-level shared-task pilot had a single
  entrant; nothing to adopt wholesale — components (aligners, NLI models, datasets) yes, a finished
  resolver no.

**Honest gaps:** published *per-concept recall* numbers are scarce — c-rater reports only holistic kappa
and %-agreement; SemEval reports response-level F1; Nielsen reports per-facet accuracy but not
per-label recall for "Expressed". The SemEval partial-entailment pilot's exact F1 table was not
recoverable from the fetched PDF. Treat "~70–85% per-node detection" as an accuracy-based estimate, not
a measured recall.

## Sources

- https://aclanthology.org/S13-2045/ (SemEval-2013 Task 7 overview, results, partial-entailment pilot)
- https://link.springer.com/article/10.1023/A:1025779619903 (Leacock & Chodorow 2003, c-rater)
- https://aclanthology.org/W09-2509.pdf (Sukkarieh & Stoyanchev 2009, automating c-rater model building)
- https://www.researchgate.net/publication/221438536_c-rater_Automatic_Content_Scoring_for_Short_Constructed_Responses (Sukkarieh & Blackmore 2009)
- https://doi.org/10.1017/s135132490999012x (Nielsen, Ward & Martin 2009, facet entailment in ITS)
- http://www.lrec-conf.org/proceedings/lrec2008/pdf/873_paper.pdf (Nielsen et al. 2008, facet corpus)
- https://aclanthology.org/P08-2061.pdf (Nielsen et al. 2008, facet representation, 79%/69% accuracy)
- https://aclanthology.org/N12-1021.pdf (Dzikovska, Nielsen & Brew 2012, SRA task design)
- https://aclanthology.org/N16-1123/ (Sultan, Salazar & Sumner 2016, alignment grader)
- https://github.com/ma-sultan/short-answer-grader (open-source alignment grader)
- https://link.springer.com/chapter/10.1007/978-3-030-52240-7_8 (Camus & Filighera 2020, MNLI transfer)
- https://doi.org/10.1109/icmla58977.2023.00255 (Kazi & Kahanda 2023, MNLI transfer for ASAG)
- https://dl.acm.org/doi/fullHtml/10.1145/3488466.3488479 (explaining transformer ASAG, Beetle/SEB F1)
- https://www.scitepress.org/PublishedPapers/2020/94224/pdf/index.html (Ghavidel et al. 2020, BERT/XLNet + full SemEval SOTA tables)
- https://aaai.org/ojs/index.php/aimagazine/article/view/1591/1490 (Graesser et al., AutoTutor expectation coverage)
- https://link.springer.com/content/pdf/10.3758/BF03195563.pdf (AutoTutor mechanics: turn combinations, thresholds .40–.85)
- https://doi.org/10.1142/s021821300600293x (Penumatsa et al. 2006, length-dependent LSA thresholds)
- https://doi.org/10.1145/3330430.3333649 (Carmon et al. 2019, ACE LSA+RegEx precision/recall vs humans)
- https://doi.org/10.1007/s44163-024-00147-y (Kortemeyer 2024, GPT-4 on SciEntsBank/Beetle)
- https://dl.acm.org/doi/10.1145/3774398.3811581 (Rubric-guided prompting 2026, hallucinated rubric elements)
- https://arxiv.org/pdf/2509.21910 (AutoSCORE 2025, extract-then-score multi-agent)
- https://doi.org/10.1145/3657604.3664685 (Jiang & Bosch 2024, GPT-4 ASAP-SAS, key elements vs examples)
- https://link.springer.com/article/10.1186/s12909-024-06026-5 (LLM ASAG in medical education, precision/leniency)
- https://dl.acm.org/doi/10.1145/3706468.3706481 (LAK 2025, GPT-4 prompt engineering vs traditional models)
- https://arxiv.org/html/2502.13337 (Zhao et al. 2025, few-shot LLM graders, RAG example selection)
- https://aclanthology.org/2022.acl-long.587/ (Filighera et al. 2022, SAF dataset)
- https://huggingface.co/datasets/nkazi/SciEntsBank (dataset mirror)
- https://huggingface.co/datasets/Short-Answer-Feedback/saf_communication_networks_english (SAF English)
