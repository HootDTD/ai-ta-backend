# Apollo v2 — Design Spec

**Date:** 2026-04-14
**Status:** Approved design, supersedes v1 spec (`2026-04-13-apollo-v1-design.md`).
**Supersedes:** v1's prompt-engineered-ignorance, concept-first-teaching, student-pilot-testing model. Week 1 Apollo work (pydantic schemas, Bernoulli DAG, 5 problems, variable normalization map) carries forward; the throwaway spike is retired.
**Approach:** Slice 0 (end-to-end vertical) then depth passes. No calendar timeline. No fallbacks.
**Repos:** Backend `ai-ta-backend` · Frontend `ai-ta-student-ui` (sibling Next.js repo)

---

## 1. Goal

Apollo v2 is a "Teach Apollo" mode surfaced mid-conversation in Hoot. When Hoot detects a concept has been sufficiently explained to the student, a button appears inline in the assistant turn. The student clicks it and enters Apollo, a knowledge-constrained agent the student teaches to solve problems.

Apollo has two layers of ignorance, enforced structurally:

- **The conversational LLM is filtered at the output level.** Apollo's LLM drafts a reply; a deterministic post-filter rejects any output containing domain terms the student has not introduced. If the filter rejects, a visible error is surfaced — no template fallback, no silent substitution.
- **The solver has no LLM at all.** Apollo's problem-solving is a forward-chaining planner over the student-populated Knowledge Graph, executed by SymPy, narrated by templates. The solver cannot leak because it has no pretrained knowledge to leak.

Sessions are multi-problem: a pool of ~7 problems per concept cluster, student-paced, student-chosen difficulty per problem. The KG persists across problems within a session (editable between), and across student absences (resumable after returning to Hoot for further study). A full session follows a closed pedagogical loop: Hoot → Apollo (discover gap) → Hoot (learn specifically what was missing) → Apollo (re-attempt).

The overarching product ambition is to replace widescale TA help with a Feynman-technique-based "Check Understanding" surface, starting with equation-based STEM.

---

## 2. Scope

### In scope for v2 (first shippable, Slice 0 target)

- **Hoot ↔ Apollo integration** via the "concept-explained" signal (Hoot-side work, cofounder-owned). Frontend button in `ai-ta-student-ui/app/page.tsx`. Overseer concept-inference on Hoot transcript. Problem-cluster match.
- **Forward-chaining solver + SymPy + template narration.** No LLM in the solving loop.
- **Apollo conversational LLM with hard output filter.** Domain-term allowlist derived from student messages + KG. Filter rejection surfaces as visible error, never template fallback.
- **KG persistence within a session** across problems. Editable between problems (read + write panel).
- **KG persistence across student absences.** Full session state persists in Supabase Postgres (KG, conversation history, current problem, phase).
- **Multi-problem sessions.** Up to ~7 problems per concept cluster, student-picks-next.
- **Difficulty tiers.** Author-assigned `intro | standard | hard` label per problem. Student picks difficulty per problem from #2 onward; Overseer picks the first problem at `intro` default.
- **Stuck flow.** 3-button choice: teach more & retry, move to next problem, return to Hoot.
- **Return-to-Hoot with gap context.** When the student returns to Hoot, Apollo posts a structured system message into the Hoot conversation thread identifying the specific gap. Hoot uses this context to address the gap in the next exchange.
- **Resume.** A "Return to your Apollo session" button in Hoot (Hoot-side UI work) restores the paused session exactly where the student left it.
- **Slice 0 first**, then depth passes on individual components. No calendar constraints.

### Out of scope for v2 (explicitly deferred)

