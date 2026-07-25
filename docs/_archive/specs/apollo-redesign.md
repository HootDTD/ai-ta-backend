  Apollo V3: Gap Analysis × Academic Literature Map

  Part 1 — Architecture summary (so context isn't lost)

  Backend (ai-ta-backend/apollo/):
  - Student turn → parser_llm.parse_utterance (GPT-4o, JSON, temp 0) extracts typed
  Nodes + Edges using a concept-templated system prompt
  - Nodes/Edges written to per-attempt subgraph in Neo4j (KG is now a real graph: 6
  node types, 4 edge types — PRECEDES, USES, DEPENDS_ON, SCOPES)
  - summarize_for_apollo flattens graph back to bullet list
  - apollo_llm.draft_reply (GPT-4o, temp 0.7) generates reply with strict ignorance
  system prompt
  - output_filter.validate_or_raise rejects on hardcoded ~30-term physics stopword list
   (allowlisted by student's history + KG content)
  - On Done button: KG frozen, reference graph derived from problem JSON,
  coverage.compute_coverage runs N independent LLM-as-judge calls (binary for
  equation/condition/simplification, 0-1 for procedure_step), rubric aggregates
  deterministically (60/25/15 procedure/justification/simplification),
  diagnostic.generate_diagnostic narrates, forward_chain.solve_kg_against_problem runs
  SymPy on student's equations

  Checklist progress: Items 1, 6, 7, 8 done (real graph, concept registry, problem
  validators, Pydantic discriminated union). Items 2, 3, 4, 5, 9, 10, 11 still open.

  Part 2 — Where Apollo is structurally weak

  Gap A — Parser is silent inference (the canonicalization tax)

  The student says "what comes in must equal what comes out"; the parser writes
  equation { symbolic: "rho*A1*v1 - rho*A2*v2" }. The KG stores what the LLM knows the
  canonical form to be, not what the student actually expressed. Down-stream, coverage
  matches a polished symbolic equation against a polished reference equation — the
  student's actual conceptual model is invisible.

  Where it fires: parser_llm.py:156-215, parser_prompt_template.md. The prompt
  instructs "Do not correct the student. If they said an equation wrong, extract it as
  stated" — but a GPT-4o that knows fluid mechanics will normalize "stuff in equals
  stuff out" into the canonical form regardless. This isn't a bug — it's a
  contradiction at the prompt level.

  Gap B — Misconceptions are smoothed

  A real misconception (e.g. confusing kinetic energy with momentum) produces a
  structurally valid but semantically wrong KG entry. Coverage flags it as "missing"
  rather than "incorrect-belief detected." There is no misconception channel anywhere
  in the pipeline.

  Gap C — Apollo's "ignorance" leaks two ways

  1. Symbol leakage: summarize_for_apollo includes canonical symbols (P, rho, v, A, h,
  g) the parser inserted. Apollo can mention them freely; the output filter only blocks
   named concepts.
  2. Paraphrase leakage: Apollo can say "speed times cross-section is constant" and
  leak continuity without naming it. The wordlist filter has no concept-level coverage.

  This is on top of the well-known finding that prompt-only ignorance does not actually
   suppress LLM knowledge — see Gap-A research below.

  Gap D — No in-conversation sufficiency signal

  The KG could be checked algorithmically every turn (does student's KG + givens entail
   target via SymPy?), but completeness is computed only at Done. Apollo will keep
  acting confused after the KG is solvable, or accept a Done click on a half-taught KG.

  Gap E — Reference solution is one canonical path

  A student who teaches mass-flow form vs. volumetric, energy form vs. pressure form,
  etc. will under-score on procedure coverage even when their solution is valid.
  Coverage is shape-matching against authored ids, not equivalence.

  Gap F — Ontology fits symbolic equation problems only

  6 node types, 4 edge types — strong for Bernoulli, weak for biology pathways,
  argumentation, probabilistic reasoning, geometric reasoning, recursive procedures.
  The "generalize beyond fluid mechanics" goal is gated by ontology shape, not just by
  removing keyword lists.

  Gap G — Coverage = N independent binary LLM calls, soft-fail to "missing"

  No retries, no batching, no joint reasoning, no calibration. A network blip silently
  downgrades the grade.

  Gap H — Single-shot teaching, no formative loop

  Student teaches → grade. There's no "you didn't justify why density appears here,
  want to fix it before grading?" check. The student can't see what got captured
  wrongly until after the freeze.

  Gap I — "Done" is button-only and irreversible

  No intent detection, no preview of what would be concluded, no rollback. Item 5 in
  the checklist captures part of this; the rollback half doesn't.

  ---
  Part 3 — Academic literature, mapped to each gap

  I treated each gap as a separate research target. Here are the highest-signal
  findings.

  → Gap A (parser canonicalization) + Gap C (ignorance leakage)

  Simulated Ignorance Fails — Hua et al., 2026 (arXiv 2601.13717) — the most important
  paper for this project. They prove systematically across 477 questions and 9 models
  that prompting an LLM to "suppress pre-cutoff knowledge" leaves a 52% performance gap
   between simulated ignorance and true ignorance. Chain-of-thought makes it worse.
  Implication: Apollo's APOLLO_SYSTEM_PROMPT cannot deliver structural ignorance
  regardless of how well-written; the model will leak by paraphrase. This is not a
  tunable issue.

  Knowledge-Level Student Simulation via Machine Unlearning — 2026 (arXiv 2603.26142) —
   the direct counter-method. They selectively unlearn targeted programming knowledge
  from an LLM to produce a stable novice-level teachable agent, then measure whether
  the agent re-learns through learning-by-teaching dialogue. Quantitative result:
  unlearning produces more novice-like responses than prompt baselines, and the agents
  recover unlearned knowledge under structured exposure. This is the only published
  technique that actually achieves what Apollo's prompt promises.

  Towards Valid Student Simulation with LLMs — 2026 (arXiv 2601.05473) — defines
  simulated students as a constrained generation task with an explicit epistemic
  boundary (concepts, strategies, representations the agent can legitimately access
  plus the misconceptions structuring its errors). Read this as the design contract
  Apollo should be honoring.

  Persona Conflict in LLMs — 2026 EACL findings — when persona constraints contradict
  user-provided info, LLMs fail in three ways: adhering, sycophantic, wavering.
  Apollo's persona ("you know nothing") is in conflict on every turn; expect drift
  toward sycophantic agreement and toward wavering ("hmm, I'm not sure if I learned
  that"). Provides a measurement schema worth borrowing for Apollo's own eval suite.

  Implicit Consistency in LLMs — 2026 (arXiv 2603.25187) — LLMs drift on unstated goals
   in 100% of multi-turn dialogues. Apollo's "stay in character as confused" is an
  unstated goal. Goal drift is mitigated by KL-divergence regularization at training
  time, not prompt instruction.

  COKE: Confidence-derived Knowledge Boundary Expression (ACL knowllm 2025) — trains
  LLMs to express knowledge boundaries by leveraging internal confidence signals.
  Better mechanism for "no, I don't know what that is" than a system-prompt
  instruction. Directly applicable as a fine-tune target for the Apollo agent.

  Practical takeaway: Apollo's structural-ignorance promise is currently held up by a
  single line in a system prompt that the literature shows cannot deliver it. The real
  options are:
  - (a) machine unlearning of the target domain from a small open-weight model used
  only as Apollo (research-grade, expensive)
  - (b) two-model architecture: a strict LLM-as-judge (cheap model) audits Apollo's
  draft against a frozen "what was the student's actual vocabulary" set, before output
  reaches the student. Stronger than the wordlist filter and concept-aware (replaces
  checklist item 3 properly).
  - (c) "Lenat's BELLA trick" — Reinforcing Math Knowledge by Immersing Students in a
  Simulated Learning-By-Teaching Experience (Lenat & Durlach, IJAIED 2014). Their
  teachable agent Elle never actually learns; a hidden super-agent (Cyc) maintains a
  model of what the human student must currently believe and configures Elle to act
  consistently with that. The illusion of teaching is preserved without requiring the
  agent to actually be ignorant. This is a design pattern Apollo can adopt without
  rebuilding anything.

  → Gap B (misconception silencing)

  MISTAKE — Modeling Incorrect Student Thinking and Key Errors (2510.11502) —
  synthesizes plausible (misconception, faulty reasoning, answer) triples via
  cycle-consistency and trains both a student-simulation model and a
  misconception-inference model. This is exactly the misconception-inference channel
  Apollo lacks. Plug it in between parser and coverage.

  Misconception Diagnosis from Student-Tutor Dialogue: Generate, Retrieve, Rerank
  (2602.02414) — fine-tuned LLM generates plausible misconceptions,
  embedding-similarity retrieves candidates from the dialogue, second LLM re-ranks.
  Outperforms larger closed-source baselines. Practical recipe — fits as a parallel
  pass at parse time.

  AdapT / AToM — Adaptive Teaching toward Misconceptions (arXiv 2405.04495) —
  student-prior inference is performed online so the teacher (here, Apollo) can pick
  informative examples / probes. Suggests Apollo can actively probe for suspected
  misconceptions ("can you tell me what happens to this if I double the area?") instead
   of just passively receiving teaching.

  Reasoning Trajectories for Socratic Debugging (2511.00371) — instructors guide
  students from a misconception to a contradicting test-case statement. Frontier models
   reach ~91% correct trajectories. Apollo's "naïve confused learner" persona could be
  upgraded to a Socratic confused learner once a misconception is detected: it would
  still appear ignorant, but its confusion would be diagnostic.

  Stepwise Verification and Remediation of Student Reasoning Errors (Macina et al.,
  EMNLP 2024) — separating verification from response generation reduces tutor
  hallucinations and produces more targeted feedback. Directly applicable as a redesign
   of Apollo's chat handler: parse → verify → remediate, three modules instead of parse
   → reply.

  → Gap D (in-conversation sufficiency)

  Entailer — Tafjord et al., AI2 (2210.12217) — backward-chaining QA system that
  generates premise sets entailing an answer hypothesis, with a verifier that checks
  the model's own beliefs. Produces chains that are both faithful (answer follows from
  chain) and truthful (chain reflects beliefs). Apollo's KG already supports this:
  every turn, run a deductive-verification pass — does the current student KG (plus
  problem givens) entail target_unknown? If yes, signal "I think I have everything"
  through Apollo. If no, return the smallest missing premise. This is a turn-level
  extension of the existing Done-time SymPy solver.

  LeanTutor (AAAI 2026, arXiv 2506.08321) — pairs LLM communication with Lean
  theorem-prover verification. Their Next Step Generator runs DFS over candidate
  tactics, filtered by Lean compile + progress check. Apollo could similarly run a
  turn-level proof-search to find the one missing fact whose addition would let the KG
  solve the problem, and feed that as a "what would help me most right now" hint
  signal.

  FiDeLiS — Stepwise Beam Search with Deductive Verification (2405.13873) — KG-grounded
   retrieval + deductive verification halts when the question becomes deducible. Gives
  a clean algorithm Apollo can copy for the "is the KG sufficient yet?" check.

  → Gap E (multiple valid paths)

  Probabilistic Equivalence Verification (PEV) for math expressions — Nguyen et al.
  (IJCAI 2013) — randomized numerical-equivalence check; avoids false negatives,
  bounded false-positive probability. Directly applicable to Apollo's equation matching
   — replaces the LLM-as-judge call for equation type with a deterministic check.

  Stainless / functional-induction equivalence checking — EPFL — used for grading
  programming assignments, clusters submissions by provable equivalence. The clustering
   idea is interesting for Apollo: instead of one canonical reference solution, store
  an equivalence-class of valid teaching paths and grade against the closest.

  Proof Blocks — autograding mathematical proofs as DAG edit-distance (2204.04196) —
  the autograder accepts any DAG-topological ordering satisfying dependencies, and
  assigns partial credit by minimum edit distance. Direct mapping to Apollo's
  procedure_step ordering: instead of binary "did the student match this step id,"
  compute edit distance from the student's PRECEDES-chain to the closest reference
  chain. This is item 10's batched matcher with a much smarter scoring function.

  Concept Map Based Assessment of Free Student Answers (Maharjan & Rus, AIED 2019) —
  tuple-level concept map matching for tutorial dialogues. Beats binary correctness.
  Same ideas as Apollo's KG matching but more mature.

  → Gap F (ontology limits)

  Ontology-Supported Scaffolding of Concept Maps (Weinbrenner et al., AIED 2011) — uses
   a domain ontology (not a fixed expert reference) as the only input to scaffold
  student concept maps. The right model for Apollo's "subject registry": the registry
  holds the ontology of valid relationships per subject, not a list of canonical
  equations.

  Learning Visually Grounded Domain Ontologies via Embodied Conversation (Park,
  Lascarides, Ramamoorthy, 2412.09770) — agent learns a domain ontology from corrective
   teacher feedback during explanation. Suggests an authoring pathway: teachers (or
  curriculum designers) can extend Apollo's ontology by teaching the system itself,
  rather than hand-authoring JSON schemas.

  Bisra et al. (2018) self-explanation meta-analysis + CIRCSIM-Tutor / Atlas-Andes /
  Geometry Explanation Tutor (review in oa.upm.es/78939) — these older but directly
  comparable ITS systems used EMT (Expectation and Misconceptions Tailoring) and CBM
  (Constraint-Based Modelling) student models. Both express domains broader than
  symbolic equations:
  - EMT: list of expected statements + list of misconceptions; matching is
  statement-level
  - CBM: every domain rule expressed as a constraint a correct solution must satisfy
  Both are richer ontologies than Apollo's six node types, and battle-tested. Worth
  reading as ontology-design precedent.

  → Gap G (coverage brittleness) + Gap-G adjacent: rubric grading validity

  Designing Reliable LLM-Assisted Rubric Scoring for Constructed Responses: Evidence
  from Physics Exams (arXiv 2604.12227) — directly relevant. Findings:
  - Fine-grained checklist rubrics > holistic
  - Temperature has limited impact
  - Reliability is highest for high- and low-performing responses, weakest for
  mid-level partial reasoning — exactly Apollo's hard case
  - Clear, well-structured rubrics matter much more than prompting format

  Criterion-referenceability determines LLM-as-a-judge validity (arXiv 2603.14732) —
  across structured questions, essays, plots: validity tracks
  criterion-referenceability (extent to which a task maps to explicit grading
  features). Essays: discriminative validity ≈ 0 even with anchored exemplars. Apollo's
   procedure_step matching is essay-like; expect noisy LLM judgments. Apollo's
  equation/condition matching is criterion-referenceable; LLM judgment is more
  reliable.

  Towards Auto-Grading via Aligned Grading Rubrics (2407.18328) — when LLMs generate
  their own analytic rubrics, the rubrics misalign with human ones; supplying
  high-quality analytic rubrics fixes the gap.

  When Verification Hurts (arXiv 2603.27076) — adding a verifier improves outcomes when
   upstream feedback is error-prone (<70% accuracy) but degrades by 4-6pp when feedback
   is already reliable. Don't blindly stack a "judge" layer on top of every coverage
  call.

  Practical takeaway for item 10: batch the N coverage calls into one structured-output
   call with a checklist rubric per ref entry, supply the canonical reference rubric
  (don't let the LLM derive it), use confidence scores for mid-band cases, retry on
  transient errors, and don't add a verifier to the binary checks (only to the
  procedure_step partial-credit calls).

  → Gap H (single-shot teaching) + Gap I (irreversible Done)

  Negotiated Open Learner Models (Mr Collins — Bull & Pain 1995; STyLE-OLM — Dimitrova;
   NDLtutor — Negotiation-based Dialog) — students inspect and dispute the system's
  view of their knowledge. Identical interaction moves available to learner and system:
   challenge, offer evidence, request explanation, agree, disagree. This is the missing
   pedagogical mechanism in Apollo. The KG sidebar already shows what Apollo "thinks"
  the student taught — extending that to "challenge this entry" / "I didn't say it that
   way" / "this is wrong, here's evidence" closes the parser-canonicalization-trust gap
   (Gap A) without solving it cleanly. Frontiers OLM meta-synthesis (2026) classifies
  four levels — inspectable, negotiable, editable, persuasive/adaptive — Apollo is
  currently inspectable; moving to negotiable is the highest-leverage UX change you can
   make.

  Betty's Brain SRL feedback principles (Segedy et al., ICLS 2012) — four design
  principles for in-conversation tutee feedback: goal-alignment, context-relevancy,
  integrated cognitive+metacognitive support, conversational delivery. Apollo's "I
  don't know what to do with v2" lines already follow these unintentionally; the
  literature says formalize them.

  Recursive Feedback (Okita & Schwartz 2013) — the protégé effect specifically requires
   the agent to demonstrate the consequences of what it has been taught. The graded
  run-after-Done partly does this; mid-conversation "given what you've taught me,
  here's what I'd conclude — does this match what you intended?" demonstrations would
  do it more strongly.

  SimStudent (Matsuda et al.) — students learn more when they detect SimStudent's
  shallow learning. Direct implication for Apollo: when the KG is graph-completable but
   the student has only taught one of two valid paths, Apollo should signal "I can
  solve this but I'm only following one approach — is there another way you'd want me
  to know?" Apollo currently never expresses partial mastery.

  → Gap I (Done detection / intent classification)

  AgentTutor (2601.04219) — multi-turn LLM tutoring with explicit confusion / learning
  / response states and conditional transitions. Transition logic is the right shape
  for Apollo's done / teaching / restart / next / return / help / off-topic intent
  router (your checklist item 5).

  → Cross-cutting / motivation evidence

  Chase, Chin, Oppezzo & Schwartz (2009) — Protégé effect — students put in more effort
   when they believe they're teaching an agent vs. learning for themselves. Strongest
  for lower-achieving students. Worth quoting in any teacher-facing pitch.

  Koedinger / SimStudent classroom studies (2013, APA) — the strongest predictors of
  student tutor-learning are: quality of explanations during tutoring, appropriateness
  of problem selection, accuracy of feedback. Apollo is partly hitting #1 (parser
  captures what student said) but completely missing #2 (problem_selector is
  rule-based, not adaptive to student priors) and #3 (Apollo's confused replies are not
   "feedback" in the SimStudent sense — they're sympathetic flailing).

  ---
  Part 4 — How to think about this strategically

  Given the literature, I'd group your gaps into three classes:

  Class 1 — Engineering hardening (the rest of the checklist)

  Items 2, 3, 4, 5, 9, 10, 11. The literature endorses these directly and gives
  recipes:
  - Item 3 (output filter): replace with LLM-as-judge using stepwise-verification
  pattern (Macina 2024); concept-scoped, not wordlist-scoped. Add COKE-style
  confidence-boundary expression as a soft signal.
  - Item 10 (coverage): batch + checklist rubric + retries (Designing Reliable
  LLM-Assisted Rubric Scoring 2604.12227); use PEV for equation equivalence; use
  Proof-Blocks edit-distance for procedure_step ordering.
  - Item 5 (intent): AgentTutor-style state machine with explicit transitions.

  These are well-trodden. Just do them.

  Class 2 — Product-shape changes the literature strongly endorses

  - Negotiable Open Learner Model. The KG sidebar becomes interactive. Student can
  challenge entries, dispute parser interpretations, edit, supply evidence. Closes Gap
  A (parser hallucination) and Gap H (no formative loop) simultaneously.
  - Turn-level entailment check (Entailer / FiDeLiS / LeanTutor pattern). Every turn,
  ask: "does student-KG ⊨ target?" If yes, Apollo signals readiness. If no, Apollo's
  confusion is targeted at the smallest missing premise, not generic.
  - Misconception-inference channel (MISTAKE / Misconception G-R-R). Run alongside the
  parser. When a probable misconception is detected, Apollo's confused-learner persona
  shifts toward Socratic-debugging style (Reasoning Trajectories paper) — still asking
  ignorant questions, but the questions probe the misconception.

  These are larger but well-supported lifts.

  Class 3 — The hard, mostly-unsolved problems

  - Genuine LLM ignorance. Prompt instructions don't deliver it (Hua 2026). Machine
  unlearning does (Liu 2026), but it's research-grade and constrains you to open-weight
   models. Realistic short-term answer: lean on the "Elle/BELLA trick" — Apollo isn't
  actually ignorant; a hidden auditor enforces consistent ignorant-acting behavior from
   a knowledgeable LLM. This is honest engineering-by-illusion and the literature
  explicitly endorses it.
  - Ontology generalization beyond symbolic equations. Concept-map / EMT / CBM
  precedents exist but there is no clean recipe. You'll need a real authoring +
  validation pass per subject area; the registry idea you've started is the right
  shape. Causal/argumentative/probabilistic node types are research projects, not
  config edits.
  - Multiple valid paths. Equivalence-class reference solutions (EPFL Stainless
  pattern, Proof Blocks edit-distance) are doable but requires authoring 2-3 valid
  solutions per problem and a more sophisticated matcher.

  ---
  Suggested reading order (if you only read 7)

  1. Hua et al. 2026 — Simulated Ignorance Fails (2601.13717) — proves the
  structural-ignorance problem you have
  2. Towards Valid Student Simulation with LLMs (2601.05473) — the design contract
  Apollo should honor
  3. MISTAKE — Modeling Incorrect Student Thinking (2510.11502) — the misconception
  channel you're missing
  4. STyLE-OLM (Dimitrova) + the Frontiers 2026 OLM meta-synthesis — negotiated open
  learner model: the highest-leverage UX move
  5. Stepwise Verification and Remediation (Macina, EMNLP 2024) — the chat-handler
  redesign that reduces parser hallucination effects
  6. Designing Reliable LLM-Assisted Rubric Scoring (2604.12227) — directly tells you
  how to redesign coverage/rubric
  7. Lenat & Durlach BELLA / Elle (IJAIED 2014) — the honest "the agent isn't really
  learning" design pattern, with classroom validation

  ---
  The deepest finding is that your two biggest unstated assumptions — (a) a system
  prompt can keep an LLM ignorant and (b) the parser's structured KG is a faithful
  image of what the student said — are both contradicted by recent literature.
  Everything else flows from those two. The good news: solid published methods exist
  for both, and your existing Neo4j graph + concept registry are the right substrate
  for them.

  Want me to take any one of these threads — say, sketching the negotiable-OLM UX
  change, or specifying the misconception-inference channel as a concrete plan — and
  turn it into a phase plan?

  Sources:
  - https://psycnet.apa.org/record/2008-15202-002
  - https://link.springer.com/article/10.1007%2Fs10956-009-9180-4
  - https://arxiv.org/abs/2601.13717
  - https://www.arxiv.org/pdf/2603.26142
  - https://arxiv.org/html/2601.05473v1
  - https://aclanthology.org/2026.findings-eacl.24/
  - https://www.arxiv.org/pdf/2603.25187
  - https://aclanthology.org/2025.knowllm-1.3.pdf
  - https://link.springer.com/article/10.1007/s40593-014-0016-x
  - https://arxiv.org/html/2510.11502v1
  - https://arxiv.org/abs/2602.02414
  - https://arxiv.org/html/2405.04495v1
  - https://arxiv.org/pdf/2511.00371
  - https://aclanthology.org/2024.emnlp-main.478/
  - https://arxiv.org/pdf/2210.12217
  - https://arxiv.org/html/2506.08321v2
  - https://www.arxiv.org/pdf/2405.13873v3
  - https://ijcai.org/Proceedings/13/Papers/299.pdf
  - https://export.arxiv.org/pdf/2204.04196v3.pdf
  - https://link.springer.com/chapter/10.1007/978-3-030-23204-7_21
  - https://telrp.springeropen.com/articles/10.1186/s41039-016-0035-3
  - https://eric.ed.gov/?id=EJ1091172
  - https://www.frontiersin.org/articles/10.3389/feduc.2025.1760183
  - https://arxiv.org/abs/2604.12227
  - https://www.arxiv.org/abs/2603.14732
  - https://www.arxiv.org/pdf/2603.27076
  - https://psycnet.apa.org/doiLanding?doi=10.1037/a0031955
  - https://gwern.net/doc/psychology/spaced-repetition/2018-bisra.pdf
  - https://oa.upm.es/78939/1/A_Systematic_Literature_Review_of_Intelligent_Tutoring_Sy
  stems_With_Dialogue_in_Natural_Language.pdf
  - https://arxiv.org/pdf/2412.09770
