---
title: "Misconception Banks in Intelligent Tutoring Systems — Prior-Art Survey"
subtitle: "Authored vs. mined vs. LLM-generated misconceptions, and what it means for Apollo"
date: 2026-06-23
status: research-memo
audience: Apollo engineering team
owns: []
last_verified: 2026-06-23
---

# Misconception Banks in ITS: Authored, Mined, or Generated?

## Context and the question we are answering

Apollo grades a student's spoken explanation against a knowledge graph and
penalizes a *soundness* score when the student asserts a known **misconception**.
Today that misconception bank is **hand-authored by curriculum experts**. We are
evaluating two alternatives:

1. **Automatic generation at provisioning time** — an LLM proposes the
   misconception set when a course/topic is first set up.
2. **Dynamic mining over time** — misconceptions are discovered from real
   student data as the system runs.

This memo surveys how established ITSs build their misconception/error libraries,
whether "dynamic/automatic misconception generation" has real precedent, and the
risks the literature flags. **Bottom line up front:** authoring is still the
dominant practice for the *misconceptions themselves*; data-driven *refinement of
the skill/knowledge model* and data-driven *hint generation* are well-established;
fully automatic *generation of valid misconceptions* (vs. detection/ranking of
existing ones) is the newest and weakest link, with consistent evidence that
LLM-only misconception generation is pedagogically unreliable and needs expert
vetting. The single closest large-scale precedent to our scenario is the
2025 Vanderbilt/Eedi **"Charting Student Math Misunderstandings"** effort, which
pairs an **expert-authored misconception taxonomy** with **LLM/ML classification
of misconceptions from free-text student explanations** — i.e., humans author the
bank, models *detect* against it. That is essentially the architecture we already
have; the literature suggests keeping the bank human-curated and using the LLM for
detection, with mining used to *propose candidates for human review*, not to
auto-publish.

---

## Q1. MATHia / Cognitive Tutor (Carnegie Learning): how the "buggy rule" library is built

**Short answer: the misconceptions themselves are hand-authored as "buggy
production rules" inside an expert cognitive model. Data-driven methods (DataShop,
learning-curve analysis, LFA/AFM, Q-matrix refinement) are used to refine the
*knowledge-component model* — i.e., the decomposition of skills — not to *discover
new misconceptions*. Discovery is human-mediated; the search proposes KC splits,
humans interpret them.**

### The cognitive-model / buggy-production approach (Anderson, Koedinger, ACT-R)

Cognitive Tutors are grounded in ACT-R (Anderson 1993; Anderson & Lebiere 1998).
Procedural knowledge is represented as **if-then production rules**; the expert
"cognitive model" encodes the multiple correct strategies students might use **plus
their typical misconceptions, encoded as "buggy" productions**
(Koedinger & Corbett 2006; Anderson, Corbett, Koedinger & Pelletier 1995). A buggy
production fires when the student's action matches a known incorrect step, and an
attached **bug-message template** generates the feedback. Quoting the lineage
directly:

> "A cognitive model uses a production system to represent the multiple strategies
> students might employ as well as their typical student misconceptions… [a] 'buggy'
> production that represents a misconception (cf. Matz, 1982)."
> — Koedinger & Corbett, *The Cambridge Handbook of the Learning Sciences*, 2006.

> "The expert model contains the complete set of productions needed to solve the
> problems, as well as the 'buggy' productions. Each buggy production represents a
> commonly occurring incorrect step."
> — Heffernan et al. (Ms. Lindquist / ATM architecture).

These buggy rules are **authored by hand** using Cognitive Task Analysis and the
Cognitive Tutor Authoring Tools (CTAT); authoring them "required AI programming
skills to build the cognitive model and task analysis… Authors wrote production
rules to characterize the variety of strategies and misconceptions students
exhibited" (Aleven et al. 2006; Koedinger et al. 2004, via ScienceDirect "Cognitive
Tutor" overview). Anderson et al.'s own *Lessons Learned* is explicit that the bug
library is shallow-by-design and authored: "This requires writing buggy productions
and attaching instruction… In general we do not attempt to provide any deep
diagnosis of the cognitive origins of the error. Rather we simply try to explain
why it is an error." MATHia is the modern descendant of this Cognitive Tutor line
(Ritter, Anderson, Koedinger & Corbett 2007); the model-tracing + buggy-rule
machinery is the same.

### The data-driven part: KC-model refinement, NOT misconception discovery

Carnegie Learning / PSLC pioneered **data-driven improvement of the *student
model***, but it targets the **knowledge-component (KC) / Q-matrix** decomposition,
not the bug set:

- **Learning-curve analysis on DataShop.** A good KC produces a smooth,
  downward-sloping error-rate curve; a flat/spiky/rising curve flags a *mis-specified
  KC* worth splitting (DataShop "Learning Curve" help; Koedinger & Mathan 2004).
- **Learning Factors Analysis (LFA) + Additive Factors Model (AFM).** AFM is an
  IRT-style logistic model over the Q-matrix `q_jk`; LFA does heuristic search over
  KC models scored by AIC/BIC/cross-validation (Cen, Koedinger & Junker 2006).
- **Automated student-model improvement at scale.** Koedinger, McLaughlin & Stamper
  (EDM 2012) ran this over 11 datasets and "in at least ten of the eleven cases…
  discover[ed] improved models," then used the *flaws* to suggest tutor redesign.