| Item | Why | When to revisit |
|---|---|---|
| Real student testing | You + me only. Students are a summer concern. | Summer |
| Rubric-based backend for mechanism-heavy subjects | Still equation-based STEM only | Post-v2, when expanding to biology/CS/orgo |
| Dynamic problem generation by the Overseer | Problem bank is authored | Post-v2 if bank authoring bottlenecks |
| Adaptive difficulty (Overseer auto-selects) | Difficulty is student-picked per problem | Post-v2 if students want guidance |
| Hoot's "concept-explained" signal | Hoot-side work, cofounder-owned | Hoot team delivers; Slice 0 uses a manual trigger for testing |
| Hoot-side "Return to Apollo" button + system-message rendering | Hoot-side UI work | Hoot team delivers in parallel with Apollo's E1/E2 work |
| Stale-session retirement policy | Design after observing real usage | Post-v2 |
| Multiple concurrent sessions per student | One session per student at a time | If demand arises |
| Authentication / multi-user isolation | Rides on existing Hoot Supabase JWT | N/A |
| Multi-topic concept clusters beyond Bernoulli at Slice 0 | Bernoulli is the first cluster; expansion is DP12 | After Slice 0 ships |
| Session-end summary report across problems | Per-problem diagnostics are sufficient initially | Post-Slice-0 if useful |

### Success criteria

With you + me as the only testers, success is measured against adversarial testing of the failure modes we care about:

1. **Structural ignorance verified.** Across 50+ adversarial teaching/probing messages, Apollo's output never contains a physics term the student hasn't introduced. Filter rejections surface as visible errors — never silent leaks.
2. **Solver fails loudly and correctly.** Given a deliberately incomplete KG, the forward-chainer identifies exact missing variables and produces a stuck diagnostic listing them. No false successes, no vague failures.
3. **End-to-end integration works.** Full Hoot → Apollo → back-to-Hoot loop completes: concept inferred, problem selected, taught, solved-or-stuck, diagnostic generated, returned to Hoot with gap context, resumed later.
4. **Persistence works across absences.** Student pauses via return-to-Hoot, returns hours or days later, finds KG and problem state exactly as left.
5. **Failure surfaces are honest.** Every breakage (parser can't extract, filter blocks, Overseer can't match concept, solver parse error) produces a clear error describing what happened. No silent degradation anywhere.

---

## 3. Failure philosophy — NO FALLBACKS

Cross-cutting design constraint. Overrides typical "graceful degradation" patterns.

**Every component has exactly one success path. Failures raise visible errors.**

Concrete manifestations:

- **Output filter** does not fall back to templates when it blocks Apollo's draft. Filter rejection surfaces as a user-visible error. Too many rejections means iterate the prompt or allowlist — not silently paper over.
- **Parser** does not silently drop messages it can't parse. `ParserCouldNotExtractError` surfaces "I didn't understand that — could you say it more precisely?" as a visible state.
- **Forward-chainer** does not invoke an LLM planner when stuck. Stuck IS the diagnostic.
- **Overseer concept-inference** does not fuzzy-match when it can't find a clear topic. Returns `409 NoMatchingConcept`; frontend shows "Apollo doesn't cover this topic yet."
- **Symbolic engine** does not skip unparseable equations. `MalformedEquationError` attributed to the specific KG entry.
- **Persistence failures** raise errors, not silent "start fresh."
- **"Teach Apollo" button** does not appear if Hoot's signal doesn't fire. No heuristic fallback.

**Rationale:** we are the testers. Loud failures are the signal we need to improve the system. Silent fallbacks make bugs invisible until real students are exposed.

**Consequence for UX:** error states are first-class surfaces, not afterthoughts. Every component has a clearly-defined error shape the frontend renders explicitly.

---

## 4. Architecture

### Core mechanic

Student utters natural language → parser extracts structured KG entries (or raises `ParserCouldNotExtractError`) → KG stored in session-persistent DB → Apollo LLM drafts reply → output filter validates (or raises `FilterRejectedError`) → validated reply sent to student. At problem end: KG frozen → forward-chain solver runs over frozen KG (or raises `MalformedEquationError`) → narrator templates the trace → Overseer generates student-facing diagnostic.

### Module layout

Apollo spans two repos.

**Backend (`ai-ta-backend/apollo/`):**

