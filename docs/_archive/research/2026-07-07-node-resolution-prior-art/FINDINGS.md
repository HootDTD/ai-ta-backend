# Node-resolution prior art — final findings

Synthesis of 12 fact-checked research memos on Apollo's resolver-recall problem
(free student transcript → canonical reference-KG node/edge alignment). Date: 2026-07-07.
The memos are the drill-down; this document is the decision layer. Every ranked move
points to at least one memo. Refuted/unverifiable claims are flagged inline.

---

## 1. Executive summary

Our resolver-recall failure is a **known, 25-year-old problem with a known architecture** — and
we are running that architecture backwards. The literature (ASAG, AutoTutor/EMT dialogue, entity
linking, NLI fact-checking, SMATCH graph metrics) converges on one diagnosis: **poor recall is a
granularity + aggregation + calibration bug, not a weak-matcher problem**, and no system in 20+
years hit near-perfect per-concept recall — they *designed around* a mediocre matcher with partial
credit and clarification dialogue. The 3–5 highest-leverage moves: **(1) Invert the loop** — for
each reference node, score its hypothesis against *all* transcript windows and max-aggregate, so
the parser stops gating which nodes can resolve (SummaC/AlignScore/MiniCheck; AutoTutor's coverage
was always cumulative across turns). **(2) Score every node exhaustively with multiple affirmative
"views" per node, credit = max over views** — with 5–20 nodes the entity-linking candidate-recall
bottleneck inverts, so there is no retrieval loss; single-label-per-node is the single-view failure
(MuVER/SapBERT; hypothesis engineering swings accuracy >12pp). **(3) Make node+edge alignment ONE
joint optimization with graded (not binary) credit** — ILP-Smatch is trivial at 5 nodes and lets a
matched edge pull an unresolvable node onto its reference; s2match/SoftSPICE give paraphrased edges
partial credit without requiring both endpoints resolved, killing the quadratic edge starvation.
**(4) Calibrate thresholds recall-first on a labeled dev set containing negatives; fuse NLI+embedding
scores instead of per-tier caps; cascade to an evidence-scoped LLM only in the gray zone** — our
precision-first fixed floors are exactly the documented over-abstention trap. **(5) Rebuild the
transcript auditor as reference-grounded, per-node binary checks that must quote a verifiable span**
— grounding cut LLM over-crediting 85% in physics grading; it stays a candidate generator, never the
score source. Cross-cutting: build a Nielsen-style per-node gold set (the field's IAA bar is κ≈0.73)
so recall is *measured*, not intuited, and expect a realistic κ≈0.5–0.75 matcher ceiling — bank the
rest with partial credit and clarification.

---

## 2. The landscape map — who owns this, and what they call it

No single survey owns "grade a student-as-teacher transcript against a reference concept graph"; it
is fragmented across ≥5 communities under ≥6 names (-> memo-surveys.md). The names are the search
keys, and each community owns a different stage of our pipeline:

| Our pipeline stage | Community & term | Key prior art |
|---|---|---|
| **Mention/assertion detection** (transcript → candidate statements) | Open IE / SRL / AMR "proposition extraction"; EL "mention detection" | Stock OpenIE collapses on dialogue (0% complete-triple precision, -> memo-semantic-parsing.md); GLiNER zero-shot span spotting (-> memo-entity-linking.md, memo-oss-tools.md) |
| **Candidate generation** (which nodes could a span be?) | EL / clinical "concept normalization": *high-recall candidate generation + rerank* | BLINK two-stage; scispacy char-ngram ANN top-k **thresholdless** (-> memo-entity-linking.md, memo-surveys.md) |
| **Matching / entailment** (does this text express node X?) | ASAG "student response analysis" (SemEval-2013 T7); "reference-answer facets"; AutoTutor "expectation coverage / semantic match"; NLI "fact verification / grounding"; ArgMining "key point matching" | Facet entailment (Nielsen); LSA+RegEx coverage; SummaC/MiniCheck/AlignScore; KPA matchers (-> memo-asag.md, memo-its-kc.md, memo-nli-calibration.md, memo-surveys.md) |
| **Clarification** (resolve a miss via dialogue) | ITS "hint→prompt→assertion"; teachable-agent clarification loops | AutoTutor dialogue backstop; StuBot probe-questions; Curiosity Notebook retry loop (-> memo-its-kc.md, memo-teachable-agents.md) |
| **Edge scoring** (relations between nodes) | Graph-metrics "SMATCH/SPICE/CaRB"; "OKB canonicalization"; zero-shot RE | Joint-alignment triple scoring; s2match/SoftSPICE graded edges; GLiREL (-> memo-graph-alignment.md, memo-semantic-parsing.md, memo-entity-linking.md) |
| **Whole-task framing** | ITS "EMT dialogue"; EDM "KC/Q-matrix tagging"; concept-map "convergence score" | AutoTutor EMT ≈ Apollo structurally; convergence score ≈ our coverage metric (-> memo-surveys.md, memo-concept-maps.md) |

Two framing facts anchor everything below. **(a) AutoTutor's EMT dialogue is structurally identical
to Apollo** — 3–7 expectations/problem, matched per node, coverage cumulative across turns,
sub-threshold expectations trigger targeted follow-ups (confirmed, -> memo-surveys.md, memo-its-kc.md).
We are re-deriving a solved system. **(b) Our node set is tiny (5–20)**, which *inverts* the classic
EL recall bottleneck: the whole heavy candidate-generation apparatus exists because real KBs have
millions of entities; at our scale we can run the strongest matcher against every node with zero
retrieval loss (confirmed, -> memo-entity-linking.md). The problem collapses to per-node scoring +
calibration + joint edge alignment.

---

## 3. Ranked adoptable approaches (by expected impact on OUR node/edge recall)

### R1 — Invert the loop: per-node max-aggregation over transcript windows. Effort S. Risk low.
Stop matching parser-extracted candidates to nodes. Instead, for EACH reference node, score its
hypothesis/label against ALL ~350-token transcript windows and take the MAX; the parser stops being
the recall ceiling. Single most-repeated recipe across independent literatures: SummaC (segment
source, score every pair, max-over-source-units per claim -> SOTA zero-shot inconsistency detection,
72.1% pure-max / 74.4% trained variant), AlignScore (350-token chunks + max-pool), MiniCheck
(multi-sentence evidence synthesis), and AutoTutor's coverage that was always cumulative across all
turns and turn-combinations (max over X,Y,Z,XY..XYZ) — all confirmed. Concretely: node-first scanning;
a node taught across three turns now resolves. Evidence: SummaC arXiv 2111.09525; AlignScore arXiv
2305.16739; AutoTutor BF03195563. -> memo-nli-calibration.md, memo-oss-tools.md, memo-its-kc.md,
memo-teachable-agents.md (StuBot node-first probe pattern).

### R2 — Multiple affirmative views per node, credit = max over views. Effort S–M. Risk low.
Our single affirmative content.label per node is one point in a design space where hypothesis wording
swings zero-shot NLI accuracy by >12pp (79.4% vs 66.6% across 24 near-identical templates) and
combining multiple hypotheses adds +7.9–10.0pp (confirmed; caveat: measured on hate-speech detection +
combined with filtering strategies, so transfer is an extrapolation). EL analog: MuVER multi-view
max-over-views (ZESHEL recall@64 90.84 vs BLINK ~85.56) and SapBERT synonym-aligned encoders
(unsupervised Acc@1 91.6–93.3% biomedical; drops to 59–68% on noisy social text — benchmark-dependent,
confirmed). Concretely: 3–5 affirmative paraphrases per node (definition-style + application-style),
max-aggregate — the mechanism most likely to move node_coverage 0.20 -> high. Evidence: arXiv
2210.00910; MuVER 2021.emnlp-main.205; SapBERT 2021.naacl-main.334. -> memo-entity-linking.md,
memo-nli-calibration.md, memo-asag.md (c-rater model-sentence sets).

### R3 — Joint node+edge alignment with graded credit (fixes edge starvation). Effort M. Risk med.
Freeze-nodes-then-score-edges is architecturally wrong. SMATCH chooses the node mapping that maximizes
total matched triples INCLUDING edges, so a matched edge can pull an otherwise-unresolvable node onto
its reference (confirmed; reference impl hill-climbs — use ILP). Smatch++ ships ILP-optimal alignment
with customizable graded triple-match functions (smatchpp v1.8.0, GPL-3.0, maintained; ILP trivial at
5 nodes; README warns hill-climbing scores are "likely false", confirmed). s2match soft-matches nodes
by embedding cosine >= tau inside the objective; SoftSPICE embeds whole verbalized triples so a
paraphrased relation earns edge credit WITHOUT either endpoint resolving first (both confirmed).
Concretely: verbalize reference KG + parser candidates as triples; run ILP alignment with an
NLI/embedding-graded match function; read node_coverage AND edge_coverage off the SAME alignment.
GPL-3.0 caution for a proprietary backend — the ILP is ~scipy linear_sum_assignment + edge terms,
reimplementable. Evidence: Smatch P13-2131; Smatch++ arXiv 2305.06993; s2match arXiv 2001.10929;
SoftSPICE 2023.findings-acl.398. -> memo-graph-alignment.md, memo-semantic-parsing.md.

### R4 — Recall-first threshold calibration on labeled negatives; fuse scores; confidence cascade. Effort M. Risk med.
Our precision-first FIXED thresholds (0.75/0.80/0.85 tier caps under an 0.85 floor) are the documented
over-abstention trap. Fixes, all confirmed: (a) nobody ships a universal threshold — tune per-task on a
dev set CONTAINING negatives to maximize a recall-weighted objective (F2 / conformal FNR bound); learned
NIL detection (BLINKout) beats fixed thresholds when data exists. (b) FUSE NLI + embedding scores by
linear interpolation (MENLI) rather than either/or tier caps — they fail on complementary phenomena.
(c) CASCADE by calibrated confidence, not method identity: trust the small NLI model only above a strict
bar (~0.99, where it hit 98.5% FEVER / 90.1% SciFact), escalate the gray zone to an LLM — beats pure-LLM
while cutting LLM calls ~40% (confirmed; gray-zone ~68% figure is FEVER-specific; source is an appendix
pipeline, not a headline result). Caveat: out-of-domain NLI calibration drifts up to 3.5x — recalibrate
on OUR transcripts. Evidence: BLINKout arXiv 2302.07189; MENLI arXiv 2208.07316; cascade arXiv
2601.22984; Desai & Durrett arXiv 2003.07892. -> memo-nli-calibration.md, memo-entity-linking.md,
memo-its-kc.md.

### R5 — Decouple edge recall via direct zero-shot relation classification. Effort S–M. Risk med (license).
Instead of requiring both endpoints resolved AND a parsed edge (quadratic), classify the relation
(USES/DEPENDS_ON/SCOPES/PRECEDES) directly over located node-pair mentions with GLiREL (zero-shot RE,
one forward pass, SOTA on FewRel/WikiZSL, confirmed). Complements R3: R3 recovers edges through
structure, R5 through direct relation labeling of implicit phrasing. Risk: GLiREL license is
contradictory — PyPI says Apache-2.0, README footer says CC-BY-NC-SA-4.0, no LICENSE file (confirmed
adoption hazard). Treat as non-commercial until clarified, or retrain. Still needs entity spans as
input. Evidence: GLiREL arXiv 2501.03172. -> memo-entity-linking.md, memo-oss-tools.md,
memo-graph-alignment.md.

### R6 — Decontextualize dialogue candidates before matching. Effort S. Risk low.
~25% of DT-Grade tutoring answers are uninterpretable out of context (ellipsis, coreference, confirmed);
42.1% of DialFact claims contain in-claim pronouns that degrade verification (confirmed, though that
figure comes from BiCon-Gate, NOT the DialFact paper it was attributed to — misattributed, substance
holds). Decontextualization/claim-split (Choi 2021; WiCE GPT-3.5 split) measurably improves entailment
at test time (confirmed). Concretely: LLM-rewrite each student turn into a declarative, self-contained,
student-attributed claim BEFORE any matching tier — and before parsing, since stock extractors get 0% on
raw conversational statements. -> memo-nli-calibration.md, memo-semantic-parsing.md, memo-benchmarks.md,
memo-llm-extraction.md.

### R7 — Multi-pass / union extraction + schema-aware (EDC) candidate generation. Effort S–M. Risk low.
Recall comes from candidate generation, precision from verification. Run K extraction passes and UNION
(never majority-vote — modal output is under-extraction): LangExtract extraction_passes (Apache-2.0, "to
improve recall"), GraphRAG "gleanings" (forced "MANY entities were missed" continuation), Atomic
Self-Consistency (~5 samples). Chunk small: GPT-4 extracted ~2x entity references at 600 vs 2400 tokens
(all confirmed). Make extraction schema-aware: EDC retrieves target-schema elements into the extraction
prompt and canonicalizes via definition-embedding retrieval + LLM verify (EMNLP 2024, confirmed).
Concretely: per-turn multi-pass extraction, feed the ~5 node labels/definitions into the prompt so
candidates are generated in the reference vocabulary. -> memo-llm-extraction.md, memo-semantic-parsing.md,
memo-surveys.md.

### R8 — Adopt battle-tested OSS matchers instead of hand-tuning tiers. Effort S. Risk low.
Permissive OSS covers every tier. MiniCheck-Flan-T5-Large (770M, Apache-2.0 repo / MIT weights,
GPT-4-level grounding at ~400x lower cost, trained for multi-sentence synthesis, confirmed) as the "is
node label supported by transcript window" primitive. MoritzLaurer deberta-v3-zeroshot-v2.0 (MIT,
2-class entailment/not-entailment built for hypothesis templates — "-c" variants commercially clean;
note training scale is 33 datasets/389 classes, not the "27/310" quoted, minor error). GLiNER/GLiClass
(Apache-2.0) for recall-first candidate spotting; scispacy CandidateGenerator (thresholdless top-k
char-ngram ANN — but its OWN downstream linker IS a hard 0.7 gate, so adopt only the generator,
confirmed). Note: MiniCheck ablation says atomic-claim decomposition adds cost without accuracy for a
multi-fact-trained checker — so R6 decontextualization yes, aggressive claim DECOMPOSITION no.
-> memo-oss-tools.md, memo-nli-calibration.md, memo-entity-linking.md.

### R9 — Partial/graded per-node credit instead of binary resolved/unresolved. Effort S. Risk low.
The most reliable concept-map scoring rates each proposition on a 3–5 level quality rubric (Yin et al.),
and proposition-level partial credit is the most reliable of six methods (McClure, r=.23–.76;
convergence-score IRR >.90 — all confirmed). AutoTutor scores partial credit by content-word proportion.
Binary gating throws away the graded-credit machinery that made these assessments reliable and absorbs
matcher uncertainty. Concretely: exact=1.0 / semantic=0.8 / weak-mention=0.4, driven by max-view
similarity. -> memo-concept-maps.md, memo-its-kc.md, memo-asag.md, memo-graph-alignment.md.

### R10 — Clarification loop as the recall backstop (already our G2 direction). Effort M. Risk low.
The canonical recall mechanism in the entire ITS lineage is dialogue, not the matcher: per uncovered
node, hint->prompt->assertion targeting the specific missing content word; follow-ups significantly
improved assessed coverage (F(1,133)=129.88, p<.001, confirmed). Concept-map evidence agrees:
constrained/selected elicitation (Kit-Build, selected linking phrases) raises reliability by eliminating
alignment (confirmed). Concretely: an "unable to detect" outcome routes to a clarification turn, never a
silent zero; Apollo restates in near-canonical terms and the student confirms (a just-in-time Kit-Build
move). -> memo-its-kc.md, memo-teachable-agents.md, memo-concept-maps.md, memo-surveys.md.

REFUTED artifact caution: the memo's clean "Constructive Tutee Inquiry = response classifier + Expected
Response Generator + 3-way Alignment Detector (aligned/not aligned/unable to detect) selecting protocol
follow-ups" is REFUTED/conflated. CTI (AIED 2023) is only a 7-class response classifier + scripted
dialog manager; the Expected Response Generator + 3-way Alignment Detector belong to a SEPARATE GPT-3.5
framework (ExpectAdapt, AIED 2024) whose follow-ups are LLM-generated and which HALTS questioning on
not-aligned rather than selecting another question. Only the RCT result (n=33, treatment > control,
p<.05, d=0.35) is accurate. Adopt the IDEA of a 3-way alignment outcome, but do not cite CTI as a worked
protocol for it. -> memo-teachable-agents.md.

---

## 4. Edge coverage specifically — the quadratic starvation fix

Our edge_coverage caps at 0.25 because it needs BOTH endpoints resolved AND an edge — recall compounds
~quadratically in node recall. Three orthogonal, stackable fixes (all confirmed):

1. Joint alignment (R3). SMATCH/Smatch++ pick node identity to maximize TOTAL node+edge triple
   agreement, so edges drive node resolution instead of depending on it. ILP costless at 5 nodes. Read
   both coverages off one alignment. -> memo-graph-alignment.md, memo-semantic-parsing.md.
2. Graded whole-triple edge credit (R9 for edges). SoftSPICE embeds the verbalized edge ("more speed
   means less pressure") and matches it against the verbalized reference edge directly — graded credit
   even when neither endpoint individually resolved. s2match does the node-soft version. SPICE lesson:
   score unary (node) tuples independently so a fumbled relation never zeroes node credit (SPICE's own
   no-partial-credit-within-a-tuple choice was justified only for generic caption relations — ours
   deserve partial credit). -> memo-graph-alignment.md.
3. Direct relation classification (R5). GLiREL labels the relation over a node pair in one pass,
   decoupling edge labeling from KB-edge existence. -> memo-entity-linking.md, memo-oss-tools.md.
4. Asymmetric multi-match recall (CaRB). Coverage is a reference-side recall question: several student
   utterances may jointly cover one reference edge (multi-match on recall, single-match on precision).
   CaRB's all-pair matching table with token-level per-tuple P/R is a ~300-line portable recipe to make
   coverage continuous instead of quadratically compounding (confirmed). -> memo-semantic-parsing.md,
   memo-graph-alignment.md.

Optional heavier machinery if ILP-Smatch is outgrown: Fused Gromov-Wasserstein OT (POT library, MIT)
gives a soft many-to-many node correspondence with fractional masses = fractional coverage; FGWEA's
anchor-then-propagate schedule (semantic anchors -> structural propagation, beat 21 baselines incl.
supervised, confirmed) matches our "1–2 confident resolutions seed the rest" case. -> memo-graph-alignment.md.

Concept-map prior art independently sanctions edges from co-occurrence, not joint parses: ALA-Reader/GIKS
credited a relation when two recognized key terms co-occurred in a sentence/window (never a parsed
relational statement), scoring r~.71 vs humans and beating LSA (r~.56) — confirmed, though the r figure
traces to a comparison study, not the original Clariana paper. Replacing "both endpoints resolved within
one parsed candidate" with "both resolved anywhere + co-occur in a k-sentence window" attacks the same
starvation cheaply. -> memo-concept-maps.md.

---

## 5. Over-credit / audit hazard — what to do with the LLM transcript auditor

Unambiguous and multiply-replicated: an LLM judge cannot be the score source, but reference-grounding +
per-node binary decomposition + span-quoting makes it a usable candidate generator.

- Grounding cuts over-credit ~85% (confirmed). In physics grading across five frontier models, supplying
  the reference solution dropped over-crediting cases 13->2 (-85%) — but raised under-crediting 9->12 and
  made ALL models defer to a deliberately corrupted reference; blind holistic judging gave unanimous full
  marks to zero-mark answers and near-zero essay rank agreement (rho~0.1). Judge validity tracks rubric
  granularity, not model capability. -> memo-llm-extraction.md, memo-asag.md.
- Per-criterion binary beats holistic (confirmed). TICK raised LLM-judge/human agreement 46.4%->52.2%;
  CheckEval improved cross-evaluator agreement +0.45 with per-item traceability. Rebuild the audit as N
  independent per-node yes/no checks ("did the student teach X? quote the turn"). -> memo-llm-extraction.md.
- Require a verifiable quoted span per YES (confirmed mechanism). LangExtract-style char-interval
  grounding: programmatically verify the quote exists in the transcript; an unverifiable quote is an
  auto-NO. The mechanism our current auditor lacks. -> memo-llm-extraction.md.
- Add a consensus/verification gate (OneNet). The audit over-credits partly from no self-consistency
  check. -> memo-entity-linking.md.
- Independent corroboration from LBT + concept-map + KC literature: Ruffle&Riley GPT-4 tutees omitted 1–2
  expectations (in 7/31 conversations — "per conversation" overstates frequency) and were "often
  lenient"; AlgoBo's GPT-4 knowledge-state updates credited incorrect teaching and drifted back to correct
  knowledge, erasing prescribed misconceptions; LLM/RAG concept-map grading hit QWK 0.146 vs experts;
  GPT-4o KC-labeling reached kappa=0.74 vs human kappa=0.86 ONLY with chain-of-thought + solution context
  (all confirmed). Realistic LLM-tier ceiling: kappa~0.74. -> memo-teachable-agents.md, memo-concept-maps.md,
  memo-its-kc.md.

FLAGGED UNVERIFIABLE (do not rely on): the "AutoSCORE extract-then-score restores accuracy" half is
confirmed (AAAI 2026 / arXiv 2509.21910, extraction agent with span-quoting + separate scorer, biggest
gains on multi-component rubrics), but the paired mechanistic claim that rubric-guided LLM rationales
hallucinate rubric elements and token attribution shows scores driven by instruction/formatting tokens
could NOT be verified — the cited DOI resolves to a different (paywalled) paper, and AutoSCORE's own text
makes no such claim. Treat the hallucinated-rationale/token-attribution mechanism as UNSOURCED until the
L@S '26 paper is obtained. -> memo-asag.md.

---

## 6. Benchmarks & evaluation — how to measure resolver recall going forward

Stop tuning against a 31-attempt corpus with no gold labels. The exact protocol we need exists.

- Build a Nielsen-style per-node gold set on our own transcripts. Nielsen et al. (LREC 2008) annotated
  15,357 answers with ~145,911 facet-level entailment labels at kappa=0.728 / 86.2% agreement (confirmed)
  — proof per-node resolution gold is reliably annotatable. Critically, label EXPRESSED vs INFERRED
  separately: students routinely imply a facet, and a resolver demanding explicit statement has a
  structural recall ceiling. Budget: ~3 experts + LLM pre-annotation + majority vote (REC-CBM pipeline) or
  2 annotators to kappa~0.73 — days, not weeks, for 31 x ~5 nodes. -> memo-benchmarks.md, memo-asag.md.
- Evaluate the resolver as a ranker, not a binary gate. WorldTree/TextGraphs replaced binary single-path
  gold with ~250k graded relevancy ratings + rank metrics because binary gold under-credited valid
  alternative explanations (confirmed; the 2021 metric was NDCG, not MAP as the memo said — minor
  correction). Report recall@k / MAP per node, then pick thresholds per operating point. -> memo-benchmarks.md.
- Hold out whole problems (UA/UQ/UD). SemEval systems drop sharply from unseen-answers to
  unseen-questions/domains — our real fear. -> memo-benchmarks.md.
- Condition the matcher on the problem statement (BEM: its entire gain over token-F1 is from giving the
  model question+reference+candidate jointly; our NLI tier omits the problem — known-bad, confirmed).

Importable external benchmarks (all confirmed): nkazi/SciEntsBank (SemEval-2013 T7, UA/UQ/UD splits,
partially_correct_incomplete = our partial-coverage class); DT-Grade (900 physics-DIALOGUE answers, ~25%
context-dependent — parser stress test); sahuarchana7/gaps-answers-dataset (2025, ClausIE-triple
directed-graph gap alignment = near-clone of our node/edge coverage; no IAA, treat as silver); WorldTree
V2 + tg2021task (graded node relevance); SciTail + EntailmentBank (science NLI fine-tuning). Calibration
anchor: GPT-4 zero-shot scores only F1=0.744 (SciEntsBank 2-way) / 0.611 (Beetle 2-way), BELOW fine-tuned
encoders, and on Beetle the reference answer LOWERED accuracy (confirmed) — independent proof the LLM
audit cannot be the score source and a fine-tuned small model is the known-good path. -> memo-benchmarks.md.

---

## 7. What NOT to pursue (dead ends, with reasons)

- Whole-transcript / document-level NLI premises. Degrades with premise length; long-context models do
  not rescue it — fix granularity (R1). -> memo-nli-calibration.md.
- Bidirectional entailment as the resolution criterion. Correct for equivalence clustering, fatal for
  coverage recall (student text superset of node content). One-directional (student->node) only.
  -> memo-nli-calibration.md.
- Negated / clever hypothesis templates. NLI negation-brittleness is systematic; keep node hypotheses
  affirmative and simple (independently confirms our NLI-tier finding). -> memo-nli-calibration.md.
- A bigger sentence encoder as "the fix." SBERT/SGPT did NOT beat in-domain LSA; RegEx keyword-proportion
  alone (F1 0.509) nearly matched the human-human ceiling (0.532) on AutoTutor electronics data;
  production ElectronixTutor weights RegEx 0.75 / LSA 0.25 (confirmed). Add a precision-anchored keyword
  channel, do not bet recall on model size. -> memo-its-kc.md.
- A single global similarity threshold. Provably dominated by length/type-conditional thresholds since
  Penumatsa 2006 (confirmed). -> memo-its-kc.md, memo-nli-calibration.md.
- Open-vocabulary concept-map extraction -> graph match. SOTA is METEOR F1 28.5 (LDK 2025, confirmed) —
  WORSE than our closed-set problem and upstream of it. Keep closed-set resolution. -> memo-concept-maps.md.
- Stock OpenIE/CoreNLP/ClausIE/AMR on raw transcript turns. 0% complete-triple precision on conversational
  statements; AMR relocates the problem (AMR concepts still need a second alignment to our keys) without
  solving it. Use the graph METRICS, not the parsers; if parsing, fine-tune a domain tuple parser (FACTUAL:
  rule-parser 13.0 -> fine-tuned Flan-T5 ~80 exact set match, confirmed). -> memo-semantic-parsing.md.
- Naive LLM-as-reranker over the full candidate list. LLMAEL: 82.01->70.95 (BLINK-only vs LLM re-rank;
  metric is disambiguation accuracy not F1, minor); candidate order alone moves F1 up to 15.2%. Use routing
  + constrained choice + explicit abstain. -> memo-llm-extraction.md, memo-entity-linking.md.
- Constrained JSON decoding as a recall fix. Guarantees parse validity, not coverage (99.97% valid can
  coexist with 48.6% correct). Robustness only. -> memo-llm-extraction.md.
- Hand-authored per-node alias lists. The literature's replacement is embedding retrieval over definitions
  + clustering (EDC/KGGen); aliases do not generalize to derived/paraphrased forms — matches our own
  Phase-1b rejection. -> memo-llm-extraction.md.
- Adopting BLINK/GENRE/large-KB linker checkpoints wholesale. Wikipedia-scoped, some non-commercial,
  archived; they solve candidate retrieval at scale — a non-problem at 20 nodes. Architecture yes, weights
  no. GENRE's "unique name" assumption also breaks for procedure-step/described-concept nodes.
  -> memo-entity-linking.md, memo-oss-tools.md.
- Menu/sentence-selection-only teaching input. Solves resolution by construction (Betty's Brain,
  SimStudent — both DESIGNED the problem away, no reusable resolver tech) but measurably reduces per-action
  learning and contradicts Apollo's free-text dialogue product. Hybrid confirm (agent restates, student
  yes/no) keeps the benefit. -> memo-teachable-agents.md.
- Trusting LLM tutee internal knowledge-state updates as the grade (AlgoBo drift); chat-log-as-state
  expectation tracking as a score (Ruffle&Riley leniency); small fine-tuned response classifiers as
  grading gates (71.3% accuracy — fine for next-move, not a grade). -> memo-teachable-agents.md.
- Hill-climbing/greedy Smatch alignment. "Likely false" scores; use ILP at our size.
  -> memo-graph-alignment.md, memo-semantic-parsing.md.
- Raw/exact graph edit distance as the grade. Symmetric edit cost answers "how different," penalizes extra
  correct student material; use alignment-derived asymmetric coverage. -> memo-graph-alignment.md.

---

## 8. Memo index

- memo-asag.md — 20 yrs of ASAG: fix recall on the REFERENCE side (many student-mined paraphrase variants
  per concept, asymmetric coverage w/ question-demoting, evidence pooled across turns, length-adaptive
  thresholds, MNLI-fine-tuned NLI); facet detection never beat ~80%; LLM judges over-credit unless split
  into span-grounded extraction then scoring.
- memo-concept-maps.md — Proposition-level partial credit vs a criterion map (convergence score ~ our
  coverage); working systems credited edges from sentence co-occurrence, not joint parses; open-vocab
  extraction unsolved (F1 28.5); constrained elicitation (Kit-Build) and closed-set bi-encoder matching are
  high-recall; LLM proposition grading agrees poorly (QWK 0.146).
- memo-teachable-agents.md — LBT lineage never solved free-text->node (Betty's Brain/SimStudent constrained
  input by design); transferable patterns are node-first probe checking (StuBot), recall-leaning matching +
  explicit-failure retry (Curiosity Notebook), visible-repairable state; LLM tutees documented
  over-crediters. CTI 3-way alignment-detector claim REFUTED/conflated with a separate framework.
- memo-entity-linking.md — EL's candidate-gen+rerank maps onto our resolver, but at 5–20 nodes the recall
  bottleneck INVERTS: score every node exhaustively, many affirmative views w/ max (MuVER/SapBERT),
  calibrate thresholds recall-first on negatives, decouple edges via GLiREL.
- memo-its-kc.md — AutoTutor solved our problem for 25 yrs via cumulative cross-turn coverage,
  length-conditioned lenient thresholds (~.55, never .85-class), content-word partial credit, and
  hint->prompt->assertion dialogue backstop; modern embeddings never beat hybrid LSA+RegEx; human match
  agreement caps kappa~.46–.86.
- memo-semantic-parsing.md — Score coverage via ONE global soft alignment with element-level partial credit
  (SMATCH/S2match/WWLK, CaRB), not per-candidate hard thresholds; stock OpenIE/AMR break on dialogue, so
  fine-tune a domain tuple parser + schema-aware decomposed candidate gen.
- memo-nli-calibration.md — Invert to per-node hypotheses scored against all ~350-token windows w/ max-agg;
  calibrate on in-domain dev data (F2/conformal); fuse NLI+embedding instead of tier caps; cascade to
  evidence-scoped LLM only in the gray zone; off-the-shelf MiniCheck/AlignScore/HHEM match GPT-4 at 770M.
- memo-llm-extraction.md — Extract liberally w/ multi-pass union (LangExtract/GraphRAG gleanings), match to
  schema via definition-embedding retrieval + LLM verify w/ abstention (EDC/GenDecider/ARTER), rebuild
  auditor as reference-grounded per-node binary checks quoting verifiable spans (grounding -85% over-credit).
- memo-graph-alignment.md — Core defect is architectural: choose node resolution JOINTLY with edge
  agreement (ILP trivial at 5 nodes) with graded node/edge similarity (s2match/SoftSPICE), asymmetric
  multi-match recall (CaRB); engines exist (smatchpp, POT/FGW).
- memo-benchmarks.md — Nielsen facet-entailment protocol (15k answers, 146k per-facet labels, kappa 0.73)
  is the exact recipe for our per-node gold set; SciEntsBank/Beetle, DT-Grade, 2025 gap-annotated dataset,
  WorldTree graded relevancy are ready external benchmarks; GPT-4's mediocre zero-shot F1 confirms LLM != score.
- memo-oss-tools.md — Mature Apache/MIT OSS covers every tier: MiniCheck, SummaC granularity+max-agg,
  MoritzLaurer 2-class entailment, GLiNER/GLiClass recall-first spotting, GLiREL edges, scispacy
  thresholdless top-k; adopt these patterns instead of gate-below-floor tiers.
- memo-surveys.md — No survey owns our problem; it is fragmented across >=5 communities (expectation
  coverage / student response analysis / reference-answer facets / entity linking / key point matching);
  the EL candidate-gen+rerank architecture is the consensus recall fix; LLM-era KGC surveys confirm low
  precision + manual curation persist everywhere.