- **"Closing the loop."** Stamper & Koedinger (2011) and the JEDM "Closing the loop"
  paper show a *human-in-the-loop* cycle: the algorithm flags a problematic KC, a
  human interprets the split (e.g., separating "decomposition planning" from
  "execution"), the tutor is changed, and a controlled study confirms the gain.

The key nuance for us: the cognitive-modeling literature explicitly contrasts these
data-driven methods with the older, fully expert approach and notes that
**expert-engineered models "often ignore content distinctions that are important for
novice learners"** (JEDM "Closing the loop," citing Nathan, Koedinger & Alibali 2001;
Koedinger & McLaughlin 2010) — a strong argument that *pure* expert authoring misses
things, and *data* helps find them. But the discovered objects are **skill
distinctions (KCs)**, surfaced for human interpretation, **not auto-generated
misconception statements**. Full Q-matrix discovery "from scratch" is possible (Liu
et al. 2012; Desmarais 2012; González-Brenes & Mostow 2013) but is noted as "hard to
understand"; refining an existing Q-matrix is preferred precisely because a human
only has to interpret a *change* (Koedinger et al. 2012).

**Verdict (Q1):** Misconceptions = **authored** buggy productions. Data is used for
**human-mediated refinement of the skill model**, not autonomous misconception
discovery.

---

## Q2. Other major ITS paradigms — how each gets its misconceptions/errors

### Constraint-Based Modeling (CBM): SQL-Tutor / Ohlsson / Mitrović

CBM **deliberately avoids modeling misconceptions at all**, and that is its central
design claim. Ohlsson's insight: "the space of incorrect knowledge is vast,"
therefore model only **correct** domain principles as **state constraints**
`(Cr, Cs)` — "if relevance condition `Cr` holds, satisfaction condition `Cs` had
better also hold, otherwise something has gone wrong" (Ohlsson 1992/1994; Mitrović &
Ohlsson 1999). A **violated constraint** *is* the error signal; there is **no
explicit buggy-rule library**:

> "An intelligent tutoring system… could be built around a knowledge base of
> constraints that encode correct domain knowledge, **without an explicit or
> generative model of students' buggy skills** and hence without the need for labor
> intensive empirical studies to identify the latter (Ohlsson, 1992)."
> — Ohlsson & Mitrović, *Constraint-Based Modeling: From Cognitive Theory to
> Computer Implementation*, IJAIED 2015.

So "misconceptions" in CBM are **authored implicitly** as the complement of
correctness: each constraint is hand-written by analyzing the domain (SQL-Tutor:
350–500+ constraints "acquired by analyzing the domain knowledge"); a violation maps
to "incomplete or incorrect knowledge" and carries a feedback message naming the
violated principle. Authoring effort is real but is framed as *easier than* writing
production/bug models (Mitrović et al. 2003). The **ASPIRE/WETAS** authoring tools
semi-automate constraint generation: syntax constraints are **induced automatically
from a domain ontology**, semantic constraints are generated with the author's help
(Mitrović, Suraweera et al., ASPIRE, ITS 2006). That ontology-driven *constraint*
induction is the closest CBM gets to "automatic error generation," and it is still
expert-seeded. Key references: Mitrović & Ohlsson "SQL-Tutor after Fifteen Years"
(IJAIED 2016); "Fifteen years of constraint-based tutors" (2012).

**Verdict:** errors are authored **negatively** (as correctness constraints); no
mined/generated misconception list.

### ANDES / physics tutors (VanLehn, Conati, Gertner)

ANDES does **not** maintain a buggy-rule misconception library either. It builds a
**Bayesian network per problem** whose nodes are physics **rules**, facts, and
goals; correct student steps raise the posterior on the rules that derive them, and
a long-term model carries rule-mastery probabilities across problems (Conati,
Gertner & VanLehn, *Using Bayesian Networks to Manage Uncertainty in Student
Modeling*, UMUAI 2002; Gertner, Conati & VanLehn, AAAI 1998). For errors, ANDES2
uses **hand-written "error handlers,"** each recognizing a specific error class and
emitting a tailored hint; non-knowledge errors are treated as **slips** (blank
entry, undefined variable, missing units) and merely flagged red (VanLehn et al.,
*The Andes Physics Tutoring System: Lessons Learned / Five Years of Evaluations*,
IJAIED 2005). So: correctness/plan modeling is **probabilistic and rule-based**;
error feedback is **authored** per error handler. No automatic misconception
discovery.

### AutoTutor / DeepTutor — Expectation–Misconception Tailored (EMT) dialogue

This is the paradigm most architecturally similar to Apollo (grade a free-text/
spoken explanation against expected content + known misconceptions). AutoTutor's
**EMT dialogue** stores, in a per-question **"curriculum script," a list of
expectations (good answers) and a list of anticipated misconceptions with canned
corrective feedback** (Graesser, Person & Magliano 1995; Graesser et al. 2001, 2004;
Olney et al. AutoTutor chapter). As the learner talks, utterances are matched
(historically via LSA/regex, later via response classifiers) to expectations and
misconceptions, and the tutor corrects any misconception it recognizes.