```
apollo/
├── overseer/
│   ├── concept_inference.py    # Hoot transcript → concept_cluster_id (isolated LLM call)
│   ├── problem_selector.py     # concept + difficulty → problem_id (deterministic)
│   ├── coverage.py             # KG vs reference solution (deterministic)
│   ├── steering.py             # Gap → concept-name-free hint (DP8, not in Slice 0)
│   └── diagnostic.py           # Gap report (isolated LLM call)
├── agent/
│   ├── apollo_llm.py           # Draft-reply LLM call
│   └── output_filter.py        # Deterministic post-filter; NO FALLBACK
├── parser/
│   └── parser_llm.py           # Student utterance → structured KG entries
├── knowledge_graph/
│   ├── store.py                # CRUD + freeze/unfreeze over persisted KG
│   └── schema.py               # (exists from Week 1)
├── solver/
│   ├── forward_chain.py        # Planner (no LLM)
│   ├── sympy_exec.py           # SymPy execution; surfaces parse errors
│   └── narrator.py             # Template-based solver-trace rendering
├── concepts/                   # DAG (exists from Week 1)
├── problems/                   # Problem bank (exists from Week 1)
├── hoot_bridge/
│   └── session_init.py         # POST /from_hoot handler
├── persistence/
│   └── models.py               # SQLAlchemy: Session, KGEntry, Message, ProblemAttempt
└── api.py                      # FastAPI router
```

**Frontend (`ai-ta-student-ui`, Next.js App Router):**

```
app/
├── page.tsx                    # Hoot chat — MODIFIED to render "Teach Apollo" button on signal
├── apollo/
│   └── page.tsx                # NEW: Apollo teaching route (chat + KG panel + solver panel)
└── api/apollo/                 # NEW: proxy routes forwarding to backend with Supabase JWT
    ├── sessions/
    │   ├── from_hoot/route.ts
    │   ├── resumable/route.ts
    │   └── [id]/
    │       ├── chat/route.ts
    │       ├── done/route.ts
    │       ├── retry/route.ts
    │       ├── next_problem/route.ts
    │       ├── select_next/route.ts
    │       ├── return_to_hoot/route.ts
    │       ├── resume/route.ts
    │       └── kg/route.ts
```

### Component responsibilities

| Component | Purpose | Dependencies |
|---|---|---|
| `Overseer.concept_inference` | Reads Hoot transcript, emits one `concept_cluster_id` or raises `NoMatchingConcept` | `concepts/` DAG; isolated LLM call |
| `Overseer.problem_selector` | Given `concept_cluster_id` + difficulty + attempted-ids, picks specific problem or raises `PoolExhausted` | `problems/` bank (deterministic) |
| `Overseer.coverage` | Compares KG against current problem's reference solution, emits per-entry status | frozen KG + reference solution |
| `Overseer.steering` (DP8) | Generates concept-name-free hints for Apollo about functional gaps | coverage output; isolated LLM call |
| `Overseer.diagnostic` | After solve, generates student-facing gap report | solver trace, KG, reference solution; isolated LLM call |
| `agent.apollo_llm` | Drafts next conversational reply | conversation history + KG summary; isolated LLM call |
| `agent.output_filter` | Deterministically validates draft against student-sourced allowlist; rejects on violation with `FilterRejectedError` | current KG + student message history |
| `parser.parser_llm` | Extracts structured KG entries in SymPy-parseable form; raises `ParserCouldNotExtractError` on zero yield from non-trivial utterance | student utterance; isolated LLM call |
| `knowledge_graph.store` | CRUD + freeze/unfreeze; writes raise error when frozen | persistence |
| `solver.forward_chain` | Builds equation system from KG + givens, feeds SymPy, derives target or returns missing vars | frozen KG + problem |
| `solver.sympy_exec` | Wraps SymPy; surfaces parse errors as `MalformedEquationError` attributing to specific KG entry | sympy |
| `solver.narrator` | Template-based natural-language solver trace (no LLM) | solver trace |
| `hoot_bridge.session_init` | Orchestrates concept-inference, problem-selection, session creation | `Overseer`, `persistence` |
| `persistence.models` | SQLAlchemy models | Supabase Postgres |

### Context isolation

Five distinct LLM calls. Each has its own system prompt and context; none share history.

