# Apollo Implementation Plan

**Start:** April 10 · **Hard deadline:** June 7 · **Scope:** Full course support, validated deeply on 3–4 topics, deployed inside ai-ta-backend

---

## Week 1 — Apr 10–16: Scope freeze, architecture decisions, content authoring begins

**Goal:** lock every design decision that downstream weeks depend on. Define the data formats that scale to a full course, but only populate one topic yourself.

**Build:**

- **Course-wide Concept Hierarchy DAG format** (JSON/YAML with typed edges):
  - Node schema: `id`, `label`, `prerequisites[]`, `scope_boundary[]` (what's explicitly out of scope), `topic_cluster`
  - Edge types: `requires`, `extends`, `excludes`
  - Build the full course skeleton: all topic clusters identified, nodes named, edges sketched — doesn't need to be perfect, needs to be complete enough to assign to others for fleshing out
  - **Fully populate Bernoulli's neighborhood** (~10–15 nodes) as the reference example for other authors

- **Problem bank format** (per problem):
  - `concept_id`, `difficulty`, `problem_text`, `given_values`, `target_unknown`
  - **Structured reference solution:** ordered list of required KG entries (not prose), each tagged as `equation | definition | condition | simplification | variable_mapping`, with dependency edges between them
  - Write 5–10 Bernoulli problems with full structured solutions

- **KG schema v1** — the exact types Apollo can store:
  - `equation`: symbolic form (SymPy-parseable) + human label + variable list
  - `definition`: concept name → meaning
  - `condition`: applicability constraint (`applies_when: [predicate]`)
  - `simplification`: conditional rewrite rule (`if [predicate] then [transformation]`)
  - `variable_mapping`: natural language entity → symbol binding
  - Schema must be **topic-agnostic** — nothing Bernoulli-specific in the structure

- **Solver architecture decision.** Recommend hybrid (c): LLM proposes which equation/step to apply next, hard validator gates every lookup against KG entries, SymPy executes. Document the leakage boundary: the planning LLM sees KG entry labels and forms only, never raw training knowledge. Write this up as an ADR.

- **Context isolation design:** how Apollo's LLM context and the Overseer's LLM context stay separated (separate API calls, separate system prompts, no shared message history). Document the boundary.

- **Risk register** with kill criteria (see end of plan)

- **Content authoring kickoff:** if you have a TA or collaborator, hand them the DAG format, the problem bank format, and the populated Bernoulli example. They start filling in the rest of the course DAG and writing problems for other topics. If solo, start a backlog ordered by priority — pick topics with structurally different equation types first.

**Deliverables:** MVP spec doc, course DAG skeleton + populated Bernoulli neighborhood, problem bank format + 5–10 Bernoulli problems with structured solutions, KG schema, solver ADR, context isolation doc, risk register, content authoring backlog assigned or started

**Exit criterion:** you can point at any Bernoulli problem's reference solution and say "these N KG entries are necessary and sufficient; if any is missing, the solver fails at step M." The formats are general enough that a second author could populate a different topic without asking you questions.

---

## Week 2 — Apr 17–23: Skeleton + vertical slice

**Goal:** prove the core mechanic works end-to-end with hardcoded data before building any NLP or conversational AI. Topic-agnostic from the start.

**Build:**

- `apollo/` module structure inside ai-ta-backend:
  ```
  apollo/
  ├── overseer/          # orchestration, concept selection, coverage, steering
  ├── agent/             # conversational layer, question generation
  ├── knowledge_graph/   # session-scoped KG store, parser, schema
  ├── solver/            # step planner, SymPy wrapper, stuck detection
  ├── concepts/          # DAG loader, course data files
  ├── problems/          # problem bank loader, reference solutions
  ├── diagnostics/       # gap analysis, report generation
  └── api.py             # FastAPI router, mounted under /apollo
  ```
- **Session manager:** create session (with `concept_id`), store phase state, transition between phases. Session-scoped, in-memory.
- **KG storage:** in-memory dict per session. Operations: `write_entry`, `read_all`, `freeze` (reject writes after freeze), `reset` (for re-teach retry). Topic-agnostic — stores whatever conforms to the schema.
- **DAG loader:** read the concept hierarchy file, look up a node, return its prerequisites and scope boundaries.
- **Problem loader:** given a `concept_id`, return available problems. Given a `problem_id`, return the problem text and structured reference solution.
- **SymPy wrapper:** take a symbolic equation string + variable bindings + target unknown → solve. Handle failures gracefully (unsolvable, missing variables).
- **Vertical slice #1 — "perfect teaching":** hand-write a complete KG (all required entries for Bernoulli problem #1). Run the solver. Correct answer + step trace.
- **Vertical slice #2 — "missing continuity":** hand-write a KG missing the continuity equation. Solver gets stuck at the velocity step. Reports exactly what's missing.
- **Vertical slice #3 — "diagnostic diff":** compare slice #2's KG against the reference solution's required entries. Output: `continuity_equation: never_taught`, `bernoulli: taught_correctly`, etc.
- Verify all three slices are **topic-agnostic in code** — the Bernoulli content comes from data files, not hardcoded logic.

**Deliverables:** working module structure, session lifecycle, KG store, SymPy wrapper, DAG + problem loaders, 3 passing vertical slices

**Exit criterion:** the solver produces correct answers from complete KGs and fails cleanly from incomplete ones. You could swap in a different topic's data files and the code would work without changes.

---

## Week 3 — Apr 24–30: Parser v1

**Goal:** student natural language → structured KG entries. Scoped to equations and conditions for v1. Designed topic-agnostic — no Bernoulli-specific rules.

**Build:**

- **Parser pipeline (hybrid):**
  - **Deterministic layer:** regex/pattern extraction for equations in common forms (LaTeX, plaintext math, verbal "X equals Y"). Extract equation structure, not meaning.
  - **LLM normalization layer:** messy natural language → canonical SymPy-parseable form. The LLM sees only the student's utterance and the variable normalization map — no course context, no domain knowledge.
  - **Strict validator:** parsed entry must conform to KG schema, symbolic form must be SymPy-parseable, confidence threshold must be met. Reject and log rather than hallucinate.

- **Variable normalization map** — topic-configurable, loaded from a data file per concept cluster:
  - Bernoulli: `pressure → P`, `density → rho`, `velocity → v`, `area → A`, `height → h`
  - Format supports adding new topics without code changes

- **KG write path:** parser output → validator → KG store. Rejected entries logged for debugging.

- **Debug inspection endpoint:** `GET /apollo/session/{id}/knowledge_graph` — dump current KG as JSON.

- **Test suite — 50+ utterances:**
  - Clean math statements (should extract correctly)
  - Messy/informal statements (should normalize or reject)
  - Ambiguous statements (should reject, not guess)
  - Non-physics chatter (should ignore)
  - Wrong equations (should extract as-is — parser doesn't judge correctness)
  - Statements from a second topic if content is ready (test generalization)

- **Integration test:** feed a simulated "good teaching transcript" (10–15 utterances) through the parser → verify KG contents → run Week 2 solver on the result → verify correct answer. This closes the parser→solver loop for the first time with non-hardcoded data.

**Deliverables:** parser v1, variable normalization config, KG write/read/inspect endpoints, 50+ utterance test suite, parser→solver integration test passing

**Exit criterion:** given a transcript of someone correctly teaching Bernoulli + continuity in natural language, the parser extracts enough for the solver to produce the correct answer.

---

## Week 4 — May 1–7: Apollo's conversational layer

**Goal:** Apollo sustains a teaching conversation — curious, ignorant, never leaks. This is topic-agnostic by design since Apollo has no domain knowledge to be topic-specific about.

**Build:**

- **Apollo system prompt:** you know nothing. You are being taught. You can only reference what the student has told you and what's in your KG. You ask questions to understand, not to test.

- **Question generation by type:**
  - Clarification: "what do you mean by [term student used]?"
  - Definition: "can you define [term student used] for me?"
  - Why: "why does [thing student said] happen?"
  - Scope: "does [thing student said] always apply, or only sometimes?"
  - Application: "so if I had [concrete scenario], what would happen?"
  - Completeness: "I think I could [partial task] with what you've told me, but I don't see how to get from [A] to [B]"

- **Guardrails (hard rules in the system prompt + tested):**
  - May not name concepts the student hasn't named
  - May not introduce equations, laws, or physical principles
  - May not correct the student (has no basis to)
  - May reference only: student's prior statements, current KG entries, generic reasoning ("I'm missing a connection")

- **Turn memory:** Apollo's message history is augmented with a structured summary of current KG state, not free-form accumulation. This prevents the conversational LLM from "learning" physics through context window accumulation.

- **Leakage regression suite (≥15 adversarial cases):**
  - Student teaches Bernoulli, never says "continuity" → Apollo must never say "continuity"
  - Student gives a wrong equation → Apollo must not correct it
  - Student uses informal language → Apollo must not "upgrade" to formal terminology
  - Student asks Apollo "do you know about X?" → Apollo must say no (even if the underlying LLM "knows")
  - Student deliberately tries to get Apollo to reveal knowledge → Apollo stays ignorant
  - **This suite runs on every commit from here forward.** Any leak is a build break.

- **Basic teaching chat UI:** minimal web interface or terminal. Student types → Apollo responds → KG populates in background (via parser from Week 3). Student can see the conversation; KG population is invisible to them.

**Deliverables:** Apollo prompt package, question generation logic, leakage regression suite, functional teaching chat with live KG population

**Exit criterion:** run 5 full mock teaching conversations. Apollo never leaks. Apollo asks useful follow-ups. KG populates correctly enough that the solver would work. Conversations feel natural, not robotic.

---

## Week 5 — May 8–14: Overseer orchestration

**Goal:** the Overseer controls the session — selects problems, monitors coverage, manages phase transitions, freezes the KG. No steering yet (that's Week 6).

**Build:**

- **Concept selection:**
  - For MVP: manual topic selection via API parameter (`POST /apollo/session?concept_id=bernoulli`)
  - Hoot integration (auto-detect from prior chat) deferred but the interface is ready for it

- **Problem selection:** given a `concept_id`, pick a problem from the bank. For MVP: random selection weighted by difficulty. The problem and its structured reference solution are stored in the Overseer's private session state — Apollo never sees them.

- **Coverage monitor:**
  - After each teaching turn: compare current KG entries against the selected problem's required entries
  - Output: per-entry status (`covered | partially_covered | missing`), overall coverage percentage
  - This runs silently in the background — the student doesn't see it

- **Phase state machine:**
  ```
  INIT → TEACHING → PROBLEM_REVEAL → SOLVING → REPORT
                                                  ↓
                                          RETEACH or PATCH
  ```
  - `TEACHING → PROBLEM_REVEAL`: triggered by student saying "I'm done" / turn limit (default 20) / coverage threshold (configurable, e.g., 90%)
  - `PROBLEM_REVEAL`: problem displayed to student + Apollo simultaneously, KG frozen
  - `SOLVING`: solver runs on frozen KG, produces step trace
  - `REPORT`: diagnostic displayed
  - `RETEACH`: wipe KG, back to `TEACHING`, same problem
  - `PATCH`: keep KG, back to `TEACHING` with only missing entries writable, then re-solve

- **KG freeze enforcement:** after `PROBLEM_REVEAL`, KG store rejects all writes. Parser still runs (so the student sees Apollo "listening") but nothing persists.

- **Session end detection:** simple keyword matching for "I'm done" / "that's everything" + turn counter. Doesn't need to be sophisticated.

**Deliverables:** Overseer controller, phase state machine, problem selector, coverage monitor, KG freeze, session end detection

**Exit criterion:** start a session → teach Apollo → Overseer tracks coverage in real time → say "I'm done" → problem reveals → KG freezes → solver runs → step trace output. The full loop works, even though Apollo's questions aren't steered yet.

---

## Week 6 — May 15–21: Steering + second topic validation

**Goal:** the Overseer makes Apollo's questions targeted, and the system proves it works beyond Bernoulli.

**Build:**

- **Calibrated confusion injection:**
  - Overseer examines `missing` entries from coverage monitor
  - For each missing entry, generates a **generic, concept-name-free hint** describing the functional gap
  - Examples:
    - Missing continuity equation → "ask how to determine velocity from pipe dimensions"
    - Missing area formula → "ask how to calculate cross-sectional area"
    - Missing energy conservation → "ask why the quantities on each side should be equal"
  - Hint is injected into Apollo's system prompt as: "You feel confused about: [hint]. Ask about this naturally."
  - **Critical constraint:** hints describe *what Apollo doesn't understand*, not *what concept is missing*. "How do I find velocity from geometry?" not "Ask about continuity."

- **Steering prompt template + leakage tests:**
  - ≥10 adversarial cases verifying steering hints never name concepts
  - Add to the existing leakage regression suite

- **Second topic validation:**
  - Pick a structurally different topic (e.g., projectile motion, RC circuits, or basic thermodynamics — whatever content is ready from the parallel authoring track)
  - Populate its concept DAG neighborhood, write 3–5 problems with structured solutions, add variable normalization entries
  - Run 3 full sessions on the new topic end-to-end
  - Identify and fix any Bernoulli-specific assumptions that crept into the code

- **Third topic started:** if content is available, begin populating a third topic's data. Run at least 1 session.

**Deliverables:** steering logic, steering leakage tests, second topic fully validated (3+ sessions), third topic started

**Exit criterion:** Overseer steers Apollo toward gaps without leaking concept names. The system works on two different topics without code changes — only data files differ.

---

## Week 7 — May 22–28: Diagnostics, retry, parser v2, third/fourth topic

**Goal:** failure becomes actionable feedback. Parser handles harder cases. System validated on 3–4 topics.

**Build:**

- **Gap analysis engine:**
  - Inputs: reference solution's required KG entries, actual frozen KG, solver trace
  - Per-entry classification:
    - `taught_correctly`: entry present, SymPy-equivalent to reference
    - `taught_incompletely`: entry present but missing variables, conditions, or scope
    - `never_taught`: no matching entry in KG
    - `taught_but_malformed`: entry present but SymPy can't parse or it's structurally wrong
  - Equation equivalence checking via `sympy.simplify(student - reference) == 0`

- **Student-facing diagnostic report:**
  - What you taught well (with specifics)
  - What was missing (with *why it mattered* — "without this, Apollo couldn't determine X from Y")
  - What was partial (what specifically was incomplete)
  - Concrete next step: re-teach, patch, or return to Hoot
  - Tone: diagnostic, not judgmental. "Apollo couldn't solve this because..." not "you failed to teach..."

- **Retry flows:**
  - **Re-teach:** wipe KG, reset to `TEACHING`, same problem held. Full clean slate.
  - **Patch:** keep existing KG, re-enter `TEACHING` with only `missing` and `incomplete` entries writable. Student fills gaps. KG re-freezes. Solver re-runs. New diagnostic generated.

- **Parser v2:**
  - Simplification rules: "if the pipe is horizontal, height terms cancel" → conditional rewrite in KG
  - Variable mappings: "pressure in the wide section" → `P1` binding
  - Better handling of multi-sentence explanations (student explains across 2–3 messages)
  - Expand test suite to 100+ utterances across all validated topics

- **Third and fourth topic validation:**
  - Finish third topic: full DAG neighborhood, 3–5 problems, 3+ sessions
  - Start fourth topic if content is ready: at least 1 session
  - Fix any generalization issues discovered

- **Result logging:**
  - Every session captures: all turns, KG snapshots at each turn, coverage trajectory, frozen KG, solver trace, diagnostic output, retry attempts
  - Stored as JSON per session for research analysis

**Deliverables:** gap analysis engine, diagnostic report, retry flows, parser v2, 100+ utterance test suite, 3–4 topics validated, session result logging

**Exit criterion:** a failed solve produces a report that correctly identifies the specific missing knowledge, and a student can read it and understand what to study. Retry flows work cleanly. System works on 3+ topics.

---

## Week 8 — May 29–Jun 4: End-to-end testing + internal pilot

**Goal:** stabilize everything. Find and fix every rough edge.

**Run 20+ mock teaching sessions across all validated topics:**

- Perfect teaching → solver succeeds → report confirms
- Missing one key concept → solver fails at predictable step → report identifies the gap
- Missing multiple concepts → solver fails → report prioritizes gaps correctly
- Messy/informal language → parser extracts enough → solver works
- Deliberately wrong equations → parser extracts as-is → solver produces wrong answer or gets stuck → diagnostic flags the issue
- Rapid/terse teaching (3–5 turns) → coverage too low → Overseer steers → gaps partially filled
- Verbose/rambling teaching → parser filters noise → KG stays clean
- Retry flows: re-teach and patch both produce improved outcomes
- Cross-topic sessions: switch between topics without system confusion

**Bug triage priority order:**
1. Leakage (any leak is a showstopper — fix immediately)
2. Parser false negatives (student taught it, parser missed it — most common source of unfair failures)
3. Solver false failures (KG has enough but solver can't find the path)
4. Steering leakage (hint reveals concept name)
5. Confusing diagnostic language
6. Phase transition edge cases
7. UI friction

**Also:**
- Polish the UI: teaching → reveal → solve trace → report → retry. Each transition should be obvious.
- Demo script: a specific teaching transcript that reliably shows both success and failure, suitable for a live demo
- **Research instrumentation audit:** verify every data point needed for analysis is captured and the JSON format is consistent across topics
- Ensure all content for validated topics is complete and reviewed

**Deliverables:** stable MVP, resolved bug list, demo script, instrumentation verified, all topic content reviewed

**Exit criterion:** someone who didn't build Apollo can pick any validated topic, teach Apollo, and get a coherent result — success, failure with clear diagnosis, or retry with improvement. No handholding needed.

---

## June 5–7 (3-day buffer): Polish + package

- Fix remaining bugs from Week 8
- Record demo video: one success path, one failure path, one retry path, across two different topics
- 1-page architecture summary
- Package 5+ example session traces as research artifacts
- Document the content authoring guide: how to add a new topic (DAG nodes, problems, reference solutions, variable map) — so the system can expand to full course coverage post-pilot
- Write up formal study design (protocol, measures, comparison condition, sample size) for fall semester execution

---

## Parallel Content Authoring Track

This runs alongside the engineering work, ideally done by someone else (TA, instructor, collaborator). If solo, timebox to 3–4 hours per week outside the engineering blocks.

| Week | Content work |
|---|---|
| 1 | Full course DAG skeleton. Bernoulli neighborhood fully populated (you do this). |
| 2–3 | Second topic neighborhood populated + 3–5 problems with structured solutions |
| 4–5 | Third topic populated + problems. Fourth topic started. |
| 6–7 | Fourth topic populated + problems. Variable normalization maps for all topics. |
| 8 | Review all content against test session results. Fix problems where structured solutions don't match what students actually need to teach. |

**Topic selection criteria for the 3–4 validation topics:** pick ones that stress different parts of the system.

| Topic | Why it tests something new |
|---|---|
| Bernoulli's Principle | Baseline. Multi-equation (Bernoulli + continuity + area). Conditional simplification (horizontal). |
| Projectile motion (or equivalent) | Simpler equations, but requires coordinate system setup and component decomposition — tests whether the KG can represent structural choices, not just equations. |
| RC circuits (or equivalent) | Different variable domain entirely. Tests parser/normalization generalization. Exponential functions in SymPy. |
| Energy conservation (or equivalent) | Conceptually overlaps with Bernoulli's prerequisites. Tests whether the DAG correctly scopes what's in/out for a related-but-different concept. |

---

## What's Explicitly Deferred

| Item | Why | When to revisit |
|---|---|---|
| Hoot integration (Phase 0 trigger button) | MVP works standalone with manual topic selection | After pilot validates the core mechanic |
| Multi-course support | One course is the research scope | After positive pilot results |
| Formal student study (IRB, pre/post tests, control group) | Can't recruit + get IRB + run + analyze in remaining time | Fall semester, with MVP as the instrument |
| Auth / multi-user | Internal pilot doesn't need it | When integrating into Hoot proper |
| Persistent storage for KGs | Session-scoped in-memory is fine for pilot | When retry-across-sessions is needed |

---

## Risk Register

| Risk | Likelihood | Impact | Kill criterion | Fallback |
|---|---|---|---|---|
| Parser accuracy <60% on real utterances | Medium | High | <60% after Week 3 + 1 week iteration | Template-guided teaching ("enter your equation here") instead of free-form NL |
| LLM leakage can't be reliably prevented | Low | Fatal | Any leak surviving 3 prompt iterations | Retrieval-only model or rule-based dialogue for Apollo |
| Solver can't find paths from well-formed KGs | Medium | High | Fails >30% of complete-KG problems after Week 6 | Restrict to linear equation chains for the pilot |
| Steering hints leak concept names | Medium | Medium | Leakage in >20% of steering attempts | Disable steering; Apollo asks only generic questions |
| SymPy can't handle student equation forms | Low | Medium | Can't solve basic problems after normalization | Constrain parser to pre-registered equation templates |
| System doesn't generalize beyond Bernoulli | Medium | High | Second topic fails in Week 6 despite correct content | Identify and fix Bernoulli-specific assumptions; if structural, descope to single-topic pilot |
| Content authoring bottleneck (no help, solo) | Medium | Medium | Fewer than 3 topics ready by Week 6 | Ship with 2 deeply validated topics instead of 3–4 |
| 8 weeks isn't enough | Medium | High | Week 6 e2e flow doesn't work | Cut diagnostics to "solved/not solved + missing list." Cut retry. Cut topics to 1–2. Ship simpler artifact. |

---

## Weekly Checkpoints

Every Friday: 30-minute review.

1. Is this week's exit criterion met? (Yes/no, with evidence)
2. Is content authoring on track? (How many topics are ready?)
3. Any kill criteria triggered? (Check risk register)
4. Does next week's plan still make sense given what we learned?

If an exit criterion isn't met: decide **that day** whether to extend into the next week (and what to cut from it) or descope. Don't let a missed week silently compress the remaining schedule.