Crucially, **these misconception lists are hand-authored** as part of script
creation — and the authoring difficulty is acknowledged in the source material:

> "A misconception is a typical wrong answer based on incorrect thinking… **It is
> usually hard for authors to pre-imag[in]e what misconception learners may have.
> Therefore, misconceptions are usually added when they are identified from
> learner's inputs.** It is fine to keep [the] misconception element blank in an
> initial EMT script."
> — AutoTutor EMT authoring description (ERIC ED618768).

That is an explicit, in-paradigm endorsement of **iteratively growing the
misconception bank from observed student data** — but the loop is human-curated, not
autonomous. DeepTutor (Rus, Niraula & Banjade, AAAI 2015) and ETS's WDBT scale the
same EMT model (a set of expectations + "a set of invalid answers frequently
expressed by students (misconceptions)") with a two-level response classifier;
misconception statements and their feedback are **authored content** aligned to
learning objectives.

### ASSISTments — "Common Wrong Answer" (CWA) feedback

ASSISTments is the clearest example of **data-mined** misconception triggers in a
deployed K-12 system. "Buggy messages" originally fired on **hand-anticipated** wrong
answers (Razzaq & Heffernan; the canonical Figure-1 example uses "the most common
wrong answer for this item **from the data collected**"). More recently the team
**mines Common Wrong Answers directly from response distributions** across years of
logs:

- Gurung et al. (LAK 2023; L@S 2023, *How Common are Common Wrong Answers?*) analyzed
  2015–2020 logs across Illustrative Math and EngageNY, taking each student's **first
  incorrect attempt** to compute the top-3 CWAs per problem, with a commonality
  threshold (≥20 students attempted, ≥10 gave the same wrong answer). They found
  **1,045 problems with CWAs stable across ≥2 academic years** — i.e., wrong answers
  are *empirically* common and persistent.
- The **feedback** for each CWA (CWAF) is then **crowd-authored by teachers**, and
  its effect tested in vivo (randomized): CWAFs significantly improved next-problem
  correctness within-skill. So: **CWA triggers are data-mined; CWA feedback messages
  are human-written.**
- A complementary study (Gurung et al., NSF 2023, *Identification, Exploration, and
  Remediation*) asked whether **teachers can predict CWAs proactively** — directly
  probing authored-vs-mined — and found prediction imperfect, motivating the
  data-driven approach.
- Caveat from the same group: Selent & Heffernan (AIED 2015), "When More Intelligent
  Tutoring in the Form of Buggy Messages Does **not** Help" — buggy/CWA messages are
  not universally beneficial.

**Verdict (Q2):** Across paradigms, the *trigger* for an error response is sometimes
mined (ASSISTments CWAs) but usually authored (CBM constraints, ANDES error
handlers, AutoTutor EMT misconceptions). The *feedback content* is almost always
human-written. AutoTutor's own docs and ASSISTments both endorse **growing the bank
from observed student data with humans in the loop** — precedent for our "dynamic
mining" idea, but not for autonomous generation.

---

## Q3. Data-driven / automatic error & misconception discovery (the core question)

**This has a long, real track record — but mostly for (a) discovering *procedural
bugs* in narrow, formal domains, (b) discovering/refining the *skill model*, and
(c) data-driven *hint* generation. Mining a *misconception bank* in the sense of
"surface a ranked list of student misconceptions for humans to adopt" is
established as a *candidate-generation* step, not as an autonomous publisher.**

### Historical precedent: BUGGY / DEBUGGY and Repair Theory (the original "automatic bug discovery")

The foundational result is 45 years old. Brown & Burton (*Diagnostic Models for
Procedural Bugs in Basic Mathematical Skills*, Cognitive Science 1978) built
**procedural networks** that **automatically synthesize a diagnostic model of a
student's bugs** as perturbations of a correct skill, validated against 20,000 test
items from 1,300 students. VanLehn's **DEBUGGY** could diagnose multi-digit
subtraction bugs **as well as or better than human expert diagnosticians**
(VanLehn 1982, ERIC ED245880). Brown & VanLehn's **Repair Theory** (*A Generative
Theory of Bugs in Procedural Skills*, Cognitive Science 1980) went further — it is a
**generative** theory that *predicts which bugs will occur* for a procedural skill
not yet analyzed, by applying deletion operators to a correct procedure and
"repairing" the resulting impasses, while explicitly **not** generating absurd
"star-bugs." Quoting VanLehn: "We could automatically generate a list of bugs for a
new skill and add these bugs to DEBUGGY, creating a diagnostic system tailored to
the new skill."

So **autonomous, generative misconception/bug enumeration is precedented** — but
only in **tightly formal, enumerable procedural domains** (subtraction, fraction
arithmetic). It does not obviously transfer to open conceptual explanations of the
kind Apollo grades.

### Data-driven hint generation: the Hint Factory (Barnes & Stamper)

The Hint Factory (Barnes & Stamper, ITS 2008; EDM 2008; IJAIED 2013) mines a
**Markov Decision Process from past student solution traces** and uses value
iteration to emit next-step hints — **no expert-authored hints, no expert bug
library**. It is the canonical proof that you can **replace authored remediation
with mined remediation**: "this approach differs from prior work… by mining actual
student data, rather than relying on teachers." Limits the authors themselves flag:
**cold-start** (a brand-new problem has no data → no hints until a semester of data
accrues; mitigated by **expert "seeding"** with a few sample solutions, Stamper et
al.), coverage gaps, and that step-level hints don't teach high-level transfer.
This is strong precedent for the *mining* half of our idea, and the cold-start +
seeding pattern maps directly onto Apollo provisioning.

### Mining misconceptions from wrong-answer distributions: Eedi Diagnostic Questions

Eedi's **Diagnostic Questions** are MCQs where **each distractor is deliberately
written to embody a specific common misconception** (Wang et al., *Diagnostic
Questions: The NeurIPS 2020 Education Challenge*, arXiv:2007.12061; PMLR v133 2021).
The dataset (≈20M+ answers, 125k students, 28k questions) lets you **infer
misconceptions from the *option* a student picks** ("option tracing"), and clusters
of correlated wrong-answer choices indicate shared misconceptions. But note the
honest provenance: **the distractor-to-misconception authoring is human/crowd**, and
the original data did **not even label which misconception each distractor
encodes** — Eedi later ran a *separate* Kaggle challenge ("Eedi — Mining
Misconceptions in Mathematics") just to **predict the misconception label for a
distractor**, and found it hard, especially for **unseen** misconceptions not in
training (Eedi blog, *From Wrong Answers to Real Insights*). Winning solutions used
retrieval + LLM rerankers (Qwen2.5, Claude-generated rationales) — i.e., **detection/
matching against a human taxonomy, not generation of the taxonomy**.

### Q-matrix / Knowledge-Component discovery

As covered in Q1: fully data-driven Q-matrix discovery exists (Tatsuoka's Q-matrix;
Liu et al. 2012; Desmarais 2012; González-Brenes & Mostow 2013) but is hard to
interpret; the field's working compromise is **human-mediated refinement** of an
existing model (LFA/AFM on DataShop). These discover **skill structure**, which is
adjacent to but not the same as a misconception bank.

**Verdict (Q3):** Data-driven discovery is real and decades-deep, but the mature,
trusted forms are: (i) generative bug enumeration in *formal procedural* domains
(Repair Theory), (ii) mined *hints* (Hint Factory), (iii) mined *trigger statistics*
(ASSISTments CWAs, Eedi option distributions), and (iv) human-mediated *skill-model*
refinement. In every conceptual-domain case, the **misconception label/taxonomy
stays human-authored** and data is used to **detect, rank, or propose-for-review** —
not to autonomously author the bank. Known limitations: cold-start/data-hunger,
poor generalization of mined patterns to unseen items, interpretability of
auto-discovered structures, and the gap between "a frequent wrong answer" and "a
correctly-named underlying misconception."

---

## Q4. LLM-based misconception / distractor generation and detection (2022–2025)

This is the closest analogue to what we are considering, and the evidence is
**consistent and slightly sobering: LLMs are good at *detecting/classifying*
misconceptions (especially with retrieval + a human taxonomy) but unreliable at
*generating valid* misconceptions on their own.**

### Generation of distractors / misconceptions — repeatedly found weak without grounding

A tight cluster of UMass ML4Ed (Lan group) and collaborators' papers converges on
the same result:

- **McNichols, Feng, Lee, Scarlatos, Smith, Woodhead & Lan (GAIED @ NeurIPS 2023),
  *Automated Distractor and Feedback Generation… via In-context Learning*
  (arXiv:2308.03234).** First systematic LLM study (Codex/ChatGPT/GPT-4, kNN
  in-context). Conclusion: "there is **a lot of room for improvement** in automated
  distractor and feedback generation."
- **Feng et al. (Findings of NAACL 2024), *Exploring Automated Distractor Generation
  for Math MCQs via LLMs* (arXiv:2404.02124).** Human eval verdict: LLM distractors
  are **mathematically valid but "do not necessarily reflect common errors or
  misconceptions among real students"** — they miss the *student-error* signal that
  is the whole point.
- **Scarlatos, Feng, Lan et al. (BEA 2024), *Improving Automated Distractor
  Generation… with Overgenerate-and-rank*.** Train a ranker to predict which
  distractor real students would pick; improves alignment, but "**LLM-generated
  distractors still do not match the quality of human-authored ones in reflecting
  student errors or misconceptions.**"
- **Feng/Lee/Lan et al. (EMNLP 2024), DiVERT — *Distractor Generation with
  Variational Errors Represented as Text*.** Learns an interpretable **error**
  representation behind distractors; a fine-tuned 7B model **beats GPT-4o** on this
  task, and a human eval with math educators finds **DiVERT's error labels are
  comparable to human-authored ones and significantly better than GPT-4o's**. The
  takeaway is double-edged: structured, error-grounded fine-tuning can reach
  human-level *error* quality, but **vanilla GPT-4-class prompting is materially
  worse than humans.**
- **HEDGE — Lee, Smith, Woodhead & Lan (2024), *Math MCQ Generation via Human-LLM
  Collaboration* (arXiv:2405.00864).** The most quotable quality figures for us:
  educators rated **70% of GPT-4 stems/keys/explanations valid (avg 4.0/5)** but
  only **37% of generated misconceptions/distractors/feedback valid (avg 2.5/5)**,
  across four math-teacher evaluators. Conclusion: "LLMs often fail to anticipate
  valid misconceptions… making **human educators' involvement crucial**." This is
  near-direct evidence that **auto-generating a misconception bank at provisioning
  time would ship a large fraction of invalid items absent expert vetting.**

Generalizing beyond math: LLM-built **educational knowledge graphs** hit the same
wall. Graphusion (zero-shot KG construction/fusion, arXiv:2407.10794) and SciMKG
both wrap LLM extraction in **explicit Verification / conflict-resolution / self-
refine** stages precisely because raw LLM triples include hallucinated or incorrect
relations — i.e., **automatic generation of a knowledge structure requires a
verification gate**, which is the analogue of expert vetting for a misconception
bank.

### Detection of misconceptions from free-text — much stronger, and the real growth area

When the task is flipped to **detecting** a misconception in a student's free-text
explanation (Apollo's actual job), results are encouraging — *especially* with
retrieval against a curated bank:

- **MAP — "Charting Student Math Misunderstandings" (Vanderbilt University + The
  Learning Agency, with Eedi/NAEP data; Kaggle 2025).** The single most on-point
  precedent. **Misconception categories were authored by Vanderbilt content
  experts** (grounded in math-cognition research) and applied by **15 trained
  annotators** to **real student free-text explanations**; the modeling task was to
  **predict the misconception from the explanation**. Scale: 1,850+ teams, ~40k
  submissions, top **MAP@3 > 0.948**. Architecture = **expert-authored taxonomy +
  ML/LLM detection** — exactly the authored-bank + LLM-detector split. (Companion:
  *Detecting Math Misconceptions: An AI Benchmark Dataset*, aimecon-wip 2025.)
- **MiRAGE (arXiv:2511.01182, 2025)** — retrieval-guided multi-stage CoT + ensemble
  for misconception detection in math free-text; MAP **0.82/0.92/0.93** at levels
  1/3/5; explicitly motivated by the fact that **pure LLMs "hallucinate" and lack
  interpretability**, so it retrieves against a candidate pool first.
- **Reasoning-Enhanced Retrieval (RAG-inspired)** for misconception prediction
  (sciprodllm 2025) — two-stage retrieve-then-LLM-rerank over a misconception set;
  frames misconception detection as a **knowledge-validation** problem.
- **RAMP (FLAIRS-39, 2026)** — for *physics* free-text, a fine-tuned **ModernBERT**
  classifier **outperformed several LLM prompting approaches** at detecting a motion
  misconception; positioned as an instructor-facing reporting tool.
- **Code-explanation gap/misconception detection (Oli et al. 2024, ProMLR; "Can LLMs
  Identify Gaps and Misconceptions in Students' Code Explanations?" arXiv:2501.10365).**
  GPT-4 best at prompting; fine-tuning (SFT/ORPO) improves open models substantially;
  **but LLMs hallucinated false problem-identifications in ~27–34% of cases when the
  explanation was actually correct** — a precision/false-positive risk that maps
  directly onto Apollo wrongly penalizing soundness.
- **Stepwise verification & remediation of reasoning errors (EMNLP 2024)** and
  **LLM math-skill diagnosis at scale (L@S 2024 WIP)** — LLM pipelines that locate
  and describe the student's *first error step*; more actionable than a bare label.
- Community signal: Kaggle MAP silver-medal LoRA fine-tunes of Qwen3-14B reach
  **MAP@3 ≈ 0.94** for misconception classification, but the model cards explicitly
  warn "**designed to support educators, not replace them… not for grading or
  high-stakes testing without human review.**"

**Validity / risk findings that recur across this literature:**
1. LLMs generate **fluent and often *mathematically* valid** wrong answers but
   **systematically miss the *student-authentic* misconception** (Feng 2024;
   Scarlatos 2024; HEDGE 2024).
2. **Roughly a third** of LLM-generated misconception content is judged invalid by
   teachers without vetting (HEDGE: 37% valid → 63% not).
3. LLM **detection** has **false-positive/hallucination** risk (~27–34% on correct
   code explanations; MiRAGE/RAG papers cite hallucination as the core motivation
   for retrieval grounding).
4. **Grounding fixes both:** retrieval against a curated bank (MiRAGE, RAG, Eedi
   rerankers) and/or **structured error-grounded fine-tuning** (DiVERT) close most
   of the gap and can reach human-level quality — but they presuppose a
   **human-curated bank or labeled error set** to retrieve/learn from.

---

## Q5. Bottom line for Apollo

**Does our scenario have precedent?** Partly.

- **"LLM-graded tutor that grades a free-text explanation against expectations +
  known misconceptions"** — strong precedent: this is exactly AutoTutor/DeepTutor's
  EMT design and the Vanderbilt/Eedi MAP detection task. Keep it.
- **"Misconception bank authored by curriculum experts"** — this is the **dominant
  practice across every major paradigm** (Cognitive Tutor/MATHia buggy productions,
  CBM constraints, ANDES error handlers, AutoTutor EMT scripts). We are in good
  company; authoring is the default for a reason.
- **"Dynamic mining of misconceptions from real student data over time"** —
  precedented as **candidate generation with humans in the loop**: ASSISTments mines
  CWAs from response distributions (then teachers write the feedback); AutoTutor's
  own guidance is to add misconceptions "**when they are identified from learner's
  inputs**"; the Hint Factory mines remediation directly. **No major deployed system
  auto-*publishes* mined misconceptions without human review.**
- **"Automatic LLM generation of the misconception set at provisioning time"** —
  **weakest precedent and the literature's clearest red flag.** Every controlled
  human evaluation (HEDGE 37% valid; Feng 2024; Scarlatos 2024) finds LLM-only
  misconception/distractor generation **pedagogically unreliable** — fluent but
  failing to capture authentic student errors — and concludes humans are required.
  The one bright spot (DiVERT beating GPT-4o) required **error-grounded fine-tuning
  on a human-labeled dataset**, not zero/few-shot prompting.

**Recommended posture (what the evidence supports):**

1. **Keep the bank human-authoritative.** Treat any LLM- or data-derived
   misconception as a **proposal**, not a published soundness-penalizing rule, until
   a curriculum expert vets it. This mirrors DataShop's human-mediated KC refinement,
   ASSISTments' teacher-written CWAFs, and the verification gates in LLM-KG pipelines.
2. **Use the LLM for *detection*, grounded by *retrieval* against the curated bank** —
   not free generation. This is the MiRAGE/RAG/Eedi-reranker pattern and matches what
   already works at MAP-competition scale (MAP@3 > 0.9). Critically, **grounding is
   also what curbs hallucination/false positives**, which for Apollo means *not
   wrongly penalizing soundness* (cf. the ~27–34% false-positive rate on correct code
   explanations).
3. **If we generate candidates, ground and rank them.** Use *overgenerate-and-rank*
   against any student-response signal we have (DiVERT/Scarlatos), and **expect a
   human vetting pass** — budget for ~⅓ of raw LLM candidates being invalid.
4. **For dynamic mining, copy the proven loop:** mine *frequent wrong patterns /
   recurrent unsound assertions* from session logs (ASSISTments-style thresholds;
   Hint-Factory-style trace mining), **surface clusters for expert labeling**, and
   only then admit them to the bank. Plan for **cold-start** (no data at provisioning)
   with **expert seeding**, as the Hint Factory does.
5. **Validate the *effect*, not just the artifact.** ASSISTments (Selent & Heffernan
   2015) shows buggy/misconception feedback does not always help — A/B the
   soundness-penalty behavior, don't assume an auto-grown bank improves outcomes.

**One-line synthesis:** The field's settled answer is *authored bank + data/LLM as a
detection-and-candidate-generation aid, always with a human gate* — autonomous
generation of a valid misconception bank is not yet a trusted practice, and the
strongest results that approach it all reintroduce either a human taxonomy to
retrieve against or human-labeled errors to fine-tune on.

---

## Sources

**Q1 — MATHia / Cognitive Tutor / DataShop**
- Koedinger, K. R., & Corbett, A. (2006). *Cognitive Tutors: Technology Bringing Learning Sciences to the Classroom.* In *The Cambridge Handbook of the Learning Sciences.* https://wiki.rice.edu/confluence/download/attachments/2765648/KoedingerCorbett05.pdf
- Anderson, J. R., Corbett, A. T., Koedinger, K. R., & Pelletier, R. (1995). *Cognitive Tutors: Lessons Learned.* J. Learning Sciences. https://apps.dtic.mil/sti/tr/pdf/ADA312246.pdf | http://act-r.psy.cmu.edu/papers/Lessons_Learned.html
- Koedinger, K. R., & Anderson, J. R. (1998). *Illustrating Principled Design: The Early Evolution of a Cognitive Tutor for Algebra Symbolization.* https://pact.cs.cmu.edu/koedinger/pubs/Koedinger%20&%20Anderson%2098.pdf
- Aleven, V. (2010) / Koedinger et al. — Cognitive Tutors & model tracing overview. https://learnlab.org/wp-content/uploads/2025/07/aleven-2010.pdf
- "Cognitive Tutor — an overview." ScienceDirect Topics (CTAT authoring of buggy productions; Aleven et al. 2006; Koedinger et al. 2004). https://www.sciencedirect.com/topics/computer-science/cognitive-tutor
- Heffernan, N. T. et al. — Ms. Lindquist / ATM architecture (expert model = correct + buggy productions). https://web.cs.wpi.edu/~nth/pubs_and_grants/papers/journals/IJAIED204Heffernanv1.pdf
- Cen, H., Koedinger, K., & Junker, B. (2006). *Learning Factors Analysis.* (AFM/LFA). See DataShop Learning Curve help. https://pslcdatashop.web.cmu.edu/help?page=learningCurve
- Koedinger, K. R., McLaughlin, E. A., & Stamper, J. C. (EDM 2012). *Automated Student Model Improvement.* https://learnlab.org/wp-content/uploads/2016/06/KoedingerMcLaughlinStamperEDM12.pdf
- Koedinger, Stamper, McLaughlin & Nixon (AIED 2013). *Using Data-Driven Discovery of Better Student Models to Improve Student Learning.* http://dev.stamper.org/publications/AIED_2013_Koedinger_et_al.pdf
- *Closing the loop: Automated data-driven cognitive model discoveries…* JEDM. https://jedm.educationaldatamining.org/index.php/JEDM/article/download/212/pdf_29
- DataShop KC Models help (Q-matrix, step-to-KC mapping). https://pslcdatashop.web.cmu.edu/help?page=kcm
- *Most of the Time, It Works Every Time* (JEDM) — Q-matrix discovery vs. refinement. https://jedm.educationaldatamining.org/index.php/JEDM/article/download/316/95
- Fancsali, S. (EDM 2015). *Carnegie Learning's Adaptive Learning Products* (CT → MATHia). https://www.educationaldatamining.org/EDM2015/uploads/papers/paper_263.pdf

**Q2 — CBM, ANDES, AutoTutor/DeepTutor, ASSISTments**
- Ohlsson, S., & Mitrović, A. (2015). *Constraint-Based Modeling: From Cognitive Theory to Computer Implementation.* IJAIED. https://link.springer.com/article/10.1007/s40593-015-0075-7
- Mitrović, A. — *SQL-Tutor* (CBM, ~350–500 constraints). https://www.csse.canterbury.ac.nz/tanja.mitrovic/702.pdf | *Constraint-Based Tutors: A Success Story.* https://www.csse.canterbury.ac.nz/tanja.mitrovic/cbmtut.pdf
- Mitrović, A., & Ohlsson, S. (2016). *Implementing CBM: SQL-Tutor after Fifteen Years.* IJAIED. https://eric.ed.gov/?id=EJ1091189
- Suraweera, P., Mitrović, A., et al. *Authoring Constraint-based Tutors in ASPIRE* (ITS 2006; ontology-induced constraints). https://www.csse.canterbury.ac.nz/tanja.mitrovic/ASPIRE-ITS06.pdf
- Mitrović, A. — *Fifteen years of constraint-based tutors.* https://www.researchgate.net/publication/225364176
- Gertner, A. S., Conati, C., & VanLehn, K. (AAAI 1998). *Procedural Help in Andes: Generating Hints Using a Bayesian Network Student Model.* https://mlanthology.org/aaai/1998/gertner1998aaai-procedural/
- Conati, C., Gertner, A., & VanLehn, K. (2002). *Using Bayesian Networks to Manage Uncertainty in Student Modeling.* UMUAI 12(4):371–417. https://asu.elsevierpure.com/en/publications/using-bayesian-networks-to-manage-uncertainty-in-student-modeling/
- VanLehn, K. et al. (2005). *The Andes Physics Tutoring System: Lessons Learned / Five Years of Evaluations.* IJAIED. https://oli.cmu.edu/wp-content/uploads/2012/05/VanLehn_2005_Andes_Physics_Tutoring_System.pdf | …/VanLehn_2005_Andes_Five_Years_of_Evaluations.pdf
- Graesser, A. C. et al. — AutoTutor / EMT dialogue. https://link.springer.com/article/10.3758/BF03195563 | Olney AutoTutor chapter: https://blogs.memphis.edu/aolney/files/2019/10/AutoTutor-chapter-olney_publications.pdf
- AutoTutor EMT authoring ("misconceptions are usually added when identified from learner's inputs"). https://files.eric.ed.gov/fulltext/ED618768.pdf
- Rus, V., Niraula, N., & Banjade, R. (AAAI 2015). *DeepTutor.* https://ojs.aaai.org/index.php/AAAI/article/view/9269
- ETS WDBT — *Dialogue-based tutoring at scale.* https://ceur-ws.org/Vol-2128/industrial1.pdf
- Feng, M., Heffernan, N., & Koedinger, K. (UMUAI 2008/2009). *Addressing the Assessment Challenge with… ASSISTments* (CWA buggy messages). https://web.cs.wpi.edu/~nth/pubs_and_grants/papers/journals/UMUAI-Feng_Heffernan_Koedinger_08-sub.pdf
- Gurung, A. et al. (L@S 2023). *How Common are Common Wrong Answers? Crowdsourcing Remediation at Scale.* https://par.nsf.gov/servlets/purl/10417271
- Gurung, A., …, Heffernan, N. (LAK 2023). *Identification, Exploration, and Remediation: Can Teachers Predict Common Wrong Answers?* https://par.nsf.gov/biblio/10451146
- Selent, D., & Heffernan, N. (AIED 2015). *When More Intelligent Tutoring in the Form of Buggy Messages Does not Help.* (via ASSISTments dataset paper) https://dl.acm.org/doi/10.1145/2876034.2893409

**Q3 — Data-driven bug/misconception discovery; Hint Factory; Eedi**
- Brown, J. S., & Burton, R. R. (1978). *Diagnostic Models for Procedural Bugs in Basic Mathematical Skills.* Cognitive Science 2(2). https://exquisitive.com/library/DiagnosticModelsForProceduralBugs.pdf
- Brown, J. S., & VanLehn, K. (1980). *Repair Theory: A Generative Theory of Bugs in Procedural Skills.* Cognitive Science 4(4):379–426. https://www.lri.fr/~mbl/Stanford/CS477/papers/RepairTheory-SeelyBrown.pdf | https://asu.elsevierpure.com/en/publications/repair-theory-a-generative-theory-of-bugs-in-procedural-skills-3/
- VanLehn, K. (1982). *Empirical Studies of Procedural Flaws, Impasses, and Repairs in Procedural Skills* (BUGGY/DEBUGGY). https://eric.ed.gov/?id=ED245880 | https://files.eric.ed.gov/fulltext/ED245880.pdf
- Barnes, T., & Stamper, J. (ITS 2008). *Toward Automatic Hint Generation… Using a Markov Decision Process.* http://dev.stamper.org/publications/ITS2008BarnesStamper.pdf
- Barnes, Stamper, Lehman & Croy (EDM 2008). *A Pilot Study on Logic Proof Tutoring…* https://educationaldatamining.org/EDM2008/uploads/proc/22_Barnes_41a.pdf
- Stamper, J. et al. (IJAIED 2013). *Experimental Evaluation of Automatic Hint Generation for a Logic Tutor.* http://dev.stamper.org/publications/IJAIED2013_Stamper_et_al.pdf
- *Enhancing the automatic generation of hints with expert seeding* (IJAIED). https://dl.acm.org/doi/abs/10.5555/2336135.2336144
- Wang, Z. et al. (2020/2021). *Diagnostic Questions: The NeurIPS 2020 Education Challenge* + *Results and Insights.* https://arxiv.org/abs/2007.12061 | https://proceedings.mlr.press/v133/wang21a.html
- Eedi. *From Wrong Answers to Real Insights — Mining Misconceptions in Mathematics (Kaggle).* https://www.eedi.com/news/from-wrong-answers-to-real-insights-how-we-used-a-kaggle-challenge-to-map-student-misconceptions

**Q4 — LLM misconception/distractor generation & detection**
- McNichols, H., Feng, W., Lee, J., Scarlatos, A., Smith, D., Woodhead, S., & Lan, A. (GAIED @ NeurIPS 2023). *Automated Distractor and Feedback Generation for Math MCQs via In-context Learning.* https://arxiv.org/abs/2308.03234 | https://people.umass.edu/~andrewlan/papers/23gaied-mathmcq.pdf
- Feng, W., Lee, J., McNichols, H., Scarlatos, A., Smith, D., Woodhead, S., Ornelas, N., & Lan, A. (Findings of NAACL 2024). *Exploring Automated Distractor Generation for Math MCQs via LLMs.* https://aclanthology.org/2024.findings-naacl.193.pdf
- Scarlatos, A., Feng, W., et al. (BEA 2024). *Improving Automated Distractor Generation for Math MCQs with Overgenerate-and-rank.* https://aclanthology.org/2024.bea-1.19.pdf | https://github.com/umass-ml4ed/distractor-ranking-BEA
- Feng, W., Lee, J., et al. (EMNLP 2024). *DiVERT: Distractor Generation with Variational Errors Represented as Text.* https://aclanthology.org/2024.emnlp-main.512.pdf
- Lee, J., Smith, D., Woodhead, S., & Lan, A. (2024). *Math MCQ Generation via Human-LLM Collaboration (HEDGE).* https://arxiv.org/html/2405.00864v1
- The Learning Agency & Vanderbilt University (Kaggle 2025). *MAP — Charting Student Math Misunderstandings* (expert taxonomy + free-text misconception classification; MAP@3 > 0.948). https://the-learning-agency.com/the-cutting-ed/article/case-study-math-misconceptions-competition/ | https://www.kaggle.com/competitions/map-charting-student-math-misunderstandings
- *Detecting Math Misconceptions: An AI Benchmark Dataset* (AIMECON WIP 2025). https://aclanthology.org/2025.aimecon-wip.3.pdf
- *MiRAGE: Misconception Detection with Retrieval-Guided Multi-Stage Reasoning and Ensemble Fusion* (2025). https://arxiv.org/html/2511.01182v1
- *Reasoning-Enhanced Retrieval for Misconception Prediction: A RAG-Inspired Approach with LLMs* (SciProdLLM 2025). https://aclanthology.org/2025.sciprodllm-1.5.pdf
- Luedeke, E. et al. (FLAIRS-39, 2026). *RAMP: Detecting Physics Student Misconceptions in Writing Assignments Using LLMs* (ModernBERT > LLM prompting). https://journals.flvc.org/FLAIRS/article/view/141769
- Oli, P. et al. (2024). *Can LLMs Identify Gaps and Misconceptions in Students' Code Explanations?* (SFT/ORPO; ~27–34% false positives on correct explanations). https://arxiv.org/html/2501.10365v1 | *Automated Assessment of Students' Code Comprehension using LLM* (PMLR 257). https://proceedings.mlr.press/v257/oli24a.html
- *Assessing Student Explanations with LLMs Using Fine-Tuning and Few-Shot Learning* (BEA 2024). https://aclanthology.org/2024.bea-1.33.pdf
- *Stepwise Verification and Remediation of Student Reasoning Errors with LLM Tutors* (EMNLP 2024). https://aclanthology.org/2024.emnlp-main.478.pdf
- *Using LLMs to Diagnose Math Problem-solving Skills at Scale* (L@S 2024 WIP). https://yoonsu0816.github.io/assets/files/l@s2024-wip.pdf
- LLM + Education KGs with verification gates: *Graphusion* (https://arxiv.org/html/2407.10794v1); *SciMKG* (https://github.com/kg-bnu/SciMKG)
- Kaggle MAP community fine-tune (Qwen3-14B LoRA, MAP@3 ≈ 0.944; "support educators, not replace them"). https://huggingface.co/jatinmehra/Qwen-3-14B-MATH-Misconception-Annotation-Project