| LLM call | Sees | Does NOT see |
|---|---|---|
| `agent.apollo_llm` | Student messages in this session, Apollo's prior replies, KG summary (student-sourced labels only) | Hoot transcript, reference solution, coverage, inferred concept, problem-bank metadata |
| `parser.parser_llm` | Single student utterance, variable normalization map | Everything else. Never sees KG, conversation history, reference solution. |
| `Overseer.concept_inference` | Hoot transcript, concept DAG | Apollo context; student's KG (empty at init) |
| `Overseer.steering` (DP8) | Coverage output, reference solution, missing entries | Apollo conversation directly; it writes to Apollo's system prompt |
| `Overseer.diagnostic` | Full KG, reference solution, solver trace | Apollo's conversation history |

`agent.output_filter` is deterministic code, not an LLM. It is the hard structural barrier between Apollo's draft and the student.

**Rule:** Nothing except the KG summary (student-sourced) and concept-name-free steering hints (DP8) crosses from the Overseer's side into Apollo's context.

### Data flow — single teaching turn

```
Student types message
  → POST /apollo/sessions/{id}/chat
  → parser.parser_llm(utterance) → list of KG entries
       [empty + non-trivial utterance → raise ParserCouldNotExtractError → surface visible error state]
  → knowledge_graph.store.write(entries)
  → agent.apollo_llm(conversation_history, kg_summary) → draft reply
  → agent.output_filter(draft, kg, history) → validated reply
       [filter rejects → raise FilterRejectedError → surface visible error state]
  → persist message to DB
  → return { apollo_reply, kg_updated, new_entries }
Frontend renders updated chat + updated KG panel
```

### Data flow — session init from Hoot

```
Hoot's concept-explained signal fires in frontend
  → "Teach Apollo" button renders in assistant turn
  → Student clicks
  → Frontend POST /apollo/sessions/from_hoot { student_id, hoot_transcript }
  → hoot_bridge.session_init:
       → Overseer.concept_inference(transcript)
            → concept_cluster_id  OR  raise NoMatchingConcept → 409
       → Overseer.problem_selector(cluster, difficulty="intro", attempted=[])
            → first_problem_id  OR  raise PoolExhausted
       → persistence: create Session row (phase=INIT) + first ProblemAttempt row
       → store reference solution privately
  → return { session_id }
  → Frontend navigates to /apollo?session={id}
  → Apollo route mounts, calls GET /apollo/sessions/{id} for state
  → UI renders empty KG panel, first problem stated, Apollo greeting
```

### Data flow — solve attempt

```
Student clicks "I'm done teaching"
  → POST /apollo/sessions/{id}/done
  → knowledge_graph.store.freeze(session)
  → solver.forward_chain(frozen_kg, problem):
       → build equation system from KG equations + given values
       → [any equation parse error → raise MalformedEquationError attributed to specific entry]
       → feed to SymPy, derive target
       → solved → { status: solved, value, trace }
       → stuck → { status: stuck, missing_variables, trace }
  → solver.narrator(trace) → natural-language step-by-step
  → Overseer.diagnostic(kg, reference_solution, solver_result) → student-facing report
  → persist ProblemAttempt (result, solver_trace, diagnostic_report)
  → return { result, narrated_trace, diagnostic_report }
Frontend renders result panel + 3-button stuck/solved choice
```

### Persistence model

Supabase Postgres tables, SQLAlchemy:

- **Session**: `id, student_id, concept_cluster_id, status (active|paused|ended), current_problem_id, phase, created_at, last_touched_at`
- **KGEntry**: `id, session_id, type, content (jsonb), source (parser|student_edit), created_at`
- **Message**: `id, session_id, role (student|apollo|system), content, turn_index, created_at`
- **ProblemAttempt**: `id, session_id, problem_id, difficulty, result (solved|stuck|skipped|returned_to_hoot), solver_trace (jsonb), diagnostic_report (jsonb), created_at`

Indexes on `session_id` for all; on `student_id` for Session. Enforce one-active-session-per-student via a unique partial index on `(student_id)` where `status='active'`.

### External dependencies

- **Hoot-side:** "concept-explained" signal, "Teach Apollo" button wiring (Apollo provides the button component; Hoot wires the signal), "Return to your Apollo session" button in Hoot UI, accepting a system-message shape from Apollo into the chat thread on return.
- **Supabase Postgres:** existing infrastructure; adds four tables.
- **SymPy:** in `requirements.txt` (from Week 1).
- **OpenAI API:** via `MAIN_MODEL` env var.

---

## 5. Session Lifecycle & Phase State Machine

### Phase diagram

```
              Hoot signal fires + student clicks "Teach Apollo"
                                   ↓
                                 INIT
                                   ↓ (auto, on concept-inference + first-problem selection)
                                TEACHING  ←────────────────────────────┐
                                   ↓                                    │
                       (student clicks "I'm done teaching")             │
                                   ↓                                    │
                            PROBLEM_REVEAL                              │
                                   ↓ (auto; KG frozen)                  │
                                SOLVING                                 │
                                   ↓ (auto; forward-chain + diagnostic) │
                                 REPORT                                 │
                                   ↓                                    │
              ┌──────────── 3-button choice ────────────┐               │
              ↓                    ↓                     ↓              │
     "teach more & retry"   "move to next"       "return to Hoot"       │
              │                    ↓                     ↓              │
              │                BETWEEN               PAUSED             │
              │                    ↓                     ↓              │
              │       (KG editable; difficulty    (persisted; gap       │
              │        picker; next loads)         posted to Hoot)      │
              │                    ↓                     ↓              │
              └────────────────────┴───────────── (Hoot button          │
                                                  "Return to Apollo")   │
                                                         ↓              │
                                                      resume            │
                                                         └──────────────┘

                   (anywhere except PAUSED/ENDED: "End session" → ENDED)
```

### Phase descriptions

| Phase | What's happening | What student sees | When it ends |
|---|---|---|---|
| **INIT** | Concept inferred, first problem selected, session row created | Brief loading spinner | Auto, ~1–2s |
| **TEACHING** | Active chat. Parser extracts each turn; Apollo replies via filter. KG panel live. | Chat + live KG panel + problem header + "I'm done teaching" + "Return to Hoot" | Student clicks "I'm done teaching" |
| **PROBLEM_REVEAL** | KG freezes. Problem restated. | Problem large; "Apollo is thinking…" | Auto, on solver start |
| **SOLVING** | Forward-chain runs; narrator produces trace | Step-by-step trace (streaming or block) | Solver completes |
| **REPORT** | Diagnostic generated | Solver result, narrated trace, diagnostic, 3-button choice | Student picks a button |
| **BETWEEN** | KG editable; difficulty picker | Editable KG panel, difficulty picker, optional "End session" | Student picks difficulty → TEACHING |
| **PAUSED** | Session persisted; gap context posted to Hoot | Student back in Hoot; "Return to Apollo" button in Hoot UI | Student clicks resume button |
| **ENDED** | Archived, not resumable | Session end screen with summary | Terminal |

### Phase transitions and API endpoints

| From → To | Trigger | Endpoint | Persistence |
|---|---|---|---|
| `—` → `INIT` | Hoot POST | `POST /apollo/sessions/from_hoot` | Create Session (phase=INIT) + first ProblemAttempt |
| `INIT` → `TEACHING` | Auto | — | Update Session.phase |
| `TEACHING` → `PROBLEM_REVEAL` | Student click | `POST /apollo/sessions/{id}/done` | KG frozen |
| `PROBLEM_REVEAL` → `SOLVING` | Auto | — | — |
| `SOLVING` → `REPORT` | Solver complete | — | Update ProblemAttempt (result, trace, diagnostic) |
| `REPORT` → `TEACHING` | "Teach more & retry" | `POST /apollo/sessions/{id}/retry` | KG unfrozen; same problem |
| `REPORT` → `BETWEEN` | "Move to next" | `POST /apollo/sessions/{id}/next_problem` | Advance; KG unfrozen; current problem cleared |
| `BETWEEN` → `TEACHING` | Student picks difficulty | `POST /apollo/sessions/{id}/select_next?difficulty=X` | New ProblemAttempt; phase=TEACHING |
| any active → `PAUSED` | "Return to Hoot" | `POST /apollo/sessions/{id}/return_to_hoot` | Session.status=paused; system message posted to Hoot |
| `PAUSED` → prior phase | "Return to Apollo" (Hoot button) | `POST /apollo/sessions/{id}/resume` | Session.status=active; phase restored |
| any → `ENDED` | "End session" | `POST /apollo/sessions/{id}/end` | Session.status=ended |

### Edge cases (under no-fallbacks)

1. **Hoot signal fires while student has a paused session.** Frontend calls `GET /apollo/sessions/resumable?student_id=X` before rendering the Teach Apollo button. If a paused session exists, button becomes a two-choice menu: "Resume your Apollo session on [X]" or "Start a new Apollo session on [Y]." Explicit student choice. Concurrent active sessions disallowed.
2. **Two browser tabs.** Backend enforces single active session via unique partial index. Second tab's request returns 409 with link to the active session.
3. **Problem pool exhausted.** BETWEEN phase with all 7 attempted. Difficulty picker disabled; "End session" prominent. Session auto-transitions to ENDED on acknowledgment.
4. **Difficulty tier exhausted but others remain.** Picker shows remaining counts per tier; 0-count tiers disabled.
5. **Session abandoned without explicit pause.** Session stays `active` in DB. On next load in same browser, prompt to resume.
6. **`Overseer.concept_inference` returns 409 at INIT.** No Session row created. Frontend shows "Apollo doesn't cover this topic yet."
7. **`ParserCouldNotExtractError`.** TEACHING remains. Apollo's reply slot shows visible error with offending utterance. Student rephrases. No auto-retry.
8. **`FilterRejectedError`.** Visible error ("Apollo's response was blocked — please rephrase your last message"). No Apollo reply this turn. Logged loudly.
9. **`MalformedEquationError` during SOLVING.** Solver halts immediately. Diagnostic attributes failure to specific KG entry. No partial-solve attempt.
10. **Persistence write fails.** 500 response with specific error code. Frontend shows "Connection lost — retry." No silent continuation.

### Return-to-Hoot system message

Structured format posted into the student's Hoot thread:

> *"You paused your Apollo session on **[concept cluster name]**. Apollo was stuck on problem N: couldn't derive **[target variable]** because it was missing **[gap description from diagnostic]**. Would you like to review that specifically?"*

Hoot-side requirements: render this as a system-note block; optionally use it to seed the next assistant turn.

---

## 6. Build Plan

No calendar. Components and their dependencies.

### Slice 0 — definition of done

Binary, measured by integration test. Full loop works end-to-end:

1. Hoot chat UI shows "Teach Apollo" button (Slice 0: manually triggerable for testing).
2. Click POSTs simulated transcript to backend.
3. `Overseer.concept_inference` returns `fluid_mechanics` (or hard 409 on unrelated transcript).
4. First problem loads. Apollo greets. Empty KG panel visible.
5. Student teaches via chat. Parser writes KG entries. Apollo probes with filtered replies. KG panel updates live.
6. Student clicks "I'm done." Forward-chainer runs. Narrated trace appears. Diagnostic renders.
7. Student picks one of three buttons. Each path works: retry same problem / pick difficulty + next problem / return to Hoot.
8. After "Return to Hoot": system message posted to Hoot; student back in Hoot; "Return to Apollo" resumes exact state.
9. Zero silent fallbacks anywhere. Every failure path surfaces a visible error.

### Slice 0 component inventory

Groups run top-down; within a group, components may build in parallel.

**Group A — Foundations (no dependencies):**
- A1. Persistence models (SQLAlchemy: Session, KGEntry, Message, ProblemAttempt) + Alembic migration + indexes
- A2. Apollo FastAPI router skeleton (all endpoints stubbed to 501)
- A3. Next.js Apollo route skeleton (`/apollo/page.tsx` loading state) + proxy routes

**Group B — Core services (← A1):**
- B1. KG store CRUD + freeze/unfreeze; writes raise when frozen
- B2. Parser LLM with `ParserCouldNotExtractError` on zero-yield non-trivial utterance
- B3. Apollo LLM (reuses v1 prompt minus "claim you get it" behavior)
- B4. Output filter — deterministic. Input: Apollo draft + current KG + student history. Output: validated reply or `FilterRejectedError`. First pass allowlist = student-sourced nouns/noun-phrases + KG entry labels/content + small base of general English. Hand-authored rejection list of physics stopwords not in student context.
- B5. Forward-chain solver — collect KG equations, substitute knowns, SymPy simultaneous solve, inspect for target. `MalformedEquationError` attributed to specific entry on any parse failure.
- B6. Solver narrator — template-based. One template per op: "I needed X. I was taught [label]: [symbolic]. Applying: [substituted]. Got X = Y."
- B7. Coverage computation — deterministic comparison of KG vs reference solution
- B8. Overseer.diagnostic LLM — student-facing gap report from coverage + trace + reference

**Group C — Orchestration (← A1, B):**
- C1. Overseer.concept_inference (LLM; `NoMatchingConcept` on miss)
- C2. Overseer.problem_selector (deterministic; `PoolExhausted` when empty tier)
- C3. hoot_bridge.session_init (orchestrates C1, C2, A1)
- C4. Session chat handler (orchestrates B2, B3, B4; surfaces each error type distinctly)
- C5. Session done handler (orchestrates B1.freeze, B5, B6, B7, B8)
- C6. Session next-problem handler (orchestrates C2, B1.unfreeze)

**Group D — Frontend UI (← A3, C):**
- D1. Hoot chat "Teach Apollo" button (Slice 0: manually visible; real signal wiring is DP10)
- D2. Apollo chat panel (streams; sends to `/api/apollo/sessions/{id}/chat`)
- D3. KG panel (read-only for Slice 0, KaTeX for symbolic strings)
- D4. Problem + solver output panel
- D5. 3-button stuck/solved choice (REPORT phase)
- D6. Difficulty picker (BETWEEN phase)
- D7. Explicit error surfaces: ParserCouldNotExtract, FilterRejected, MalformedEquation, NoMatchingConcept, PoolExhausted, persistence-unreachable

**Group E — Session continuation (← C5, A1):**
- E1. Return-to-Hoot endpoint + structured system-message post (via Hoot's chat-message API)
- E2. Resume endpoint + state load
- E3. Hoot-side "Return to Apollo" button + system-message rendering (external; cofounder-owned; Apollo provides the contract)

**Group F — Integration (← everything):**
- F1. Full end-to-end integration test scripted by the 9-step DoD above

### Dependency summary

```
A1 → B1 ─┬→ B2 → C4 ─┐
         ├→ B3 → B4 →┤
         ├→ B5 ─┬→ B6│
         │      └→ B7 → B8 → C5
         │
concepts → C1 ──────┐
problems → C2 ──────┼→ C3, C6
                    ↓
A2 → C3, C4, C5, C6, E1, E2 → API stable
A3 → D1–D7 ─────────────────┬→ F1
                             ↓
                          Slice 0 green
```

### Depth passes (after Slice 0 green)

**Priority 1 — correctness under adversarial conditions:**
- DP1. Output filter hardening (50+ adversarial probe suite, regression tests)
- DP2. Parser robustness on prose (30+ realistic utterances, ≥90% yield or explicit "couldn't parse")
- DP3. Forward-chainer robustness (overconstrained, multi-root, ill-conditioned systems; no silent bad answers)

**Priority 2 — pedagogical quality:**
- DP4. KG editor (read → read+write panel; pydantic+SymPy validation on save)
- DP5. Diagnostic report quality
- DP6. Solver narrator templates refined
- DP7. Apollo conversational prompt iteration (introspection-on-gaps behavior)

**Priority 3 — feature depth:**
- DP8. Overseer.steering (calibrated confusion hints)
- DP9. Session resume UX polish
- DP10. Hoot "concept-explained" signal real integration (when cofounder ships)

**Priority 4 — content and scale:**
- DP11. Problem bank expansion to 7 per cluster
- DP12. Additional concept clusters (kinematics, thermo, circuits, etc.)

### Testing strategy (you + me, no students)

- Slice 0 DoD walkthrough (9-step integration test in browser, primary signal)
- DP1 adversarial suite runs on every commit as CI test once written
- DP2 parser corpus versioned in repo; regression tested on parser changes
- Unit tests per B1–B8, C1–C6 with mocked deps
- Integration tests hit real Supabase + OpenAI; unit tests mock
- Manual exploratory testing by you and me during development; issues filed for anything weird

### Explicitly NOT in Slice 0

- DP8 Overseer.steering
- DP4 KG editing (read-only only)
- Stale-session retirement policy
- Concurrent-sessions support
- DP10 Hoot signal real integration
- Multi-topic beyond Bernoulli
- Auth work
- DP11 problem bank expansion
- Session-end summary report

---

## 7. Open questions (to resolve during build)

- **Output filter allowlist specifics.** How to classify a token as "domain term" vs. "general English"? First-pass approach: hand-authored rejection list (`continuity`, `viscosity`, ...) + heuristic (word appearing in student history OR KG entry). Refined in DP1.
- **Solver trace streaming vs. block.** Does the narrated trace stream in real time during SOLVING, or appear as a block once solver completes? Ship block-first; streaming is a DP.
- **KG summary format for Apollo's context.** Dense (all entries) vs. selective (only recent). Ship dense-first; iterate if Apollo's context gets too large.
- **Stale-session policy.** Deferred until we see real usage patterns.
- **`MAIN_MODEL` per call-site.** All five LLM calls default to `MAIN_MODEL` env var (GPT-4o). Potential future per-site overrides (cheaper for parser, more capable for diagnostic).
- **"Non-trivial utterance" definition for `ParserCouldNotExtractError`.** The parser should raise this error when it extracts zero entries from a message that *looks* like a teaching utterance (contains content words, equation-like patterns, etc.) but *not* when the student types a conversational acknowledgement ("ok", "yes", "hmm"). First-pass heuristic: raise when the parser returns empty AND the utterance is ≥10 characters and contains any of (`=`, `*`, `**`, `^`, `/`, `+`, numeric tokens, or a term present in the variable normalization map). Refine in DP2.

---

## Appendix A — What carries forward from v1 Week 1 work

Committed on `beginApollo` branch, reused in v2:

- `apollo/schemas/` (DAG, variable map, problem pydantic models + tests)
- `apollo/concepts/bernoulli_dag.json` (14 nodes, 16 edges)
- `apollo/concepts/variable_normalization/fluid_mechanics.json` (23 mappings)
- `apollo/problems/bernoulli/problem_01–05.json` (5 problems with structured reference solutions)
- `apollo/__init__.py`, `apollo/schemas/tests/` (13 passing tests)
- `requirements.txt` addition of `sympy>=1.12`

## Appendix B — What changes from v1 spec

| Area | v1 | v2 |
|---|---|---|
| Apollo ignorance | Prompt-engineered ("act ignorant") | Structural — deterministic output filter (`agent.output_filter`) with no fallback |
| Solver | SymPy with light LLM-adjacent plumbing | Forward-chaining planner + SymPy execution + template narration; no LLM in solving loop |
| Teaching mode | Concept-first (reveal problem after teaching) | Problem-first (student sees the problem they're teaching toward) |
| Session structure | Single problem per session | Multi-problem (~7 per concept cluster), student-paced, student-chosen difficulty |
| KG persistence | Session-scoped in-memory | Supabase Postgres; persists across student absences; editable between problems (DP4 adds write UI) |
| Difficulty | Not modeled | Author-assigned (`intro` / `standard` / `hard`); student picks per-problem from #2 onward |
| Stuck flow | Re-teach or return to Hoot | 3-button: teach more & retry / move to next / return to Hoot |
| Return to Hoot | End session | Pause with gap-context posted to Hoot thread; resume restores full state |
| Testing | Real students in Weeks 4–8 | You + me only; students deferred to summer |
| Timeline | 8 weeks with go/no-go gates | No calendar; Slice 0 + depth passes |
| Failure philosophy | Graceful degradation with named fallbacks (template-guided teaching, retrieval-only Apollo, etc.) | **NO FALLBACKS.** Every failure surfaces visibly. |
| Hoot handoff | Deferred-then-added late | Engineered from the start (Slice 0 includes it) |
